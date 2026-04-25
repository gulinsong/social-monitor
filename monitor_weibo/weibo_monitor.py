#!/usr/bin/env python3
"""
Weibo Keyword Monitor - Safe Crawling Framework
Uses mobile API + Cookie authentication, low-frequency collection
"""

import argparse
import csv
import json
import random
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests


def load_config():
    cfg_path = Path(__file__).parent / "config.json"
    with open(cfg_path, "r", encoding="utf-8") as f:
        return json.load(f)


class WeiboMonitor:
    # Mobile search API, weaker anti-crawl
    SEARCH_URL = "https://m.weibo.cn/api/container/getIndex"
    # Mobile post detail API
    DETAIL_URL = "https://m.weibo.cn/detail/{id}"
    # Mobile comments API
    COMMENTS_URL = "https://m.weibo.cn/api/comments/show"

    def __init__(self, config):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Mobile Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://m.weibo.cn/",
            "X-Requested-With": "XMLHttpRequest",
        })
        self.session.cookies.set("Cookie", config["cookies"])

        # Parse cookie string into individual cookies
        for item in config["cookies"].split(";"):
            item = item.strip()
            if "=" in item:
                k, v = item.split("=", 1)
                self.session.cookies.set(k.strip(), v.strip(), domain=".weibo.cn")

        self.db_path = Path(config["output_dir"]) / "weibo_data.db"
        self.db_path.parent.mkdir(exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS posts (
                    id TEXT PRIMARY KEY,
                    keyword TEXT,
                    user_name TEXT,
                    user_id TEXT,
                    text TEXT,
                    created_at TEXT,
                    reposts_count INTEGER,
                    comments_count INTEGER,
                    attitudes_count INTEGER,
                    fetched_at TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS comments (
                    id TEXT PRIMARY KEY,
                    post_id TEXT,
                    user_name TEXT,
                    text TEXT,
                    created_at TEXT,
                    fetched_at TEXT
                )
            """)

    def _delay(self):
        """Random delay to simulate human behavior"""
        delay = random.uniform(
            self.config["request_delay_min"],
            self.config["request_delay_max"],
        )
        time.sleep(delay)

    def _safe_get(self, url, params=None, retries=2):
        """Safe request, auto-stop on failure to avoid account ban"""
        for attempt in range(retries):
            try:
                self._delay()
                resp = self.session.get(url, params=params, timeout=15)
                resp.raise_for_status()

                # Detect rate limiting or login requirement
                if "登录" in resp.text and "密码" in resp.text:  # Detect Chinese login page
                    print("[!] Cookie expired, please update cookies in config.json")
                    return None
                if resp.status_code == 403:
                    print("[!] Rate limited (403), stopping requests")
                    return None

                return resp.json() if "json" in resp.headers.get("Content-Type", "") else resp
            except requests.exceptions.RequestException as e:
                print(f"[!] Request failed (attempt {attempt+1}/{retries}): {e}")
                if attempt < retries - 1:
                    time.sleep(10 * (attempt + 1))  # Incremental wait
        return None

    def search_keyword(self, keyword, page=1):
        """Search keyword to get Weibo post list"""
        containerid = f"100103type=1&q={quote(keyword)}"
        params = {
            "containerid": containerid,
            "page_type": "searchall",
            "page": page,
        }
        data = self._safe_get(self.SEARCH_URL, params=params)
        if not data:
            return []

        cards = data.get("data", {}).get("cards", [])
        posts = []
        for card in cards:
            # Search results may be nested in card_group
            card_group = card.get("card_group", [])
            if not card_group:
                card_group = [card]
            for item in card_group:
                mblog = item.get("mblog")
                if not mblog:
                    continue
                posts.append(self._parse_post(mblog, keyword))
        return posts

    def _parse_post(self, mblog, keyword=""):
        """Parse a single Weibo post"""
        # Clean HTML tags
        text = mblog.get("text", "")
        text = re.sub(r"<[^>]+>", "", text)

        post = {
            "id": mblog.get("mid", mblog.get("id", "")),
            "keyword": keyword,
            "user_name": mblog.get("user", {}).get("screen_name", ""),
            "user_id": str(mblog.get("user", {}).get("id", "")),
            "text": text.strip(),
            "created_at": mblog.get("created_at", ""),
            "reposts_count": mblog.get("reposts_count", 0),
            "comments_count": mblog.get("comments_count", 0),
            "attitudes_count": mblog.get("attitudes_count", 0),
            "fetched_at": datetime.now().isoformat(),
        }
        return post

    def get_comments(self, post_id, max_count=20):
        """Get comments for a single Weibo post"""
        comments = []
        page = 1
        while len(comments) < max_count:
            params = {"id": post_id, "page": page}
            data = self._safe_get(self.COMMENTS_URL, params=params)
            if not data:
                break

            hotflow = data.get("data", {})
            # Comments may be under data or hotflow field
            raw_comments = hotflow.get("data", hotflow.get("comments", []))
            if not raw_comments:
                break

            for c in raw_comments:
                text = c.get("text", "")
                text = re.sub(r"<[^>]+>", "", text)
                comments.append({
                    "id": str(c.get("id", "")),
                    "post_id": post_id,
                    "user_name": c.get("user", {}).get("screen_name", ""),
                    "text": text.strip(),
                    "created_at": c.get("created_at", ""),
                    "fetched_at": datetime.now().isoformat(),
                })

            page += 1
            if page > 5:  # Safety limit
                break

        return comments[:max_count]

    def save_posts(self, posts):
        """Save Weibo posts to database"""
        with sqlite3.connect(self.db_path) as conn:
            for p in posts:
                conn.execute("""
                    INSERT OR IGNORE INTO posts
                    (id, keyword, user_name, user_id, text, created_at,
                     reposts_count, comments_count, attitudes_count, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    p["id"], p["keyword"], p["user_name"], p["user_id"],
                    p["text"], p["created_at"], p["reposts_count"],
                    p["comments_count"], p["attitudes_count"], p["fetched_at"],
                ))

    def save_comments(self, comments):
        """Save comments to database"""
        with sqlite3.connect(self.db_path) as conn:
            for c in comments:
                conn.execute("""
                    INSERT OR IGNORE INTO comments
                    (id, post_id, user_name, text, created_at, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    c["id"], c["post_id"], c["user_name"],
                    c["text"], c["created_at"], c["fetched_at"],
                ))

    def run_once(self):
        """Execute a full crawl once"""
        print(f"\n{'='*50}")
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting crawl")
        print(f"{'='*50}")

        for keyword in self.config["keywords"]:
            print(f"\n[#] Keyword: {keyword}")
            all_posts = []

            for page in range(1, self.config["max_pages_per_keyword"] + 1):
                print(f"  [>] Page {page}...")
                posts = self.search_keyword(keyword, page=page)
                if not posts:
                    print(f"  [-] No more results")
                    break
                all_posts.extend(posts)
                print(f"  [+] Got {len(posts)} posts")

            # Deduplicate
            seen = set()
            unique_posts = []
            for p in all_posts:
                if p["id"] not in seen:
                    seen.add(p["id"])
                    unique_posts.append(p)

            self.save_posts(unique_posts)
            print(f"  [OK] Saved {len(unique_posts)} posts")

            # Crawl comments
            for post in unique_posts:
                if post["comments_count"] > 0:
                    max_c = self.config.get("max_comments_per_post", 20)
                    print(f"  [>] Comments: {post['user_name']} - {post['text'][:30]}...")
                    comments = self.get_comments(post["id"], max_count=max_c)
                    if comments:
                        self.save_comments(comments)
                        print(f"    [+] {len(comments)} comments")

        print(f"\n[*] Crawl completed\n")

    def export_json(self, keyword=None):
        """Export data as JSON"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if keyword:
                rows = conn.execute(
                    "SELECT * FROM posts WHERE keyword = ? ORDER BY fetched_at DESC",
                    (keyword,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM posts ORDER BY fetched_at DESC"
                ).fetchall()

            posts = []
            for row in rows:
                post = dict(row)
                comments = conn.execute(
                    "SELECT * FROM comments WHERE post_id = ?", (post["id"],)
                ).fetchall()
                post["comments"] = [dict(c) for c in comments]
                posts.append(post)

        out_path = Path(self.config["output_dir"]) / f"export_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(posts, f, ensure_ascii=False, indent=2)
        print(f"[OK] Exported {len(posts)} posts to {out_path}")
        return out_path

    def export_csv(self, keyword=None):
        """Export data as CSV (posts.csv + comments.csv)"""
        output_dir = Path(self.config["output_dir"])

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row

            # Export posts
            if keyword:
                rows = conn.execute(
                    "SELECT * FROM posts WHERE keyword = ? ORDER BY fetched_at DESC",
                    (keyword,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM posts ORDER BY fetched_at DESC"
                ).fetchall()

            posts_path = output_dir / f"posts_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
            with open(posts_path, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["id", "keyword", "user_name", "user_id", "text",
                                 "created_at", "reposts_count", "comments_count",
                                 "attitudes_count", "fetched_at"])
                for row in rows:
                    writer.writerow([row["id"], row["keyword"], row["user_name"],
                                     row["user_id"], row["text"], row["created_at"],
                                     row["reposts_count"], row["comments_count"],
                                     row["attitudes_count"], row["fetched_at"]])

            # Export comments
            post_ids = [row["id"] for row in rows]
            comments_path = output_dir / f"comments_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
            comment_count = 0
            with open(comments_path, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["id", "post_id", "user_name", "text",
                                 "created_at", "fetched_at"])
                for pid in post_ids:
                    comments = conn.execute(
                        "SELECT * FROM comments WHERE post_id = ?", (pid,)
                    ).fetchall()
                    for c in comments:
                        writer.writerow([c["id"], c["post_id"], c["user_name"],
                                         c["text"], c["created_at"], c["fetched_at"]])
                        comment_count += 1

        print(f"[OK] Exported {len(rows)} posts to {posts_path}")
        print(f"[OK] Exported {comment_count} comments to {comments_path}")
        return posts_path, comments_path


def main():
    parser = argparse.ArgumentParser(description="Weibo Keyword Monitor Tool")
    parser.add_argument("--export-csv", action="store_true", help="Export data as CSV")
    parser.add_argument("--keyword", default=None, help="Filter by keyword (use with --export-csv)")
    args = parser.parse_args()

    config = load_config()

    if not config["cookies"] or "在这里" in config["cookies"]:  # Check for Chinese placeholder
        print("=" * 50)
        print("First-time setup, please configure Cookie:")
        print()
        print("1. Open https://m.weibo.cn in your browser and log in")
        print("2. Press F12 to open Developer Tools -> Network tab")
        print("3. Refresh the page, click any request")
        print("4. Find Cookie in the Headers, copy the full value")
        print("5. Paste into the cookies field in config.json")
        print("=" * 50)
        return

    monitor = WeiboMonitor(config)

    if args.export_csv:
        monitor.export_csv(keyword=args.keyword)
    else:
        monitor.run_once()


if __name__ == "__main__":
    main()
