"""
Data migration script: migrate from legacy monitor_weibo SQLite + monitor_wechat JSON to unified database
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from db.schema import init_db, get_connection

BASE_DIR = Path(__file__).parent.parent
LEGACY_WEIBO_DB = BASE_DIR / "legacy" / "monitor_weibo" / "data" / "weibo_data.db"
LEGACY_WECHAT_JSON = BASE_DIR / "legacy" / "monitor_wechat" / "data" / "articles.json"
UNIFIED_DB = BASE_DIR / "db" / "monitor.db"


def migrate_weibo_posts(old_conn, new_conn) -> int:
    rows = old_conn.execute("SELECT * FROM posts").fetchall()
    count = 0
    for row in rows:
        new_conn.execute(
            """INSERT OR IGNORE INTO posts
               (id, platform, keyword, user_name, user_id, title, content, url,
                created_at, fetched_at, reposts_count, comments_count, likes_count)
               VALUES (?, 'weibo', ?, ?, ?, '', ?, '', ?, ?, ?, ?, ?)""",
            (
                row["id"],
                row["keyword"],
                row["user_name"],
                row["user_id"],
                row["text"],
                row["created_at"],
                row["fetched_at"],
                row["reposts_count"],
                row["comments_count"],
                row["attitudes_count"],
            ),
        )
        count += 1
    return count


def migrate_weibo_comments(old_conn, new_conn) -> int:
    rows = old_conn.execute("SELECT * FROM comments").fetchall()
    count = 0
    for row in rows:
        new_conn.execute(
            """INSERT OR IGNORE INTO comments
               (id, post_id, platform, user_name, content, created_at, fetched_at)
               VALUES (?, ?, 'weibo', ?, ?, ?, ?)""",
            (
                row["id"],
                row["post_id"],
                row["user_name"],
                row["text"],
                row["created_at"],
                row["fetched_at"],
            ),
        )
        count += 1
    return count


def migrate_weibo() -> tuple[int, int]:
    if not LEGACY_WEIBO_DB.exists():
        print(f"[SKIP] Legacy Weibo database not found: {LEGACY_WEIBO_DB}")
        return 0, 0

    old_conn = sqlite3.connect(str(LEGACY_WEIBO_DB))
    old_conn.row_factory = sqlite3.Row

    new_conn = get_connection(UNIFIED_DB)
    try:
        posts_count = migrate_weibo_posts(old_conn, new_conn)
        comments_count = migrate_weibo_comments(old_conn, new_conn)
        new_conn.commit()
        return posts_count, comments_count
    finally:
        old_conn.close()
        new_conn.close()


def migrate_wechat() -> int:
    if not LEGACY_WECHAT_JSON.exists():
        print(f"[SKIP] Legacy WeChat data not found: {LEGACY_WECHAT_JSON}")
        return 0

    articles = json.loads(LEGACY_WECHAT_JSON.read_text(encoding="utf-8"))
    new_conn = get_connection(UNIFIED_DB)
    count = 0
    try:
        for url_key, article in articles.items():
            extra = json.dumps({
                "account": article.get("account", ""),
                "source": article.get("source", "unknown"),
                "digest": article.get("digest", ""),
            }, ensure_ascii=False)

            new_conn.execute(
                """INSERT OR IGNORE INTO posts
                   (id, platform, keyword, user_name, title, content, url,
                    created_at, fetched_at, extra)
                   VALUES (?, 'wechat', 'target-keyword', ?, ?, ?, ?, ?, ?, ?)""",
                (
                    url_key,
                    article.get("account", ""),
                    article.get("title", ""),
                    article.get("digest", ""),
                    article.get("url", ""),
                    article.get("pub_time", ""),
                    article.get("found_at", ""),
                    extra,
                ),
            )
            count += 1
        new_conn.commit()
        return count
    finally:
        new_conn.close()


def run_migration():
    print("=" * 50)
    print("Data migration started")
    print("=" * 50)

    init_db(UNIFIED_DB)
    print(f"[OK] Unified database initialized: {UNIFIED_DB}")

    posts, comments = migrate_weibo()
    print(f"[OK] Weibo migration completed: {posts} posts, {comments} comments")

    articles = migrate_wechat()
    print(f"[OK] WeChat migration completed: {articles} articles")

    # Verify
    conn = get_connection(UNIFIED_DB)
    try:
        total_posts = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        total_comments = conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
        by_platform = conn.execute(
            "SELECT platform, COUNT(*) as cnt FROM posts GROUP BY platform"
        ).fetchall()

        print(f"\n{'=' * 50}")
        print(f"Migration completed! Total {total_posts} posts, {total_comments} comments")
        for row in by_platform:
            print(f"  - {row['platform']}: {row['cnt']} posts")
        print(f"{'=' * 50}")
    finally:
        conn.close()


if __name__ == "__main__":
    run_migration()
