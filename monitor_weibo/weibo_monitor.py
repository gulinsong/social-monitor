#!/usr/bin/env python3
"""
微博关键词监控 - 安全爬取框架
使用移动端接口 + Cookie登录态，低频率采集
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
    # 移动端搜索接口，反爬较弱
    SEARCH_URL = "https://m.weibo.cn/api/container/getIndex"
    # 移动端微博详情接口
    DETAIL_URL = "https://m.weibo.cn/detail/{id}"
    # 移动端评论接口
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

        # 解析cookie字符串为独立cookie
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
        """随机延时，模拟人类操作"""
        delay = random.uniform(
            self.config["request_delay_min"],
            self.config["request_delay_max"],
        )
        time.sleep(delay)

    def _safe_get(self, url, params=None, retries=2):
        """安全请求，失败自动停止避免封号"""
        for attempt in range(retries):
            try:
                self._delay()
                resp = self.session.get(url, params=params, timeout=15)
                resp.raise_for_status()

                # 检测是否被限流或需要登录
                if "登录" in resp.text and "密码" in resp.text:
                    print("[!] Cookie已失效，请更新config.json中的cookies")
                    return None
                if resp.status_code == 403:
                    print("[!] 被限流(403)，停止请求")
                    return None

                return resp.json() if "json" in resp.headers.get("Content-Type", "") else resp
            except requests.exceptions.RequestException as e:
                print(f"[!] 请求失败 (尝试 {attempt+1}/{retries}): {e}")
                if attempt < retries - 1:
                    time.sleep(10 * (attempt + 1))  # 递增等待
        return None

    def search_keyword(self, keyword, page=1):
        """搜索关键词获取微博列表"""
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
            # 搜索结果可能嵌套在 card_group 中
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
        """解析单条微博"""
        # 清理HTML标签
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
        """获取单条微博的评论"""
        comments = []
        page = 1
        while len(comments) < max_count:
            params = {"id": post_id, "page": page}
            data = self._safe_get(self.COMMENTS_URL, params=params)
            if not data:
                break

            hotflow = data.get("data", {})
            # 评论可能在 data 或 hotflow 字段下
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
            if page > 5:  # 安全上限
                break

        return comments[:max_count]

    def save_posts(self, posts):
        """存储微博到数据库"""
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
        """存储评论到数据库"""
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
        """执行一次完整的爬取"""
        print(f"\n{'='*50}")
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 开始爬取")
        print(f"{'='*50}")

        for keyword in self.config["keywords"]:
            print(f"\n[#] 关键词: {keyword}")
            all_posts = []

            for page in range(1, self.config["max_pages_per_keyword"] + 1):
                print(f"  [>] 第 {page} 页...")
                posts = self.search_keyword(keyword, page=page)
                if not posts:
                    print(f"  [-] 无更多结果")
                    break
                all_posts.extend(posts)
                print(f"  [+] 获取 {len(posts)} 条微博")

            # 去重
            seen = set()
            unique_posts = []
            for p in all_posts:
                if p["id"] not in seen:
                    seen.add(p["id"])
                    unique_posts.append(p)

            self.save_posts(unique_posts)
            print(f"  [✓] 存储 {len(unique_posts)} 条微博")

            # 爬取评论
            for post in unique_posts:
                if post["comments_count"] > 0:
                    max_c = self.config.get("max_comments_per_post", 20)
                    print(f"  [>] 评论: {post['user_name']} - {post['text'][:30]}...")
                    comments = self.get_comments(post["id"], max_count=max_c)
                    if comments:
                        self.save_comments(comments)
                        print(f"    [+] {len(comments)} 条评论")

        print(f"\n[*] 本次爬取完成\n")

    def export_json(self, keyword=None):
        """导出数据为JSON"""
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
        print(f"[✓] 导出 {len(posts)} 条到 {out_path}")
        return out_path

    def export_csv(self, keyword=None):
        """导出数据为CSV（posts.csv + comments.csv）"""
        output_dir = Path(self.config["output_dir"])

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row

            # 导出帖子
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

            # 导出评论
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

        print(f"[✓] 导出 {len(rows)} 条帖子到 {posts_path}")
        print(f"[✓] 导出 {comment_count} 条评论到 {comments_path}")
        return posts_path, comments_path


def main():
    parser = argparse.ArgumentParser(description="微博关键词监控工具")
    parser.add_argument("--export-csv", action="store_true", help="导出数据为CSV")
    parser.add_argument("--keyword", default=None, help="按关键词过滤（配合--export-csv使用）")
    args = parser.parse_args()

    config = load_config()

    if not config["cookies"] or "在这里" in config["cookies"]:
        print("=" * 50)
        print("首次使用，请配置Cookie:")
        print()
        print("1. 浏览器打开 https://m.weibo.cn 并登录")
        print("2. F12打开开发者工具 -> Network(网络)")
        print("3. 刷新页面，点击任意请求")
        print("4. 在Headers中找到Cookie，复制完整值")
        print("5. 粘贴到 config.json 的 cookies 字段")
        print("=" * 50)
        return

    monitor = WeiboMonitor(config)

    if args.export_csv:
        monitor.export_csv(keyword=args.keyword)
    else:
        monitor.run_once()


if __name__ == "__main__":
    main()
