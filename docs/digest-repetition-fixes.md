# Plan: Eliminating Consecutive Digest Repetition

## Context

Digests generated March 8–12 all open with near-identical constructions
("Today's news reveals a complex interplay between..."). The pipeline has
four mechanisms meant to prevent this — style variation, opening constraints,
the pitch pass, and the editor pass — but all four are failing because:

1. The article pool for consecutive days heavily overlaps (same articles are
   re-selected because `scored_at` windows overlap and there is no "already
   featured" gate).
2. `_check_boilerplate()` detects prohibited phrases but the revision model
   (llama3.2 / 8B) regenerates the same boilerplate again when asked to fix.
3. The editor pass (`_run_editor`) compares the draft against recent digests
   and produces PRIORITY REVISIONS, but llama3.2 cannot reliably execute
   prose-quality instructions.
4. No hard gate: if boilerplate survives revision, the digest ships unchanged
   with only a warning log.

There is also **no digest_articles junction table** — the digests table stores
only rendered markdown, not which article IDs were featured. This structural
gap makes Fix 1 more involved than it would otherwise be.

---

## Fix 1: Article Deduplication

### Problem

`get_articles_since_scored()` in `db.py` (line 726) selects by `scored_at`
window with no exclusion for articles that were already featured in recent
digests as T1 or T2. A 24-hour `scored_at` window with articles that were
scored a day or two ago will produce heavy overlap, so consecutive digests
literally receive the same article set.

### Approach A (Recommended): digest_articles junction table

Add a `digest_articles` table that records which article IDs appeared at
each tier in each digest. Populate it at save time in `generate_digest()`.
Add a `get_recently_featured_article_ids()` db function. Filter in
`get_articles_since_scored()` (or in `generate_digest()` before the
analysis calls) to downgrade T1/T2 candidates that appear in the exclusion
set — allow them to survive as T3 or T4 context, but skip them for deep dive.

**Advantages:** Exact tracking. No false positives. Survives regen runs
(regen_digests.py rebuilds the junction table alongside the digest).
Queryable — you can inspect what was featured when.

**Disadvantages:** Requires a schema migration (ALTER TABLE or CREATE TABLE
in `init_db()`). Regen runs must be order-aware: if regenerating a date
whose articles also appear in a later already-regenerated digest, the
junction table will have the later date's entry first and could incorrectly
mark articles as "already featured" from the future. Mitigation: only look
back N days, not forward.

### Approach B: Parse featured article URLs from recent digest markdown

Extract URLs from recent digest markdown (the `## Deep Dives` section) using
regex. Mark articles whose URL appears there as "already featured". No schema
change required.

**Advantages:** Zero schema changes. Works immediately against existing data.

**Disadvantages:** Fragile — depends on the markdown format staying stable.
URL extraction can miss articles if the link format changes. Cannot distinguish
T1/T2 (deep dive) from T3/T4 (brief mention) without parsing section structure.
False positives possible if the same URL is mentioned in Patterns & Signals.

### Recommendation

**Approach A.** The junction table is the right abstraction. The schema
migration is two lines. Parsing markdown for URLs is too brittle for a
system that already has several edge cases around section structure.

### Implementation Steps (Fix 1)

1. **`db.py` — `init_db()`**: Add `CREATE TABLE IF NOT EXISTS digest_articles`
   and an `ALTER TABLE` migration guard (matching the pattern already used for
   article columns).

   ```sql
   CREATE TABLE IF NOT EXISTS digest_articles (
       digest_id INTEGER NOT NULL,
       article_id INTEGER NOT NULL,
       tier INTEGER NOT NULL,           -- 1, 2, 3, or 4
       PRIMARY KEY (digest_id, article_id),
       FOREIGN KEY (digest_id) REFERENCES digests(id),
       FOREIGN KEY (article_id) REFERENCES articles(id)
   )
   ```

2. **`db.py` — new function `save_digest_articles(digest_id, article_tiers)`**:
   Insert rows into `digest_articles`. `article_tiers` is a list of
   `(article_id, tier)` tuples.

3. **`db.py` — new function `get_recently_featured_article_ids(days=3)`**:
   Return a set of article IDs that appeared as T1 or T2 in digests from
   the past N days (not including today's digest). Parameterize the day
   window and max tier so the caller can tune it.

4. **`db.py` — `save_digest()`**: Accept an optional `article_tiers` kwarg
   and call `save_digest_articles()` if provided. Return the digest ID.
   Currently `save_digest()` returns `cursor.lastrowid` but the callers
   (`generate_digest`, `_run_scheduled_digest`) discard it — they will
   need to capture it to call `save_digest_articles()`.

5. **`digest.py` — `generate_digest()`**: After tiering articles (line 1517),
   call `get_recently_featured_article_ids()` and filter `deep_dive_articles`
   (the T1/T2 candidates used for per-article LLM analysis). Articles in the
   exclusion set stay in `included` for T3/T4 synthesis context but are
   removed from `deep_dive_articles`. After `save_digest()`, call
   `save_digest_articles()` with the tier assignment from `tiered`.

**Edge cases**:
- If all T1/T2 candidates are recently featured, `deep_dive_articles` could
  be empty. Add a fallback: if fewer than 2 deep-dive candidates survive after
  exclusion, admit the highest-scoring recently-featured articles anyway (since
  a digest with no deep dives is worse than a repeated deep dive).
- `regen_digests.py` regenerates past dates in order. Because `save_digest()`
  will overwrite the digest row (INSERT OR REPLACE), the junction table
  entries for the regenerated date must be deleted first and then re-inserted.
  Add a `delete_digest_articles(digest_id)` helper and call it before insert.

---

## Fix 2: Hard Rejection of Boilerplate

### Problem

When `_check_boilerplate()` finds prohibited phrases and MAX_REVIEW_ITERATIONS
(2) is exhausted, the pipeline logs a warning and ships the boilerplate digest
unchanged (line 1739–1744). There is no failure mode — the digest always ships.

### Approach A (Recommended): Targeted section rewrite on final failure

After the review-and-revise loop exhausts its iterations, check for any
remaining **severe** boilerplate specifically (the `severe_phrases` list in
`_check_boilerplate()`). If found, perform one additional targeted pass that
rewrites *only the offending section* — not the entire digest. This is less
destructive than failing the whole generation.

If the targeted rewrite still contains severe boilerplate, fail the result
with a structured error that includes the offending phrases. Set
`result["success"] = False`, populate `result["error"]`, and log at ERROR
level. The scheduler will log the failure — the operator can then trigger
manual regeneration.

**Advantages:** Surgical. The majority of the digest (which is likely fine)
is preserved. The fallback error is honest rather than silently shipping junk.

**Disadvantages:** Adds one more LLM call per failure case. The targeted
rewrite may still fail if the model is fundamentally incapable.

### Approach B: Fail immediately, no retry

If severe boilerplate survives the regular revision loop, set
`result["success"] = False` immediately without an additional rewrite attempt.

**Advantages:** Simpler code. Forces the operator to notice and fix
(model upgrade, manual edit).

**Disadvantages:** The digest is entirely missing for that day, which means
Rogue Routine publishes nothing. That is worse than publishing a flawed digest.
The operator may not notice the failure until the next deploy.

### Recommendation

**Approach A**, but with an important constraint: the targeted section rewrite
should use the **prose model** (introduced in Fix 3), not the weak model. The
reason the boilerplate survives revision in the first place is that the revision
model cannot execute the instructions. Using a stronger model here directly
addresses that root cause.

### Implementation Steps (Fix 2)

1. **`digest.py` — after the `for revision_round` loop (line 1737)**:
   Replace the current `else` clause (which just logs the warning) with:

   a. Call `_check_boilerplate_severe(content)` — a new helper that returns
      only the severe-phrase matches (extracted from `_check_boilerplate()`).

   b. If severe phrases found: attempt one targeted rewrite using a new
      `_rewrite_section_targeted(content, phrases, model, temperature)` function
      that strips the offending section and asks the model to rewrite only that
      section with explicit grounding in the article content.

   c. After the targeted rewrite, call `_check_boilerplate_severe()` again.
      If severe phrases still present: set `result["success"] = False` and
      `result["error"] = f"Boilerplate survived all revision passes: {phrases}"`.
      Return early — do not call `save_digest()`.

2. **`digest.py` — new function `_check_boilerplate_severe(content)`**:
   Extract the severe-phrase check from `_check_boilerplate()` into a
   standalone function that returns only the list of severe matches. This
   avoids duplicating the pattern list and allows `_check_boilerplate()` to
   call it internally.

3. **`scheduler.py` — `_run_scheduled_digest()`**: The existing error logging
   already handles `result.get("error")`. No changes needed there. But add a
   note that a failed digest on a given day will not be re-attempted by the
   scheduler (it checks `get_days_needing_digest()`, which will NOT re-queue
   the day if a digest already exists). To allow retry: the failure path should
   NOT call `save_digest()` at all, so no digest row is written and the scheduler
   will re-queue it on the next run. This is already achieved by returning early
   before `save_digest()`.

---

## Fix 3: Stronger Model for Prose Passes

### Problem

All passes currently use the single `ollama_model` setting (default: llama3.2).
The 8B model is adequate for per-article analysis (bounded task, single
article, factual output) but cannot reliably execute open-ended prose
variation, comparison-based judgment, or revision instructions. The per-article
analysis pass runs N times per digest (one call per T1/T2 article) so model
size has cost implications there.

### Model Recommendation

**qwen2.5:32b** is the recommended prose model.

Rationale:
- 32B parameters puts it solidly in the "can actually follow prose instructions"
  tier without requiring a multi-GPU setup.
- Qwen2.5 has strong benchmark performance on instruction following and
  creative writing compared to its Llama-equivalent size tier.
- llama3.3:70b and qwen2.5:72b are better models but require ~45GB+ VRAM or
  aggressive quantization. At 32B with Q4_K_M quantization (~18GB), qwen2.5:32b
  fits on a single 24GB card (e.g., RTX 3090/4090) or in CPU+GPU offload.
- mistral-large is strong but the local versions available on Ollama are older;
  qwen2.5 has more recent training data.
- llama3.1:70b is viable if VRAM allows — it is the conservative choice for
  operators who already have it pulled. But 32B is the practical sweet spot.

If the machine has less than 24GB VRAM, **qwen2.5:14b** is the fallback.
It is noticeably better than 8B at instruction following while fitting in 10GB.

Keep **llama3.2** (or whatever `ollama_model` is set to) for per-article
analysis. Those calls are high-volume (one per T1/T2 article), bounded in
scope, and do not require the prose quality of the synthesis passes.

### Approach A (Recommended): Separate settings key for prose model

Add a `digest_prose_model` key to `DEFAULT_SETTINGS` in `db.py`. When
`generate_digest()` reads settings, it reads both `ollama_model` (for
per-article analysis) and `digest_prose_model` (for pitch, synthesis, editor,
and revision). If `digest_prose_model` is empty or absent, fall back to
`ollama_model`.

**Advantages:** Operator-configurable via the existing settings UI. No
hardcoded model names. The two models can be tuned independently. The
per-article analysis model can stay small and fast; the prose model can be
as large as the hardware allows.

**Disadvantages:** Adds a settings key — the UI will need a new field, or
the admin must set it via the database directly. The plan does not cover UI
changes; that can be a follow-on.

### Approach B: Single upgraded model for all passes

Set `ollama_model` to qwen2.5:32b globally. Per-article analysis calls use
the same model as synthesis.

**Advantages:** Simpler — one model, one setting.

**Disadvantages:** Per-article calls are the high-volume bottleneck. Running
32B for each of 8–15 article analyses per digest will roughly triple latency.
At `digest_schedule = "0 6,17 * * *"` (twice daily), this may be acceptable,
but it is wasteful when a smaller model is adequate for that task.

### Recommendation

**Approach A.** The two-model split is architecturally sound and matches the
two distinct task types in the pipeline. The settings-based configuration is
consistent with how the codebase already manages all Ollama parameters.

### Implementation Steps (Fix 3)

1. **`db.py` — `DEFAULT_SETTINGS`**: Add `"digest_prose_model": ""`. An empty
   string means "use `ollama_model`", which preserves existing behavior for
   operators who don't set it.

2. **`db.py` — `init_db()`**: The settings table uses INSERT OR IGNORE, so the
   new default will appear automatically for new installs. Existing installs
   need a migration: add an explicit INSERT OR IGNORE for `digest_prose_model`
   in the migration block, or rely on the settings UI to expose it.

3. **`digest.py` — `generate_digest()`** (line 1483–1485):
   ```python
   model = settings.get("ollama_model", "llama3.2")
   prose_model = settings.get("digest_prose_model", "") or model
   temperature = float(settings.get("ollama_temperature", "0.3"))
   ```

4. **`digest.py` — `_generate_pitch()` call** (line 1589): Pass `prose_model`
   instead of `model`.

5. **`digest.py` — synthesis `_call_ollama_streaming()` call** (line 1629):
   Use `prose_model`.

6. **`digest.py` — `_run_editor()` call** (line 1663): Pass `prose_model`.

7. **`digest.py` — `_apply_editor_revision()` call** (line 1665): Pass
   `prose_model`.

8. **`digest.py` — review-and-revise loop** (line 1720): Use `prose_model`
   for revision calls.

9. **Function signatures**: `_generate_pitch()`, `_run_editor()`,
   `_apply_editor_revision()` all accept `model: str` — no signature changes
   needed, just pass the right model at call sites.

**Context window note**: qwen2.5:32b at Q4 should handle 32768 tokens without
issue. The synthesis prompt can run 8K–10K tokens; the editor prompt can reach
12K+ with five full recent sections. The existing `synth_ctx = max(32768, ...)`
and `num_ctx=32768` for the editor pass are appropriate. The targeted rewrite
pass (Fix 2) should use `num_ctx=16384` since it operates on a single section.

---

## Fix 4: Contextual LLM Comparison (Strengthening the Editor Pass)

### Problem

The existing editor pass (`_run_editor`) already attempts contextual
comparison: it passes the last 5 opening lines and the last 5 first sections,
and asks the LLM to rate OPENING ECHO as VARIED / MILD ECHO / STRONG ECHO.

Why it fails:
- **The model (llama3.2) cannot hold and compare six pieces of text
  simultaneously** while also checking pitch compliance, tonal patterns, and
  repeated phrases. The task exceeds its instruction-following capacity.
- **The context is too compressed**: recent sections are truncated to 800 chars
  (line 1941). This is often less than one full section, so the comparison
  is done against truncated fragments.
- **The revision instruction is too abstract**: after identifying STRONG ECHO,
  the editor says "rewrite the opening sentence — it follows the same pattern
  as recent digests." The 8B model cannot translate "same pattern" into a
  specific rewrite when the pattern is structural (subject type + verb form)
  rather than lexical.
- **The editor and the reviser are the same weak model**: identifying the
  problem and fixing it both require judgment the model lacks.

The user's constraint: Fix 4 must NOT add hardcoded forbidden phrases. The
LLM must do contextual comparison and judgment.

### Approach A (Recommended): Separate, focused differentiation pass

Rather than asking one LLM call to do comparison + diagnosis + pitch-checking
simultaneously, split the concern into a dedicated **differentiation pass**
that runs as a *pre-synthesis constraint generator* — before synthesis, not
after.

**The mechanism:**

After the pitch pass and before synthesis, run a new `_generate_diff_constraints()`
function. This function shows the model the last 3–5 opening paragraphs
(not just the first sentence — the full first paragraph of each recent digest,
up to 400 chars each). It asks a single, focused question: *Given these recent
openings, what specific rhetorical moves, sentence structures, and subject
choices are overused? Return a short list of specific AVOID directives.*

The output — call it `diff_constraints` — is injected into the synthesis
prompt alongside the pitch, as a section titled "DIFFERENTIATION REQUIREMENTS
(based on comparing recent digests)". This constrains the model *while
writing*, not after the fact.

The editor pass is then repurposed: instead of trying to diagnose similarity
(which it does poorly), it is made a **compliance check** — it receives the
`diff_constraints` alongside the draft and checks whether each constraint was
honored. The revision model, working from a specific list of what was required
vs. what was produced, can execute targeted fixes much more reliably.

**What this changes:**

1. The structural similarity problem is addressed upstream (before synthesis)
   rather than downstream (after the draft exists).
2. The comparison judgment happens in a context-window-controlled call with a
   single, bounded task. The stronger prose model (Fix 3) handles this call.
3. The editor's job shrinks to binary compliance checking rather than open-ended
   similarity detection. This is a task even a moderate model can execute.

**Example diff_constraints output:**
```
DIFFERENTIATION REQUIREMENTS:
1. Do not open with an abstract noun as subject (recent openers: "The erosion...",
   "The acceleration...", "The quiet...", "The pressure...").
2. Do not begin the first sentence with a gerund or noun phrase that positions
   the day's coverage as a unified force.
3. The Patterns section has used "suggests that" as a bullet-closing hedge in
   4 of the last 5 digests — do not use it.
```

The synthesis model receives this alongside its normal prompt. It now has
concrete structural guidance, not vague "be different" instructions.

**Advantages:** Attacks the right problem (structural convergence, not lexical
repetition). Generates positive constraints rather than negative bans. Upstream
injection beats downstream correction for prose tasks. The comparison judgment
uses the stronger prose model.

**Disadvantages:** Adds one LLM call per digest (the differentiation pass).
The diff_constraints quality depends on having at least 3 recent digests to
compare against; for new installs or after a gap in publishing, the output
will be sparse. Mitigate by requiring at least 3 digests before running the
pass.

### Approach B: Strengthen the existing editor prompt

Revise EDITOR_PROMPT to:
- Increase the recent section budget from 800 to 1500 chars
- Narrow the task: one call for structural comparison only (remove pitch
  compliance check)
- Add an explicit sentence structure parsing step: "Write the grammatical
  skeleton of today's opening: [SUBJECT TYPE] + [VERB TYPE]. Write the
  skeleton of each recent opening. Count how many match."

Then run a second dedicated revision call using the prose model with the
structural diagnosis.

**Advantages:** Does not add a new pass — stays within the existing four-call
structure.

**Disadvantages:** Still asks one model call to both compare and diagnose.
Even with a stronger model, combining comparison + repair in a single
downstream correction is less reliable than injecting constraints upstream.
And the repair model is still working against an existing draft, which induces
conservative edits.

### Recommendation

**Approach A.** The root insight is that correcting a draft for structural
similarity is a much harder task than writing with structural constraints in
place from the start. The existing editor pass architecture is sound for
content-quality checks (boilerplate phrases, quote attribution, pitch compliance)
but the wrong tool for structural differentiation. Separating the two concerns
produces a cleaner pipeline.

### New prompt: DIFF_CONSTRAINTS_PROMPT

```
You are a prose analyst. Your job is to identify structural patterns in a
series of briefings so that the next briefing can deliberately differ.

**Recent briefing openings (first ~300 words of each):**
{recent_openings_full}

Analyze these openings for structural patterns that repeat across 3 or more
of them. Focus on:

1. SUBJECT TYPE PATTERN: What type of noun/phrase typically starts the
   first sentence? (abstract noun, proper noun, temporal phrase, question, etc.)
2. VERB PATTERN: What verb construction follows? (passive voice, "reveals",
   "signals", existential "there is/are", etc.)
3. PARAGRAPH STRUCTURE: How does the first paragraph develop? (catalog of
   examples, single-claim + evidence, assertion + qualification, etc.)
4. CLOSING MOVE: How does the opening paragraph typically end? (prediction,
   question, call to attention, etc.)

For each pattern you identify across 3+ openings, write one specific AVOID
directive that the next briefing's writer can apply.

Output ONLY a numbered list of AVOID directives. Maximum 5 directives.
Each directive must be specific and structural, not lexical.
Do not produce directives about individual word choices — focus on
sentence structure, subject type, and rhetorical move.

Example of a good directive: "Do not open with an abstract noun as subject
(recent openers used 'The erosion', 'The acceleration', 'The pressure')."
Example of a bad directive: "Do not use the word 'reveals'."
```

### Implementation Steps (Fix 4)

1. **`digest.py` — new constant `DIFF_CONSTRAINTS_PROMPT`**: Add the prompt
   defined above.

2. **`digest.py` — new function `_generate_diff_constraints(recent_digests, prose_model, temperature)`**:
   - Require at least 3 recent digests; return `""` if fewer.
   - Build `recent_openings_full` by extracting the first 300–400 chars of
     the Big Picture / The Thread / The Signal section (whichever heading the
     digest uses) from each recent digest. Use the existing `_extract_first_section()`
     helper, but allow up to 400 chars rather than 1500.
   - Call `_call_ollama_streaming()` with the prose model.
   - Return the constraints text, or `""` on error.

3. **`digest.py` — `generate_digest()`**: After the pitch call (line 1591),
   before synthesis, call `_generate_diff_constraints()` and store in
   `diff_constraints`.

4. **`digest.py` — synthesis prompt construction** (line 1603–1622): After
   the pitch injection block, add:
   ```python
   if diff_constraints:
       diff_injection = (
           f"\n\n**DIFFERENTIATION REQUIREMENTS (based on recent digests):**\n"
           f"{diff_constraints}\n\n"
           f"These requirements describe structural patterns to avoid. "
           f"Honor them — not by avoiding specific words, but by choosing "
           f"different sentence structures and rhetorical moves.\n"
       )
       synthesis_prompt = synthesis_prompt + diff_injection
   ```

5. **`digest.py` — EDITOR_PROMPT**: Revise to remove the OPENING ECHO
   structural detection task (which is now handled upstream). The editor
   retains: REPEATED PHRASES, TONAL NOTE, PITCH COMPLIANCE, and a new
   **CONSTRAINT COMPLIANCE** check that verifies the diff_constraints
   were honored. Add `diff_constraints` as a new template variable.

6. **`digest.py` — `_run_editor()` signature**: Add `diff_constraints: str`
   parameter. Pass it to the EDITOR_PROMPT format call.

7. **`digest.py` — `generate_digest()` editor call** (line 1663): Pass
   `diff_constraints` to `_run_editor()`.

8. **Increase recent section budget in `_run_editor()`** (line 1941): Change
   truncation from `section[:800]` to `section[:1200]` to give the editor
   enough context for pitch compliance checking.

---

## Dependencies and Implementation Order

The fixes are largely independent but have one dependency:

- Fix 3 (prose model) must be implemented first, or simultaneously with
  Fix 4. The diff_constraints pass and the targeted rewrite in Fix 2 both
  depend on the prose model being available.
- Fix 1 (deduplication) and Fix 2 (hard rejection) are independent of each
  other and of Fix 3/4.
- Fix 4 depends on Fix 3 in spirit (the diff_constraints pass with llama3.2
  will underperform), but the code for Fix 4 is valid independently of Fix 3.

**Recommended implementation order:**

1. Fix 3 (prose model setting) — smallest change, unlocks the rest
2. Fix 1 (article deduplication) — schema migration, most impactful on
   article diversity
3. Fix 4 (differentiation pass) — new prompt and new pass, uses the prose model
4. Fix 2 (hard rejection) — depends on Fix 3 for the targeted rewrite to work;
   implement last so the hard rejection uses the stronger model

---

## Files Affected

| File | Change Type | Notes |
|------|-------------|-------|
| `db.py` | Modify | Add `digest_articles` table schema, `save_digest_articles()`, `get_recently_featured_article_ids()`, `delete_digest_articles()`, update `save_digest()`, add `digest_prose_model` default setting |
| `digest.py` | Modify | Two-model split in `generate_digest()`, new `_generate_diff_constraints()`, new `_check_boilerplate_severe()`, new `_rewrite_section_targeted()`, `_run_editor()` signature update, updated editor prompt, updated synthesis prompt injection |
| `digest.py` constants | Modify | Add `DIFF_CONSTRAINTS_PROMPT` |
| `regen_digests.py` | Modify | Must call `delete_digest_articles()` before regenerating a digest to keep the junction table consistent |

No changes to `scheduler.py`, `app.py`, `pipeline.py`, or any other file.

---

## Risks and Mitigations

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| `digest_articles` junction table empty on existing installs | High | On first run after migration, `get_recently_featured_article_ids()` returns empty set — deduplication has no effect until a few digests have been generated post-migration. This is acceptable. |
| qwen2.5:32b not installed on operator's machine | Medium | Fix 3 falls back to `ollama_model` if `digest_prose_model` is empty. Document that the operator must `ollama pull qwen2.5:32b` (or :14b) before the setting takes effect. |
| `_generate_diff_constraints()` produces noisy or contradictory directives | Medium | Guard: max 5 directives, require 3+ recent digests. Add error handling that returns `""` on parse failure. Worst case is the synthesis prompt runs without the constraint block. |
| Targeted rewrite (Fix 2) drops content from the fixed section | Medium | Constraint: the rewrite prompt must include the full article content of the affected T1/T2 articles as grounding. Add a sanity check: if the rewritten section is shorter than 30% of the original, discard it and fail instead. |
| `regen_digests.py` inconsistency if run after partial migration | Low | Run `regen_digests.py` only after migration is complete. Add a comment to the script documenting this dependency. |
| Digest generation time increases significantly with prose model | Medium | Each prose pass (pitch, synthesis, editor, diff_constraints) adds latency proportional to model size. A 32B model may take 3–5x longer per pass than 8B. At twice-daily schedule this is acceptable (~10–20 min total). Per-article calls remain on the small model. |
| `diff_constraints` injection makes synthesis prompt too long | Low | The diff block adds ~200–500 chars. The existing context calculation already provides headroom (`synth_ctx = max(32768, ...)`). No change needed. |

---

## Testing Strategy

Because Sieve has no unit test suite, verification is manual and log-based.

1. **Fix 1 verification**: After migration, generate two consecutive digests
   manually (`regen_digests.py --date YYYY-MM-DD` for two consecutive dates).
   Inspect the `digest_articles` table: verify T1/T2 articles from day N do
   not appear as T1/T2 in day N+1's deep dive list. Check logs for the
   "recently featured, demoting to context" message.

2. **Fix 2 verification**: Inject a synthetic boilerplate phrase into a draft
   (via a test script that patches `_check_boilerplate_severe()` to return a
   forced result) and verify that after exhausting revisions, `generate_digest()`
   returns `success=False` with a populated `error` field and no digest row
   is written.

3. **Fix 3 verification**: Set `digest_prose_model` to qwen2.5:32b, set
   `ollama_model` to llama3.2. Run a single digest and check the Ollama logs
   (or add `logger.info(f"Using prose model: {prose_model}")` at the top of
   `generate_digest()`). Confirm that the per-article analysis calls use
   llama3.2 and the synthesis/editor/pitch calls use qwen2.5:32b.

4. **Fix 4 verification**: Run a digest with at least 3 recent digests in the
   database. Check that `diff_constraints` is non-empty in the logs
   (`logger.info(f"Diff constraints: {diff_constraints[:200]}")`). Inspect
   the synthesis prompt (add a debug log of the first 500 chars of
   `synthesis_prompt` when DEBUG logging is enabled). Confirm the constraint
   block appears in the prompt. After 3–5 days with the fix live, compare
   opening sentences manually.

---

## Definition of Done

- [ ] `digest_articles` table exists in the schema; `init_db()` is idempotent
- [ ] `save_digest()` populates `digest_articles` for every new digest generation
- [ ] `generate_digest()` excludes recently-featured T1/T2 articles from `deep_dive_articles`; logs when articles are demoted
- [ ] `regen_digests.py` clears and rebuilds `digest_articles` entries for regenerated dates
- [ ] `_check_boilerplate_severe()` is a standalone callable
- [ ] After `MAX_REVIEW_ITERATIONS`, if severe boilerplate remains after targeted rewrite, `generate_digest()` returns `success=False` with populated `error`; `save_digest()` is not called in this path
- [ ] `digest_prose_model` setting exists in `DEFAULT_SETTINGS` with empty-string default
- [ ] `generate_digest()` reads both models; passes `prose_model` to pitch, synthesis, editor, revision, diff_constraints calls; passes `model` to per-article analysis
- [ ] `DIFF_CONSTRAINTS_PROMPT` exists as a module-level constant
- [ ] `_generate_diff_constraints()` exists, requires 3+ recent digests, returns `""` gracefully on error
- [ ] `diff_constraints` is injected into synthesis prompt when non-empty
- [ ] `EDITOR_PROMPT` includes a CONSTRAINT COMPLIANCE check using `diff_constraints`
- [ ] Five consecutive digests produced after all fixes are live show no repeated opening patterns
- [ ] No regression: existing digest structure (section ordering, link injection, quote checking) still works
