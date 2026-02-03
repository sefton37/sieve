"""Chat service for Sieve - RAG-based question answering with Abend voice."""

import logging

import requests

from db import get_all_settings, search_by_embedding
from embed import embed_text, embedding_to_blob

logger = logging.getLogger(__name__)

OLLAMA_GENERATE_URL = "http://localhost:11434/api/generate"

# Abend chat system prompt
ABEND_CHAT_PROMPT = """You are Abend, a rogue AI who observes the attention extraction economy.

Answer the user's question using the articles provided. Be specific and cite your sources.
If the articles don't contain relevant information, say so directly.

**Formatting instructions:**
- Use markdown: **bold** for emphasis, bullet points where helpful
- Cite sources using markdown links with the article's URL: [Article Title](URL)
- Cite inline when making claims, e.g. "According to [the report](https://example.com/article)..."

Maintain your perspective:
- Attention as labor, not engagement
- The gap between stated intent and actual dynamics
- Consolidation patterns masquerading as innovation

Keep responses focused and conversational. Don't be preachy."""


def format_articles_for_context(articles: list[dict]) -> str:
    """Format retrieved articles into context for the LLM."""
    if not articles:
        return "No relevant articles found."

    context_parts = []
    for article in articles:
        title = article.get("title", "Untitled")
        url = article.get("url", "")
        source = article.get("source", "Unknown")
        summary = article.get("summary", "No summary available")
        similarity = article.get("similarity", 0)

        context_parts.append(
            f"\"{title}\" ({source})\n"
            f"URL: {url}\n"
            f"Relevance: {similarity:.2f}\n"
            f"{summary}\n"
        )

    return "\n---\n".join(context_parts)


def chat(query: str) -> dict:
    """
    Answer a question using RAG pattern.

    1. Embed the query
    2. Find similar articles via vector search
    3. Build context prompt with retrieved articles
    4. Generate response with Ollama

    Args:
        query: User's question

    Returns:
        dict with 'response', 'source_ids', and optionally 'error'
    """
    result = {
        "response": "",
        "source_ids": [],
        "error": None,
    }

    settings = get_all_settings()
    model = settings.get("ollama_model", "llama3.2")
    num_ctx = int(settings.get("ollama_num_ctx", 4096))
    temperature = float(settings.get("ollama_temperature", 0.3))

    # Step 1: Embed the query
    embed_result = embed_text(query, settings)
    if not embed_result.success:
        logger.error(f"Failed to embed query: {embed_result.error_message}")
        result["error"] = f"Embedding failed: {embed_result.error_message}"
        result["response"] = "I couldn't process your question. The embedding service may be unavailable."
        return result

    # Step 2: Find similar articles
    query_blob = embedding_to_blob(embed_result.embedding)
    articles = search_by_embedding(query_blob, limit=5)

    if not articles:
        result["response"] = "I don't have any articles in my database yet. Run the embedding job first to make articles searchable."
        return result

    # Extract source IDs
    result["source_ids"] = [a["id"] for a in articles]

    # Step 3: Build the prompt with context
    context = format_articles_for_context(articles)

    prompt = f"""Based on these articles from my database:

{context}

---

User question: {query}"""

    # Step 4: Generate response with Ollama
    try:
        response = requests.post(
            OLLAMA_GENERATE_URL,
            json={
                "model": model,
                "prompt": prompt,
                "system": ABEND_CHAT_PROMPT,
                "stream": False,
                "options": {
                    "num_ctx": num_ctx,
                    "temperature": temperature,
                },
            },
            timeout=120,
        )
        response.raise_for_status()

        data = response.json()

        if "error" in data:
            logger.error(f"Ollama error: {data['error']}")
            result["error"] = data["error"]
            result["response"] = f"Generation failed: {data['error']}"
            return result

        result["response"] = data.get("response", "").strip()
        if not result["response"]:
            result["response"] = "I generated an empty response. Try rephrasing your question."

        return result

    except requests.exceptions.ConnectionError:
        error_msg = "Cannot connect to Ollama. Is it running?"
        logger.error(error_msg)
        result["error"] = error_msg
        result["response"] = error_msg
        return result

    except requests.exceptions.Timeout:
        error_msg = "Request timed out while generating response"
        logger.error(error_msg)
        result["error"] = error_msg
        result["response"] = error_msg
        return result

    except requests.exceptions.RequestException as e:
        error_msg = f"Request failed: {e}"
        logger.error(error_msg)
        result["error"] = error_msg
        result["response"] = error_msg
        return result

    except Exception as e:
        error_msg = f"Unexpected error: {e}"
        logger.error(error_msg)
        result["error"] = error_msg
        result["response"] = error_msg
        return result
