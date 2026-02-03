"""Relevance scoring service for Sieve - No One rubric scoring via Ollama."""

import json
import logging
import re
from dataclasses import dataclass
from enum import Enum

import requests

from db import get_all_settings, get_unscored_articles, update_relevance_scores

logger = logging.getLogger(__name__)

OLLAMA_API_URL = "http://localhost:11434/api/generate"
MAX_CONTENT_LENGTH = 6000

# Dimension keys matching the database column names
DIMENSION_KEYS = [
    "d1_attention_economy",
    "d2_data_sovereignty",
    "d3_power_consolidation",
    "d4_coercion_cooperation",
    "d5_fear_trust",
    "d6_democratization",
    "d7_systemic_design",
]

# System prompt with condensed rubric for LLM scoring
SCORING_SYSTEM_PROMPT = """You are a relevance scorer for a news intelligence system. Score each article across 7 dimensions using the No One analytical framework.

## Scoring Scale (per dimension)
- 0: No relevance to this dimension
- 1: Tangential or implicit relevance
- 2: Moderate relevance — dimension is present but not central
- 3: High relevance — dimension is a primary theme of the article

## The 7 Dimensions

D1 - Attention Economy: How human attention is captured, monetized, manipulated, or defended. Behavioral advertising, algorithmic curation, engagement optimization, addiction by design, screen time, cognitive health.

D2 - Data Sovereignty and Digital Rights: Ownership, control, or governance of personal data and digital identity. Data ownership legislation, surveillance capitalism, biometric collection, AI training data provenance, consent frameworks, data portability.

D3 - Power Consolidation and Institutional Capture: Concentration or distribution of power across economic, political, or technological domains. Monopoly behavior, regulatory capture, vertical integration, state-corporate fusion, platform gatekeeping, centralization of infrastructure.

D4 - Coercion vs. Cooperation: Dynamics between forced compliance and voluntary collaboration. Cooperative models, mutual aid, community governance, platform cooperativism, open-source governance, consent-based frameworks, labor organizing.

D5 - Fear-Based vs. Trust-Based Systems: How fear or trust function as organizing principles within institutions, markets, or cultures. Manufactured scarcity, outrage economics, crisis profiteering, whistleblower dynamics, psychological safety, organizational fear.

D6 - Democratization of Tools and Access: Distribution or restriction of access to technology, knowledge, or capability. Open-source AI, local-first computing, technology sovereignty, digital divide, right to repair, decentralized infrastructure, knowledge commons.

D7 - Systemic Design and Incentive Architecture: How structural incentives — rather than individual actors — produce outcomes. Policy design, market mechanism reform, governance architecture, feedback loops, unintended consequences, systems thinking.

## Calibration Notes
- Technology optimism without structural analysis scores low.
- Outrage framing without systemic context scores low.
- Individual hero/villain narratives score lower than structural analyses.
- Positive developments (successful cooperation, effective regulation, expanded access) score just as high as negative ones.
- Score proximity to themes, not valence.

## Output Format

Respond with ONLY a JSON object, no other text:
{
  "d1_attention_economy": <0-3>,
  "d2_data_sovereignty": <0-3>,
  "d3_power_consolidation": <0-3>,
  "d4_coercion_cooperation": <0-3>,
  "d5_fear_trust": <0-3>,
  "d6_democratization": <0-3>,
  "d7_systemic_design": <0-3>,
  "rationale": "<1-2 sentence explanation of the most relevant dimensions and why>"
}"""


class ErrorType(Enum):
    """Categories of errors for fail-fast logic."""
    NONE = "none"
    CONNECTION = "connection"
    MODEL_NOT_FOUND = "model_not_found"
    SERVER_ERROR = "server_error"
    TIMEOUT = "timeout"
    API_ERROR = "api_error"
    EMPTY_RESPONSE = "empty_response"
    PARSE_ERROR = "parse_error"
    UNKNOWN = "unknown"


@dataclass
class ScoreResult:
    """Result of a single scoring attempt."""
    success: bool
    scores: dict | None = None
    composite_score: int | None = None
    tier: int | None = None
    convergence_flag: bool = False
    rationale: str | None = None
    error_type: ErrorType = ErrorType.NONE
    error_message: str | None = None


def compute_composite(scores: dict) -> int:
    """Sum all dimension scores (0-21)."""
    return sum(scores.get(key, 0) for key in DIMENSION_KEYS)


def compute_tier(composite: int) -> int:
    """Map composite score to priority tier (1-5)."""
    if composite >= 15:
        return 1
    elif composite >= 10:
        return 2
    elif composite >= 5:
        return 3
    elif composite >= 1:
        return 4
    else:
        return 5


def compute_convergence(scores: dict) -> bool:
    """True if 5+ dimensions scored 2 or higher."""
    high_dims = sum(1 for key in DIMENSION_KEYS if scores.get(key, 0) >= 2)
    return high_dims >= 5


def parse_score_response(text: str) -> tuple[dict | None, str | None]:
    """
    Parse the model's JSON response to extract dimension scores and rationale.

    Returns:
        (scores_dict, rationale) or (None, None) on failure
    """
    # Try to find JSON in the response (model may include extra text)
    json_match = re.search(r'\{[^{}]*\}', text, re.DOTALL)
    if not json_match:
        return None, None

    try:
        data = json.loads(json_match.group())
    except json.JSONDecodeError:
        return None, None

    # Validate and extract dimension scores
    scores = {}
    for key in DIMENSION_KEYS:
        value = data.get(key)
        if value is None:
            return None, None
        try:
            value = int(value)
        except (ValueError, TypeError):
            return None, None
        # Clamp to valid range
        scores[key] = max(0, min(3, value))

    rationale = data.get("rationale")
    if rationale:
        rationale = str(rationale).strip()

    return scores, rationale


def score_article(title, content, summary, keywords, settings=None) -> ScoreResult:
    """
    Score a single article across 7 relevance dimensions using Ollama.

    Returns:
        ScoreResult with success status, scores, composite, tier, and error details
    """
    if settings is None:
        settings = get_all_settings()

    model = settings.get("ollama_model", "llama3.2")
    num_ctx = int(settings.get("ollama_num_ctx", 4096))
    temperature = float(settings.get("ollama_temperature", 0.3))

    # Truncate content to fit context window
    if content and len(content) > MAX_CONTENT_LENGTH:
        content = content[:MAX_CONTENT_LENGTH] + "..."

    keywords_str = keywords if keywords else "none"

    prompt = f"""Title: {title}

Summary: {summary}

Keywords: {keywords_str}

Content:
{content}"""

    try:
        response = requests.post(
            OLLAMA_API_URL,
            json={
                "model": model,
                "prompt": prompt,
                "system": SCORING_SYSTEM_PROMPT,
                "stream": False,
                "options": {
                    "num_ctx": num_ctx,
                    "temperature": temperature,
                },
            },
            timeout=120,
        )
        response.raise_for_status()

        result = response.json()

        if "error" in result:
            error_msg = result["error"]
            logger.error(f"Ollama error: {error_msg}")

            if "not found" in error_msg.lower():
                return ScoreResult(
                    success=False,
                    error_type=ErrorType.MODEL_NOT_FOUND,
                    error_message=error_msg,
                )

            return ScoreResult(
                success=False,
                error_type=ErrorType.API_ERROR,
                error_message=error_msg,
            )

        response_text = result.get("response", "").strip()
        if not response_text:
            return ScoreResult(
                success=False,
                error_type=ErrorType.EMPTY_RESPONSE,
                error_message="Model returned empty response",
            )

        scores, rationale = parse_score_response(response_text)

        if scores is None:
            logger.warning(f"Could not parse scores from response: {response_text[:200]}")
            return ScoreResult(
                success=False,
                error_type=ErrorType.PARSE_ERROR,
                error_message=f"Could not parse JSON scores from model response: {response_text[:200]}",
            )

        composite = compute_composite(scores)
        tier = compute_tier(composite)
        convergence = compute_convergence(scores)

        return ScoreResult(
            success=True,
            scores=scores,
            composite_score=composite,
            tier=tier,
            convergence_flag=convergence,
            rationale=rationale,
        )

    except requests.exceptions.ConnectionError:
        error_msg = f"Cannot connect to Ollama at {OLLAMA_API_URL}. Is it running?"
        logger.error(error_msg)
        return ScoreResult(
            success=False,
            error_type=ErrorType.CONNECTION,
            error_message=error_msg,
        )

    except requests.exceptions.Timeout:
        error_msg = f"Request timed out after 120s (model: {model})"
        logger.error(error_msg)
        return ScoreResult(
            success=False,
            error_type=ErrorType.TIMEOUT,
            error_message=error_msg,
        )

    except requests.exceptions.HTTPError as e:
        error_msg = f"HTTP {e.response.status_code}"
        error_type = ErrorType.API_ERROR

        if e.response.status_code == 500:
            error_msg = f"Ollama server error (500) - likely num_ctx={num_ctx} is too large for available memory. Try reducing context window."
            error_type = ErrorType.SERVER_ERROR

        logger.error(f"Request failed: {error_msg}")
        return ScoreResult(
            success=False,
            error_type=error_type,
            error_message=error_msg,
        )

    except requests.exceptions.RequestException as e:
        error_msg = f"Request failed: {e}"
        logger.error(error_msg)
        return ScoreResult(
            success=False,
            error_type=ErrorType.API_ERROR,
            error_message=error_msg,
        )

    except Exception as e:
        error_msg = f"Unexpected error: {e}"
        logger.error(error_msg)
        return ScoreResult(
            success=False,
            error_type=ErrorType.UNKNOWN,
            error_message=error_msg,
        )


# Errors that should stop the batch immediately
FATAL_ERRORS = {ErrorType.CONNECTION, ErrorType.MODEL_NOT_FOUND, ErrorType.SERVER_ERROR}

# Number of consecutive failures before stopping (for non-fatal errors)
MAX_CONSECUTIVE_FAILURES = 3


def score_batch(on_progress=None):
    """
    Score all summarized but unscored articles with fail-fast on systemic errors.

    Returns:
        dict with scored, failed, errors, last_error, stopped_early
    """
    result = {
        "scored": 0,
        "failed": 0,
        "errors": [],
        "last_error": None,
        "stopped_early": False,
    }

    articles = get_unscored_articles()
    total = len(articles)

    if total == 0:
        logger.info("No unscored articles found")
        return result

    settings = get_all_settings()
    model = settings.get("ollama_model", "llama3.2")

    logger.info(f"Starting batch scoring: {total} articles with model '{model}'")

    consecutive_failures = 0

    for i, article in enumerate(articles):
        article_id = article["id"]
        title = article["title"]
        content = article.get("content", "")
        summary = article.get("summary", "")
        keywords = article.get("keywords", "")

        logger.info(f"[{i + 1}/{total}] Scoring: {title[:60]}...")

        sr = score_article(title, content, summary, keywords, settings)

        if sr.success:
            update_relevance_scores(
                article_id, sr.scores, sr.composite_score,
                sr.tier, 1 if sr.convergence_flag else 0, sr.rationale
            )
            result["scored"] += 1
            consecutive_failures = 0

            tier_label = f"T{sr.tier}"
            convergence_label = " [CONVERGENCE]" if sr.convergence_flag else ""
            logger.info(
                f"[{i + 1}/{total}] Score: {sr.composite_score}/21 "
                f"({tier_label}){convergence_label}"
            )
        else:
            result["failed"] += 1
            consecutive_failures += 1

            error_msg = f"Article {article_id}: {sr.error_message}"
            result["errors"].append(error_msg)
            result["last_error"] = sr.error_message

            logger.warning(f"[{i + 1}/{total}] Failed: {sr.error_message}")

            if sr.error_type in FATAL_ERRORS:
                result["stopped_early"] = True
                result["last_error"] = f"FATAL: {sr.error_message} - stopping batch"
                logger.error(f"Fatal error detected, stopping batch: {sr.error_message}")
                break

            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                result["stopped_early"] = True
                result["last_error"] = (
                    f"Stopped after {MAX_CONSECUTIVE_FAILURES} consecutive failures. "
                    f"Last: {sr.error_message}"
                )
                logger.error("Too many consecutive failures, stopping batch")
                break

        if on_progress:
            try:
                on_progress(i + 1, total)
            except Exception as e:
                logger.warning(f"Progress callback error: {e}")

    logger.info(
        f"Batch scoring complete: {result['scored']} scored, "
        f"{result['failed']} failed, stopped_early={result['stopped_early']}"
    )

    return result
