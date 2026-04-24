"""
微信公众号监控 — 改造自 legacy/monitor_wechat/wechat_monitor.py
双数据源：搜狗微信搜索 + 微信读书 (WeRead)
"""

import hashlib
import json
import logging
import random
import re
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from core.base_monitor import BaseMonitor, CrawlResult
from db.schema import get_connection

log = logging.getLogger(__name__)

BASE_URL = "https://weixin.sogou.com/weixin"

USER_AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]


class Monitor(BaseMonitor):
    PLATFORM_NAME = "wechat"

    def crawl(self, keyword: str, max_pages: int = 5) -> CrawlResult:
        result = CrawlResult()
        sogou_articles = []
        weread_articles = []

        # 搜狗源
        if self.config.get("sogou", {}).get("enabled", True):
            sogou_articles = self._crawl_sogou(keyword, max_pages)

        # 微信读书源
        if self.config.get("weread", {}).get("enabled", True):
            weread_articles = self._crawl_weread(keyword)

        # 合并去重
        all_articles = []
        seen_keys = set()

        for a in sogou_articles:
            a["source"] = "sogou"
            key = self._dedupe_key(a.get("url", ""))
            if key not in seen_keys:
                seen_keys.add(key)
                all_articles.append(a)

        for a in weread_articles:
            a["source"] = "weread"
            key = self._dedupe_key(a.get("url", ""))
            if key not in seen_keys:
                seen_keys.add(key)
                all_articles.append(a)

        # 转为统一格式
        for article in all_articles:
            result.new_posts.append({
                "id": self._dedupe_key(article.get("url", "")),
                "keyword": keyword,
                "user_name": article.get("account", ""),
                "user_id": "",
                "title": article.get("title", ""),
                "content": article.get("digest", ""),
                "url": article.get("url", ""),
                "created_at": article.get("pub_time", ""),
                "fetched_at": datetime.now().isoformat(),
                "extra": {
                    "account": article.get("account", ""),
                    "source": article.get("source", "unknown"),
                    "digest": article.get("digest", ""),
                },
            })

        result.posts_scanned = len(all_articles)

        # 检查哪些是新的（不在数据库中）
        conn = get_connection(self.db_path)
        try:
            existing_ids = {row[0] for row in conn.execute("SELECT id FROM posts WHERE platform='wechat'").fetchall()}
        finally:
            conn.close()

        result.new_posts = [p for p in result.new_posts if p["id"] not in existing_ids]
        return result

    def _crawl_sogou(self, keyword: str, max_pages: int) -> list[dict]:
        session = requests.Session()
        session.headers.update({
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://weixin.sogou.com/",
        })
        session.cookies.set(
            "SUID",
            hashlib.md5(str(time.time()).encode()).hexdigest()[:16],
            domain=".sogou.com",
        )

        all_results = []
        delay_cfg = self.config.get("sogou", {}).get("request_delay", {"min": 3.0, "max": 6.0})

        for page in range(1, max_pages + 1):
            params = {"type": "2", "query": keyword, "page": str(page), "ie": "utf8"}
            try:
                self.rate_limiter.wait()
                resp = session.get(BASE_URL, params=params, timeout=15)
                resp.raise_for_status()
                resp.encoding = "utf-8"
            except requests.RequestException as e:
                log.error("[搜狗] 请求失败: %s", e)
                break

            soup = BeautifulSoup(resp.text, "lxml")
            results = self._parse_sogou_page(soup, page)
            all_results.extend(results)

            if page < max_pages:
                time.sleep(random.uniform(delay_cfg.get("min", 3.0), delay_cfg.get("max", 6.0)))

        log.info("[搜狗] 关键词'%s'获取 %d 条", keyword, len(all_results))
        return all_results

    def _parse_sogou_page(self, soup: BeautifulSoup, page: int) -> list[dict]:
        results = []
        for box in soup.select("div.news-box, ul.news-list li, div.news-list li"):
            title_tag = box.select_one("h3 a, div.txt-box h3 a")
            if not title_tag:
                continue

            title = title_tag.get_text(strip=True)
            url = title_tag.get("href", "")

            digest_tag = box.select_one("p.txt-info, div.txt-info")
            digest = digest_tag.get_text(strip=True) if digest_tag else ""

            account_tag = box.select_one("span.all-time-y2, a.account, span.s2 a, div.s-p a")
            account = account_tag.get_text(strip=True) if account_tag else ""

            pub_time = ""
            script_tag = box.select_one("div.s-p script")
            if script_tag and script_tag.string:
                m = re.search(r"timeConvert\('(\d+)'\)", script_tag.string)
                if m:
                    pub_time = datetime.fromtimestamp(int(m.group(1))).strftime("%Y-%m-%d %H:%M")

            if not pub_time:
                for sel in ["span.s2", "span.time", "div.s-p span"]:
                    tag = box.select_one(sel)
                    if tag and tag.get_text(strip=True):
                        pub_time = tag.get_text(strip=True)
                        break

            if title and url:
                results.append({
                    "title": title,
                    "url": url,
                    "digest": digest[:200],
                    "account": account,
                    "pub_time": pub_time,
                })

        log.info("[搜狗] 第%d页解析 %d 条", page, len(results))
        return results

    def _crawl_weread(self, keyword: str) -> list[dict]:
        try:
            from platforms.wechat.weread_client import WeReadClient
        except ImportError:
            log.warning("[WeRead] 模块导入失败，跳过")
            return []

        client = WeReadClient(self.db_path)
        account = client.load_account()
        if not account or not account.get("token"):
            log.info("[WeRead] 未登录，跳过")
            return []

        try:
            articles = client.fetch_all_subscribed(keyword=keyword)
        except Exception as e:
            log.error("[WeRead] 获取失败: %s", e)
            return []

        log.info("[WeRead] 获取 %d 篇文章", len(articles))
        return articles

    @staticmethod
    def _dedupe_key(url: str) -> str:
        if "mp.weixin.qq.com" in url:
            parsed = urllib.parse.urlparse(url)
            params = urllib.parse.parse_qs(parsed.query)
            sn = params.get("sn", [None])[0]
            if sn:
                return sn
        return hashlib.md5(url.encode()).hexdigest()[:12]

    def verify_auth(self) -> bool:
        # 搜狗无需认证，始终可用
        return True

    def get_comments(self, post_id: str, max_count: int = 20) -> list[dict]:
        # 微信公众号文章评论无法通过此方式获取
        return []

    def get_login_qrcode(self) -> dict:
        try:
            from platforms.wechat.weread_client import WeReadClient
            client = WeReadClient(self.db_path)
            return client.get_login_qrcode()
        except Exception:
            return {
                "qr_url": "https://weread.qq.com",
                "uuid": "",
                "message": "WeRead 登录不可用",
            }

    def check_login_status(self, uuid: str) -> dict:
        try:
            from platforms.wechat.weread_client import WeReadClient
            client = WeReadClient(self.db_path)
            return client.check_login_status(uuid)
        except Exception:
            return {"status": "error"}
