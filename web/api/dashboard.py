"""仪表盘 API"""
import logging
from flask import Blueprint, jsonify, current_app
from db.schema import get_connection

log = logging.getLogger(__name__)
bp = Blueprint("dashboard", __name__)


@bp.route("/api/dashboard/stats")
def stats():
    db = current_app.config["DB_PATH"]
    conn = get_connection(db)
    try:
        total_posts = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        total_comments = conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
        by_platform = conn.execute(
            "SELECT platform, COUNT(*) as cnt FROM posts GROUP BY platform"
        ).fetchall()
        sentiment_dist = conn.execute(
            """SELECT COALESCE(sentiment,'未分析') as sentiment, COUNT(*) as cnt
               FROM posts GROUP BY sentiment"""
        ).fetchall()
        recent_runs = conn.execute(
            """SELECT platform, keyword, started_at, finished_at, status, posts_found, error_message
               FROM scheduler_runs ORDER BY started_at DESC LIMIT 20"""
        ).fetchall()
        return jsonify({
            "total_posts": total_posts,
            "total_comments": total_comments,
            "by_platform": [dict(r) for r in by_platform],
            "sentiment_distribution": [dict(r) for r in sentiment_dist],
            "recent_runs": [dict(r) for r in recent_runs],
        })
    finally:
        conn.close()


@bp.route("/api/dashboard/platforms")
def platform_status():
    db = current_app.config["DB_PATH"]
    conn = get_connection(db)
    try:
        auth_rows = conn.execute("SELECT platform, auth_status, last_validated FROM platform_auth").fetchall()
        auth_map = {r["platform"]: dict(r) for r in auth_rows}
    finally:
        conn.close()

    cfg = current_app.config["MONITOR_CONFIG"]
    platforms = cfg.get("platforms", {})
    result = []
    for name, pcfg in platforms.items():
        auth = auth_map.get(name, {"auth_status": "inactive", "last_validated": None})
        result.append({
            "name": name,
            "enabled": pcfg.get("enabled", False),
            "interval_hours": pcfg.get("interval_hours", 6),
            "keywords": pcfg.get("keywords", cfg.get("default_keywords", [])),
            "auth_status": auth.get("auth_status", "inactive"),
            "last_validated": auth.get("last_validated"),
            "source": pcfg.get("source", ""),
        })
    return jsonify(result)


@bp.route("/api/scheduler/status")
def scheduler_status():
    if hasattr(current_app, "scheduler") and current_app.scheduler:
        return jsonify(current_app.scheduler.get_status())
    return jsonify([])
