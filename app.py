"""
NewsIntel — Pipeline Results Dashboard
========================================
Interactive version: submitting a search runs fetch.py then classifier.py
as subprocesses and renders the combined JSON output as a business
intelligence dashboard.

Usage:
    python app.py                          # starts with data/demo_output.json loaded
    python app.py --json path/to/file.json # start with a specific prior run loaded
    python app.py --port 8080
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, render_template, request, jsonify, abort, send_file

from pipeline import PipelineRunner

APP_ROOT = Path(__file__).parent.resolve()
DEFAULT_JSON_PATH = APP_ROOT / "data" / "demo_output.json"

app = Flask(__name__)
runner = PipelineRunner(APP_ROOT)

# Currently displayed dataset. Starts with the demo/default file and is
# replaced whenever a job finishes or the user picks a past run.
STATE: dict = {"path": None, "raw": None}


# --------------------------------------------------------------------------
# Data loading + shaping  (unchanged from the static viewer, still the core
# of how raw pipeline JSON becomes template-ready data)
# --------------------------------------------------------------------------

def load_pipeline_data(json_path: Path) -> dict:
    if not json_path.exists():
        raise FileNotFoundError(f"No pipeline JSON found at '{json_path}'.")
    with json_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    required = {"query", "articles"}
    missing = required - data.keys()
    if missing:
        raise ValueError(f"Pipeline JSON is missing required field(s): {sorted(missing)}")
    return data


def _parse_iso(ts: str | None):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _relative_time(dt) -> str:
    if dt is None:
        return "Unknown"
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = now - dt
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "Just now"
    if seconds < 3600:
        return f"{max(1, seconds // 60)}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    days = seconds // 86400
    if days < 7:
        return f"{days}d ago"
    return dt.strftime("%b %d, %Y")


def _domain(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return ""


def _confidence_band(score: float) -> str:
    if score >= 0.9:
        return "high"
    if score >= 0.7:
        return "medium"
    return "low"


CATEGORY_PALETTE = {
    "Product Launches": "#3B6FE0",
    "Market & Competitive Moves": "#059669",
    "Regulatory & Policy": "#D97706",
    "Funding & Investment": "#7C3AED",
    "Partnerships & Alliances": "#0EA5E9",
    "Leadership & Talent": "#DB2777",
    "Research & Technology": "#DC2626",
}
FALLBACK_COLORS = ["#64748B", "#0D9488", "#CA8A04", "#4338CA"]


def category_color(name: str, fallback_index: int = 0) -> str:
    if name in CATEGORY_PALETTE:
        return CATEGORY_PALETTE[name]
    return FALLBACK_COLORS[fallback_index % len(FALLBACK_COLORS)]


def build_view_model(data: dict) -> dict:
    articles_raw = data.get("articles", [])

    articles = []
    for i, a in enumerate(articles_raw):
        published = _parse_iso(a.get("published_at"))
        conf = float(a.get("confidence") or 0)
        articles.append({
            **a,
            "domain": _domain(a.get("url", "")),
            "relative_time": _relative_time(published),
            "published_sort": published or datetime.min.replace(tzinfo=timezone.utc),
            "confidence_pct": round(conf * 100),
            "confidence_band": _confidence_band(conf),
            "color": category_color(a.get("category", "Uncategorized"), i),
            "description": a.get("description") or "No summary available from source.",
            "has_description": bool(a.get("description")),
        })
    articles.sort(key=lambda a: a["published_sort"], reverse=True)

    cat_counter = Counter(a.get("category", "Uncategorized") for a in articles_raw)
    categories = [
        {
            "name": name,
            "count": count,
            "pct": round(count / len(articles_raw) * 100) if articles_raw else 0,
            "color": category_color(name, idx),
        }
        for idx, (name, count) in enumerate(
            sorted(cat_counter.items(), key=lambda kv: kv[1], reverse=True)
        )
    ]

    source_results = data.get("source_results", [])
    sources = []
    for s in source_results:
        status = s.get("status", "unknown")
        sources.append({
            **s,
            "status_label": {"ok": "Operational", "failed": "Failed", "skipped": "Skipped"}
                .get(status, status.title()),
            "duration_display": f"{s.get('duration_seconds', 0):.2f}s"
                if s.get("duration_seconds") else "—",
        })

    domain_counter = Counter(a["domain"] for a in articles if a["domain"])
    top_domains = domain_counter.most_common(6)

    day_counter: dict[str, int] = defaultdict(int)
    for a in articles:
        d = a["published_sort"]
        if d.year > 1:
            day_counter[d.strftime("%Y-%m-%d")] += 1
    timeline = [
        {"date": d, "count": c, "label": datetime.strptime(d, "%Y-%m-%d").strftime("%b %d")}
        for d, c in sorted(day_counter.items())
    ]
    max_timeline_count = max((t["count"] for t in timeline), default=1)

    avg_conf = (
        round(sum(a["confidence_pct"] for a in articles) / len(articles)) if articles else 0
    )

    summary = data.get("summary", {})
    date_range = data.get("date_range", {})
    generated_at = _parse_iso(data.get("generated_at"))

    return {
        "query": data.get("query", ""),
        "date_from": date_range.get("from", "—"),
        "date_to": date_range.get("to", "—"),
        "generated_at_display": generated_at.strftime("%B %d, %Y at %H:%M UTC")
            if generated_at else "Unknown",
        "requested_sources": data.get("requested_sources", []),
        "summary": summary,
        "sources": sources,
        "sources_ok_count": sum(1 for s in sources if s.get("status") == "ok"),
        "sources_total_count": len(sources),
        "articles": articles,
        "article_count": len(articles),
        "categories": categories,
        "top_domains": top_domains,
        "timeline": timeline,
        "max_timeline_count": max_timeline_count,
        "avg_confidence": avg_conf,
        "classification_meta": data.get("classification_meta", {}),
    }


# --------------------------------------------------------------------------
# Routes — page
# --------------------------------------------------------------------------

@app.route("/")
def index():
    vm = build_view_model(STATE["raw"]) if STATE["raw"] else None
    return render_template(
        "index.html",
        vm=vm,
        source_path=str(STATE["path"]) if STATE["path"] else None,
        recent_runs=runner.list_runs()[:8],
    )


# --------------------------------------------------------------------------
# Routes — search / job API
# --------------------------------------------------------------------------

ALLOWED_SOURCES = {"bing_news_rss", "gnews", "google_news_rss", "newsapi", "all"}


@app.route("/api/search", methods=["POST"])
def api_search():
    payload = request.get_json(force=True, silent=True) or {}

    query = (payload.get("query") or "").strip()
    if not query:
        return jsonify({"error": "Query is required."}), 400
    if len(query) > 200:
        return jsonify({"error": "Query is too long (max 200 characters)."}), 400

    sources_list = payload.get("sources") or ["all"]
    if not isinstance(sources_list, list) or not sources_list:
        sources_list = ["all"]
    bad = [s for s in sources_list if s not in ALLOWED_SOURCES]
    if bad:
        return jsonify({"error": f"Unknown source(s): {', '.join(bad)}"}), 400
    sources_str = "all" if "all" in sources_list else ",".join(sources_list)

    date_mode = payload.get("date_mode", "since")
    params = {
        "query": query,
        "sources": sources_str,
        "date_mode": date_mode,
        "since": payload.get("since") or "7d",
        "from_date": payload.get("from_date") or None,
        "to_date": payload.get("to_date") or None,
        "no_scrape_fallback": bool(payload.get("no_scrape_fallback")),
        "no_dedup": bool(payload.get("no_dedup")),
        "dedup_threshold": payload.get("dedup_threshold"),
        "dedup_title_weight": payload.get("dedup_title_weight"),
        "dedup_content_weight": payload.get("dedup_content_weight"),
        "batch_size": payload.get("batch_size"),
        "description_limit": payload.get("description_limit"),
        "provider": payload.get("provider") or None,
        "categories": payload.get("categories") or None,
    }

    if date_mode == "range" and not params["from_date"]:
        return jsonify({"error": "Start date is required when using a custom date range."}), 400

    job = runner.start_job(query, params)
    return jsonify({"job_id": job.id}), 202


@app.route("/api/jobs/<job_id>")
def api_job_status(job_id):
    job = runner.get_job(job_id)
    if job is None:
        return jsonify({"error": "Unknown job id."}), 404
    return jsonify(job.to_dict())


@app.route("/api/jobs/<job_id>/load", methods=["POST"])
def api_job_load(job_id):
    """Called once a job is done to make its output the active dataset."""
    job = runner.get_job(job_id)
    if job is None:
        return jsonify({"error": "Unknown job id."}), 404
    if job.status != "done" or not job.result_path:
        return jsonify({"error": "Job has not completed successfully."}), 409

    try:
        STATE["raw"] = load_pipeline_data(Path(job.result_path))
        STATE["path"] = Path(job.result_path)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as e:
        return jsonify({"error": f"Job finished but output could not be loaded: {e}"}), 500

    return jsonify({"ok": True, "redirect": "/"})


@app.route("/api/runs")
def api_runs():
    return jsonify(runner.list_runs())


@app.route("/api/runs/load", methods=["POST"])
def api_runs_load():
    payload = request.get_json(force=True, silent=True) or {}
    path = payload.get("path")
    if not path:
        return jsonify({"error": "path is required."}), 400
    p = Path(path)
    if not p.is_relative_to(runner.runs_dir) and p != DEFAULT_JSON_PATH:
        return jsonify({"error": "Path not allowed."}), 403
    try:
        STATE["raw"] = load_pipeline_data(p)
        STATE["path"] = p
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, "redirect": "/"})


@app.route("/api/export/xlsx")
def api_export_xlsx():
    if not STATE["raw"]:
        return jsonify({"error": "No dataset is currently loaded."}), 404

    from export import build_workbook
    vm = build_view_model(STATE["raw"])
    buf = build_workbook(vm)

    query_slug = "".join(c if c.isalnum() else "_" for c in vm["query"]).strip("_")[:40] or "results"
    filename = f"newsintel_{query_slug}.xlsx"

    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )


@app.errorhandler(500)
def handle_error(e):
    return render_template("error.html", message=getattr(e, "description", str(e))), 500


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="NewsIntel pipeline results dashboard")
    parser.add_argument("--json", dest="json_path", default=str(DEFAULT_JSON_PATH))
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    json_path = Path(args.json_path).resolve()
    try:
        STATE["raw"] = load_pipeline_data(json_path)
        STATE["path"] = json_path
        print(f"\n  \u2713  NewsIntel loaded {len(STATE['raw'].get('articles', []))} "
              f"articles from {json_path}")
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as e:
        print(f"\n  \u26a0  Starting with no dataset loaded: {e}")
        STATE["raw"] = None
        STATE["path"] = None

    print(f"  \u2192  http://{args.host}:{args.port}\n")
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
