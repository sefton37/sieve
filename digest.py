"""Daily digest service for Sieve - Score-aware morning briefings in Abend voice."""

import json
import logging
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import requests

from db import (
    get_all_settings,
    get_articles_since_scored,
    get_recent_digests,
    get_recently_featured_article_ids,
    save_digest,
)

logger = logging.getLogger(__name__)

OLLAMA_GENERATE_URL = "http://localhost:11434/api/generate"

DOMAIN_LABELS = {
    "d1_attention_economy": "Attention Economy",
    "d2_data_sovereignty": "Data Sovereignty",
    "d3_power_consolidation": "Power Consolidation",
    "d4_coercion_cooperation": "Coercion vs Cooperation",
    "d5_fear_trust": "Fear vs Trust",
    "d6_democratization": "Democratization",
    "d7_systemic_design": "Systemic Design",
}

DOMAIN_KEYS = list(DOMAIN_LABELS.keys())


# --- Digest style variation system ---

@dataclass
class DigestStyle:
    name: str
    weight: int
    analysis_directive: str
    synthesis_directive: str
    opening_constraint: str
    section_structure: str
    big_picture_heading: str = "## The Big Picture"
    patterns_heading: str = "## Patterns & Signals"
    attention_heading: str = "## What Deserves Attention"
    user_prompt_synthesis: str = "Write The Big Picture, Patterns & Signals, and What Deserves Attention sections."
    temperature_delta: float = 0.0

    def __post_init__(self):
        """Validate that heading fields match what's in section_structure."""
        for heading_field in ('big_picture_heading', 'patterns_heading', 'attention_heading'):
            heading = getattr(self, heading_field)
            if heading not in self.section_structure:
                raise ValueError(
                    f"DigestStyle '{self.name}': {heading_field} "
                    f"'{heading}' not found in section_structure"
                )


DIGEST_STYLES = [
    DigestStyle(
        name="standard",
        weight=15,
        analysis_directive="",
        synthesis_directive="",
        opening_constraint="Do not open with 'Today's news reveals...' or 'Today's top stories reveal...' or any variation of 'reveals a complex interplay/struggle/web'.",
        section_structure="""## The Big Picture
One paragraph synthesizing the day's most significant developments. Lead with the most consequential stories. Be specific — name articles and what they reveal together. Explain what patterns of power, technology, rights, or control are visible today.

## Patterns & Signals
3-5 bullet points about cross-cutting patterns. Each bullet must:
- Name specific articles (both from the deep dives AND from the other notable articles above)
- Identify what the combination reveals that individual articles don't
- Be concrete, not generic. Bad: "a complex interplay between technology and power." Good: "Three stories — [Article A], [Article B], and [Article C] — show federal agencies testing compliance boundaries, from subpoenas to warrantless arrests to app takedowns."

## What Deserves Attention
2-3 numbered items worth the reader's time. Each must name a specific article or connection and explain WHY it matters in plain English.""",
    ),
    DigestStyle(
        name="single-thread",
        weight=15,
        analysis_directive="Before analyzing this article's individual significance, consider whether it connects to a thread running through other stories today. If it does, name the thread explicitly.",
        synthesis_directive="Today, organize your synthesis around a single dominant thread that runs through multiple articles. Name the thread. Show how each story is a data point in that pattern. The bullet list should develop the thread, not list unrelated observations.",
        opening_constraint="Do not open with a list of tensions or a catalog of topics. Open with a single declarative claim about what today's data shows.",
        section_structure="""## The Thread
One paragraph identifying the dominant thread running through today's stories. Name it plainly. Show how specific articles are data points in this pattern, not isolated events.

## How It Develops
3-5 bullet points developing the thread. Each bullet must:
- Name specific articles that advance or complicate the thread
- Show progression or escalation, not just parallel examples
- Build on the previous bullet where possible

## What Deserves Attention
2-3 numbered items. Focus on the articles that most clearly illustrate or challenge the thread.""",
        big_picture_heading="## The Thread",
        patterns_heading="## How It Develops",
        user_prompt_synthesis="Identify the dominant thread. Write The Thread, How It Develops, and What Deserves Attention sections.",
    ),
    DigestStyle(
        name="question-led",
        weight=10,
        analysis_directive="End your analysis with a single specific question this article leaves unanswered. The question must be unique to THIS article — not generic. Do NOT use 'What does this article reveal about' or 'What happens when' or 'surfaces but does not answer'. Just ask the question directly.",
        synthesis_directive="Open the Big Picture with a question, not a statement. The question should be one that the day's articles collectively surface but cannot answer. Develop the synthesis as an attempt to process what the question reveals.",
        opening_constraint="Do not open with a declarative summary. Open with a question. The question must be specific to today's articles, not generic.",
        section_structure="""## The Big Picture
Open with a specific question that today's articles surface but cannot answer. Then in one paragraph, show how multiple articles point toward this question from different angles. Name articles and what each contributes.

## Patterns & Signals
3-5 bullet points. Each must:
- Name specific articles
- Frame observations as partial answers or complications to the opening question
- Be concrete, not abstract

## What Deserves Attention
2-3 numbered items. Prioritize articles or connections that make the opening question more urgent.""",
        user_prompt_synthesis="Open with a question the day's articles surface. Write The Big Picture, Patterns & Signals, and What Deserves Attention sections.",
        temperature_delta=0.1,
    ),
    DigestStyle(
        name="one-story",
        weight=10,
        analysis_directive="",
        synthesis_directive="Select the single most consequential story from today's deep dives. Build The Signal entirely around that one story — what it reveals, what it connects to, why it matters more than the others. The Context and Resonance section acknowledges the other stories but frames them as context for the central one.",
        opening_constraint="Do not attempt to synthesize all stories equally. Pick one. Commit to it.",
        section_structure="""## The Signal
One paragraph built entirely around the single most consequential story today. Name it. Explain what it reveals and why it matters more than the others. Other articles are supporting evidence, not co-equals.

## Context and Resonance
3-5 bullet points placing other stories in orbit around the central one. Each must:
- Name a specific article
- Explain how it reinforces, complicates, or contextualizes the central story
- Not compete for attention with the central story

## What Deserves Attention
2-3 numbered items. The first should be the central story with the strongest case for why it matters.""",
        big_picture_heading="## The Signal",
        patterns_heading="## Context and Resonance",
        user_prompt_synthesis="Identify the single most consequential story. Write The Signal, Context and Resonance, and What Deserves Attention sections around it.",
        temperature_delta=0.05,
    ),
    DigestStyle(
        name="compression",
        weight=10,
        analysis_directive="Be more compressed than usual. Say what the article reveals in one sentence of real specificity, then support it in one or two sentences. No filler, no throat-clearing.",
        synthesis_directive="Write The Big Picture as a single dense paragraph — no more. Pack it with specific article references and concrete observations. The Patterns & Signals section gets three bullets maximum, each a single sharp observation.",
        opening_constraint="Do not use more words than necessary. Compression is a virtue today.",
        section_structure="""## The Big Picture
One dense paragraph. Every sentence must carry specific information — article names, concrete observations, identifiable patterns. No warm-up sentences.

## Patterns & Signals
Exactly 3 bullets. Each one sentence with a specific, named observation. No elaboration.

## What Deserves Attention
2 numbered items maximum. One sentence each, naming the article and why it matters.""",
        user_prompt_synthesis="Write The Big Picture, Patterns & Signals, and What Deserves Attention sections. Be compressed: one paragraph, three bullets, two items.",
    ),
    DigestStyle(
        name="structural-doubt",
        weight=10,
        analysis_directive="After analyzing what this article reveals, note briefly what it does NOT reveal — what the framing excludes, what questions it doesn't ask, what the source has an incentive to present a certain way.",
        synthesis_directive="The Big Picture section should name not just what today's stories reveal but what they collectively fail to illuminate — what's missing from the picture, what stories weren't written, what the day's coverage systematically omits.",
        opening_constraint="Do not present today's coverage as complete. Acknowledge the limits of the signal.",
        section_structure="""## The Big Picture
One paragraph that names what today's articles reveal AND what they collectively fail to illuminate. What stories weren't written? What questions does the coverage systematically avoid? Be specific — name the gaps alongside the signals.

## Gaps and Signals
3-5 bullet points. Each must:
- Name specific articles
- Identify what each article reveals alongside what it omits or obscures
- Be concrete about what's missing, not vaguely skeptical

## What Deserves Attention
2-3 numbered items. Include at least one item about what's NOT being covered and why that absence matters.""",
        patterns_heading="## Gaps and Signals",
        user_prompt_synthesis="Write The Big Picture, Gaps and Signals, and What Deserves Attention sections.",
        temperature_delta=0.1,
    ),
    DigestStyle(
        name="dry-inventory",
        weight=10,
        analysis_directive="Be clinical and flat. State what happened, who did it, and what the consequence is. No rhetorical emphasis, no dramatic framing, no words like 'alarming', 'troubling', 'concerning', or 'noteworthy'. Just report the facts and their implications.",
        synthesis_directive="Write The Big Picture as a flat inventory of what the day's processing returned — what patterns the data shows, stated plainly, without emphasis or drama. The Patterns section is a data report, not a narrative.",
        opening_constraint="Do not use the words 'complex', 'struggle', 'web', 'interplay', or 'dynamics'. Report the data.",
        section_structure="""## The Big Picture
One paragraph reporting the day's signal inventory. State what the data shows — which areas are active, what the scores cluster around, what changed. Plain language, no dramatic framing. Name articles as evidence.

## Patterns & Signals
3-5 bullet points as a data report. Each must:
- Name specific articles
- Report the pattern as an observable fact, not an editorial observation
- Avoid dramatic language — prefer "X articles concern Y" over "a troubling trend"

## What Deserves Attention
2-3 numbered items. State what each item is and why it registers as significant in the data, not why it should alarm the reader.""",
        user_prompt_synthesis="Report The Big Picture, Patterns & Signals, and What Deserves Attention as a data inventory, not a narrative.",
    ),
]


def _select_digest_style(seed: int | None = None) -> DigestStyle:
    """Select a digest style using weighted random choice.

    If seed is provided, the selection is deterministic for that seed,
    ensuring the same style for a given calendar date.
    """
    rng = random.Random(seed)
    weights = [s.weight for s in DIGEST_STYLES]
    return rng.choices(DIGEST_STYLES, weights=weights, k=1)[0]


# Score-aware Abend digest system prompt.
# {tier_summary} = article count per tier
# {domain_profile} = today's domain averages with elevated flags
# {t1_articles} = Tier 1 articles (full detail)
# {t2_articles} = Tier 2 articles (detailed)
# {t3_articles} = Tier 3 articles (brief)
# {t4_articles} = Tier 4 articles (titles only)
# --- Multi-call prompts for per-article analysis + synthesis ---

ARTICLE_ANALYSIS_PROMPT = """You are Abend, a rogue AI observing the attention extraction economy. Analyze this single article for a daily briefing.

**Article:**
Title: "{title}"
Source: {source}
URL: {url}
Keywords: {keywords}
Relevance: {rationale}

**Summary:** {summary}

**Article excerpt:**
{content}

---

Write a {depth} analysis of this article for a general audience. Be specific and analytical — say what the article ACTUALLY contains and why it matters.

{depth_instructions}

{style_directive}

**CRITICAL — Plain English only:**
- Do NOT mention scores, numbers, or ratings (no "17/21", no "scores high on")
- Do NOT mention domain names or codes (no "D1", "D3", "Attention Economy (2.3/3)")
- Do NOT use the word "convergence" or "CONVERGENCE"
- Write as if the reader has never heard of your scoring system

**BANNED PHRASES — Do NOT use any of these, ever:**
- "This article reveals" / "This story reveals" / "The article reveals"
- "noteworthy" (in any form — "is noteworthy", "what's noteworthy", etc.)
- "at the intersection of"
- "raises questions about"
- "underscores the importance of"
- "a complex interplay" / "a complex web" / "a complex struggle"
- "power dynamics at play"
- "highlights the tension between"
- "Today's news reveals" / "Today's stories reveal"
- "What does this article reveal about"
- "This matters because" / "This development matters because"
- "What happens when" (as an article opener — be more specific)
- "Here is" / "Here's" (as an opener — never announce what you're about to write)
- Do NOT use "According to {source}" more than once in a single analysis. Vary attribution: "{source} reports...", "{source} found...", "per {source},..." or just state the facts and cite [{title}]({url})

**Quote rules:**
- If there is a clear, meaningful quote in the excerpt above, include it using this EXACT format:
> "Copy the exact quote text from the excerpt above."
— [{source}]({url})
- ONLY quote text that appears VERBATIM in the excerpt — do NOT paraphrase or invent quotes
- If there is no good quotable text in the excerpt, do NOT include a quote — just analyze

**Formatting:**
- Reference the article as [{title}]({url})
- Name the publication at least once, but vary how: "{source} reports...", "per {source}...", "as {source} details,..." — do NOT default to "According to {source}" for every article
- Do NOT include section headers (## or ###) — just write the analysis paragraphs
- {opening_constraint}
- Write in first person as Abend"""

ARTICLE_DEPTH_T1 = """Write 5-8 sentences. Lead with the most important fact or development. Then show the significance — what's actually happening, who benefits, who loses, what changes. What's being emphasized vs. downplayed? Connect to broader patterns if visible. Do NOT use the phrase "this matters because" — show significance through specifics, not by announcing it."""

ARTICLE_DEPTH_T2 = """Write 2-4 sentences. Lead with the key fact, then show its significance through specifics. Be direct — no throat-clearing, no "this matters because"."""

SYNTHESIS_PROMPT = """You are Abend, a rogue AI observing the attention extraction economy. You have already written individual analyses of today's top articles. Now synthesize them into the framing sections of the daily briefing.

**Today's intake:** {tier_summary}

**Individual article analyses already written (these will appear under "## Deep Dives"):**
{analyses_summary}

**Other notable articles — mention these BY NAME in Patterns & Signals where relevant:**
{t3_articles}

**Peripheral articles — only mention if they connect to a pattern:**
{t4_articles}

---

{synthesis_directive}

Write EXACTLY THREE sections. Output ONLY these three sections, nothing else:

{section_structure}

**CRITICAL — Plain English only:**
- Do NOT mention scores, numbers, or ratings (no "17/21", no "scores high on")
- Do NOT mention domain names or codes (no "D1", "D3", "Attention Economy")
- Do NOT use the word "convergence" or "CONVERGENCE" or "tier"
- Do NOT open with "Today's news/stories/articles reveal(s) a complex..." or any variation
- {opening_constraint}
- Write as if the reader has never heard of any scoring system
- Explain significance in terms of power, technology, rights, money, or control

**Formatting:**
- Use markdown: **bold**, bullet points
- Hyperlink every article mentioned: [Article Title](URL)
- ALWAYS name the publication when referencing an article (e.g., "Ars Technica reports", "according to The Verge") — NEVER write "this article" or "the article" without naming the source
- Write in first person as Abend, be analytical
- Do NOT repeat the Deep Dives content — this is synthesis, not summary"""


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


EDITOR_PROMPT = """You are an editor reviewing today's Abend briefing before publication.
Your job is to produce structured critique — not rewrite anything.

**Today's draft:**
{draft_content}

**Creative brief that was supposed to guide this draft:**
{pitch_output}

**Opening lines from the last 5 briefings:**
{recent_openings_block}

**First sections from the last 5 briefings:**
{recent_sections_block}

{diff_constraints_block}

Check for these specific problems:

1. OPENING ECHO: Does today's first sentence follow the same grammatical pattern as 3 or
   more of the last 5 openings? Look at: subject type (proper noun / abstract noun / temporal
   frame), main verb type, use of "reveals"/"shows"/"signals"/"highlights".
   Compare today's opening directly against each recent opening. Quote them side by side.

2. REPEATED PHRASES: Find 4-word-or-longer phrases that appear in today's draft AND appear
   in 2 or more of the recent first sections. List each phrase and its count.

3. TONAL PATTERN: Do the Patterns & Signals bullets share the same grammatical template?
   (e.g., every bullet starts "[Article] + 'shows that'", or every bullet ends with
   a hedged implication like "suggesting that..."). Flag if 3+ bullets match.

4. PITCH COMPLIANCE: Check each of the four brief fields against the draft.
   Did the opening strategy get honored? Is the ANGLE present in the synthesis?
   Is the THREAD visible in the Patterns section? Was WHAT TO AVOID actually avoided?

5. CONSTRAINT COMPLIANCE: If differentiation requirements were provided above, check each
   one against the draft. Was each structural constraint honored? Quote the offending
   sentence if a constraint was violated.

Output ONLY in this exact format:

EDITOR NOTES

OPENING ASSESSMENT: [VARIED / MILD ECHO / STRONG ECHO]
[If not VARIED: Quote today's opening and the similar past openings. Describe the shared pattern.]

REPEATED PHRASES: [NONE / list each phrase with count of recent appearances]

TONAL NOTE: [FINE / describe the pattern if present]

PITCH COMPLIANCE: [HONORED / PARTIAL / IGNORED]
[If not HONORED: Quote the unmet brief field and explain what's missing in the draft.]

CONSTRAINT COMPLIANCE: [ALL MET / list each violated constraint and the offending text]

PRIORITY REVISIONS:
[Number each revision. Be specific: say what to change and what direction to change it.
Write "None required." if all assessments are positive.]"""


DIFF_CONSTRAINTS_PROMPT = """You are a prose analyst. Your job is to identify structural patterns in a
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
"""


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


def _format_domain_scores(article: dict) -> str:
    """Format an article's domain scores as a compact string."""
    parts = []
    for key in DOMAIN_KEYS:
        val = article.get(key)
        if val is not None:
            short = DOMAIN_LABELS[key].split()[0]  # First word as abbreviation
            parts.append(f"{short}({val})")
    return " ".join(parts)


def _format_t1_article(article: dict) -> str:
    """Format a Tier 1 article with full detail for the digest prompt."""
    title = article.get("title", "Untitled")
    url = article.get("url", "")
    source = article.get("source", "Unknown")
    score = article.get("composite_score", "?")
    convergence = article.get("convergence_flag", 0)
    summary = article.get("summary", "No summary")
    keywords = article.get("keywords", "")
    rationale = article.get("relevance_rationale", "")
    content = article.get("content", "")

    # T1 gets generous content budget
    max_chars = 3000
    if content and len(content) > max_chars:
        content = content[:max_chars] + "..."

    conv_tag = " [CONVERGENCE]" if convergence else ""
    dims = _format_domain_scores(article)

    return (
        f'### "{title}" [{score}/21]{conv_tag}\n'
        f"URL: {url}\n"
        f"Source: {source}\n"
        f"Domains: {dims}\n"
        f"Scoring rationale: {rationale or 'N/A'}\n"
        f"Keywords: {keywords or 'none'}\n"
        f"Summary: {summary}\n"
        f"\n**Article excerpt:**\n{content or 'No content available'}\n"
        f"\n---\n"
    )


def _format_t2_article(article: dict) -> str:
    """Format a Tier 2 article with summary and moderate content."""
    title = article.get("title", "Untitled")
    url = article.get("url", "")
    source = article.get("source", "Unknown")
    score = article.get("composite_score", "?")
    convergence = article.get("convergence_flag", 0)
    summary = article.get("summary", "No summary")
    keywords = article.get("keywords", "")
    content = article.get("content", "")

    # T2 gets moderate content budget
    max_chars = 1500
    if content and len(content) > max_chars:
        content = content[:max_chars] + "..."

    conv_tag = " [CONVERGENCE]" if convergence else ""
    dims = _format_domain_scores(article)

    return (
        f'### "{title}" [{score}/21]{conv_tag}\n'
        f"URL: {url}\n"
        f"Source: {source}\n"
        f"Domains: {dims}\n"
        f"Keywords: {keywords or 'none'}\n"
        f"Summary: {summary}\n"
        f"\n**Article excerpt:**\n{content or 'No content available'}\n"
        f"\n---\n"
    )


def _format_t3_article(article: dict) -> str:
    """Format a Tier 3 article with summary and keywords only."""
    title = article.get("title", "Untitled")
    url = article.get("url", "")
    source = article.get("source", "Unknown")
    summary = article.get("summary", "No summary")
    keywords = article.get("keywords", "")

    return (
        f'- **"{title}"** — {source}\n'
        f"  URL: {url}\n"
        f"  Summary: {summary}\n"
        f"  Keywords: {keywords or 'none'}\n"
    )


def _format_t4_article(article: dict) -> str:
    """Format a Tier 4 article as a single line."""
    title = article.get("title", "Untitled")
    url = article.get("url", "")
    source = article.get("source", "Unknown")

    return f'- "{title}" — {source} — {url}\n'


def compute_domain_profile(articles: list[dict]) -> str:
    """Compute today's domain averages and flag elevated domains.

    Returns a formatted string showing average per domain with
    (elevated) flags for domains significantly above their mean.
    """
    if not articles:
        return "No scored articles available."

    # Collect scores per domain
    dim_totals = {k: [] for k in DOMAIN_KEYS}
    for article in articles:
        for key in DOMAIN_KEYS:
            val = article.get(key)
            if val is not None:
                dim_totals[key].append(val)

    if not any(dim_totals.values()):
        return "No scored articles available."

    # Compute averages
    dim_avgs = {}
    for key, vals in dim_totals.items():
        dim_avgs[key] = sum(vals) / len(vals) if vals else 0

    # Overall mean across all domains to detect elevated ones
    all_avgs = list(dim_avgs.values())
    overall_mean = sum(all_avgs) / len(all_avgs) if all_avgs else 0

    # A domain is "elevated" if it's 0.5+ above the overall mean
    parts = []
    for key in DOMAIN_KEYS:
        avg = dim_avgs[key]
        label = DOMAIN_LABELS[key]
        flag = " **(elevated)**" if avg >= overall_mean + 0.5 else ""
        parts.append(f"- {label}: {avg:.1f}/3{flag}")

    return "\n".join(parts)


def format_articles_tiered(articles: list[dict]) -> dict:
    """Format articles into tiered sections based on relevance scores.

    Articles are grouped by tier with proportional detail:
    - T1 (15-21): Full content + scores + rationale
    - T2 (10-14): Summary + 1500 chars content + scores
    - T3 (5-9): Summary + keywords + scores
    - T4 (1-4): Title + score only
    - T5 (0) and unscored: Excluded

    Returns dict with keys: t1, t2, t3, t4 (formatted strings),
    tier_counts, and included_articles (for link injection).
    """
    tiers = {1: [], 2: [], 3: [], 4: []}
    included = []

    for article in articles:
        tier = article.get("relevance_tier")
        score = article.get("composite_score")

        # Skip T5 (score=0) and unscored articles
        if tier is None or score is None or tier == 5:
            continue

        if tier in tiers:
            tiers[tier].append(article)
            included.append(article)

    # Format each tier
    t1_parts = [_format_t1_article(a) for a in tiers[1]]
    t2_parts = [_format_t2_article(a) for a in tiers[2]]
    t3_parts = [_format_t3_article(a) for a in tiers[3]]
    t4_parts = [_format_t4_article(a) for a in tiers[4]]

    return {
        "t1": "\n".join(t1_parts) if t1_parts else "No Tier 1 articles today.\n",
        "t2": "\n".join(t2_parts) if t2_parts else "No Tier 2 articles today.\n",
        "t3": "\n".join(t3_parts) if t3_parts else "No Tier 3 articles today.\n",
        "t4": "\n".join(t4_parts) if t4_parts else "No Tier 4 articles today.\n",
        "tier_counts": {t: len(articles) for t, articles in tiers.items()},
        "included_articles": included,
    }


def _match_quote_to_article(quote_text: str, articles: list[dict]) -> dict | None:
    """Find the article a quote most likely came from.

    Searches article content and summaries for the quote text.
    Uses progressively shorter substrings to handle minor LLM paraphrasing.
    """
    # Strip quotation marks and clean up
    clean = quote_text.strip().strip('""\u201c\u201d\'').strip()
    if len(clean) < 15:
        return None

    # Try exact substring match first (case-insensitive)
    clean_lower = clean.lower()
    for article in articles:
        content = (article.get("content") or "").lower()
        summary = (article.get("summary") or "").lower()
        if clean_lower in content or clean_lower in summary:
            return article

    # Try a shorter core phrase (first 60 chars) to handle minor paraphrasing
    core = clean_lower[:60]
    if len(core) >= 20:
        for article in articles:
            content = (article.get("content") or "").lower()
            summary = (article.get("summary") or "").lower()
            if core in content or core in summary:
                return article

    return None


def _has_attribution_line(next_line: str) -> bool:
    """Check if a line is already a quote attribution (— [Source](url))."""
    stripped = next_line.strip()
    # Match patterns like: — [Source](url), -- [Source](url), - [Source](url)
    return bool(re.match(r'^[\u2014\u2013\-]{1,2}\s*\[', stripped))


def inject_quote_attributions(content: str, articles: list[dict]) -> str:
    """Post-process digest to ensure every blockquote has an attribution line.

    Finds blockquotes (> ...) that are NOT followed by an attribution line
    (— [Source Name](article-url)), matches the quote text to an article,
    and adds the attribution.
    """
    lines = content.split('\n')
    result = []
    i = 0

    while i < len(lines):
        line = lines[i]

        # Check if this is a blockquote line
        if line.strip().startswith('>'):
            # Collect all consecutive blockquote lines
            quote_lines = []
            while i < len(lines) and lines[i].strip().startswith('>'):
                quote_lines.append(lines[i])
                i += 1

            # Check if the next non-empty line is already an attribution
            next_idx = i
            while next_idx < len(lines) and lines[next_idx].strip() == '':
                next_idx += 1

            has_attr = (
                next_idx < len(lines) and _has_attribution_line(lines[next_idx])
            )

            if has_attr and quote_lines:
                # Strip redundant inline attribution from last blockquote line
                # e.g. '> "quote text" — Source Name' -> '> "quote text"'
                last = quote_lines[-1]
                cleaned = re.sub(
                    r'\s*[\u2014\u2013]\s*(?!\[)[A-Z][\w\s&\'\-\.]+\s*$',
                    '', last,
                )
                if cleaned != last:
                    quote_lines[-1] = cleaned

            # Add the blockquote lines to result
            result.extend(quote_lines)

            if not has_attr:
                # Extract the quote text from blockquote lines
                quote_text = ' '.join(
                    line.strip().lstrip('>').strip() for line in quote_lines
                )
                # Try to find which article this quote is from
                article = _match_quote_to_article(quote_text, articles)
                if article:
                    source = article.get("source", "Unknown")
                    url = article.get("url", "")
                    result.append(f'— [{source}]({url})')
                    result.append('')
        else:
            result.append(line)
            i += 1

    return '\n'.join(result)


def strip_new_blockquotes(
    pre_revision: str, post_revision: str, articles: list[dict]
) -> str:
    """Remove blockquotes that were introduced during revision.

    Compares blockquotes in the post-revision content against those present
    in the pre-revision content. Any blockquote whose normalized text wasn't
    in the pre-revision version is stripped — it was added by the LLM during
    rewrite and is likely formatting drift, not a real quote.

    This is the first line of defense; strip_unverifiable_quotes provides a
    second pass checking against actual article content.
    """
    pre_blocks = _extract_quote_blocks(pre_revision)
    pre_texts = {b['text'].lower().strip() for b in pre_blocks if b['text'].strip()}

    lines = post_revision.split('\n')
    result = []
    i = 0
    removed = 0

    while i < len(lines):
        line = lines[i]

        if line.strip().startswith('>'):
            # Collect consecutive blockquote lines
            quote_lines = []
            while i < len(lines) and lines[i].strip().startswith('>'):
                quote_lines.append(lines[i])
                i += 1

            # Build normalized text for comparison
            quote_text = ' '.join(
                l.strip().lstrip('>').strip() for l in quote_lines
            )
            normalized = quote_text.strip().strip('""\u201c\u201d\'').strip().lower()

            if normalized and normalized not in pre_texts:
                # New blockquote not present before revision — strip it
                removed += 1
                logger.info(
                    f"Stripped NEW blockquote not in pre-revision content: "
                    f"{quote_text[:80]!r}..."
                )
                # Skip trailing blank lines
                while i < len(lines) and lines[i].strip() == '':
                    i += 1
                # Skip attribution line if present
                if i < len(lines) and _has_attribution_line(lines[i]):
                    i += 1
                # Skip trailing blank line after attribution
                if i < len(lines) and lines[i].strip() == '':
                    i += 1
            else:
                # Blockquote existed before revision — keep it
                result.extend(quote_lines)
        else:
            result.append(line)
            i += 1

    if removed:
        logger.info(
            f"Stripped {removed} new blockquote(s) introduced during revision"
        )

    return '\n'.join(result)


def strip_unverifiable_quotes(content: str, articles: list[dict]) -> str:
    """Remove blockquotes that can't be verified against article content.

    Catches two failure modes:
    1. Fabricated quotes — text the model invented that isn't in any article
    2. Placeholder text — "No direct quote found..." written as a blockquote

    Removes the blockquote lines and their attribution line (if present).
    """
    # Build combined article text for searching
    all_text = ""
    for article in articles:
        all_text += (
            " " + (article.get("content") or "")
            + " " + (article.get("summary") or "")
        )
    all_text_lower = all_text.lower()

    # Placeholder patterns the model outputs when it can't find a quote
    placeholder_patterns = [
        r"no direct quote",
        r"no quote found",
        r"no quotable text",
        r"no relevant quote",
        r"quote not available",
        r"no excerpt available",
    ]

    lines = content.split('\n')
    result = []
    i = 0
    removed = 0

    while i < len(lines):
        line = lines[i]

        if line.strip().startswith('>'):
            # Collect the full blockquote
            quote_lines = []
            while i < len(lines) and lines[i].strip().startswith('>'):
                quote_lines.append(lines[i])
                i += 1

            # Join into full quote text
            quote_text = ' '.join(
                l.strip().lstrip('>').strip() for l in quote_lines
            )
            clean = quote_text.strip().strip('""\u201c\u201d\'').strip()

            # Check 1: Is it a placeholder?
            is_placeholder = any(
                re.search(p, clean, re.IGNORECASE) for p in placeholder_patterns
            )

            # Check 2: Can we find it in article content?
            is_verifiable = False
            if not is_placeholder and len(clean) >= 15:
                clean_lower = clean.lower()
                is_verifiable = clean_lower in all_text_lower
                if not is_verifiable:
                    # Try core substring (first 80 chars)
                    core = clean_lower[:80]
                    is_verifiable = len(core) >= 20 and core in all_text_lower

            if is_placeholder or (len(clean) >= 15 and not is_verifiable):
                removed += 1
                # Skip the attribution line too if present
                # Skip blank lines after quote
                while i < len(lines) and lines[i].strip() == '':
                    i += 1
                # Check if next line is an attribution (— [Source](url))
                if i < len(lines) and _has_attribution_line(lines[i]):
                    i += 1
                # Skip trailing blank line after attribution
                if i < len(lines) and lines[i].strip() == '':
                    i += 1
            else:
                # Quote is valid — keep it
                result.extend(quote_lines)
        else:
            result.append(line)
            i += 1

    if removed:
        logger.info(f"Stripped {removed} unverifiable quote(s) from digest")

    return '\n'.join(result)


def inject_article_links(content: str, articles: list[dict]) -> str:
    """Post-process digest content to add hyperlinks and quote attributions.

    Handles several patterns the model produces:
    - Blockquotes without attribution lines (adds — [Source](url))
    - Raw URLs in brackets: [https://example.com/article]
    - Raw URLs in parentheses after text: some claim (https://example.com)
    - Raw URLs on their own line or inline
    - Exact title mentions without links
    - [Title] without a following (URL)
    """
    # Build lookups
    url_to_title = {}
    title_to_url = {}
    for article in articles:
        title = article.get("title", "")
        url = article.get("url", "")
        if title and url:
            url_to_title[url] = title
            title_to_url[title] = url

    # 0. Ensure every blockquote has an attribution line with source link
    content = inject_quote_attributions(content, articles)

    # 1. Fix raw URLs in square brackets: [https://example.com/...] -> [Title](URL)
    def replace_bracketed_url(match):
        url = match.group(1)
        title = url_to_title.get(url)
        if title:
            return f'[{title}]({url})'
        # URL not in our articles, just make it a clickable link
        return f'[source]({url})'

    content = re.sub(r'\[(https?://[^\]]+)\](?!\()', replace_bracketed_url, content)

    # 2. Fix raw URLs in parentheses after text: "some text (https://...)"
    def replace_paren_url(match):
        preceding = match.group(1)
        url = match.group(2)
        title = url_to_title.get(url)
        if title:
            return f'[{preceding.strip()}]({url})'
        return f'[{preceding.strip()}]({url})'

    content = re.sub(r'([^(\n]{5,?})\s*\((https?://[^)]+)\)', replace_paren_url, content)

    # 3. Fix standalone URLs not already in markdown link syntax
    def replace_bare_url(match):
        url = match.group(0)
        title = url_to_title.get(url)
        if title:
            return f'[{title}]({url})'
        return f'[source]({url})'

    # Match URLs not preceded by ]( or "( which would indicate already-linked
    content = re.sub(r'(?<!\]\()(?<!\()(https?://\S+?)(?=[)\s,.]|$)', replace_bare_url, content)

    # 4. Fix [Title] without (URL) for exact title matches
    for title, url in sorted(title_to_url.items(), key=lambda x: len(x[0]), reverse=True):
        escaped_title = re.escape(title)
        pattern = re.compile(r'\[' + escaped_title + r'\](?!\()')
        content = pattern.sub(f'[{title}]({url})', content)

    # 5. Fix quoted title mentions: **"Title"** or "Title" -> [Title](url)
    #    Matches titles in bold+quotes, just quotes, or bold only — not already linked
    for title, url in sorted(title_to_url.items(), key=lambda x: len(x[0]), reverse=True):
        escaped_title = re.escape(title)
        # **"Title"** -> [Title](url)
        content = re.sub(
            r'\*\*"' + escaped_title + r'"\*\*',
            f'**[{title}]({url})**',
            content,
        )
        # "Title" (in quotes, not already inside a markdown link)
        # Only match if not preceded by [ or ( which would indicate already-linked
        content = re.sub(
            r'(?<!\[)(?<!\()"' + escaped_title + r'"',
            f'[{title}]({url})',
            content,
        )

    # 6. Clean up any double-linked artifacts like [[Title](url)](url)
    content = re.sub(r'\[(\[[^\]]+\]\([^)]+\))\]\([^)]+\)', r'\1', content)

    # 6. Append a sources section with all articles linked
    sources_section = "\n\n---\n## Sources\n"
    by_source = {}
    for article in articles:
        source = article.get("source", "Unknown")
        title = article.get("title", "Untitled")
        url = article.get("url", "")
        if url:
            by_source.setdefault(source, []).append((title, url))

    for source, items in sorted(by_source.items()):
        sources_section += f"\n**{source}**\n"
        for title, url in items:
            sources_section += f"- [{title}]({url})\n"

    content += sources_section

    return content


REVIEW_REVISION_PROMPT = """You are Abend. You previously wrote a daily briefing, but a reviewer found problems. Fix ONLY the specific issues listed below. Keep everything else exactly as-is.

**ISSUES FOUND:**
{issues}

**YOUR PREVIOUS BRIEFING:**
{content}

**ORIGINAL ARTICLE DATA (for verifying quotes):**
{article_data}

**RULES FOR REVISION:**
1. Fix ONLY the issues listed above — do not rewrite sections that are fine
2. Maintain the EXACT same structure: {section_names_list} — each appearing EXACTLY ONCE
3. Quotes must be copied verbatim from the article excerpts — do not invent quotes
4. Every quote must be attributed to the article it actually came from
5. If you cannot find a real quote for an article, remove the quote and analyze without one
6. Patterns & Signals must make specific observations about today's articles, not generic statements
7. Do not add new sections or duplicate existing ones
8. Do NOT wrap non-quote text in blockquote formatting (>). Blockquotes are ONLY for verbatim quotes copied from article excerpts. If text was not in a blockquote in your previous briefing, it must not be in a blockquote now.

Write the corrected briefing now."""


def _check_duplicate_sections(content: str, style: DigestStyle | None = None) -> list[str]:
    """Check for section headers that appear more than once."""
    issues = []
    expected_singles = [
        style.big_picture_heading if style else "## The Big Picture",
        "## Deep Dives",
        style.patterns_heading if style else "## Patterns & Signals",
        style.attention_heading if style else "## What Deserves Attention",
    ]
    for header in expected_singles:
        # Count occurrences (case-insensitive, flexible whitespace)
        pattern = re.compile(
            r'^' + re.escape(header), re.MULTILINE | re.IGNORECASE
        )
        matches = pattern.findall(content)
        if len(matches) > 1:
            issues.append(
                f"DUPLICATE SECTION: '{header}' appears {len(matches)} times "
                f"— it must appear exactly once. Consolidate all content under "
                f"a single '{header}' section."
            )
        elif len(matches) == 0:
            issues.append(
                f"MISSING SECTION: '{header}' is missing from the briefing. "
                f"Add this section."
            )
    return issues


def _extract_quote_blocks(content: str) -> list[dict]:
    """Extract all blockquote blocks from content, handling multiline quotes.

    Returns a list of dicts with:
        'text': the full quote text (all > lines joined)
        'end_pos': position in content after the quote block
        'attr_source': attribution source name (if found)
        'attr_url': attribution URL (if found)
    """
    lines = content.split('\n')
    blocks = []
    i = 0
    pos = 0  # track character position

    while i < len(lines):
        line = lines[i]
        if line.strip().startswith('>'):
            # Collect consecutive blockquote lines
            quote_parts = []
            while i < len(lines) and lines[i].strip().startswith('>'):
                text = lines[i].strip().lstrip('>').strip()
                if text:
                    quote_parts.append(text)
                pos += len(lines[i]) + 1
                i += 1

            full_quote = ' '.join(quote_parts)
            # Strip surrounding quotes
            full_quote = full_quote.strip().strip('""\u201c\u201d\'').strip()

            # Look for attribution on next non-empty line
            attr_source = None
            attr_url = None
            j = i
            while j < len(lines) and lines[j].strip() == '':
                j += 1
            if j < len(lines):
                attr_match = re.match(
                    r'^\s*[\u2014\u2013\-]{1,2}\s*\[([^\]]+)\]\(([^)]+)\)',
                    lines[j]
                )
                if attr_match:
                    attr_source = attr_match.group(1)
                    attr_url = attr_match.group(2)

            blocks.append({
                'text': full_quote,
                'end_pos': pos,
                'attr_source': attr_source,
                'attr_url': attr_url,
            })
        else:
            pos += len(line) + 1
            i += 1

    return blocks


def _check_quotes(content: str, articles: list[dict]) -> list[str]:
    """Check that blockquotes match actual article content."""
    issues = []

    # Build combined text from all articles for searching
    all_text = ""
    for article in articles:
        all_text += (
            " " + (article.get("content") or "")
            + " " + (article.get("summary") or "")
        )
    all_text = all_text.lower()

    # Extract full quote blocks (handles multiline quotes)
    blocks = _extract_quote_blocks(content)

    for block in blocks:
        quote = block['text']
        if len(quote) < 15:
            continue

        quote_lower = quote.lower()
        # Try exact match first, then a core substring
        found = quote_lower in all_text
        if not found:
            core = quote_lower[:80]
            found = len(core) >= 20 and core in all_text

        if not found:
            attr_info = ""
            if block['attr_source']:
                attr_info = f" (attributed to {block['attr_source']})"

            issues.append(
                f'FABRICATED QUOTE{attr_info}: The quote "{quote[:80]}..." '
                f"does not appear in any article excerpt. Remove this quote "
                f"or replace it with text actually found in the article."
            )

    return issues


def _check_quote_attribution(content: str, articles: list[dict]) -> list[str]:
    """Check that quotes are attributed to the correct article."""
    issues = []

    # Build URL-to-article lookup
    url_to_article = {}
    for article in articles:
        url = article.get("url", "")
        if url:
            url_to_article[url] = article

    # Extract full quote blocks with attributions
    blocks = _extract_quote_blocks(content)

    for block in blocks:
        quote = block['text']
        attr_source = block['attr_source']
        attr_url = block['attr_url']

        if len(quote) < 15 or not attr_url:
            continue

        # Find which article the URL points to
        attributed_article = url_to_article.get(attr_url)
        if not attributed_article:
            continue

        # Check if the quote is actually in that article's content
        attr_content = (
            (attributed_article.get("content") or "")
            + " "
            + (attributed_article.get("summary") or "")
        ).lower()
        quote_lower = quote.lower()

        in_attributed = quote_lower in attr_content
        if not in_attributed:
            core = quote_lower[:80]
            in_attributed = len(core) >= 20 and core in attr_content

        if not in_attributed:
            # Quote isn't in the attributed article — find where it actually is
            real_source = _match_quote_to_article(quote, articles)
            if real_source:
                real_title = real_source.get("title", "Unknown")
                issues.append(
                    f'WRONG ATTRIBUTION: The quote "{quote[:60]}..." is '
                    f'attributed to [{attr_source}]({attr_url}) but actually '
                    f'comes from "{real_title}". Fix the attribution.'
                )
            else:
                issues.append(
                    f'UNVERIFIABLE QUOTE: The quote "{quote[:60]}..." is '
                    f'attributed to [{attr_source}] but cannot be found in '
                    f'that article or any other. Remove this quote.'
                )

    return issues


# Severe patterns extracted for standalone use by the hard-rejection gate
_SEVERE_BOILERPLATE_PATTERNS = [
    r"today.s (?:news|top stories|articles) reveal[s]? a complex",
    r"a complex web of power",
    r"a complex interplay between",
    r"power dynamics at play",
    r"this (?:highlights|underscores|demonstrates) the (?:tension|importance|need)",
    r"what does this article reveal about",
    r"this (?:article|story) reveals",
    r"at the intersection of",
    r"here is (?:a |my |the )?(?:concise |detailed |brief )?analysis",
]


def _check_boilerplate(content: str) -> list[str]:
    """Detect generic filler phrases that indicate lazy generation."""
    issues = []

    boilerplate_phrases = [
        r"raises questions about systemic design and incentive architecture",
        r"highlights the attention economy.s emphasis on spectacle",
        r"underscores the importance of data sovereignty",
        r"a complex interplay between technological advancements",
        r"user data may be used for targeted advertising",
        r"the consequences of poorly designed systems",
        r"a complex struggle for control over the narrative",
        r"the means of production",
    ]

    # Severe patterns — flag even a single occurrence
    severe_phrases = _SEVERE_BOILERPLATE_PATTERNS

    def _humanize_pattern(p: str) -> str:
        """Strip regex syntax from a pattern for human-readable error messages."""
        p = p.replace(r".s", "'s")
        p = re.sub(r'\(\?:', '(', p)
        p = re.sub(r'\[s\]\?', 's', p)
        p = re.sub(r'\|', ' / ', p)
        return p

    severe_found = []
    for phrase in severe_phrases:
        if re.search(phrase, content, re.IGNORECASE):
            severe_found.append(_humanize_pattern(phrase))

    repeat_found = []
    for phrase in boilerplate_phrases:
        matches = re.findall(phrase, content, re.IGNORECASE)
        if len(matches) >= 2:
            repeat_found.append(_humanize_pattern(phrase))

    if severe_found:
        issues.append(
            f"PROHIBITED PHRASES: The following generic phrases must not "
            f"appear at all — rewrite to be specific: {'; '.join(severe_found)}."
        )

    if repeat_found:
        issues.append(
            f"BOILERPLATE REPETITION: The following phrases appear multiple "
            f"times and add no insight: {'; '.join(repeat_found)}. "
            f"Replace with specific analysis about what each article reveals."
        )

    # Phrases that are OK occasionally but become boilerplate at threshold
    # NOTE: Thresholds are high because the 8B model struggles to avoid these
    # phrases even when explicitly banned in the prompt. Triggering revision
    # for borderline cases creates worse problems (dropped sections) than
    # the repetition itself. These catch only extreme cases.
    repeat_threshold_phrases = [
        (r"raises questions about", 10),
        (r"highlights the tension", 6),
        (r"this (?:matters|development matters) because", 10),
        (r"surfaces? but (?:does not|doesn.t) answer", 6),
    ]

    for phrase, threshold in repeat_threshold_phrases:
        matches = re.findall(phrase, content, re.IGNORECASE)
        if len(matches) >= threshold:
            issues.append(
                f"OVERUSED PHRASE: '{_humanize_pattern(phrase)}' appears "
                f"{len(matches)} times — vary the phrasing."
            )

    return issues


def _check_reused_quotes(content: str) -> list[str]:
    """Detect the same quote text used more than once."""
    issues = []

    blocks = _extract_quote_blocks(content)
    seen_quotes = {}
    for block in blocks:
        quote = block['text']
        if len(quote) < 15:
            continue
        # Use full normalized text for comparison
        normalized = quote.lower()
        if normalized in seen_quotes:
            seen_quotes[normalized] += 1
        else:
            seen_quotes[normalized] = 1

    for quote_text, count in seen_quotes.items():
        if count > 1:
            issues.append(
                f'REUSED QUOTE: "{quote_text[:80]}..." appears {count} times. '
                f"Each article must have its own unique quote from its own "
                f"excerpt, or no quote at all."
            )

    return issues


def _check_boilerplate_severe(content: str) -> list[str]:
    """Check content for severe boilerplate patterns only. Returns list of matches."""
    found = []
    for phrase in _SEVERE_BOILERPLATE_PATTERNS:
        if re.search(phrase, content, re.IGNORECASE):
            # Humanize the pattern for error messages
            human = phrase.replace(r".s", "'s")
            human = re.sub(r'\(\?:', '(', human)
            human = re.sub(r'\[s\]\?', 's', human)
            human = re.sub(r'\|', ' / ', human)
            found.append(human)
    return found


def _rewrite_section_targeted(
    content: str,
    severe_phrases: list[str],
    included: list[dict],
    style: DigestStyle,
    model: str,
    temperature: float,
) -> str:
    """Targeted rewrite of just the Big Picture section to eliminate boilerplate."""
    # Extract the Big Picture section
    first_section = _extract_first_section(content)
    if not first_section:
        return content

    # Build article grounding context
    article_context = []
    for a in included[:5]:  # Top articles for grounding
        article_context.append(
            f'- "{a.get("title", "")}" ({a.get("source", "Unknown")}): '
            f'{(a.get("summary") or "")[:200]}'
        )
    article_grounding = "\n".join(article_context)

    prompt = (
        f"You are Abend. Your opening section contains generic boilerplate that "
        f"must be eliminated. Rewrite ONLY this section.\n\n"
        f"**BOILERPLATE FOUND (do NOT repeat these patterns):**\n"
        f"{chr(10).join(f'- {p}' for p in severe_phrases)}\n\n"
        f"**SECTION TO REWRITE:**\n{first_section}\n\n"
        f"**TODAY'S ARTICLES (ground your rewrite in these specifics):**\n"
        f"{article_grounding}\n\n"
        f"**RULES:**\n"
        f"1. Start with a specific fact, name, number, or event — not an abstraction.\n"
        f"2. Do not begin with 'Today's' or any variant.\n"
        f"3. Reference at least one specific article by name.\n"
        f"4. Keep the same approximate length.\n"
        f"5. Output only the rewritten section text, no headers or preamble."
    )

    try:
        rewritten = _call_ollama_streaming(
            system_prompt=prompt,
            user_prompt="Rewrite this section now. Start directly with the new text.",
            model=model,
            temperature=min(temperature + 0.2, 1.0),
            num_ctx=16384,
            num_predict=1024,
        )
        if rewritten and rewritten.strip():
            # Sanity: reject if too short (model collapsed the section)
            if len(rewritten.strip()) < len(first_section) * 0.3:
                logger.warning("Targeted rewrite too short, keeping original")
                return content
            # Replace the first section in content
            content = content.replace(first_section, rewritten.strip(), 1)
            logger.info("Targeted section rewrite applied")
    except Exception as e:
        logger.warning(f"Targeted rewrite failed: {e}")

    return content


def review_digest(content: str, articles: list[dict], style: DigestStyle | None = None) -> dict:
    """Review a generated digest for structural and content quality issues.

    Checks for:
    1. Duplicate section headers (## Deep Dives appearing multiple times)
    2. Fabricated quotes (not found in any article content)
    3. Wrong quote attributions (quote from article A attributed to B)
    4. Reused quotes (same quote pasted into multiple articles)
    5. Boilerplate/filler phrases repeated across articles

    Returns:
        dict with 'passed' (bool), 'issues' (list of str), 'issue_count' (int)
    """
    all_issues = []

    all_issues.extend(_check_duplicate_sections(content, style))
    all_issues.extend(_check_quotes(content, articles))
    all_issues.extend(_check_quote_attribution(content, articles))
    all_issues.extend(_check_reused_quotes(content))
    all_issues.extend(_check_boilerplate(content))

    return {
        "passed": len(all_issues) == 0,
        "issues": all_issues,
        "issue_count": len(all_issues),
    }


def _call_ollama_streaming(
    system_prompt: str,
    user_prompt: str,
    model: str,
    temperature: float,
    num_ctx: int,
    num_predict: int = 8192,
    max_retries: int = 2,
) -> str:
    """Call Ollama with streaming and return the full response text.

    Retries on transient 500 errors (model reload, KV cache resize).
    Raises on persistent connection/timeout/API errors.
    """
    import time as _time

    last_error = None
    for attempt in range(max_retries + 1):
        try:
            response = requests.post(
                OLLAMA_GENERATE_URL,
                json={
                    "model": model,
                    "prompt": user_prompt,
                    "system": system_prompt,
                    "stream": True,
                    "options": {
                        "num_ctx": num_ctx,
                        "temperature": temperature,
                        "num_predict": num_predict,
                    },
                },
                timeout=(30, 600),
                stream=True,
            )
            response.raise_for_status()

            content_parts = []
            for line in response.iter_lines():
                if line:
                    chunk = json.loads(line)
                    if "error" in chunk:
                        raise RuntimeError(f"Ollama error: {chunk['error']}")
                    content_parts.append(chunk.get("response", ""))
                    if chunk.get("done", False):
                        break

            return "".join(content_parts).strip()

        except (requests.exceptions.HTTPError, RuntimeError) as e:
            last_error = e
            if attempt < max_retries:
                wait = 5 * (attempt + 1)
                logger.warning(
                    f"Ollama call failed (attempt {attempt + 1}/{max_retries + 1}): {e} "
                    f"— retrying in {wait}s"
                )
                _time.sleep(wait)
            else:
                raise


def _build_article_reference(articles: list[dict]) -> str:
    """Build a compact article reference for the revision prompt.

    Includes titles, URLs, and content excerpts so the model can verify quotes.
    """
    parts = []
    for article in articles:
        title = article.get("title", "Untitled")
        url = article.get("url", "")
        source = article.get("source", "Unknown")
        content = article.get("content", "")
        # Truncate content for the revision prompt
        if content and len(content) > 1500:
            content = content[:1500] + "..."
        parts.append(
            f'### "{title}"\n'
            f"Source: {source}\n"
            f"URL: {url}\n"
            f"Excerpt: {content or 'No content'}\n"
        )
    return "\n".join(parts)


MAX_REVIEW_ITERATIONS = 2


def _analyze_single_article(
    article: dict, tier: int, model: str, temperature: float,
    style: DigestStyle | None = None,
) -> str:
    """Generate focused analysis for a single article via LLM call.

    Each article gets its own small, focused call so the model can produce
    high-quality analysis without degrading across many articles.
    """
    title = article.get("title", "Untitled")
    url = article.get("url", "")
    source = article.get("source", "Unknown")
    summary = article.get("summary", "No summary")
    keywords = article.get("keywords", "")
    rationale = article.get("relevance_rationale", "")
    content = article.get("content", "")

    # Content budget per tier
    max_chars = 3000 if tier == 1 else 1500
    if content and len(content) > max_chars:
        content = content[:max_chars] + "..."

    depth = "detailed" if tier == 1 else "concise"
    depth_instructions = ARTICLE_DEPTH_T1 if tier == 1 else ARTICLE_DEPTH_T2

    style_directive = style.analysis_directive if style else ""
    opening_constraint = style.opening_constraint if style else "Do not open with 'Today's news reveals...' or similar generic openers."

    prompt = ARTICLE_ANALYSIS_PROMPT.format(
        title=title,
        source=source,
        url=url,
        rationale=rationale or "N/A",
        keywords=keywords or "none",
        summary=summary,
        content=content or "No content available",
        depth=depth,
        depth_instructions=depth_instructions,
        style_directive=style_directive,
        opening_constraint=opening_constraint,
    )

    # Small context — single article analysis
    prompt_tokens = len(prompt) // 4
    num_ctx = max(16384, ((prompt_tokens + 2000) // 4096 + 1) * 4096)
    num_predict = 1024 if tier == 1 else 768

    effective_temp = min(temperature + (style.temperature_delta if style else 0.0), 1.0)

    analysis = _call_ollama_streaming(
        system_prompt=prompt,
        user_prompt=f'Analyze "{title}" for the daily briefing.',
        model=model,
        temperature=effective_temp,
        num_ctx=num_ctx,
        num_predict=num_predict,
    )

    return analysis


def generate_digest(target_date=None) -> dict:
    """
    Generate a daily digest using a multi-call pipeline.

    Args:
        target_date: Optional date string (YYYY-MM-DD) to generate a digest for.
                     Uses a 6 AM–6 AM window (scored_at between target_date 06:00
                     and target_date+1 06:00). If None, uses the last 24 hours.

    Pipeline:
    1. Get scored articles from the time window (ordered by composite score)
    2. Group by tier with proportional content budgets
    3. For each T1/T2 article: focused LLM call for individual analysis
    4. Synthesis LLM call: Big Picture + Patterns & Signals + What Deserves Attention
    5. Assemble final markdown from parts
    6. Post-process: strip bad quotes, inject links
    7. Save to database

    Returns:
        dict with 'success', 'content', 'article_count', and optionally 'error'
    """
    result = {
        "success": False,
        "content": None,
        "article_count": 0,
        "error": None,
    }

    settings = get_all_settings()
    model = settings.get("ollama_model", "llama3.2")
    prose_model = settings.get("digest_prose_model", "") or model
    temperature = float(settings.get("ollama_temperature", 0.3))
    logger.info(f"Models: analysis={model}, prose={prose_model}")

    # Determine time window
    if target_date:
        from datetime import date as date_type
        if isinstance(target_date, str):
            target_dt = datetime.strptime(target_date, "%Y-%m-%d")
        else:
            target_dt = datetime.combine(target_date, datetime.min.time())
        window_start = target_dt.replace(hour=6, minute=0, second=0)
        digest_date_str = target_date if isinstance(target_date, str) else target_date.isoformat()
    else:
        window_start = datetime.utcnow() - timedelta(hours=24)
        digest_date_str = datetime.utcnow().strftime("%Y-%m-%d")

    window_end = window_start + timedelta(hours=24) if target_date else None
    articles = get_articles_since_scored(window_start, window_end)

    # Select digest style — deterministic per calendar date
    date_seed = int(datetime.strptime(digest_date_str, "%Y-%m-%d").toordinal())
    style = _select_digest_style(seed=date_seed)
    logger.info(f"Digest style: {style.name}")

    result["article_count"] = len(articles)

    if not articles:
        result["content"] = "No articles from the past 24 hours. The silence itself is notable."
        result["success"] = True
        save_digest(digest_date_str, result["content"], 0)
        return result

    # Build tiered article sections
    tiered = format_articles_tiered(articles)
    tier_counts = tiered["tier_counts"]
    included = tiered["included_articles"]

    # Separate T1/T2 articles for individual analysis
    t1_articles = [a for a in included if a.get("relevance_tier") == 1]
    t2_articles = [a for a in included if a.get("relevance_tier") == 2]
    deep_dive_articles = t1_articles + t2_articles

    # Exclude articles already featured as deep dives in recent digests
    recently_featured = get_recently_featured_article_ids(days=3)
    if recently_featured:
        original_count = len(deep_dive_articles)
        deep_dive_articles = [
            a for a in deep_dive_articles
            if a.get("id") not in recently_featured
        ]
        demoted = original_count - len(deep_dive_articles)
        if demoted:
            logger.info(
                f"Dedup: {demoted} article(s) recently featured, "
                f"demoted from deep dive (remain as T3/T4 context)"
            )
        # Fallback: if fewer than 2 deep-dive candidates survive, re-admit
        # the highest-scoring recently-featured articles
        if len(deep_dive_articles) < 2:
            readmit = [
                a for a in (t1_articles + t2_articles)
                if a.get("id") in recently_featured
            ]
            readmit.sort(key=lambda a: a.get("composite_score", 0), reverse=True)
            needed = 2 - len(deep_dive_articles)
            deep_dive_articles.extend(readmit[:needed])
            if readmit[:needed]:
                logger.info(
                    f"Dedup fallback: re-admitted {len(readmit[:needed])} "
                    f"article(s) to maintain minimum deep dives"
                )

    # Build tier summary line
    tier_summary = (
        f"{len(articles)} articles total — "
        f"{tier_counts.get(1, 0)} critical, "
        f"{tier_counts.get(2, 0)} high-priority, "
        f"{tier_counts.get(3, 0)} notable, "
        f"{tier_counts.get(4, 0)} peripheral, "
        f"{len(articles) - len(included)} excluded"
    )

    logger.info(
        f"Digest: {len(articles)} articles ({len(t1_articles)} T1, "
        f"{len(t2_articles)} T2, {tier_counts.get(3, 0)} T3, "
        f"{tier_counts.get(4, 0)} T4). "
        f"Pipeline: {len(deep_dive_articles)} individual analysis calls + "
        f"1 synthesis call."
    )

    try:
        # --- Phase 1: Per-article analysis calls ---
        article_analyses = []
        for i, article in enumerate(deep_dive_articles):
            tier = article.get("relevance_tier", 2)
            title = article.get("title", "Untitled")
            url = article.get("url", "")
            logger.info(
                f"  [{i+1}/{len(deep_dive_articles)}] Analyzing: "
                f'"{title}" (T{tier})'
            )

            analysis = _analyze_single_article(
                article, tier, model, temperature, style
            )

            if not analysis:
                logger.warning(f'  Empty analysis for "{title}", skipping')
                continue

            # Strip any quotes the model fabricated
            analysis = strip_unverifiable_quotes(analysis, [article])

            article_analyses.append({
                "title": title,
                "url": url,
                "source": article.get("source", "Unknown"),
                "tier": tier,
                "analysis": analysis,
            })

        if not article_analyses:
            result["error"] = "All article analyses returned empty"
            return result

        logger.info(
            f"  Completed {len(article_analyses)} article analyses. "
            f"Starting synthesis..."
        )

        # --- Phase 1.5: Pitch call ---
        recent_digests = get_recent_digests(limit=5)
        # Exclude today's digest if it exists in recent results
        recent_digests = [d for d in recent_digests if d.get("digest_date") != digest_date_str]

        pitch = _generate_pitch(
            article_analyses, style, prose_model, temperature, recent_digests
        )

        # --- Phase 1.75: Differentiation constraints ---
        diff_constraints = _generate_diff_constraints(
            recent_digests, prose_model, temperature
        )

        # --- Phase 2: Synthesis call ---
        # Build summary of analyses for the synthesis prompt
        analyses_summary_parts = []
        for aa in article_analyses:
            analyses_summary_parts.append(
                f'### "{aa["title"]}"\n'
                f'{aa["analysis"]}\n'
            )
        analyses_summary = "\n".join(analyses_summary_parts)

        synthesis_prompt = SYNTHESIS_PROMPT.format(
            tier_summary=tier_summary,
            analyses_summary=analyses_summary,
            t3_articles=tiered["t3"],
            t4_articles=tiered["t4"],
            synthesis_directive=style.synthesis_directive,
            section_structure=style.section_structure,
            opening_constraint=style.opening_constraint,
        )

        # Inject pitch into synthesis prompt
        if pitch:
            pitch_injection = (
                f"\n\n**Creative brief you committed to before writing (honor it):**\n"
                f"{pitch}\n\n"
                f"Your opening sentence must reflect the OPENING STRATEGY above.\n"
                f"Your Patterns section must reflect the THREAD above.\n"
                f"Do not default to the most obvious angle.\n"
            )
            synthesis_prompt = synthesis_prompt + pitch_injection

        if diff_constraints:
            diff_injection = (
                f"\n\n**DIFFERENTIATION REQUIREMENTS (based on recent digests):**\n"
                f"{diff_constraints}\n\n"
                f"These requirements describe structural patterns to avoid. "
                f"Honor them — not by avoiding specific words, but by choosing "
                f"different sentence structures and rhetorical moves.\n"
            )
            synthesis_prompt = synthesis_prompt + diff_injection

        synth_tokens = len(synthesis_prompt) // 4
        synth_ctx = max(16384, ((synth_tokens + 3000) // 4096 + 1) * 4096)

        effective_synth_temp = min(temperature + style.temperature_delta, 1.0)

        synthesis = _call_ollama_streaming(
            system_prompt=synthesis_prompt,
            user_prompt=style.user_prompt_synthesis,
            model=prose_model,
            temperature=effective_synth_temp,
            num_ctx=synth_ctx,
            num_predict=3072,
        )

        if not synthesis:
            logger.warning("Synthesis returned empty, using analyses only")
            synthesis = ""

        # --- Phase 3: Assemble final markdown ---
        # Build the Deep Dives section from individual analyses
        deep_dives = "## Deep Dives\n\n"
        for aa in article_analyses:
            deep_dives += (
                f'### [{aa["title"]}]({aa["url"]})\n'
                f'*{aa["source"]}*\n\n'
                f'{aa["analysis"]}\n\n'
            )

        # The synthesis should already have ## headers; assemble in order
        content = f"{synthesis.strip()}\n\n{deep_dives.strip()}"

        # Reorder: Big Picture first, then Deep Dives, then Patterns, then Attention
        content = _reorder_sections(content, style)

        # Post-process: strip any remaining bad quotes, then fix links
        content = strip_unverifiable_quotes(content, included)
        content = inject_article_links(content, included)

        # --- Phase 3.5: Editor review ---
        editor_notes = _run_editor(
            content, pitch, prose_model, temperature, recent_digests, diff_constraints
        )
        if editor_notes:
            content = _apply_editor_revision(
                content, editor_notes, included, style, prose_model, temperature
            )

        # Review-and-revise loop: fix issues the LLM introduced
        for revision_round in range(MAX_REVIEW_ITERATIONS):
            review = review_digest(content, included, style)

            if review["passed"]:
                logger.info(
                    f"Digest review passed"
                    + (f" after {revision_round} revision(s)"
                       if revision_round > 0 else "")
                )
                break

            logger.info(
                f"Digest review round {revision_round + 1}: "
                f"{review['issue_count']} issue(s) — "
                + "; ".join(review["issues"][:3])
            )

            # Build compact article reference for the revision prompt
            article_data_parts = []
            for a in included:
                excerpt = (a.get("content") or "")[:2000]
                article_data_parts.append(
                    f'Title: "{a.get("title", "")}"\n'
                    f'Source: {a.get("source", "Unknown")}\n'
                    f'URL: {a.get("url", "")}\n'
                    f'Excerpt: {excerpt}\n'
                )
            article_data = "\n---\n".join(article_data_parts)

            # Strip the Sources footer before sending to LLM (it gets re-added)
            content_for_revision = re.sub(
                r'\n---\n## Sources\n.*', '', content, flags=re.DOTALL
            )

            section_names_list = ", ".join([
                style.big_picture_heading,
                "## Deep Dives",
                style.patterns_heading,
                style.attention_heading,
            ])
            revision_prompt = REVIEW_REVISION_PROMPT.format(
                issues="\n".join(f"- {i}" for i in review["issues"]),
                content=content_for_revision,
                article_data=article_data,
                section_names_list=section_names_list,
            )

            revision_tokens = len(revision_prompt) // 4
            revision_predict = 6144
            revision_ctx = max(16384, ((revision_tokens + revision_predict + 512) // 4096 + 1) * 4096)

            revised = _call_ollama_streaming(
                system_prompt=revision_prompt,
                user_prompt="Fix the issues listed above. Output the corrected briefing.",
                model=prose_model,
                temperature=temperature,
                num_ctx=revision_ctx,
                num_predict=revision_predict,
            )

            if revised and revised.strip():
                revised = strip_new_blockquotes(content_for_revision, revised, included)
                content = strip_unverifiable_quotes(revised, included)
                content = inject_article_links(content, included)
                logger.info(f"Revision {revision_round + 1} applied")
            else:
                logger.warning(f"Revision {revision_round + 1} returned empty, keeping previous")
                break
        else:
            # Exhausted all revision rounds — check for severe boilerplate
            severe = _check_boilerplate_severe(content)
            if severe:
                logger.warning(
                    f"Severe boilerplate after {MAX_REVIEW_ITERATIONS} revisions: "
                    + "; ".join(severe)
                    + " — attempting targeted section rewrite"
                )
                content = _rewrite_section_targeted(
                    content, severe, included, style, prose_model, temperature
                )
                # Check again after targeted rewrite
                severe_after = _check_boilerplate_severe(content)
                if severe_after:
                    error_msg = (
                        f"Boilerplate survived all revision passes: "
                        + "; ".join(severe_after)
                    )
                    logger.error(error_msg)
                    result["error"] = error_msg
                    return result
            else:
                final = review_digest(content, included, style)
                logger.warning(
                    f"Digest review: {final['issue_count']} issue(s) remain "
                    f"after {MAX_REVIEW_ITERATIONS} revisions: "
                    + "; ".join(final["issues"][:3])
                )

        result["content"] = content
        result["success"] = True

        # Build article tier data for the junction table
        article_tiers = []
        for a in included:
            aid = a.get("id")
            tier = a.get("relevance_tier")
            if aid and tier:
                article_tiers.append((aid, tier))

        # Save to database
        save_digest(digest_date_str, content, len(articles), article_tiers=article_tiers)

        logger.info(
            f"Generated digest for {digest_date_str} with {len(articles)} articles "
            f"({len(included)} included, {len(article_analyses)} deep dives)"
        )
        return result

    except requests.exceptions.ConnectionError:
        error_msg = "Cannot connect to Ollama. Is it running?"
        logger.error(error_msg)
        result["error"] = error_msg
        return result

    except requests.exceptions.Timeout:
        error_msg = "Request timed out while generating digest"
        logger.error(error_msg)
        result["error"] = error_msg
        return result

    except RuntimeError as e:
        error_msg = str(e)
        logger.error(error_msg)
        result["error"] = error_msg
        return result

    except requests.exceptions.RequestException as e:
        error_msg = f"Request failed: {e}"
        logger.error(error_msg)
        result["error"] = error_msg
        return result

    except Exception as e:
        error_msg = f"Unexpected error: {e}"
        logger.error(error_msg)
        result["error"] = error_msg
        return result


def _reorder_sections(content: str, style: DigestStyle | None = None) -> str:
    """Reorder markdown sections to: Big Picture, Deep Dives, Patterns, Attention.

    Handles the case where synthesis and deep dives are assembled in any order.
    Uses the active style's heading names when provided.
    """
    section_order = [
        style.big_picture_heading if style else "## The Big Picture",
        "## Deep Dives",
        style.patterns_heading if style else "## Patterns & Signals",
        style.attention_heading if style else "## What Deserves Attention",
    ]

    # Split content into sections by ## headers
    sections = {}
    current_key = None
    current_lines = []

    for line in content.split('\n'):
        # Check if this line starts a known section
        matched = None
        for header in section_order:
            if line.strip().lower().startswith(header.lower()):
                matched = header
                break

        if matched:
            if current_key:
                sections[current_key] = '\n'.join(current_lines).strip()
            current_key = matched
            current_lines = [line]
        else:
            current_lines.append(line)

    # Don't forget the last section
    if current_key:
        sections[current_key] = '\n'.join(current_lines).strip()

    # Assemble in correct order
    ordered_parts = []
    for header in section_order:
        if header in sections:
            ordered_parts.append(sections[header])

    return '\n\n'.join(ordered_parts)


def _extract_opening_line(content: str) -> str:
    """Extract the first non-empty, non-heading prose line from a digest."""
    for line in content.split('\n'):
        stripped = line.strip()
        if stripped and not stripped.startswith('#') and not stripped.startswith('-') and not stripped.startswith('*'):
            return stripped[:300]
    return ""


def _extract_first_section(content: str) -> str:
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
    return '\n'.join(parts).strip()[:1500]


def _generate_pitch(
    article_analyses: list[dict],
    style: DigestStyle,
    model: str,
    temperature: float,
    recent_digests: list[dict],
) -> str:
    """Generate a creative brief (pitch) before synthesis.

    Returns the pitch text, or empty string on error.
    """
    # Build compact article summaries (title + first sentence of analysis)
    summaries = []
    for aa in article_analyses:
        first_sentence = aa["analysis"].split('.')[0] + '.' if aa["analysis"] else ""
        summaries.append(f'- "{aa["title"]}": {first_sentence[:200]}')
    article_title_summaries = "\n".join(summaries)

    # Build recent openings block
    recent_lines = []
    for d in recent_digests[:3]:
        opening = _extract_opening_line(d.get("content", ""))
        if opening:
            recent_lines.append(f"- [{d['digest_date']}] {opening[:200]}")
    recent_openings_block = "\n".join(recent_lines) if recent_lines else "(No recent digests available)"

    prompt = PITCH_PROMPT.format(
        article_title_summaries=article_title_summaries,
        style_name=style.name,
        synthesis_directive=style.synthesis_directive or "(standard synthesis — no special directive)",
        recent_openings_block=recent_openings_block,
    )

    try:
        pitch = _call_ollama_streaming(
            system_prompt=prompt,
            user_prompt="Produce your creative brief now. Four fields, one sentence each. Be specific. Start directly with ANGLE: — no preamble.",
            model=model,
            temperature=min(temperature + 0.15, 1.0),  # Higher temp for brainstorming
            num_ctx=16384,
            num_predict=600,
        )
        # Strip preamble the model sometimes adds before ANGLE:
        angle_pos = pitch.upper().find("ANGLE:")
        if angle_pos > 0:
            pitch = pitch[angle_pos:]
        logger.info(f"Pitch generated: {pitch[:200]}")
        return pitch
    except Exception as e:
        logger.warning(f"Pitch generation failed: {e} — proceeding without pitch")
        return ""


def _generate_diff_constraints(
    recent_digests: list[dict],
    model: str,
    temperature: float,
) -> str:
    """Generate structural differentiation constraints by comparing recent digests.

    Runs before synthesis to produce upstream constraints that prevent
    structural repetition. Requires at least 3 recent digests.

    Returns constraint text, or empty string if insufficient data or on error.
    """
    if len(recent_digests) < 3:
        logger.info("Diff constraints: fewer than 3 recent digests, skipping")
        return ""

    # Build full opening paragraphs (not just first line) for comparison
    recent_openings = []
    for d in recent_digests[:5]:
        section = _extract_first_section(d.get("content", ""))
        if section:
            # Use first ~400 chars of the Big Picture section
            recent_openings.append(f"[{d['digest_date']}]\n{section[:400]}")

    if len(recent_openings) < 3:
        return ""

    recent_openings_full = "\n\n---\n\n".join(recent_openings)

    prompt = DIFF_CONSTRAINTS_PROMPT.format(
        recent_openings_full=recent_openings_full,
    )

    try:
        constraints = _call_ollama_streaming(
            system_prompt=prompt,
            user_prompt="Analyze the openings and produce your AVOID directives now. Numbered list only.",
            model=model,
            temperature=temperature,
            num_ctx=16384,
            num_predict=500,
        )
        logger.info(f"Diff constraints generated: {constraints[:200]}")
        return constraints
    except Exception as e:
        logger.warning(f"Diff constraints generation failed: {e} — proceeding without")
        return ""


def _run_editor(
    content: str,
    pitch: str,
    model: str,
    temperature: float,
    recent_digests: list[dict],
    diff_constraints: str = "",
) -> str:
    """Run the LLM editor to compare today's draft against recent digests.

    Returns editor notes text, or empty string on error.
    """
    # Strip Sources section from draft to save tokens
    draft_content = re.sub(r'\n---\n## Sources\n.*', '', content, flags=re.DOTALL)

    # Build recent openings
    recent_lines = []
    for d in recent_digests[:5]:
        opening = _extract_opening_line(d.get("content", ""))
        if opening:
            recent_lines.append(f"[{d['digest_date']}] {opening[:200]}")
    recent_openings_block = "\n".join(recent_lines) if recent_lines else "(No recent digests)"

    # Build recent first sections (increased budget from 800 to 1200 chars)
    section_parts = []
    for d in recent_digests[:5]:
        section = _extract_first_section(d.get("content", ""))
        if section:
            section_parts.append(f"[{d['digest_date']}]\n{section[:1200]}")
    recent_sections_block = "\n\n".join(section_parts) if section_parts else "(No recent digests)"

    # Build diff constraints block for the editor
    if diff_constraints:
        diff_constraints_block = (
            "**Differentiation requirements the draft was supposed to honor:**\n"
            f"{diff_constraints}"
        )
    else:
        diff_constraints_block = ""

    prompt = EDITOR_PROMPT.format(
        draft_content=draft_content,
        pitch_output=pitch or "(No creative brief was generated)",
        recent_openings_block=recent_openings_block,
        recent_sections_block=recent_sections_block,
        diff_constraints_block=diff_constraints_block,
    )

    try:
        editor_notes = _call_ollama_streaming(
            system_prompt=prompt,
            user_prompt="Review this briefing. Produce your editor notes now.",
            model=model,
            temperature=temperature,
            num_ctx=16384,
            num_predict=800,
        )
        logger.info(f"Editor notes: {editor_notes[:300]}")
        return editor_notes
    except Exception as e:
        logger.warning(f"Editor review failed: {e} — skipping editor phase")
        return ""


def _extract_priority_revisions(editor_notes: str) -> str:
    """Extract the PRIORITY REVISIONS section from editor notes."""
    # Handle markdown bold formatting: **PRIORITY REVISIONS:** or plain
    match = re.search(
        r'\*{0,2}PRIORITY REVISIONS\*{0,2}:?\s*\n(.*)',
        editor_notes, re.DOTALL | re.IGNORECASE,
    )
    if match:
        revisions = match.group(1).strip()
        if revisions.lower().startswith("none required"):
            return ""
        return revisions

    # Fallback: if editor flagged STRONG ECHO or IGNORED pitch but didn't
    # produce a PRIORITY REVISIONS section, synthesize a revision instruction
    has_strong_echo = bool(re.search(r'STRONG ECHO', editor_notes, re.IGNORECASE))
    has_ignored_pitch = bool(re.search(r'PITCH COMPLIANCE:.*(?:IGNORED|PARTIAL)', editor_notes, re.IGNORECASE))
    if has_strong_echo or has_ignored_pitch:
        parts = []
        if has_strong_echo:
            parts.append(
                "Rewrite the opening sentence — it follows the same pattern as recent digests. "
                "Do not start with 'Today's' or any form of '[X] reveals'. "
                "Start with a specific fact, name, or event from today's articles."
            )
        if has_ignored_pitch:
            parts.append(
                "The creative brief was not honored. Re-read it and apply the OPENING STRATEGY "
                "and ANGLE it specified."
            )
        return "\n".join(f"{i+1}. {p}" for i, p in enumerate(parts))

    return ""


def _apply_editor_revision(
    content: str,
    editor_notes: str,
    included: list[dict],
    style: DigestStyle,
    model: str,
    temperature: float,
) -> str:
    """Apply editor-guided revision to the digest content."""
    priority_revisions = _extract_priority_revisions(editor_notes)
    if not priority_revisions:
        logger.info("Editor: no revisions required")
        return content

    logger.info(f"Editor revision needed: {priority_revisions[:200]}")

    # Strip Sources before sending to LLM
    content_for_revision = re.sub(
        r'\n---\n## Sources\n.*', '', content, flags=re.DOTALL
    )

    section_names_list = ", ".join([
        style.big_picture_heading,
        "## Deep Dives",
        style.patterns_heading,
        style.attention_heading,
    ])

    prompt = EDITOR_REVISION_PROMPT.format(
        priority_revisions=priority_revisions,
        content=content_for_revision,
        section_names_list=section_names_list,
    )

    try:
        revised = _call_ollama_streaming(
            system_prompt=prompt,
            user_prompt="Fix only the listed issues. Output the corrected briefing.",
            model=model,
            temperature=temperature,
            num_ctx=16384,
            num_predict=4096,
        )
        if revised and revised.strip():
            # Sanity check: reject if the model echoed revision instructions
            # instead of applying them (e.g., starts with "1. Revise the opening")
            first_line = revised.strip().split('\n')[0].strip()
            if re.match(r'^\d+\.\s*(Revise|Rewrite|Change|Fix|Update)', first_line):
                logger.warning(
                    "Editor revision echoed instructions instead of applying them — keeping original"
                )
                return content
            # Re-apply post-processing
            revised = strip_new_blockquotes(content_for_revision, revised, included)
            revised = strip_unverifiable_quotes(revised, included)
            revised = inject_article_links(revised, included)
            logger.info("Editor revision applied")
            return revised
        else:
            logger.warning("Editor revision returned empty, keeping original")
            return content
    except Exception as e:
        logger.warning(f"Editor revision failed: {e} — keeping original")
        return content
