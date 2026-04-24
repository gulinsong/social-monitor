import os
import sqlite3
import stat
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "db" / "monitor.db"

SCHEMA_POSTS = """
CREATE TABLE IF NOT EXISTS posts (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    keyword TEXT NOT NULL DEFAULT '',
    user_name TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL DEFAULT '',
    reposts_count INTEGER NOT NULL DEFAULT 0,
    comments_count INTEGER NOT NULL DEFAULT 0,
    likes_count INTEGER NOT NULL DEFAULT 0,
    shares_count INTEGER NOT NULL DEFAULT 0,
    sentiment TEXT,
    sentiment_score REAL,
    keywords TEXT,
    llm_analysis TEXT,
    extra TEXT,
    pushed_to_feishu INTEGER NOT NULL DEFAULT 0
);
"""

SCHEMA_COMMENTS = """
CREATE TABLE IF NOT EXISTS comments (
    id TEXT PRIMARY KEY,
    post_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    user_name TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL DEFAULT '',
    sentiment TEXT,
    sentiment_score REAL,
    keywords TEXT,
    extra TEXT,
    FOREIGN KEY (post_id) REFERENCES posts(id)
);
"""

SCHEMA_SCHEDULER_RUNS = """
CREATE TABLE IF NOT EXISTS scheduler_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    keyword TEXT DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT DEFAULT 'running',
    posts_found INTEGER DEFAULT 0,
    error_message TEXT
);
"""

SCHEMA_PLATFORM_AUTH = """
CREATE TABLE IF NOT EXISTS platform_auth (
    platform TEXT PRIMARY KEY,
    cookies TEXT,
    auth_status TEXT DEFAULT 'inactive',
    last_validated TEXT,
    extra TEXT
);
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_posts_platform ON posts(platform);",
    "CREATE INDEX IF NOT EXISTS idx_posts_keyword ON posts(keyword);",
    "CREATE INDEX IF NOT EXISTS idx_posts_sentiment ON posts(sentiment);",
    "CREATE INDEX IF NOT EXISTS idx_posts_fetched ON posts(fetched_at);",
    "CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id);",
    "CREATE INDEX IF NOT EXISTS idx_sched_platform ON scheduler_runs(platform);",
    "CREATE INDEX IF NOT EXISTS idx_sched_started ON scheduler_runs(started_at);",
]


def get_connection(db_path: str | Path = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str | Path = None):
    conn = get_connection(db_path)
    try:
        conn.executescript(SCHEMA_POSTS)
        conn.executescript(SCHEMA_COMMENTS)
        conn.executescript(SCHEMA_SCHEDULER_RUNS)
        conn.executescript(SCHEMA_PLATFORM_AUTH)
        for idx in INDEXES:
            conn.execute(idx)
        conn.commit()
    finally:
        conn.close()

    path = Path(db_path) if db_path else DB_PATH
    os.chmod(str(path), stat.S_IRUSR | stat.S_IWUSR)
    os.chmod(str(path.parent), stat.S_IRWXU)


if __name__ == "__main__":
    init_db()
    print(f"[OK] Database initialized at {DB_PATH}")
