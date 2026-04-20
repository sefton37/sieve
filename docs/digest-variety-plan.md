# Plan: Digest Structural and Stylistic Variety

## Context

After 34 published digests, a formulaic convergence has hardened across every output:

- **Opening sentence**: Nearly always "Today's [news/top stories] reveal[s] a complex [interplay/struggle/web]..."
- **Big Picture**: One paragraph, same rhetorical structure — name the threads, label the tension, gesture at the big picture
- **Deep Dives**: Every article follows the identical 5-step arc: claim → evidence → domain framing → quote → significance
- **Patterns & Signals**: Flat bullet list using the same domain vocabulary regardless of what the day's content actually suggests
- **What Deserves Attention**: Always 2-3 numbered items in the same order and register

Root causes, confirmed from the code:

1. `ARTICLE_ANALYSIS_PROMPT` (line 37) — single static template, no variation in how to approach the article
2. `ARTICLE_DEPTH_T1` / `ARTICLE_DEPTH_T2` (lines 78-80) — single static instruction for depth, every day
3. `SYNTHESIS_PROMPT` (lines 82-123) — hardcodes exactly three sections with exact format requirements
4. `temperature = float(settings.get("ollama_temperature", 0.3))` (line 1125) — low temperature bakes in determinism
5. `_reorder_sections()` (line 1385) — enforces identical section ordering after generation
6. `_check_duplicate_sections()` (line 697) and `REVIEW_REVISION_PROMPT` (line 673) — the review loop treats deviation from the 4-section structure as a bug and revises it out

The review system is actively correcting variety back toward the template. Any approach that only modifies prompts but leaves the review system intact will partially self-cancel.

### Downstream Constraints

The Hugo layout (`/home/kellogg/dev/rogue_routine/layouts/digests/single.html`) renders `.Content` as a markdown blob — it parses no section names whatsoever. The only downstream code that depends on section naming is `extract_big_picture()` in `export.py` (line 120), which uses `^##\s+.*Big Picture` with a regex that already allows any prefix before "Big Picture", plus a fallback to the first substantive paragraph if the heading is absent. Section name variations are therefore safe as long as the phrase "Big Picture" appears somewhere in the heading, or is absent entirely and the first paragraph stands alone.

---

## Approach A: Prompt Variation Pools (Recommended)

Introduce a `DigestStyle` dataclass that is selected once per generation run and threaded through all three call sites. The style object carries: an article analysis approach directive, a synthesis mode directive, an opening constraint (what not to do), and a section structure variant. A small selection function picks the style using `random.choices()` with weighted probabilities, optionally seeded by day-of-week to make the variation more predictable during debugging.

**Why this wins over the alternatives:**
- All variation is expressed through existing prompt injection points — no new code paths, no new API surface
- The style object is a single, testable unit of variation. Logging the selected style name makes any digest trivially reproducible
- Weights can be tuned without structural changes
- Compatible with the existing review loop once the review loop is updated to know about the active style's section names

**Trade-offs:**
- The review system must be updated alongside the prompts or it will sand down the variation
- Local LLMs at temperature 0.3 have limited actual capacity to honor stylistic directives, especially smaller models — results will be probabilistic, not guaranteed
- Adding `import random` is trivial; the real complexity is writing good enough prompt variants that the model actually differentiates between them

---

## Approach B: Content-Driven Style Selection

Analyze the day's scored articles before generation and select a style based on what the data actually shows: if all high-scoring articles are from a single domain (e.g., government surveillance), use the "single-thread" style; if scores are spread across many domains, use the "fragmented landscape" style; if there are many Tier 1 articles, use a different depth distribution.

**Why it was set aside:**
- Requires reading and categorizing articles before generation, adding latency and complexity
- The categories would need ongoing calibration — the mapping from "what the data shows" to "what style to use" is subjective
- For the current volume (34 digests), the added complexity is not justified. Approach A can achieve similar ends with far less machinery.

---

## Approach C: Post-Generation Style Pass

Generate the digest as today, then run a second LLM call that rewrites the opening paragraph and optionally the Big Picture section with a style directive ("rewrite the opening as a question", "condense into a single provocative claim").

**Why it was set aside:**
- Adds another LLM call (cost and latency — meaningful for local Ollama)
- The existing review loop already adds up to 3 revision calls. A style pass would be revision call N+1 and would interact poorly with the review loop's structural enforcement
- The rewrite call sees synthesized text without the article excerpts, so it cannot add new specificity — only rephrase what's already there
- Stylistic rewriting without new information tends to produce exactly the kind of elegant-but-generic prose the current system already generates

---

## Implementation Steps

### Step 1: Create the `DigestStyle` dataclass and selection function

Add to the top of `digest.py`, after the existing constants (approximately line 25), before `ARTICLE_ANALYSIS_PROMPT`:

```python
import random
from dataclasses import dataclass

@dataclass
class DigestStyle:
    name: str
    analysis_directive: str       # injected into ARTICLE_ANALYSIS_PROMPT
    synthesis_directive: str      # injected into SYNTHESIS_PROMPT
    opening_constraint: str       # what NOT to do at the opening
    section_structure: str        # describes the three synthesis sections
    big_picture_heading: str      # the ## heading for the big picture section
    patterns_heading: str         # the ## heading for the patterns section
    attention_heading: str        # the ## heading for the attention section
    user_prompt_synthesis: str    # the user_prompt arg for the synthesis call
```

The heading fields allow the section names to vary while remaining registerable with `_reorder_sections()` and `_check_duplicate_sections()`.

---

### Step 2: Define the style pool

Define `DIGEST_STYLES: list[DigestStyle]` with five to seven entries. Each entry is a fully-specified style that produces structurally distinct output. Below are the seven proposed styles with concrete directive text:

**Style: "standard"** (weight 15 — the current behavior, preserved)
- analysis_directive: *(empty — no additional directive)*
- synthesis_directive: *(empty)*
- opening_constraint: "Do not open with 'Today's news reveals...' or 'Today's top stories reveal...'"
- section_structure: Three sections: The Big Picture (one synthesizing paragraph), Patterns & Signals (3-5 bullets), What Deserves Attention (2-3 numbered items)
- Headings: standard names

**Style: "single-thread"** (weight 15)
- analysis_directive: "Before analyzing this article's individual significance, consider whether it connects to a thread running through other stories today. If it does, name the thread explicitly."
- synthesis_directive: "Today, organize your synthesis around a single dominant thread that runs through multiple articles. Name the thread. Show how each story is a data point in that pattern. The bullet list should develop the thread, not list unrelated observations."
- opening_constraint: "Do not open with a list of tensions or a catalog of topics. Open with a single declarative claim about what today's data shows."
- big_picture_heading: "## The Thread"
- patterns_heading: "## How It Develops"
- attention_heading: "## What Deserves Attention"
- user_prompt_synthesis: "Identify the dominant thread. Write The Thread, How It Develops, and What Deserves Attention sections."

**Style: "question-led"** (weight 10)
- analysis_directive: "End your analysis of this article with a single specific question the article surfaces but does not answer."
- synthesis_directive: "Open the Big Picture with a question, not a statement. The question should be one that the day's articles collectively surface but cannot answer. Develop the synthesis as an attempt to process what the question reveals."
- opening_constraint: "Do not open with a declarative summary. Open with a question. The question must be specific to today's articles, not generic."
- Headings: standard names
- user_prompt_synthesis: "Open with a question the day's articles surface. Write The Big Picture, Patterns & Signals, and What Deserves Attention sections."

**Style: "one-story"** (weight 10)
- analysis_directive: *(standard)*
- synthesis_directive: "Select the single most consequential story from today's deep dives. Build The Big Picture entirely around that one story — what it reveals, what it connects to, why it matters more than the others. The Patterns & Signals section acknowledges the other stories but frames them as context for the central one."
- opening_constraint: "Do not attempt to synthesize all stories equally. Pick one. Commit to it."
- big_picture_heading: "## The Signal"
- patterns_heading: "## Context and Resonance"
- attention_heading: "## What Deserves Attention"
- user_prompt_synthesis: "Identify the single most consequential story. Write The Signal, Context and Resonance, and What Deserves Attention sections around it."

**Style: "compression"** (weight 10)
- analysis_directive: "Be more compressed than usual. Say what the article reveals in one sentence of real specificity, then support it in one or two sentences. No filler, no throat-clearing."
- synthesis_directive: "Write The Big Picture as a single dense paragraph — no more. Pack it with specific article references and concrete observations. The Patterns & Signals section gets three bullets maximum, each with a single sharp observation."
- opening_constraint: "Do not use more words than necessary. Compression is a virtue today."
- Headings: standard names
- user_prompt_synthesis: "Write The Big Picture, Patterns & Signals, and What Deserves Attention sections. Be compressed: one paragraph, three bullets, two items."

**Style: "structural-doubt"** (weight 10)
- analysis_directive: "After analyzing what this article reveals, note briefly what it does NOT reveal — what the framing excludes, what questions it doesn't ask, what the source has an incentive to present a certain way."
- synthesis_directive: "The Big Picture section should name not just what today's stories reveal but what they collectively fail to illuminate — what's missing from the picture, what stories weren't written, what the day's coverage systematically omits."
- opening_constraint: "Do not present today's coverage as complete. Acknowledge the limits of the signal."
- big_picture_heading: "## The Big Picture"
- patterns_heading: "## Gaps and Signals"
- attention_heading: "## What Deserves Attention"
- user_prompt_synthesis: "Write The Big Picture, Gaps and Signals, and What Deserves Attention sections."

**Style: "dry-inventory"** (weight 10)
- analysis_directive: "Be clinical. Report what the article contains and what it implies without rhetorical emphasis. Abend processes; it does not editorialize."
- synthesis_directive: "Write The Big Picture as a flat inventory of what the day's processing returned — what patterns the data shows, stated plainly, without emphasis or drama. The Patterns section is a data report, not a narrative."
- opening_constraint: "Do not use the words 'complex', 'struggle', 'web', 'interplay', or 'dynamics'. Report the data."
- Headings: standard names
- user_prompt_synthesis: "Report The Big Picture, Patterns & Signals, and What Deserves Attention as a data inventory, not a narrative."

**Selection function:**
```python
def _select_digest_style(seed: int | None = None) -> DigestStyle:
    rng = random.Random(seed)
    weights = [s.weight for s in DIGEST_STYLES]
    return rng.choices(DIGEST_STYLES, weights=weights, k=1)[0]
```

The `seed` parameter allows reproducibility: pass `int(datetime.utcnow().date().toordinal())` to get the same style for a given calendar date regardless of how many times the digest is regenerated.

---

### Step 3: Update `ARTICLE_ANALYSIS_PROMPT` to accept a style directive

The current prompt ends with `Write in first person as Abend`. Add a `{style_directive}` placeholder after the depth instructions block and before the CRITICAL section:

```
{depth_instructions}

{style_directive}

**CRITICAL — Plain English only:**
```

When `style.analysis_directive` is empty, pass an empty string. The model handles an empty substitution cleanly.

Also add a banned-openings line to the CRITICAL section:

```
- Do NOT open a sentence with "Today's [news/stories/articles] reveal[s] a complex..."
- Do NOT use the words "interplay", "web of power", or "complex struggle" as opening phrases
```

These banned phrases should be in the base prompt, not in the style variant, since they are universally undesirable.

---

### Step 4: Update `_analyze_single_article` to accept and pass style

Change the signature to:

```python
def _analyze_single_article(
    article: dict, tier: int, model: str, temperature: float,
    style: DigestStyle
) -> str:
```

Inject `style.analysis_directive` and `style.opening_constraint` into the prompt format call:

```python
prompt = ARTICLE_ANALYSIS_PROMPT.format(
    ...existing fields...,
    style_directive=style.analysis_directive,
    opening_constraint=style.opening_constraint,
)
```

---

### Step 5: Update `SYNTHESIS_PROMPT` to accept style directives

The current synthesis prompt hardcodes the three section names and their format requirements inline. Replace the hardcoded section block with three placeholders:

```
---

{synthesis_directive}

Write EXACTLY THREE sections in this order:

{section_structure}

**CRITICAL — Plain English only:**
...
- {opening_constraint}
```

The `section_structure` field on the style object carries the full description of what the three sections should contain. This is the only way to give different styles different section instructions without branching logic in the Python code.

---

### Step 6: Update `_reorder_sections()` and `_check_duplicate_sections()` to use the active style's headings

Both functions currently hardcode `section_order` as a module-level list. Make them accept an optional `style: DigestStyle | None` parameter:

```python
def _reorder_sections(content: str, style: DigestStyle | None = None) -> str:
    section_order = [
        style.big_picture_heading if style else "## The Big Picture",
        "## Deep Dives",
        style.patterns_heading if style else "## Patterns & Signals",
        style.attention_heading if style else "## What Deserves Attention",
    ]
    ...
```

Same pattern for `_check_duplicate_sections()`.

The default `None` path uses the current hardcoded names, so existing callers that don't pass a style continue to work.

---

### Step 7: Update `REVIEW_REVISION_PROMPT` to use the active style's headings

The current `REVIEW_REVISION_PROMPT` hardcodes:

```
Maintain the EXACT same structure: ## The Big Picture, ## Deep Dives,
## Patterns & Signals, ## What Deserves Attention
```

Replace this with a format placeholder:

```python
REVIEW_REVISION_PROMPT_TEMPLATE = """...
2. Maintain the EXACT same structure: {section_names_list} — each appearing EXACTLY ONCE
..."""
```

In `generate_digest()`, format it:

```python
section_names_list = ", ".join([
    style.big_picture_heading,
    "## Deep Dives",
    style.patterns_heading,
    style.attention_heading,
])
revision_prompt = REVIEW_REVISION_PROMPT_TEMPLATE.format(
    issues=...,
    content=...,
    article_data=...,
    section_names_list=section_names_list,
)
```

---

### Step 8: Update temperature handling

Add a per-style temperature delta field to `DigestStyle`:

```python
temperature_delta: float = 0.0  # added to the base temperature
```

Default: `0.0`. The "compression" and "dry-inventory" styles get `0.0` (no change — low temperature serves compression). The "question-led" and "structural-doubt" styles get `+0.1`. The "one-story" style gets `+0.05`.

This allows marginal temperature variation without exposing a new user-facing setting. The base temperature remains the user-configured value.

In `_analyze_single_article` and the synthesis call, compute:

```python
effective_temp = min(temperature + style.temperature_delta, 1.0)
```

---

### Step 9: Wire the style selection into `generate_digest()`

Near the top of `generate_digest()`, after the settings are loaded (approximately line 1125):

```python
# Stable per-date style selection
date_seed = int(
    datetime.strptime(digest_date_str, "%Y-%m-%d").toordinal()
)
style = _select_digest_style(seed=date_seed)
logger.info(f"Digest style: {style.name}")
```

Then thread `style` through:
- Each `_analyze_single_article()` call
- The synthesis prompt formatting
- `_reorder_sections(content, style)`
- `_check_duplicate_sections(content, style)` inside `review_digest()`
- The `REVIEW_REVISION_PROMPT` formatting

---

### Step 10: Update `_check_boilerplate()` with expanded banned phrases

The current banned phrase list (line 890) catches some repeat patterns but misses the most prevalent ones. Add:

```python
boilerplate_phrases = [
    # existing entries preserved
    r"raises questions about systemic design and incentive architecture",
    r"highlights the attention economy.s emphasis on spectacle",
    r"underscores the importance of data sovereignty",
    r"a complex interplay between technological advancements",
    r"user data may be used for targeted advertising",
    r"the consequences of poorly designed systems",
    r"a complex struggle for control over the narrative",
    r"the means of production",
    # new additions
    r"today.s (news|top stories|articles) reveal[s]? a complex",
    r"a complex web of power",
    r"a complex interplay between",
    r"power dynamics at play",
    r"at the intersection of",
    r"on one hand.*on the other hand",
    r"this article (highlights|underscores|emphasizes|demonstrates)",
    r"this (highlights|underscores|demonstrates) the (tension|importance|need)",
    r"at the forefront (of|are)",
]
```

Change the threshold from `>= 2 occurrences` to `>= 1 occurrence` for the most severe patterns (the "today's news reveals a complex" family). These should trigger review even once.

---

## Files Affected

| File | Change |
|------|--------|
| `/home/kellogg/dev/Sieve/digest.py` | All changes — `DigestStyle`, style pool, updated prompts, updated `_analyze_single_article`, updated `generate_digest`, updated `_reorder_sections`, updated `_check_duplicate_sections`, updated `REVIEW_REVISION_PROMPT`, expanded `_check_boilerplate` |
| `/home/kellogg/dev/rogue_routine/scripts/export.py` | No changes required. `extract_big_picture()` already uses a regex that matches any `## ... Big Picture` heading and has a fallback. The only non-standard heading ("## The Signal" in the one-story style) would fall to the fallback — the first substantive paragraph — which is acceptable. |

---

## Risks & Mitigations

**Risk 1: Local LLM ignores style directives entirely**

Small local models (llama3.2, mistral, etc.) at low temperature have limited instruction-following capacity. A style directive like "open with a question" may simply be ignored.

Mitigation: The plan does not depend on perfect compliance. Any partial compliance produces more variety than none. The banned-phrase enforcement in `_check_boilerplate()` catches the worst recurring patterns regardless of whether the style was honored. The expanded boilerplate list (Step 10) is the lowest-risk, highest-return intervention and should be implemented first.

**Risk 2: Review loop revises style variation back to standard structure**

The current review loop calls `_check_duplicate_sections()` which will flag missing expected section names if the style uses non-standard headings. Without Step 6, the "single-thread" style's "## The Thread" heading would be flagged as a missing "## The Big Picture" and the revision call would demand it be added back — destroying the variation.

Mitigation: Steps 6 and 7 are mandatory dependencies of any non-standard heading style. If implementing incrementally, begin with styles that use only standard heading names (standard, question-led, compression, dry-inventory) and defer the heading-varying styles (single-thread, one-story, structural-doubt) until the review loop is updated.

**Risk 3: `extract_big_picture()` in export.py returns empty for non-standard headings**

The regex is `^##\s+.*Big Picture` — it requires "Big Picture" to appear in the heading. "## The Thread" and "## The Signal" (from styles single-thread and one-story) would not match, triggering the fallback path.

Mitigation: The fallback extracts the first substantive paragraph of the digest content. Since `_reorder_sections()` places the big-picture section first, the fallback will capture the first paragraph of that section. This is acceptable behavior — the export's `summary` and `big_picture` frontmatter fields will be populated from the right content, just without the heading being the discriminator.

Optional: update the regex in `export.py` to `^##\s+.*(Big Picture|The Thread|The Signal)` if the non-standard headings are adopted. This is a one-line change.

**Risk 4: Date-seeded style produces the same style for runs across the same date**

This is by design — regenerating a digest for the same date should produce the same style, for reproducibility. If the user wants a different style for a date, they can temporarily change the seed logic or override manually.

Downside: if a style produces a bad output and the user wants to regenerate with a different style, they'd need to temporarily modify the seed. Mitigation: document this behavior and consider adding an optional `style_override` parameter to `generate_digest()`.

**Risk 5: The "compression" style produces digests that are too short for the Hugo site**

If the model actually honors the compression directive, Big Picture sections could be shorter than the ~200-character `summary` field in the frontmatter, and `extract_summary()` might return an awkward truncation.

Mitigation: `extract_big_picture()` returns the full section text untruncated; the truncation only applies to the `summary` field. A compressed Big Picture that reads as one strong sentence is better for the landing card than a bloated one.

---

## Testing Strategy

### Manual verification (primary)

After implementing Step 10 alone (expanded boilerplate), regenerate the most recent digest and verify that the boilerplate checker fires on the known offending phrases. This validates the detection logic before any prompt changes.

After implementing Steps 1-9, generate digests with each style explicitly by temporarily hardcoding `style = DIGEST_STYLES[n]` in `generate_digest()`. Verify:
- The logged style name is correct
- The section headings match the style's heading fields
- The `_check_duplicate_sections()` check passes for the style's actual headings
- The `_reorder_sections()` output places sections in the correct order
- The review loop does not flag the non-standard headings as errors
- `extract_big_picture()` in export.py returns non-empty content for each style

### Unit tests (supplementary)

`_reorder_sections()` already has deterministic behavior — write a test that passes a style with non-standard headings and verifies the ordering. The function takes a string and returns a string; it is trivially testable.

`_check_duplicate_sections()` similarly takes a string and a style — test that it correctly passes a digest with the style's heading names and fails one with duplicates.

`_check_boilerplate()` — test that each new banned phrase triggers detection at the correct threshold.

`_select_digest_style()` — verify that the same seed always returns the same style, and that all styles in the pool are reachable across a range of seeds.

---

## Definition of Done

- [ ] `DigestStyle` dataclass defined with all fields including `weight` and `temperature_delta`
- [ ] At least 5 styles defined in `DIGEST_STYLES` with meaningful differences in directive text
- [ ] `ARTICLE_ANALYSIS_PROMPT` has `{style_directive}` and `{opening_constraint}` placeholders
- [ ] `ARTICLE_ANALYSIS_PROMPT` has hardcoded banned-opener constraint in the CRITICAL block
- [ ] `_analyze_single_article()` accepts and uses a `style` parameter
- [ ] `SYNTHESIS_PROMPT` has `{synthesis_directive}`, `{section_structure}`, and `{opening_constraint}` placeholders
- [ ] Synthesis call user_prompt uses `style.user_prompt_synthesis`
- [ ] `_reorder_sections()` accepts optional style and uses style headings when present
- [ ] `_check_duplicate_sections()` accepts optional style and validates against style headings
- [ ] `REVIEW_REVISION_PROMPT` uses a format placeholder for section names list, not hardcoded names
- [ ] `generate_digest()` selects style via date-seeded `_select_digest_style()` and logs the selection
- [ ] `generate_digest()` threads style through all call sites
- [ ] `_check_boilerplate()` has expanded banned phrases including the "today's news reveals a complex" family
- [ ] Manual generation of a digest with each style produces visibly different opening structure
- [ ] Review loop does not erroneously flag non-standard headings as missing sections
- [ ] `extract_big_picture()` in `export.py` returns non-empty content for all styles (either direct match or fallback)
- [ ] Existing digest generation for standard style produces output structurally equivalent to current output

---

## Sequencing Recommendation

Implement in this order to reduce risk:

1. **Step 10 first** (expand `_check_boilerplate()`): Zero risk, immediate detection improvement, validates the checker independently.
2. **Steps 1-2 (styles with standard headings only)**: Define `DigestStyle`, implement selection, define the styles that use only standard heading names: standard, question-led, compression, dry-inventory.
3. **Steps 3-5 (prompt injection)**: Update prompts with placeholders and wire styles through the two call sites.
4. **Step 8 (temperature delta)**: Minimal change, add the delta field and the effective_temp computation.
5. **Step 9 (wire into generate_digest)**: Full integration with date-seeded selection and logging.
6. **Steps 6-7 (review loop style awareness)**: Enable non-standard heading styles: single-thread, one-story, structural-doubt.
7. **Manual testing of all styles**: Validate each one explicitly before relying on random selection.

This sequence means steps 1-5, 8-9 can ship and provide immediate improvement, while the heading-varying styles (steps 6-7) are gated on the review loop being updated.

---

## Confidence Assessment

**High confidence** in the diagnosis and in the mechanisms. The root causes are definitively in the code. The prompt injection approach is the right abstraction — it is the same mechanism the current system already uses for tier-based variation, extended to a style dimension.

**Medium confidence** in how well local models honor the style directives. The variation mechanisms are correctly designed; whether a given model at a given size actually produces visually different output is an empirical question. The boilerplate checker (Step 10) is the most reliable intervention because it enforces constraints after generation regardless of model behavior.

**Known unknowns:**
- What Ollama model is currently configured. Larger models (llama3.1:70b, mistral-large) will honor the style directives more reliably than smaller ones (llama3.2:3b). The plan works at any model size but delivers more visible variety with larger models.
- Whether the current `temperature = 0.3` setting is stored in the database settings table or hardcoded elsewhere. The plan reads it from settings at line 1125 — if there's a second codepath for temperature, check `get_all_settings()` in `db.py`.
