"""Sentiment Analysis API"""
import json
import logging
from flask import Blueprint, jsonify, request, current_app
from db.schema import get_connection

log = logging.getLogger(__name__)
bp = Blueprint("analysis", __name__)


@bp.route("/api/analysis/summary")
def summary():
    db = current_app.config["DB_PATH"]
    conn = get_connection(db)
    try:
        # Sentiment distribution
        sentiment = conn.execute(
            """SELECT COALESCE(sentiment,'Not Analyzed') as label, COUNT(*) as count
               FROM posts GROUP BY sentiment ORDER BY count DESC"""
        ).fetchall()

        # Sentiment distribution by platform
        platform_sentiment = conn.execute(
            """SELECT platform, sentiment, COUNT(*) as count
               FROM posts WHERE sentiment IS NOT NULL
               GROUP BY platform, sentiment"""
        ).fetchall()

        # Top keywords
        all_keywords = []
        rows = conn.execute("SELECT keywords FROM posts WHERE keywords IS NOT NULL").fetchall()
        for row in rows:
            try:
                kws = json.loads(row["keywords"])
                all_keywords.extend(kws)
            except (json.JSONDecodeError, TypeError):
                continue  # skip malformed keywords

        keyword_freq = {}
        for kw in all_keywords:
            keyword_freq[kw] = keyword_freq.get(kw, 0) + 1
        top_keywords = sorted(keyword_freq.items(), key=lambda x: -x[1])[:30]

        # Daily trend (last 7 days)
        daily = conn.execute(
            """SELECT DATE(fetched_at) as day,
                      COUNT(*) as total,
                      SUM(CASE WHEN sentiment='positive' THEN 1 ELSE 0 END) as positive,
                      SUM(CASE WHEN sentiment='negative' THEN 1 ELSE 0 END) as negative,
                      SUM(CASE WHEN sentiment='neutral' THEN 1 ELSE 0 END) as neutral
               FROM posts
               WHERE fetched_at >= DATE('now', '-7 days')
               GROUP BY DATE(fetched_at)
               ORDER BY day"""
        ).fetchall()

        return jsonify({
            "sentiment_distribution": [dict(r) for r in sentiment],
            "platform_sentiment": [dict(r) for r in platform_sentiment],
            "top_keywords": [{"word": w, "count": c} for w, c in top_keywords],
            "daily_trend": [dict(r) for r in daily],
        })
    finally:
        conn.close()


@bp.route("/api/analysis/run", methods=["POST"])
def run_analysis():
    """Run sentiment analysis on unanalyzed data"""
    from analysis.sentiment import SentimentAnalyzer
    from core.config_loader import load_config

    cfg = load_config()
    analyzer = SentimentAnalyzer(cfg.get("sentiment", {}).get("custom_dict"))

    db = current_app.config["DB_PATH"]
    conn = get_connection(db)
    try:
        rows = conn.execute(
            "SELECT id, title, content, extra FROM posts WHERE sentiment IS NULL LIMIT 500"
        ).fetchall()

        count = 0
        for row in rows:
            text = f"{row['title']} {row['content']}".strip()
            if not text:
                continue
            result = analyzer.analyze(text)
            tags = analyzer.extract_tags(text)
            summary = analyzer.generate_summary(text)
            risk_level = analyzer.assess_risk(text, result["score"])

            extra = {}
            if row["extra"]:
                try:
                    extra = json.loads(row["extra"]) if isinstance(row["extra"], str) else row["extra"]
                except (json.JSONDecodeError, TypeError):
                    extra = {}
            extra["tags"] = tags
            extra["summary"] = summary
            extra["risk_level"] = risk_level

            conn.execute(
                "UPDATE posts SET sentiment=?, sentiment_score=?, keywords=?, extra=? WHERE id=?",
                (result["sentiment"], result["score"],
                 json.dumps(result["keywords"], ensure_ascii=False),
                 json.dumps(extra, ensure_ascii=False), row["id"]),
            )
            count += 1

        conn.commit()
        return jsonify({"analyzed": count})
    finally:
        conn.close()


@bp.route("/api/analysis/tags")
def tags_summary():
    """Tag frequency from posts extra JSON"""
    db = current_app.config["DB_PATH"]
    conn = get_connection(db)
    try:
        rows = conn.execute("SELECT extra FROM posts WHERE extra IS NOT NULL").fetchall()
        tag_freq = {}
        for row in rows:
            try:
                extra = json.loads(row["extra"]) if isinstance(row["extra"], str) else row["extra"]
                for tag in extra.get("tags", []):
                    tag_freq[tag] = tag_freq.get(tag, 0) + 1
            except (json.JSONDecodeError, TypeError, AttributeError):
                continue
        top_tags = sorted(tag_freq.items(), key=lambda x: -x[1])[:30]
        return jsonify({"tags": [{"tag": t, "count": c} for t, c in top_tags]})
    finally:
        conn.close()


@bp.route("/api/analysis/high-risk")
def high_risk():
    """High/medium risk posts"""
    db = current_app.config["DB_PATH"]
    conn = get_connection(db)
    try:
        rows = conn.execute(
            """SELECT id, platform, content, sentiment, created_at, extra
               FROM posts WHERE extra IS NOT NULL
               ORDER BY fetched_at DESC LIMIT 100"""
        ).fetchall()
        posts = []
        for row in rows:
            try:
                extra = json.loads(row["extra"]) if isinstance(row["extra"], str) else row["extra"]
                risk = extra.get("risk_level", "low")
                if risk in ("high", "medium"):
                    posts.append({
                        "id": row["id"],
                        "platform": row["platform"],
                        "content": row["content"],
                        "sentiment": row["sentiment"],
                        "risk_level": risk,
                        "created_at": row["created_at"],
                        "extra": extra,
                    })
            except (json.JSONDecodeError, TypeError, AttributeError):
                continue
        return jsonify({"posts": posts[:50]})
    finally:
        conn.close()
