# Plan: Creative Pipeline — Pitch Phase and Editor Phase

## Context

### What exists today

The digest pipeline as of 2026-03-08 has seven `DigestStyle` variants (implemented per `digest-variety-plan.md`), a per-article analysis pass (one LLM call per T1/T2 article), a synthesis call, and a review-and-revise loop of up to three iterations. The review loop checks for fabricated quotes, duplicate sections, wrong attribution, and boilerplate phrases.

The prior plan (structural variety) was the right first move. It is in production. The structural variety works — the section headings, organizational logic, and synthesis framing do differ across days. What it did not fix is **prose formulaicism at the sentence level**, because that operates below the level where style directives are applied.

### The specific failure modes documented

From the user's empirical observation:

- **41% of openings** begin with "Today's [X] reveals..." — the model has learned a crutch pattern that recurs even when the prompt bans it by name
- **54 instances of "raises questions about"** despite it being explicitly banned — the model learned to treat banned-phrase lists as suggestions
- **New formulaic patterns emerge** to replace banned ones — whack-a-mole is not a strategy

### Why the existing review loop does not solve this

The existing review loop (`review_digest` → `REVIEW_REVISION_PROMPT`) operates on structural and factual correctness: fabricated quotes, missing sections, boilerplate repetition at threshold. It is well-suited to that. It is ill-suited to prose variety for two reasons:

1. **It runs post-hoc against one digest in isolation.** It cannot compare today's output against yesterday's. It does not know that "raises questions about" appeared 7 times last week — it only knows it appeared N times in this digest.
2. **Revision calls at this model size degrade structure.** The revision prompt asks the model to rewrite the full briefing, which introduces dropped sections, duplicated headings, and formatting drift — bugs the review loop then has to catch in the next iteration. The loop is already fighting itself.

The new interventions must operate at different points in the pipeline and address different failure modes than the existing loop.

---

## The Core Diagnosis

The model at 8B parameters with temperature 0.3 is operating in a very narrow region of its output distribution. It has strong priors for "what a good briefing looks like" — priors that come from its training data, which includes enormous amounts of journalism and summarization content that begins with "Today's [X] reveals...".

Banning phrases does not change the underlying distribution. The model finds the next-highest-probability token that isn't banned. This is why new crutches emerge.

The two proposed interventions attack this from different angles:

- **The pitch phase** forces the model to commit to a specific creative strategy *before* it writes. It is harder for the model to generate a plan that says "I will open with 'Today's top stories reveal...'" when the pitch prompt explicitly asks for a concrete, unusual angle. The plan constrains the synthesis call.
- **The editor phase** compares *across days* rather than within a single digest. It surfaces concrete similarity signals — specific phrases, sentence structures, opening patterns — that the model can use as specific revision targets. "Be more creative" is useless. "Your first sentence follows the same structure as Monday and Tuesday's first sentence — here are all three" is actionable.

---

## Approach (Recommended): Pitch + Editor, replacing the revision loop

### High-level flow

```
[existing] Per-article analysis calls (N calls for T1/T2 articles)
[NEW]      Pitch call (1 call, ~30-60 sec)
[existing] Synthesis call (1 call, reads the pitch)
[existing] Post-processing: strip bad quotes, inject links
[NEW]      Editor call (1 call, reads the last 5 digests + today's draft)
[NEW]      Revision call (1 call, reads the editor notes) — REPLACES current loop iteration 1
[existing] Final quote verification pass
[existing] Save to database
```

The existing review loop (structural/factual checks) is **retained** but simplified: it runs once after the editor-guided revision, not up to three times. Its job remains structural — duplicate sections, fabricated quotes — not prose quality.

---

## Phase 1: The Pitch

### Position in the pipeline

The pitch call runs **after all per-article analyses are complete and before the synthesis call**. It receives the article analyses as input — this is important, because the pitch needs to know what the day's actual content is to generate a specific strategy, not a generic one.

The pitch is passed to the synthesis call as an additional prompt section that instructs the synthesis model on the creative approach it committed to before writing.

### What the pitch call receives

The pitch prompt gets:
- A compact summary of today's T1/T2 articles (titles + one-sentence summaries of each analysis, not the full analyses — keep context short)
- The current DigestStyle name and its synthesis_directive (so the pitch operates within the structural frame already chosen)
- The opening line from the last 3 digests (to help the model understand what it has done recently and needs to avoid)
- An explicit instruction: produce a plan, not the prose itself

### What the pitch produces

The pitch produces a structured block, 150-300 words, with four named fields:

```
ANGLE: [One sentence — what conceptual angle is today's synthesis taking? Not "I will discuss the articles" but a genuine interpretive claim. E.g., "The week's top stories are all downstream of a single institutional failure: the U.S. regulatory agencies have decided that reacting is cheaper than preventing."]

OPENING STRATEGY: [One sentence — how does the synthesis begin? The opening sentence is not a summary. E.g., "Open with the specific finding from [Article X] that is the sharpest evidence for the angle, without announcing what it means."]

THREAD: [One sentence — what runs through the Patterns section? Not a restatement of the angle but the specific connective tissue between articles. E.g., "Three articles today are each about a different industry betting that the government will look the other way."]

WHAT TO AVOID: [One sentence — what is the most obvious/formulaic way to write this digest, which should be avoided? E.g., "The obvious move is to open with AI dominating the news cycle — resist it; the more interesting signal today is the healthcare stories."]
```

### How the pitch constrains the synthesis call

The pitch output is injected into the synthesis prompt as a new section between the article summaries and the section structure:

```
**Today's creative brief (you produced this before writing — honor it):**
{pitch_output}

Now write the digest sections. Your opening sentence must reflect the OPENING STRATEGY above.
Your Patterns section must reflect the THREAD above. Do not default to the most obvious angle.
```

The key phrase is "you produced this before writing — honor it." At this model size, telling the model it made a prior commitment increases the probability it follows through. It is a lightweight form of chain-of-thought anchoring.

### Preventing the pitch itself from being formulaic

This is a real risk. The pitch prompt must be written so the model cannot produce a generic pitch. Several mechanisms:

1. **Require specificity in the ANGLE**: The prompt explicitly instructs "Your ANGLE must name at least one specific article and one specific fact from that article. A generic angle ('today's stories reveal tensions between technology and regulation') will be rejected."

2. **Require contrast in WHAT TO AVOID**: "WHAT TO AVOID must name the specific formulaic move the model would default to today, given these specific articles. Generic instructions ('avoid being generic') will not be accepted."

3. **Keep the pitch call context small**: The pitch prompt receives article titles + one-sentence analysis summaries, not full analyses. This forces the model to engage with specifics rather than generating prose from a position of content abundance.

4. **Temperature**: The pitch call uses `temperature + 0.15` above the base (so 0.45 at default settings). The pitch is a brainstorming call; higher entropy is appropriate. The synthesis call retains the configured temperature.

### Example pitch prompt (system prompt)

```
You are Abend, a rogue AI that writes a daily briefing. Before you write today's briefing,
you must produce a creative brief that will constrain your writing.

Today's articles to synthesize:
{article_title_summaries}

The structural frame for today (you must work within it):
Style: {style_name}
Synthesis directive: {synthesis_directive}

Recent opening lines (do not repeat these patterns):
- {digest_minus_1_opening}
- {digest_minus_2_opening}
- {digest_minus_3_opening}

Produce a creative brief with EXACTLY these four fields. Each field is one sentence.
Be specific — name articles and facts. Generic answers are not acceptable.

ANGLE: [What is the single interpretive claim today's synthesis advances?
Name at least one specific article and one specific fact that anchors it.]

OPENING STRATEGY: [How does the first sentence of The Big Picture begin?
Not "I will open with..." — describe the actual rhetorical move and what it refers to.
Do NOT say "Today's [X] reveals" or any variation of that construction.]

THREAD: [What specific pattern connects the Patterns & Signals bullets?
Name at least two articles and what they have in common that is not obvious.]

WHAT TO AVOID: [What is the most tempting formulaic move given today's content?
Name the specific crutch and why it is wrong today.]
```

### Example pitch output (what a good pitch looks like)

```
ANGLE: Meta's antitrust loss and the FTC's new AI guidelines both represent the same
bureaucratic momentum — regulators who spent years losing are now winning, and the industry
hasn't adjusted its playbook to match.

OPENING STRATEGY: Open on the specific dollar figure in the Meta settlement — not as a
number but as a timestamp marking when the regulatory era actually changed.

THREAD: Three stories — Meta, Apple App Store, and the FDA's algorithm guidance — are each
about institutions that built business models on the assumption that enforcement was theater;
all three are now discovering that assumption was wrong.

WHAT TO AVOID: The obvious move is to lead with AI because two AI stories scored highest —
resist it; the AI stories today are symptoms, not the story, and the regulatory thread is
more specific and more interesting.
```

### Example pitch output (what a BAD pitch looks like — to show the contrast)

```
ANGLE: Today's stories reveal the complex interplay between technology and regulatory power.

OPENING STRATEGY: Open with the most important story and explain why it matters.

THREAD: Multiple articles connect themes of data sovereignty and power consolidation.

WHAT TO AVOID: Being too generic or repetitive.
```

The prompt must be written so the bad version is structurally impossible to produce. The "name at least one specific article" constraint eliminates the first bad example. The "do NOT say 'Today's [X] reveals'" constraint eliminates the second. The "name the specific crutch" constraint eliminates the fourth.

---

## Phase 2: The Editor

### Position in the pipeline

The editor call runs **after the synthesis, after post-processing (link injection, quote verification), and before any revision**. It receives:
- The complete draft digest (stripped of the Sources section for token efficiency)
- Extracted first sentences from the last 5 digests
- Extracted opening paragraphs from the last 5 digests (the Big Picture / equivalent section)
- A list of the 10 most recently overused phrases (extracted via regex from the last 5 digest bodies)

The editor produces structured markup — not a rewrite, a critique — which is then passed to a single revision call.

### What the editor looks for (specific similarity signals)

The editor prompt asks the model to check for four specific categories, each with examples:

**1. Structural echo in the opening**

Does the first sentence follow the same grammatical template as any of the last 5 openings? Examples of structural echo:
- All five start with a proper noun subject: "Meta's antitrust ruling...", "Apple's new policy...", "The FCC's decision..."
- All five start with a present-tense claim about what "signals" or "reveals" or "shows"
- All five start with a temporal frame: "This week...", "Over the past 24 hours..."

The editor flags the pattern if 3 or more of the last 5 openings share the same grammatical structure, and today's opening matches it.

**2. Phrase repetition across recent digests**

Are specific phrases from today's draft also present in 2 or more of the last 5 digests? The editor looks for phrases of 4+ words (not just single words) that recur. Examples: "raises questions about", "highlights the tension between", "what it means for", "as the evidence suggests".

The editor lists each repeated phrase, how many times it appeared in recent digests, and where it appears in today's draft.

**3. Tonal sameness**

Does today's Patterns & Signals section use the same register as recent ones? The editor compares:
- Are all bullets declarative observations ("X reveals Y")?
- Are all bullets structured as "[Article] + [verb] + [claim]" in the same sequence?
- Does every bullet use the same hedging vocabulary (e.g., "suggests", "points to", "indicates")?

**4. The pitch-honoring check**

Did the synthesis honor the pitch? The editor checks:
- Does the opening sentence reflect the OPENING STRATEGY from the pitch?
- Does the ANGLE appear anywhere in the Big Picture?
- Does the THREAD appear in the Patterns section?

If the synthesis ignored the pitch, the editor notes this specifically — because the revision call should re-enforce it.

### How the editor formats its feedback

The editor produces a structured critique, not a rewrite. The format is:

```
EDITOR NOTES — [date]

OPENING ASSESSMENT: [One of: VARIED / MILD ECHO / STRONG ECHO]
[If MILD or STRONG ECHO: Describe the specific structural similarity. Quote the similar openings.]

REPEATED PHRASES: [NONE / list of phrases with counts]
[For each: "Phrase X appears in today's draft and in N of the last 5 digests."]

TONAL NOTE: [FINE / NOTE]
[If NOTE: Describe the specific tonal pattern (all declarative, all hedged, etc.)]

PITCH COMPLIANCE: [HONORED / PARTIAL / IGNORED]
[If PARTIAL or IGNORED: Quote the pitch field that was not honored and explain what's missing.]

PRIORITY REVISIONS:
1. [Most important specific fix with what to change and how]
2. [Second fix]
3. [Third fix if applicable — otherwise omit]
```

The PRIORITY REVISIONS section is the key output. It must be written as concrete surgical instructions, not general guidance. The implementer writing the revision prompt will use this directly.

### How the editor is different from the existing review loop

| Dimension | Existing review loop | Editor phase |
|-----------|---------------------|--------------|
| Checks against | This digest only | Last 5 digests + today |
| What it detects | Structural bugs, fabricated quotes, banned phrases | Cross-day repetition, prose formulaicism, pitch compliance |
| Output | List of issues | Structured critique with specific priority revisions |
| Triggers | Always (up to 3 iterations) | Always (1 pass, produces notes even if "all good") |
| Acts on | Full rewrite request | Targeted surgical revision |

The existing review loop is not replaced — it continues to catch structural and factual bugs. The editor phase catches prose quality degradation.

### Example editor call (user prompt)

```
You are an editor reviewing today's Abend digest before publication.
Your job is to flag specific problems — not rewrite anything.

TODAY'S DRAFT:
{draft_content}

CREATIVE BRIEF THAT WAS SUPPOSED TO GUIDE THIS DRAFT:
{pitch_output}

RECENT OPENING LINES (last 5 digests):
1. [{date_5}] {opening_5}
2. [{date_4}] {opening_4}
3. [{date_3}] {opening_3}
4. [{date_2}] {opening_2}
5. [{date_1}] {opening_1}

RECENT BIG PICTURE SECTIONS (last 5 digests):
[{date_5}] {big_picture_5}
[{date_4}] {big_picture_4}
[{date_3}] {big_picture_3}
[{date_2}] {big_picture_2}
[{date_1}] {big_picture_1}

Check for these specific problems:

1. OPENING ECHO: Does today's opening sentence follow the same grammatical structure
   as 3 or more of the last 5 openings? Look at: subject type (proper noun vs. pronoun
   vs. temporal frame), main verb type, presence of "reveals"/"shows"/"signals".

2. REPEATED PHRASES: Find 4-word-or-longer phrases that appear in today's draft AND
   in 2 or more recent Big Picture sections. List them.

3. TONAL PATTERN: Do the Patterns & Signals bullets all share the same grammatical
   structure? (e.g., all "[Source] reports that [claim]", or all "X highlights Y")

4. PITCH COMPLIANCE: Did the synthesis honor the creative brief? Check each of the
   four ANGLE / OPENING STRATEGY / THREAD / WHAT TO AVOID fields.

Output ONLY the structured editor notes in this format:

EDITOR NOTES — {date}

OPENING ASSESSMENT: [VARIED / MILD ECHO / STRONG ECHO]
[explanation if not VARIED]

REPEATED PHRASES: [NONE / list]

TONAL NOTE: [FINE / description of pattern]

PITCH COMPLIANCE: [HONORED / PARTIAL / IGNORED]
[explanation if not HONORED]

PRIORITY REVISIONS:
[numbered list of specific fixes, or "None required." if all assessments are positive]
```

### Extracting opening lines and Big Picture sections from prior digests

The `get_recent_digests(limit=5)` function already exists in `db.py`. Extracting the opening line and Big Picture section from each digest requires a helper function. The Big Picture is reliably the first `##`-level section (after `_reorder_sections()` runs). The opening line is the first non-empty, non-heading line of content.

Two helper functions are needed in `digest.py`:

```python
def _extract_opening_line(content: str) -> str:
    """Extract the first non-empty, non-heading prose line from a digest."""
    for line in content.split('\n'):
        stripped = line.strip()
        if stripped and not stripped.startswith('#') and not stripped.startswith('-'):
            return stripped[:300]  # cap at 300 chars
    return ""

def _extract_big_picture_section(content: str) -> str:
    """Extract the text of the first ## section (Big Picture equivalent)."""
    lines = content.split('\n')
    in_section = False
    parts = []
    for line in lines:
        if line.strip().startswith('## ') and not in_section:
            in_section = True
            continue
        elif line.strip().startswith('## ') and in_section:
            break
        elif in_section:
            parts.append(line)
    return '\n'.join(parts).strip()[:1500]  # cap at 1500 chars
```

---

## Integration with the Existing Pipeline

### New `generate_digest()` flow

```python
# [unchanged] Load settings, select style, fetch articles, format tiers

# [unchanged] Phase 1: Per-article analysis calls
article_analyses = [_analyze_single_article(...) for each T1/T2 article]

# [NEW] Phase 1.5: Pitch call
pitch = _generate_pitch(article_analyses, style, recent_openings)
logger.info(f"Pitch generated: {pitch[:200]}")

# [modified] Phase 2: Synthesis call — now receives pitch
synthesis = _call_synthesis(article_analyses, tiered, style, pitch)

# [unchanged] Phase 3: Assembly and post-processing
content = assemble_and_postprocess(synthesis, article_analyses, included)

# [NEW] Phase 3.5: Editor call
recent_digests = get_recent_digests(limit=5)
editor_notes = _run_editor(content, pitch, recent_digests)
logger.info(f"Editor notes: {editor_notes[:300]}")

# [NEW] Phase 4: Editor-guided revision (1 call, replaces the first iteration of the loop)
if editor_notes_require_revision(editor_notes):
    content = _apply_editor_revision(content, editor_notes, included, style)
    content = strip_unverifiable_quotes(content, included)
    content = inject_article_links(content, included)

# [unchanged, simplified] Phase 5: Structural review loop (max 2 iterations instead of 3)
for revision_round in range(2):  # reduced from 3 — editor handles prose quality
    review = review_digest(content, included, style)
    if review["passed"]:
        break
    content = _apply_structural_revision(content, review, included, style)
```

### Changes to the synthesis call

The synthesis call receives one new argument: the pitch output, injected into the synthesis system prompt. The current `SYNTHESIS_PROMPT` string gets a new section at the bottom of the preamble:

```
**Creative brief you committed to before writing (honor it):**
{pitch_output}

Your opening sentence must not begin with "Today's" or any form of "X reveals".
Your opening must reflect the OPENING STRATEGY from the brief above.
```

The `{pitch_output}` placeholder is added to `SYNTHESIS_PROMPT`. When pitch is not available (error in pitch call), it falls back to empty string gracefully.

### What to remove from the current system

**Keep:**
- The `DigestStyle` system and all 7 styles — structural variety is still valuable and orthogonal to prose variety
- The boilerplate `_check_boilerplate()` function — it catches structural-level phrase repetition within a single digest and is fast (no LLM call)
- The `review_digest()` / structural review loop — retain for quote verification and section structure, reduce from 3 iterations to 2
- The `strip_unverifiable_quotes()` and `inject_article_links()` post-processors — these are mechanical corrections, not LLM calls
- Temperature deltas per style — still relevant

**Modify:**
- `SYNTHESIS_PROMPT`: add `{pitch_output}` injection point
- `generate_digest()`: add pitch and editor phases, reduce review loop max iterations from 3 to 2
- The banned phrase lists in `ARTICLE_ANALYSIS_PROMPT` and `SYNTHESIS_PROMPT`: can be reduced — the editor phase handles phrase repetition cross-day, and keeping an enormous banned list just causes the model to find adjacent crutches. Keep only the most severe (direct "This article reveals", "Today's news reveals a complex") and let the editor catch the rest.

**Remove:**
- Nothing entirely — no code should be deleted in this change. The mechanisms are sound; the new phases add to them.

---

## Performance Budget

### Current pipeline timing (estimated at ~4 tokens/sec)

| Phase | LLM calls | Estimated tokens | Estimated time |
|-------|-----------|-----------------|----------------|
| Per-article analysis | N (typically 4-8) | ~800 tokens/call | 3-5 min total |
| Synthesis | 1 | ~3072 tokens | ~13 min |
| Review/revision (up to 3) | 0-3 | ~6144 tokens/call | 0-26 min |
| **Current total range** | | | **16-44 min** |

### New pipeline timing

| Phase | LLM calls | Estimated tokens | Estimated time |
|-------|-----------|-----------------|----------------|
| Per-article analysis | N (unchanged) | ~800 tokens/call | 3-5 min total |
| **Pitch call [NEW]** | 1 | ~600 tokens output | ~2-3 min |
| Synthesis | 1 | ~3072 tokens | ~13 min |
| **Editor call [NEW]** | 1 | ~800 tokens output | ~3-4 min |
| **Editor revision [NEW]** | 0-1 | ~4096 tokens | 0-17 min |
| Review/revision (max 2) | 0-2 | ~6144 tokens/call | 0-17 min |
| **New total range** | | | **21-59 min** |

### Net impact

- Best case (no revisions needed): +2 calls, +5-7 min
- Worst case (editor revision + 2 structural revisions): +2-3 calls, +10-20 min
- Typical case: +2 calls, +5-10 min

The pitch call adds the least time (small output). The editor call adds modest time. The editor revision replaces what was previously the first structural revision call (similar token budget), so the net additional revision time is approximately one extra call in the median case.

This is within acceptable bounds given that:
- The pipeline already runs at 06:00 and 17:00 on a schedule
- A 10-15 min increase at an unattended scheduled run has no UX impact
- The alternative (continued phrase whack-a-mole with no cross-day comparison) has no ceiling

---

## What to Keep vs. Remove: Summary Table

| Component | Keep | Modify | Remove |
|-----------|------|--------|--------|
| `DigestStyle` system (7 styles) | Yes | No | No |
| Temperature deltas per style | Yes | No | No |
| `ARTICLE_ANALYSIS_PROMPT` banned phrases | Partial | Trim to severe only | Remove mild entries |
| `SYNTHESIS_PROMPT` banned phrases | Partial | Add pitch injection | Remove duplicates of editor's job |
| `_check_boilerplate()` | Yes | No change | No |
| `review_digest()` structural loop | Yes | Reduce max to 2 iterations | No |
| `strip_unverifiable_quotes()` | Yes | No | No |
| `inject_article_links()` | Yes | No | No |
| **Pitch phase** | — | — | Add new |
| **Editor phase** | — | — | Add new |

---

## Files Affected

| File | Change type | Description |
|------|-------------|-------------|
| `/home/kellogg/dev/Sieve/digest.py` | Modify | Add `PITCH_PROMPT`, `EDITOR_PROMPT`, `_generate_pitch()`, `_run_editor()`, `_extract_opening_line()`, `_extract_big_picture_section()`, `_apply_editor_revision()`. Modify `generate_digest()` to call them in sequence. Modify `SYNTHESIS_PROMPT` to add `{pitch_output}` placeholder. Reduce `MAX_REVIEW_ITERATIONS` from 3 to 2. |
| `/home/kellogg/dev/Sieve/db.py` | No change | `get_recent_digests(limit=5)` already exists at line 843 — no changes required. |

No other files are affected. The downstream consumers (`rogue_routine/scripts/export.py`, the Flask app's digest display) see only the final digest content and are unaffected by pipeline changes.

---

## Prompt Designs

### `PITCH_PROMPT` (system prompt for the pitch call)

```python
PITCH_PROMPT = """You are Abend, a rogue AI that writes a daily briefing on the attention economy.
Before writing today's briefing, you must produce a creative brief.
This brief will constrain your writing. Take it seriously.

**Today's article summaries (what you have to work with):**
{article_title_summaries}

**Structural frame for today:**
Style: {style_name}
Directive: {synthesis_directive}

**Opening lines from recent briefings (do not repeat these patterns):**
{recent_openings_block}

Produce a creative brief with EXACTLY these four fields.
Each field is one sentence. Be specific — name articles and concrete facts.
A brief that could apply to any day is a failed brief.

ANGLE: [The single interpretive claim this briefing advances.
Must name at least one specific article and one specific fact from it.
Not: "today's stories reveal tensions." Yes: "The FTC's Meta ruling and the App Store case
both mark the same inflection point: enforcement timelines that took 5+ years have arrived
simultaneously, and the industry has no playbook for simultaneous loss."]

OPENING STRATEGY: [How the first sentence of the synthesis begins.
Describe the rhetorical move and what it refers to — not "I will open with X" but
"Open on [specific fact/image/tension], without explaining what it means yet."
Do NOT plan to begin with "Today's" or any form of "[X] reveals".]

THREAD: [The specific pattern connecting the Patterns section.
Name at least two articles and what they share that isn't obvious from their headlines.]

WHAT TO AVOID: [The most tempting formulaic move given today's content.
Name the specific crutch. Why is it wrong today, specifically?]"""
```

### `EDITOR_PROMPT` (system prompt for the editor call)

```python
EDITOR_PROMPT = """You are an editor reviewing today's Abend briefing before publication.
Your job is to produce structured critique — not rewrite anything.

**Today's draft:**
{draft_content}

**Creative brief that was supposed to guide this draft:**
{pitch_output}

**Opening lines from the last 5 briefings:**
{recent_openings_block}

**Big Picture sections from the last 5 briefings:**
{recent_big_pictures_block}

Check for these specific problems:

1. OPENING ECHO: Does today's first sentence follow the same grammatical pattern as 3 or
   more of the last 5 openings? Look at: subject type (proper noun / abstract noun / temporal
   frame), main verb type, use of "reveals"/"shows"/"signals"/"highlights".

2. REPEATED PHRASES: Find 4-word-or-longer phrases that appear in today's draft AND appear
   in 2 or more of the recent Big Picture sections. List each phrase and its count.

3. TONAL PATTERN: Do the Patterns & Signals bullets share the same grammatical template?
   (e.g., every bullet starts "[Article] + 'shows that'", or every bullet ends with
   a hedged implication like "suggesting that..."). Flag if 3+ bullets match.

4. PITCH COMPLIANCE: Check each of the four brief fields against the draft.
   Did the opening strategy get honored? Is the ANGLE present in the synthesis?
   Is the THREAD visible in the Patterns section? Was WHAT TO AVOID actually avoided?

Output ONLY in this exact format:

EDITOR NOTES

OPENING ASSESSMENT: [VARIED / MILD ECHO / STRONG ECHO]
[If not VARIED: Quote today's opening and the similar past openings. Describe the shared pattern.]

REPEATED PHRASES: [NONE / list each phrase with count of recent appearances]

TONAL NOTE: [FINE / describe the pattern if present]

PITCH COMPLIANCE: [HONORED / PARTIAL / IGNORED]
[If not HONORED: Quote the unmet brief field and explain what's missing in the draft.]

PRIORITY REVISIONS:
[Number each revision. Be specific: say what to change and what direction to change it.
"Rewrite the opening sentence — it echoes Tuesday's structure (proper noun + present tense
verb + 'signals'). The pitch committed to opening on the Meta dollar figure without
explaining it. Do that instead."
Write "None required." if all assessments are positive.]"""
```

### The revision call that follows editor notes

The editor notes feed into a revision call that is structurally similar to the existing `REVIEW_REVISION_PROMPT` but scoped to the editor's priority revisions, not structural bugs:

```python
EDITOR_REVISION_PROMPT = """You are Abend. An editor reviewed your briefing and found specific problems.
Fix ONLY the issues listed in PRIORITY REVISIONS. Do not rewrite sections that are working.

**EDITOR'S PRIORITY REVISIONS:**
{priority_revisions}

**YOUR BRIEFING:**
{content}

**RULES:**
1. Fix only what is listed. Every other sentence stays exactly as-is.
2. If the editor asks you to change the opening, change only the opening.
3. If the editor references the creative brief, honor it.
4. Do not add new sections or remove existing ones.
5. Do not introduce new quotes — only use quotes already present.
6. Maintain the exact section structure: {section_names_list}

Write the corrected briefing now."""
```

---

## Risks and Mitigations

### Risk 1: The pitch is ignored by the synthesis call

The model may generate a pitch and then write the synthesis from its prior distribution without reference to it. Evidence: the model already ignores banned phrase lists in prompts.

Mitigation: The pitch is injected into the synthesis prompt in the position of highest attention — after the article summaries, immediately before the section structure instructions. The phrase "you committed to this before writing — honor it" exploits the model's tendency to treat prior context as self-authored constraints. This is more reliable than external bans.

If this proves ineffective: add a stronger mechanism — begin the synthesis user_prompt with a one-line restatement of the pitch's OPENING STRATEGY: `"Begin your synthesis by: {pitch_opening_strategy}. Now write the full briefing."` This makes the opening strategy a direct instruction, not a cited commitment.

### Risk 2: The pitch itself is formulaic

If the pitch prompt is not specific enough, the model will produce generic briefs. The example of a bad pitch above shows exactly what to prevent.

Mitigation: The constraints built into `PITCH_PROMPT` — "must name at least one specific article and one specific fact", "A brief that could apply to any day is a failed brief" — are the primary defense. Secondary: run the pitch at higher temperature (0.45 at default base) so the model is exploring a wider distribution when planning. A formulaic pitch at higher temperature is still a regression — if this pattern emerges in practice, add a light pitch-review check (Python regex, no LLM call) that fails if ANGLE contains no article title from today's set.

### Risk 3: The editor call is too long and times out

The editor receives the full draft plus 5 Big Picture sections from recent digests. At 1500 chars each, that's 7500 chars of prior-digest context plus the draft (~6000 chars). Total input is ~15,000 chars / ~3750 tokens. Output is ~500-800 tokens. This is within the 16384 context window used for synthesis calls. No timeout risk beyond what the system already tolerates.

### Risk 4: The editor produces vague priority revisions

"Be more creative" is useless. The editor prompt includes specific examples of acceptable revision instructions inline to calibrate the expected format. Additionally, the editor prompt asks for specific evidence (quoted text from openings, specific phrase lists) before stating revisions — this forces the critique to be grounded before the recommendation is written.

If this proves insufficient in practice: constrain the PRIORITY REVISIONS format further: "Each revision must quote the text to be changed and describe the direction of change in one sentence."

### Risk 5: Editor-guided revision drops structural sections

A revision call scoped to prose changes can still drift — the model may reorganize sections during rewrite.

Mitigation: The editor revision is followed by the existing structural review loop (retained, max 2 iterations). If the editor revision introduces structural problems, the structural loop catches them. The combined pipeline has two quality gates: prose-quality (editor revision) then structural-correctness (existing loop). Neither is eliminated.

### Risk 6: `get_recent_digests()` returns fewer than 5 digests early in deployment

The database may have fewer than 5 digests when the pipeline is new.

Mitigation: The helper functions `_extract_opening_line()` and `_extract_big_picture_section()` are called on however many recent digests exist (0 to 5). The pitch and editor prompts handle this gracefully: if `recent_openings_block` is empty (no prior digests), the pitch simply lacks the "avoid these patterns" constraint. The pitch is still useful without it.

---

## Testing Strategy

### Manual integration test (primary)

1. Run `generate_digest()` with logging at DEBUG level. Verify:
   - Pitch call fires and produces ANGLE/OPENING STRATEGY/THREAD/WHAT TO AVOID fields
   - Pitch is logged and injected into synthesis prompt (check logs)
   - Editor call fires and produces structured notes
   - Editor notes are logged
   - If PRIORITY REVISIONS is non-empty, revision call fires
   - Final digest has structurally correct sections

2. Compare opening sentences across 5 consecutive days after deployment. Measure: what fraction follow the same grammatical template? Target: less than 20% (vs. current 41%).

3. Regenerate the last 5 days of digests (using `regen_digests.py` if it accepts per-date arguments, otherwise manually). This tests: the editor has real prior digests to compare against.

### Unit tests (supplementary, in `tests/` if they exist)

- `_extract_opening_line()`: assert it returns the first non-heading, non-bullet prose line
- `_extract_big_picture_section()`: assert it returns text between the first and second `##` headers
- `_generate_pitch()` mock: assert that when the LLM returns a malformed pitch (missing fields), the function returns a default/empty string gracefully and does not crash `generate_digest()`
- `_run_editor()` mock: assert that when editor returns "None required.", the revision call is skipped

### Regression test

Run with the `standard` style on a day with 5+ T1 articles. Verify the output still passes `review_digest()` (structural check) on the first pass — adding the pitch and editor phases should not break structural correctness.

---

## Definition of Done

- [ ] `_extract_opening_line()` and `_extract_big_picture_section()` helper functions implemented
- [ ] `PITCH_PROMPT` defined as a module-level constant with all four `{}` placeholders
- [ ] `_generate_pitch()` function implemented: builds prompt, calls Ollama, returns raw pitch text (or empty string on error)
- [ ] `EDITOR_PROMPT` defined as a module-level constant with all `{}` placeholders
- [ ] `_run_editor()` function implemented: fetches recent 5 digests, extracts openings and Big Pictures, builds prompt, calls Ollama, returns editor notes
- [ ] `EDITOR_REVISION_PROMPT` defined for the follow-on revision call
- [ ] `_apply_editor_revision()` function implemented: extracts PRIORITY REVISIONS from editor notes, sends revision call if non-empty, returns revised content
- [ ] `SYNTHESIS_PROMPT` has `{pitch_output}` placeholder injected before the section structure
- [ ] `generate_digest()` calls pitch phase after per-article analyses
- [ ] `generate_digest()` injects pitch into synthesis prompt
- [ ] `generate_digest()` calls editor phase after post-processing
- [ ] `generate_digest()` calls editor revision if priority revisions exist
- [ ] `MAX_REVIEW_ITERATIONS` reduced from 3 to 2
- [ ] All new LLM calls are logged with phase name, input token estimate, and output length
- [ ] Error in pitch call (timeout, empty response) does not crash the pipeline — falls back to synthesis without pitch
- [ ] Error in editor call does not crash the pipeline — falls back to existing structural review only
- [ ] Manual test: pitch output contains specific article names and facts (not generic)
- [ ] Manual test: editor output when run against 3+ days of real digests identifies at least one specific repeated phrase or structural echo
- [ ] Manual test: 5 consecutive digests generated with the new pipeline show measurable opening variety

---

## Confidence Assessment

**High confidence** in the diagnosis: the cross-day comparison mechanism is the right structural answer to cross-day repetition. The existing review loop can only see one digest; it cannot solve cross-day problems. The pitch mechanism is a well-documented technique for improving LLM output quality (chain-of-thought planning before execution).

**Medium confidence** in the model's ability to honor the pitch.** The 8B model at temperature 0.3 is at the lower edge of instruction-following reliability. The pitch injection position (immediately before section structure instructions) is chosen to maximize attention. The pitch is presented as self-authored prior commitment rather than external instruction — this framing is slightly more reliable with smaller models. But behavioral compliance at this model size is probabilistic.

**Medium confidence** in the editor's critique quality.** The editor is asked to do something genuinely difficult: identify grammatical patterns across multiple texts. At 8B parameters, this kind of meta-linguistic analysis is less reliable than direct text generation. The editor prompt compensates by asking for evidence first (quote the similar openings) before stating the conclusion, which grounds the output. If editor notes prove consistently vague in practice, the phrase-repetition check (item 2) can be replaced with a Python regex pass against the prior-digest corpus — no LLM call needed for that specific check.

**Known unknowns:**
- How many actual digests are in the database. If fewer than 3, the editor comparison is too thin to be useful and should be skipped (replace with a no-op that returns "EDITOR NOTES\n\nPRIORITY REVISIONS:\nNone required." until 5 digests exist).
- Whether `regen_digests.py` supports per-date regeneration for testing. If not, a small test script calling `generate_digest(target_date="YYYY-MM-DD")` directly works.
- The exact phrase distribution in the current digest corpus. The user reports "raises questions about" 54 times — this is empirical data that should inform the initial version of the editor prompt. Pulling the actual top-10 repeated 4-gram phrases from the corpus before implementation would make the editor prompt's examples more grounded.
