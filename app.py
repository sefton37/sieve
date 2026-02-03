"""Flask application for Sieve - News intelligence web interface."""

import logging
import threading
from math import ceil

import requests
from flask import Flask, jsonify, redirect, render_template, request, url_for

from db import (
    clear_chat_history,
    get_all_settings,
    get_article,
    get_article_count,
    get_articles,
    get_articles_by_ids,
    get_chat_history,
    get_digest,
    get_embedded_count,
    get_keywords,
    get_recent_digests,
    get_sources,
    get_summarized_count,
    init_db,
    save_chat_message,
    set_setting,
    update_summary,
)
from ingest import ingest_articles
from pipeline import run_pipeline
from scheduler import (
    get_next_digest_run,
    get_next_pipeline_run,
    is_pipeline_running,
    remove_ingest_job,
    schedule_digest,
    schedule_ingest,
    start_scheduler,
)
from summarize import summarize_article, summarize_batch

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Job state tracking (in-memory, single user)
job_state = {
    "running": False,
    "type": None,  # "ingest", "summarize", "pipeline", "embed", "digest", or "chat"
    "stage": None,  # For pipeline: "ingest", "compress", "summarize", "embed"
    "current": 0,
    "total": 0,
    "message": None,
    "error": None,
    "result": None,
}
job_lock = threading.Lock()


def reset_job_state():
    """Reset job state to idle."""
    global job_state
    with job_lock:
        job_state = {
            "running": False,
            "type": None,
            "stage": None,
            "current": 0,
            "total": 0,
            "message": None,
            "error": None,
            "result": None,
        }


def run_ingest_job(filepath):
    """Run ingestion in background thread."""
    global job_state
    try:
        with job_lock:
            job_state["running"] = True
            job_state["type"] = "ingest"
            job_state["current"] = 0
            job_state["total"] = 1  # Ingest is single-step
            job_state["error"] = None

        result = ingest_articles(filepath)

        with job_lock:
            job_state["current"] = 1
            job_state["result"] = result

    except Exception as e:
        logger.error(f"Ingest job failed: {e}")
        with job_lock:
            job_state["error"] = str(e)
    finally:
        with job_lock:
            job_state["running"] = False


def run_summarize_job():
    """Run batch summarization in background thread."""
    global job_state

    def on_progress(current, total):
        with job_lock:
            job_state["current"] = current
            job_state["total"] = total

    try:
        with job_lock:
            job_state["running"] = True
            job_state["type"] = "summarize"
            job_state["current"] = 0
            job_state["total"] = 0
            job_state["error"] = None

        result = summarize_batch(on_progress=on_progress)

        with job_lock:
            job_state["result"] = result

    except Exception as e:
        logger.error(f"Summarize job failed: {e}")
        with job_lock:
            job_state["error"] = str(e)
    finally:
        with job_lock:
            job_state["running"] = False


def run_pipeline_job():
    """Run full pipeline in background thread."""
    global job_state

    def on_progress(stage, current, total, message):
        with job_lock:
            job_state["stage"] = stage
            job_state["current"] = current
            job_state["total"] = total
            job_state["message"] = message

    try:
        with job_lock:
            job_state["running"] = True
            job_state["type"] = "pipeline"
            job_state["stage"] = "starting"
            job_state["current"] = 0
            job_state["total"] = 0
            job_state["message"] = "Starting pipeline..."
            job_state["error"] = None

        result = run_pipeline(on_progress=on_progress)

        with job_lock:
            job_state["result"] = result
            if not result.get("success"):
                job_state["error"] = result.get("error")

    except Exception as e:
        logger.error(f"Pipeline job failed: {e}")
        with job_lock:
            job_state["error"] = str(e)
    finally:
        with job_lock:
            job_state["running"] = False


def run_embed_job():
    """Run batch embedding in background thread."""
    global job_state
    from embed import embed_batch

    def on_progress(current, total):
        with job_lock:
            job_state["current"] = current
            job_state["total"] = total

    try:
        with job_lock:
            job_state["running"] = True
            job_state["type"] = "embed"
            job_state["current"] = 0
            job_state["total"] = 0
            job_state["error"] = None

        result = embed_batch(on_progress=on_progress)

        with job_lock:
            job_state["result"] = result

    except Exception as e:
        logger.error(f"Embed job failed: {e}")
        with job_lock:
            job_state["error"] = str(e)
    finally:
        with job_lock:
            job_state["running"] = False


def run_digest_job():
    """Run digest generation in background thread."""
    global job_state
    from digest import generate_digest

    try:
        with job_lock:
            job_state["running"] = True
            job_state["type"] = "digest"
            job_state["current"] = 0
            job_state["total"] = 1
            job_state["message"] = "Generating digest..."
            job_state["error"] = None

        result = generate_digest()

        with job_lock:
            job_state["current"] = 1
            job_state["result"] = result
            if not result.get("success"):
                job_state["error"] = result.get("error")

    except Exception as e:
        logger.error(f"Digest job failed: {e}")
        with job_lock:
            job_state["error"] = str(e)
    finally:
        with job_lock:
            job_state["running"] = False


@app.route("/")
def index():
    """Browse articles with filtering and pagination."""
    # Get filter parameters
    source = request.args.get("source", "")
    has_summary = request.args.get("has_summary", "")
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    search = request.args.get("search", "")
    keyword = request.args.get("keyword", "")
    page = request.args.get("page", 1, type=int)
    per_page = 20

    # Build filters dict
    filters = {}
    if source:
        filters["source"] = source
    if has_summary == "yes":
        filters["has_summary"] = True
    elif has_summary == "no":
        filters["has_summary"] = False
    if date_from:
        filters["date_from"] = date_from
    if date_to:
        filters["date_to"] = date_to
    if search:
        filters["search"] = search
    if keyword:
        filters["keyword"] = keyword

    articles, total = get_articles(filters=filters, page=page, per_page=per_page)
    total_pages = ceil(total / per_page) if total > 0 else 1
    sources = get_sources()
    keywords = get_keywords()

    # Stats
    article_count = get_article_count()
    summarized_count = get_summarized_count()

    # Check if this is an HTMX request for just the article list
    if request.headers.get("HX-Request"):
        return render_template(
            "partials/article_list.html",
            articles=articles,
            page=page,
            total_pages=total_pages,
            total=total,
            source=source,
            has_summary=has_summary,
            date_from=date_from,
            date_to=date_to,
            search=search,
            keyword=keyword,
        )

    return render_template(
        "index.html",
        articles=articles,
        page=page,
        total_pages=total_pages,
        total=total,
        sources=sources,
        keywords=keywords,
        source=source,
        has_summary=has_summary,
        date_from=date_from,
        date_to=date_to,
        search=search,
        keyword=keyword,
        article_count=article_count,
        summarized_count=summarized_count,
    )


@app.route("/article/<int:article_id>")
def article_view(article_id):
    """View a single article."""
    article = get_article(article_id)
    if not article:
        return "Article not found", 404
    return render_template("article.html", article=article)


@app.route("/article/<int:article_id>/summarize", methods=["POST"])
def regenerate_summary(article_id):
    """Regenerate summary for a single article."""
    article = get_article(article_id)
    if not article:
        return "Article not found", 404

    # Check if a job is already running
    with job_lock:
        if job_state["running"]:
            return "Another job is running", 409

    # Summarize synchronously (single article is fast enough)
    result = summarize_article(article["title"], article["content"])

    if result.success:
        update_summary(article_id, result.summary, result.keywords)
        # Refetch article to get updated data
        article = get_article(article_id)
    else:
        logger.warning(f"Failed to summarize article {article_id}: {result.error_message}")

    if request.headers.get("HX-Request"):
        return render_template("partials/summary_section.html", article=article)

    return redirect(url_for("article_view", article_id=article_id))


def get_ollama_models():
    """Fetch available models from Ollama."""
    try:
        response = requests.get("http://localhost:11434/api/tags", timeout=5)
        response.raise_for_status()
        data = response.json()
        return [m["name"] for m in data.get("models", [])]
    except Exception as e:
        logger.warning(f"Could not fetch Ollama models: {e}")
        return []


@app.route("/settings")
def settings_page():
    """Settings page."""
    settings = get_all_settings()
    article_count = get_article_count()
    summarized_count = get_summarized_count()
    embedded_count = get_embedded_count()
    ollama_models = get_ollama_models()
    next_pipeline = get_next_pipeline_run()
    next_digest = get_next_digest_run()
    with job_lock:
        state = dict(job_state)
    return render_template(
        "settings.html",
        settings=settings,
        article_count=article_count,
        summarized_count=summarized_count,
        embedded_count=embedded_count,
        job_state=state,
        ollama_models=ollama_models,
        next_pipeline_run=next_pipeline,
        next_digest_run=next_digest,
    )


@app.route("/stats")
def stats():
    """Return current article stats for live updates."""
    article_count = get_article_count()
    summarized_count = get_summarized_count()
    embedded_count = get_embedded_count()
    return render_template(
        "partials/stats.html",
        article_count=article_count,
        summarized_count=summarized_count,
        embedded_count=embedded_count,
    )


@app.route("/settings", methods=["POST"])
def update_settings():
    """Update settings from form submission."""
    # Ollama settings
    set_setting("ollama_model", request.form.get("ollama_model", "llama3.2"))
    set_setting("ollama_num_ctx", request.form.get("ollama_num_ctx", "4096"))
    set_setting("ollama_temperature", request.form.get("ollama_temperature", "0.3"))
    set_setting("ollama_system_prompt", request.form.get("ollama_system_prompt", ""))

    # Ingestion settings
    set_setting("jsonl_path", request.form.get("jsonl_path", ""))

    # Schedule settings
    new_schedule = request.form.get("ingest_schedule", "")
    auto_ingest = "true" if request.form.get("auto_ingest") else "false"

    set_setting("ingest_schedule", new_schedule)
    set_setting("auto_ingest", auto_ingest)

    # Update scheduler
    if auto_ingest == "true" and new_schedule:
        try:
            schedule_ingest(new_schedule)
        except Exception as e:
            logger.error(f"Failed to update schedule: {e}")
    else:
        remove_ingest_job()

    if request.headers.get("HX-Request"):
        return '<div class="notice">Settings saved</div>'

    return redirect(url_for("settings_page"))


@app.route("/ingest", methods=["POST"])
def trigger_ingest():
    """Trigger JSONL ingestion job."""
    with job_lock:
        if job_state["running"]:
            return jsonify({"error": "Another job is running"}), 409

    filepath = get_all_settings().get("jsonl_path", "/home/kellogg/data/rssfeed.jsonl")

    thread = threading.Thread(target=run_ingest_job, args=(filepath,))
    thread.daemon = True
    thread.start()

    if request.headers.get("HX-Request"):
        return render_template("partials/job_status.html", job_state={
            "running": True, "type": "ingest", "current": 0, "total": 0, "error": None, "result": None
        })

    return jsonify({"status": "started", "type": "ingest"})


@app.route("/summarize", methods=["POST"])
def trigger_summarize():
    """Trigger batch summarization job."""
    with job_lock:
        if job_state["running"]:
            return jsonify({"error": "Another job is running"}), 409

    thread = threading.Thread(target=run_summarize_job)
    thread.daemon = True
    thread.start()

    if request.headers.get("HX-Request"):
        return render_template("partials/job_status.html", job_state={
            "running": True, "type": "summarize", "current": 0, "total": 0, "error": None, "result": None
        })

    return jsonify({"status": "started", "type": "summarize"})


@app.route("/pipeline", methods=["POST"])
def trigger_pipeline():
    """Trigger full pipeline job (ingest + compress + summarize)."""
    with job_lock:
        if job_state["running"]:
            return jsonify({"error": "Another job is running"}), 409

    # Also check if scheduled pipeline is running
    if is_pipeline_running():
        return jsonify({"error": "Scheduled pipeline is currently running"}), 409

    thread = threading.Thread(target=run_pipeline_job)
    thread.daemon = True
    thread.start()

    if request.headers.get("HX-Request"):
        return render_template("partials/job_status.html", job_state={
            "running": True,
            "type": "pipeline",
            "stage": "starting",
            "current": 0,
            "total": 0,
            "message": "Starting pipeline...",
            "error": None,
            "result": None
        })

    return jsonify({"status": "started", "type": "pipeline"})


@app.route("/status")
def job_status():
    """Return current job status for polling."""
    with job_lock:
        state = dict(job_state)

    if request.headers.get("HX-Request"):
        return render_template("partials/job_status.html", job_state=state)

    return jsonify(state)


@app.route("/embed", methods=["POST"])
def trigger_embed():
    """Trigger batch embedding job."""
    with job_lock:
        if job_state["running"]:
            return jsonify({"error": "Another job is running"}), 409

    thread = threading.Thread(target=run_embed_job)
    thread.daemon = True
    thread.start()

    if request.headers.get("HX-Request"):
        return render_template("partials/job_status.html", job_state={
            "running": True, "type": "embed", "current": 0, "total": 0, "error": None, "result": None
        })

    return jsonify({"status": "started", "type": "embed"})


@app.route("/chat")
def chat_page():
    """Chat interface page."""
    history = get_chat_history(limit=50)
    return render_template("chat.html", messages=history)


@app.route("/chat", methods=["POST"])
def send_chat_message():
    """Handle a chat message and generate response."""
    from chat import chat

    query = request.form.get("message", "").strip()
    if not query:
        if request.headers.get("HX-Request"):
            return "", 400
        return jsonify({"error": "No message provided"}), 400

    # Save user message
    save_chat_message("user", query)

    # Generate response via RAG
    result = chat(query)

    # Save assistant response
    sources = result.get("source_ids", [])
    save_chat_message("assistant", result["response"], sources=sources)

    if request.headers.get("HX-Request"):
        # Get source articles for display
        source_articles = get_articles_by_ids(sources) if sources else []
        return render_template(
            "partials/chat_response.html",
            user_message=query,
            assistant_message=result["response"],
            source_articles=source_articles,
            error=result.get("error"),
        )

    return jsonify({
        "response": result["response"],
        "sources": sources,
        "error": result.get("error"),
    })


@app.route("/chat/clear", methods=["POST"])
def clear_chat():
    """Clear chat history."""
    clear_chat_history()

    if request.headers.get("HX-Request"):
        return '<div class="notice">Chat history cleared</div>'

    return jsonify({"status": "cleared"})


@app.route("/digest")
def digest_page():
    """Daily digest page."""
    digests = get_recent_digests(limit=14)
    next_digest = get_next_digest_run()
    with job_lock:
        state = dict(job_state)
    return render_template(
        "digest.html",
        digests=digests,
        next_digest_run=next_digest,
        job_state=state,
    )


@app.route("/digest/generate", methods=["POST"])
def trigger_digest():
    """Trigger digest generation job."""
    with job_lock:
        if job_state["running"]:
            return jsonify({"error": "Another job is running"}), 409

    thread = threading.Thread(target=run_digest_job)
    thread.daemon = True
    thread.start()

    if request.headers.get("HX-Request"):
        return render_template("partials/job_status.html", job_state={
            "running": True, "type": "digest", "current": 0, "total": 1,
            "message": "Generating digest...", "error": None, "result": None
        })

    return jsonify({"status": "started", "type": "digest"})


@app.template_filter("truncate_content")
def truncate_content(content, length=200):
    """Truncate content for display."""
    if not content:
        return ""
    if len(content) <= length:
        return content
    return content[:length].rsplit(" ", 1)[0] + "..."


@app.template_filter("format_date")
def format_date(date_str):
    """Format ISO date for display."""
    if not date_str:
        return ""
    try:
        from dateutil import parser
        dt = parser.parse(date_str)
        return dt.strftime("%b %d, %Y %H:%M")
    except Exception:
        return date_str


if __name__ == "__main__":
    # Initialize database
    init_db()
    logger.info("Database initialized")

    # Start scheduler
    start_scheduler(app)

    # Run Flask development server
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=True)
