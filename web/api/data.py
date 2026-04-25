"""Data Query API"""
import json
import logging
from flask import Blueprint, jsonify, request, current_app
from db.schema import get_connection

log = logging.getLogger(__name__)
bp = Blueprint("data", __name__)


@bp.route("/api/data/posts")
def list_posts():
    db = current_app.config["DB_PATH"]
    platform = request.args.get("platform", "")
    keyword = request.args.get("keyword", "")
    sentiment = request.args.get("sentiment", "")
    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 20))

    conn = get_connection(db)
    try:
        where_clauses = []
        params = []

        if platform:
            where_clauses.append("platform = ?")
            params.append(platform)
        if keyword:
            where_clauses.append("keyword LIKE ?")
            params.append(f"%{keyword}%")
        if sentiment:
            where_clauses.append("sentiment = ?")
            params.append(sentiment)

        where = " AND ".join(where_clauses) if where_clauses else "1=1"

        total = conn.execute(f"SELECT COUNT(*) FROM posts WHERE {where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM posts WHERE {where} ORDER BY fetched_at DESC LIMIT ? OFFSET ?",
            params + [per_page, (page - 1) * per_page],
        ).fetchall()

        posts = []
        for row in rows:
            p = dict(row)
            if p.get("extra"):
                try:
                    p["extra"] = json.loads(p["extra"])
                except (json.JSONDecodeError, TypeError):
                    pass
            if p.get("keywords"):
                try:
                    p["keywords"] = json.loads(p["keywords"])
                except (json.JSONDecodeError, TypeError):
                    pass
            posts.append(p)

        return jsonify({
            "posts": posts,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page,
        })
    finally:
        conn.close()


@bp.route("/api/data/comments/<post_id>")
def list_comments(post_id):
    db = current_app.config["DB_PATH"]
    conn = get_connection(db)
    try:
        rows = conn.execute(
            "SELECT * FROM comments WHERE post_id = ? ORDER BY fetched_at",
            (post_id,),
        ).fetchall()
        comments = [dict(r) for r in rows]
        return jsonify({"comments": comments, "total": len(comments)})
    finally:
        conn.close()


@bp.route("/api/data/export")
def export_data():
    import csv
    import io
    from flask import Response

    db = current_app.config["DB_PATH"]
    platform = request.args.get("platform", "")
    fmt = request.args.get("format", "json")

    conn = get_connection(db)
    try:
        if platform:
            rows = conn.execute("SELECT * FROM posts WHERE platform=? ORDER BY fetched_at DESC", (platform,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM posts ORDER BY fetched_at DESC").fetchall()

        if fmt == "csv":
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["id", "platform", "keyword", "user_name", "title", "content", "url", "created_at", "sentiment", "sentiment_score"])
            for row in rows:
                writer.writerow([row["id"], row["platform"], row["keyword"], row["user_name"], row["title"], row["content"], row["url"], row["created_at"], row["sentiment"], row["sentiment_score"]])
            return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=posts.csv"})

        posts = [dict(r) for r in rows]
        return jsonify({"posts": posts})
    finally:
        conn.close()
