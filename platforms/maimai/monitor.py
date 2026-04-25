"""
Maimai keyword monitoring - Playwright browser crawling

Supports two data sources:
- search: Keyword search (/sdk/search/web_get, limited results)
- colleague_circle: Colleague circle (/groundhog/gossip/v3/feed, larger data volume, supports pagination)
"""

import asyncio
import json
import logging
import random
from datetime import datetime
from urllib.parse import quote

from core.base_monitor import BaseMonitor, CrawlResult

log = logging.getLogger(__name__)


class Monitor(BaseMonitor):
    PLATFORM_NAME = "maimai"

    def _configure_session(self):
        super()._configure_session()
        self.session.headers.update({
            "Referer": "https://maimai.cn/",
            "Origin": "https://maimai.cn",
        })

    def _get_cookies_dict(self) -> dict:
        from db.schema import get_connection
        from core.base_monitor import decrypt_cookie

        conn = get_connection(self.db_path)
        try:
            row = conn.execute(
                "SELECT cookies FROM platform_auth WHERE platform=?",
                (self.PLATFORM_NAME,),
            ).fetchone()
            if not row or not row["cookies"]:
                return {}
            cookie_str = decrypt_cookie(row["cookies"])
            cookies = {}
            for part in cookie_str.split(";"):
                part = part.strip()
                if "=" in part:
                    k, v = part.split("=", 1)
                    cookies[k.strip()] = v.strip()
            return cookies
        finally:
            conn.close()

    def _get_auth_conn(self):
        from db.schema import get_connection
        return get_connection(self.db_path)

    def _get_source(self) -> str:
        return self.config.get("source", "colleague_circle")

    def verify_auth(self) -> bool:
        cookies = self._get_cookies_dict()
        if not cookies:
            return False
        has_session = "session" in cookies and "session.sig" in cookies
        has_u = "u" in cookies
        if not (has_session or has_u):
            log.warning("[Maimai] Missing critical cookies")
            return False
        conn = self._get_auth_conn()
        conn.execute(
            "UPDATE platform_auth SET auth_status='active', "
            "last_validated=datetime('now','localtime') WHERE platform=?",
            (self.PLATFORM_NAME,),
        )
        conn.commit()
        conn.close()
        log.info("[Maimai] Cookies are valid")
        return True

    def crawl(self, keyword: str, max_pages: int = 1) -> CrawlResult:
        result = CrawlResult()
        source = self._get_source()
        try:
            if source in ("colleague_circle", "both"):
                posts = self._run_async(
                    self._crawl_colleague_circle(keyword, max_pages)
                )
                result.posts_scanned += len(posts)
                result.new_posts.extend(posts)

            if source in ("search", "both") and source != "colleague_circle":
                posts = self._run_async(
                    self._crawl_search(keyword, max_pages)
                )
                result.posts_scanned += len(posts)
                result.new_posts.extend(posts)
        except Exception as e:
            log.error("[Maimai] Crawl failed: %s", e)
        return result

    def _run_async(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    # -- Colleague circle crawling --

    async def _crawl_colleague_circle(self, keyword: str, max_pages: int) -> list[dict]:
        from playwright.async_api import async_playwright

        cookies_dict = self._get_cookies_dict()
        if not cookies_dict:
            log.warning("[Maimai] Not logged in, skipping crawl")
            return []

        # Get webcid first
        webcid = self._get_webcid(cookies_dict)
        if not webcid:
            log.warning("[Maimai] Failed to get colleague circle webcid")
            return []

        pw = await async_playwright().start()
        browser = None
        try:
            browser = await pw.chromium.launch(
                executable_path="/usr/bin/google-chrome",
                headless=True,
                args=["--no-sandbox", "--disable-gpu"],
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800},
            )

            cookie_list = [
                {"name": k, "value": v, "domain": ".maimai.cn", "path": "/"}
                for k, v in cookies_dict.items()
            ]
            await context.add_cookies(cookie_list)

            page = await context.new_page()

            # Intercept colleague circle feed API
            all_items = []

            async def on_response(resp):
                url = resp.url
                if resp.status != 200:
                    return
                if "/groundhog/gossip/v3/feed" not in url:
                    return
                try:
                    body = await resp.text()
                    data = json.loads(body)
                    items = data.get("data", [])
                    if items:
                        all_items.extend(items)
                        remain = data.get("remain", 0)
                        log.info("[Maimai] Colleague circle intercepted %d items (%d remaining), url=%s",
                                 len(items), remain, url[:100])
                except Exception:
                    pass

            page.on("response", lambda r: asyncio.ensure_future(on_response(r)))

            # Navigate to colleague circle page
            gossip_url = f"https://maimai.cn/company/gossip_discuss?webcid={webcid}"
            await page.goto(gossip_url, wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(random.randint(3000, 5000))

            # Scroll to load more pages
            for page_num in range(max_pages):
                for scroll in range(random.randint(4, 8)):
                    delta = random.randint(500, 1200)
                    await page.evaluate(f"window.scrollBy(0, {delta})")
                    await page.wait_for_timeout(random.randint(1500, 3500))

                if page_num < max_pages - 1:
                    pause = random.randint(3000, 6000)
                    log.info("[Maimai] Colleague circle pausing between pages %.1f seconds", pause / 1000)
                    await page.wait_for_timeout(pause)

            # Filter by keyword and parse
            posts = []
            seen_ids = set()
            for item in all_items:
                post = self._parse_gossip(item)
                if post and post["id"] not in seen_ids:
                    seen_ids.add(post["id"])
                    # In colleague circle mode: no keyword means no filter; with keyword, apply filter
                    if not keyword or keyword.lower() in post["content"].lower() or keyword.lower() in post["title"].lower():
                        posts.append(post)

            log.info("[Maimai] Colleague circle retrieved %d items (after keyword filter)", len(posts))
            return posts

        finally:
            if browser:
                await browser.close()
            await pw.stop()

    def _get_webcid(self, cookies_dict: dict) -> str:
        import requests
        try:
            s = requests.Session()
            s.headers.update({
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://maimai.cn/",
            })
            s.cookies.update(cookies_dict)
            r = s.get("https://maimai.cn/community/api/common/get-company-circle-entry-list"
                       "?__platform=community_web", timeout=10)
            data = r.json()
            if data.get("result") == "ok" and data.get("data"):
                entry = data["data"][0]
                webcid = entry.get("webcid", "")
                name = entry.get("name", "")
                log.info("[Maimai] Colleague circle: %s (webcid=%s)", name, webcid)
                return webcid
        except Exception as e:
            log.error("[Maimai] Failed to get webcid: %s", e)
        return ""

    def _parse_gossip(self, item: dict) -> dict | None:
        if not isinstance(item, dict):
            return None

        post_id = str(item.get("id") or item.get("egid", ""))
        if not post_id:
            return None

        text = str(item.get("text", ""))
        author = item.get("author_info", {}) or {}
        if not isinstance(author, dict):
            author = {}

        created_at = item.get("publish_time", "")
        if isinstance(created_at, (int, float)):
            created_at = _parse_timestamp(created_at)
        else:
            created_at = str(item.get("time", ""))

        url = str(item.get("target", ""))
        if url.startswith("taoumaimai://"):
            egid = item.get("egid", "")
            url = f"https://maimai.cn/gossip_detail/{egid}" if egid else ""

        return {
            "id": post_id,
            "keyword": "",
            "user_name": author.get("name", ""),
            "user_id": "",
            "title": "",
            "content": text,
            "url": url,
            "created_at": created_at,
            "fetched_at": datetime.now().isoformat(),
            "reposts_count": _parse_count(item.get("spreads", 0)),
            "comments_count": _parse_count(item.get("cmts", 0)),
            "likes_count": _parse_count(item.get("likes", 0)),
            "shares_count": _parse_count(item.get("shares", 0)),
            "extra": {
                "source": "colleague_circle",
                "ip_loc": str(item.get("ip_loc", "")),
                "gossip_category": str(item.get("gossip_category", "")),
            },
        }

    # -- Search crawling --

    async def _crawl_search(self, keyword: str, max_pages: int) -> list[dict]:
        from playwright.async_api import async_playwright

        cookies_dict = self._get_cookies_dict()
        if not cookies_dict:
            return []

        pw = await async_playwright().start()
        browser = None
        try:
            browser = await pw.chromium.launch(
                executable_path="/usr/bin/google-chrome",
                headless=True,
                args=["--no-sandbox", "--disable-gpu"],
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800},
            )

            cookie_list = [
                {"name": k, "value": v, "domain": ".maimai.cn", "path": "/"}
                for k, v in cookies_dict.items()
            ]
            await context.add_cookies(cookie_list)

            page = await context.new_page()

            api_results = []

            async def on_response(resp):
                url = resp.url
                if resp.status != 200:
                    return
                if "/sdk/search/" not in url:
                    return
                try:
                    body = await resp.text()
                    data = json.loads(body)
                    items = _extract_items(data)
                    if items:
                        api_results.extend(items)
                        log.info("[Maimai] Search intercepted %d items, url=%s", len(items), url[:120])
                except Exception:
                    pass

            page.on("response", lambda r: asyncio.ensure_future(on_response(r)))

            await page.goto("https://maimai.cn/", wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(random.randint(2000, 4000))

            search_input = (
                await page.query_selector("input[type='search']")
                or await page.query_selector("input[placeholder*='搜索']")
                or await page.query_selector("input.search-input")
            )
            if search_input:
                await search_input.fill(keyword)
                await search_input.press("Enter")
                await page.wait_for_timeout(random.randint(3000, 6000))
            else:
                search_url = f"https://maimai.cn/web/search_center?type=feed&query={quote(keyword)}&highlight=true"
                await page.goto(search_url, wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_timeout(random.randint(3000, 6000))

            for scroll in range(random.randint(3, 6)):
                delta = random.randint(300, 900)
                await page.evaluate(f"window.scrollBy(0, {delta})")
                await page.wait_for_timeout(random.randint(1500, 4000))

            posts = []
            seen_ids = set()
            for item in api_results:
                post = self._parse_post(item, keyword)
                if post and post["id"] not in seen_ids:
                    seen_ids.add(post["id"])
                    posts.append(post)

            log.info("[Maimai] Search '%s' retrieved %d items", keyword, len(posts))
            return posts

        finally:
            if browser:
                await browser.close()
            await pw.stop()

    def _parse_post(self, item: dict, keyword: str) -> dict | None:
        if not isinstance(item, dict):
            return None
        feed = item.get("feed")
        if isinstance(feed, dict):
            return self._parse_feed(feed, keyword)
        return self._parse_feed(item, keyword)

    def _parse_feed(self, f: dict, keyword: str) -> dict | None:
        post_id = str(f.get("id") or f.get("fid") or f.get("feed_id") or "")
        if not post_id:
            return None

        content = str(f.get("text", "") or f.get("content", "") or f.get("body", ""))
        title = str(f.get("title", ""))

        user = f.get("user", {}) or {}
        if not isinstance(user, dict):
            user = {}
        user_name = str(user.get("name", "") or user.get("nickname", ""))
        user_id = str(f.get("uid", "") or user.get("id", ""))

        created_at = f.get("created_at", "") or f.get("time", "") or f.get("timestamp", "")
        if isinstance(created_at, (int, float)):
            created_at = _parse_timestamp(created_at)
        else:
            created_at = str(created_at)

        url = str(f.get("url", "") or f.get("link", ""))
        if not url and post_id:
            url = f"https://maimai.cn/detail/{post_id}"

        return {
            "id": post_id,
            "keyword": keyword,
            "user_name": user_name,
            "user_id": user_id,
            "title": title,
            "content": content,
            "url": url,
            "created_at": created_at,
            "fetched_at": datetime.now().isoformat(),
            "reposts_count": _parse_count(f.get("reposts_count") or f.get("share_count") or 0),
            "comments_count": _parse_count(f.get("comments_count") or f.get("comment_count") or 0),
            "likes_count": _parse_count(f.get("likes_count") or f.get("like_count") or f.get("digg_count") or 0),
            "shares_count": _parse_count(f.get("shares_count") or f.get("share_count") or 0),
            "extra": {
                "source": "search",
                "type": str(f.get("type", "")),
            },
        }

    def get_comments(self, post_id: str, max_count: int = 20) -> list[dict]:
        return []


def _extract_items(data: dict) -> list:
    if not isinstance(data, dict):
        return []
    outer_feeds = data.get("feeds")
    if isinstance(outer_feeds, dict):
        inner_feeds = outer_feeds.get("feeds")
        if isinstance(inner_feeds, list):
            return inner_feeds
    for key in ("feeds", "items", "list"):
        val = data.get(key)
        if isinstance(val, list):
            return val
    inner = data.get("data")
    if isinstance(inner, dict):
        for key in ("feeds", "items", "list"):
            val = inner.get(key)
            if isinstance(val, list):
                return val
    return []


def _parse_timestamp(ts) -> str:
    try:
        ts = int(ts)
        if ts > 1e12:
            ts = ts // 1000
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError, OSError):
        return str(ts)


def _parse_count(raw) -> int:
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        raw = raw.replace(" ", "").replace(",", "")
        if "万" in raw:
            return int(float(raw.replace("万", "")) * 10000)
        try:
            return int(raw)
        except ValueError:
            return 0
    return 0
