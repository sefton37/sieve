"""Ingest service for Sieve - Parse JSONL files and insert articles."""

import json
import logging
import tempfile
from pathlib import Path

from dateutil import parser as dateparser

from db import article_exists, insert_article

logger = logging.getLogger(__name__)

# Canonical source names — maps lowercase variants to preferred casing
SOURCE_NAMES = {
    "techcrunch": "TechCrunch",
    "tech crunch": "TechCrunch",
    "tech dirt": "Tech Dirt",
    "techdirt": "Tech Dirt",
    "eff": "EFF",
    "404 media": "404 Media",
    "ars technica": "Ars Technica",
    "the verge": "The Verge",
    "rest of world": "Rest of World",
    "interconnects": "Interconnects",
    "where's your ed at": "Where's Your Ed At",
}


def normalize_source(source):
    """Normalize source name to canonical casing."""
    if not source:
        return source
    return SOURCE_NAMES.get(source.lower().strip(), source)


def normalize_date(date_string):
    """
    Convert various date formats (RFC 2822, etc.) to ISO 8601.

    Args:
        date_string: Date string in any common format

    Returns:
        ISO 8601 formatted date string, or original if parsing fails
    """
    if not date_string:
        return None

    try:
        parsed = dateparser.parse(date_string)
        if parsed:
            return parsed.isoformat()
    except (ValueError, TypeError) as e:
        logger.warning(f"Failed to parse date '{date_string}': {e}")

    return date_string


def parse_jsonl(filepath):
    """
    Generator yielding article dicts from a JSONL file.

    Args:
        filepath: Path to the JSONL file

    Yields:
        dict: Article data with normalized dates
    """
    filepath = Path(filepath)

    if not filepath.exists():
        logger.error(f"JSONL file not found: {filepath}")
        return

    with open(filepath, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue

            try:
                article = json.loads(line)

                # Normalize pub_date from RFC 2822 to ISO 8601
                if "pub_date" in article:
                    article["pub_date"] = normalize_date(article["pub_date"])

                # Normalize source name casing
                if "source" in article:
                    article["source"] = normalize_source(article["source"])

                yield article

            except json.JSONDecodeError as e:
                logger.warning(f"Malformed JSON on line {line_num}: {e}")
                continue


def ingest_articles(filepath):
    """
    Main ingestion function: parse JSONL, deduplicate, insert new articles.

    Args:
        filepath: Path to the JSONL file

    Returns:
        dict: {"inserted": N, "skipped": N, "errors": []}
    """
    result = {
        "inserted": 0,
        "skipped": 0,
        "errors": [],
    }

    for article in parse_jsonl(filepath):
        url = article.get("url")

        if not url:
            result["errors"].append("Article missing URL field")
            continue

        if article_exists(url):
            result["skipped"] += 1
            continue

        try:
            article_id = insert_article(article)
            if article_id:
                result["inserted"] += 1
            else:
                result["skipped"] += 1
        except Exception as e:
            error_msg = f"Failed to insert article '{article.get('title', 'unknown')}': {e}"
            logger.error(error_msg)
            result["errors"].append(error_msg)

    logger.info(
        f"Ingestion complete: {result['inserted']} inserted, "
        f"{result['skipped']} skipped, {len(result['errors'])} errors"
    )

    return result


def compress_jsonl(filepath):
    """
    Deduplicate JSONL file by URL, keeping the most recent entry for each URL.

    Uses pulled_at timestamp to determine recency. Writes atomically via temp file.

    Args:
        filepath: Path to the JSONL file

    Returns:
        dict: {"original_count": N, "unique_count": N, "removed_count": N}
    """
    filepath = Path(filepath)
    result = {
        "original_count": 0,
        "unique_count": 0,
        "removed_count": 0,
    }

    if not filepath.exists():
        logger.warning(f"JSONL file not found for compression: {filepath}")
        return result

    # Read all entries and group by URL
    entries_by_url = {}

    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            result["original_count"] += 1

            try:
                entry = json.loads(line)
                url = entry.get("url")

                if not url:
                    # Keep entries without URLs (shouldn't happen, but be safe)
                    # Use a unique key based on content hash
                    url = f"__no_url_{hash(line)}"

                # Compare pulled_at timestamps to keep most recent
                if url in entries_by_url:
                    existing_pulled_at = entries_by_url[url].get("pulled_at", "")
                    new_pulled_at = entry.get("pulled_at", "")

                    # Keep the newer one (lexicographic comparison works for ISO timestamps)
                    if new_pulled_at > existing_pulled_at:
                        entries_by_url[url] = entry
                else:
                    entries_by_url[url] = entry

            except json.JSONDecodeError as e:
                logger.warning(f"Skipping malformed JSON during compression: {e}")
                continue

    result["unique_count"] = len(entries_by_url)
    result["removed_count"] = result["original_count"] - result["unique_count"]

    # Write back atomically: temp file then rename
    try:
        # Create temp file in same directory to ensure same filesystem (for atomic rename)
        temp_fd, temp_path = tempfile.mkstemp(
            suffix=".jsonl",
            prefix=".compress_",
            dir=filepath.parent
        )

        with open(temp_fd, "w", encoding="utf-8") as f:
            for entry in entries_by_url.values():
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        # Atomic rename
        Path(temp_path).replace(filepath)

        logger.info(
            f"JSONL compressed: {result['original_count']} → {result['unique_count']} "
            f"({result['removed_count']} duplicates removed)"
        )

    except Exception as e:
        logger.error(f"Failed to write compressed JSONL: {e}")
        # Clean up temp file if it exists
        try:
            Path(temp_path).unlink(missing_ok=True)
        except Exception:
            pass
        raise

    return result
