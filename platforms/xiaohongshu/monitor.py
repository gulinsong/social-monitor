"""
XHS 关键词监控 — Playwright 浏览器爬取

策略：Web API 需要 x-s/x-t 签名，
通过 Playwright 打开搜索页，拦截 edith API 响应来获取数据。
"""

import asyncio
import base64
import io
import json
import logging
import re
from datetime import datetime
from urllib.parse import quote

from core.base_monitor import BaseMonitor, CrawlResult
from db.schema import get_connection

log = logging.getLogger(__name__)


class Monitor(BaseMonitor):
    PLATFORM_NAME = "xiaohongshu"

    def _configure_session(self):
        super()._configure_session()
        # Playwright 模式下 session 不直接用于 API 请求
        self.session.headers.update({
            "Referer": "https://www.xiaohongshu.com/",
            "Origin": "https://www.xiaohongshu.com",
        })

    def _get_cookies_for_playwright(self) -> dict:
        """从 platform_auth 获取 Cookie 并解析为 Playwright 格式"""
        conn = self._get_auth_conn()
        try:
            row = conn.execute(
                "SELECT cookies FROM platform_auth WHERE platform=?",
                (self.PLATFORM_NAME,),
            ).fetchone()
            if not row or not row["cookies"]:
                return {}
            from core.base_monitor import decrypt_cookie
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

    def verify_auth(self) -> bool:
        cookies = self._get_cookies_for_playwright()
        if not cookies.get("web_session") or not cookies.get("a1"):
            return False
        # API 需要 x-s 签名，纯 HTTP 无法验证
        # 有 web_session + a1 即认为有效
        conn = self._get_auth_conn()
        conn.execute(
            "UPDATE platform_auth SET auth_status='active', "
            "last_validated=datetime('now','localtime') WHERE platform=?",
            (self.PLATFORM_NAME,),
        )
        conn.commit()
        conn.close()
        log.info("[XHS] Cookie 有效")
        return True

    def crawl(self, keyword: str, max_pages: int = 1) -> CrawlResult:
        result = CrawlResult()
        try:
            all_notes = self._run_async(
                self._crawl_with_playwright(keyword, max_pages)
            )
            result.posts_scanned = len(all_notes)
            result.new_posts = all_notes
        except Exception as e:
            log.error("[XHS] 爬取失败: %s", e)
        return result

    def _run_async(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    async def _crawl_with_playwright(self, keyword: str, max_pages: int) -> list[dict]:
        import random
        from playwright.async_api import async_playwright
        cookies_dict = self._get_cookies_for_playwright()
        if not cookies_dict.get("web_session"):
            log.warning("[XHS] 未登录，跳过爬取")
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

            # 注入 Cookie
            cookie_list = []
            for name, value in cookies_dict.items():
                cookie_list.append({
                    "name": name, "value": value,
                    "domain": ".xiaohongshu.com", "path": "/",
                })
            await context.add_cookies(cookie_list)

            page = await context.new_page()

            # 拦截搜索 API 响应
            search_results = []
            async def on_response(resp):
                url = resp.url
                if resp.status == 200 and ("search" in url or "homefeed" in url or "feed" in url):
                    try:
                        body = await resp.text()
                        data = json.loads(body)
                        items = data.get("data", {}).get("items", [])
                        if items:
                            search_results.extend(items)
                            log.info("[XHS] 拦截到 %d 条, url=%s", len(items), url[:100])
                    except Exception:
                        pass

            page.on("response", lambda r: asyncio.ensure_future(on_response(r)))

            for page_num in range(1, max_pages + 1):
                search_url = (
                    f"https://www.xiaohongshu.com/search_result?"
                    f"keyword={quote(keyword)}&source=web_search_result_notes"
                )
                await page.goto(search_url, wait_until="domcontentloaded", timeout=15000)

                # 模拟真人：页面加载后先停留浏览
                await page.wait_for_timeout(random.randint(3000, 6000))

                # 模拟真人：随机停顿 + 不等距滚动
                for scroll in range(random.randint(3, 6)):
                    # 随机滚动距离 300~900px
                    delta = random.randint(300, 900)
                    await page.evaluate(f"window.scrollBy(0, {delta})")
                    # 随机停顿 1~4 秒
                    await page.wait_for_timeout(random.randint(1000, 4000))

                # 页面间随机休息 5~12 秒
                if page_num < max_pages and search_results:
                    pause = random.randint(5000, 12000)
                    log.info("[XHS] 翻页休息 %.1f 秒", pause / 1000)
                    await page.wait_for_timeout(pause)

                if not search_results:
                    break

            # 解析结果
            notes = []
            seen_ids = set()
            for item in search_results:
                note = self._parse_note(item, keyword)
                if note and note["id"] not in seen_ids:
                    seen_ids.add(note["id"])
                    notes.append(note)

            log.info("[XHS] 关键词'%s'获取 %d 条", keyword, len(notes))
            return notes

        finally:
            if browser:
                await browser.close()
            await pw.stop()

    def _parse_note(self, item: dict, keyword: str) -> dict | None:
        note_card = item.get("note_card") or item.get("model", {}).get("noteCard")
        if not note_card:
            return None

        # note_id 在外层 item.id，不在 note_card 里
        note_id = item.get("id", "") or note_card.get("note_id", "")
        if not note_id:
            return None

        user = note_card.get("user", {})
        interact = note_card.get("interact_info", {})

        title = note_card.get("display_title", "")

        cover = ""
        cover_info = note_card.get("cover")
        if isinstance(cover_info, dict):
            cover = cover_info.get("url_default", cover_info.get("url", ""))

        # 时间从 corner_tag_info 提取
        created_at = ""
        for tag in note_card.get("corner_tag_info", []):
            if tag.get("type") == "publish_time":
                created_at = self._parse_relative_time(tag.get("text", ""))

        return {
            "id": note_id,
            "keyword": keyword,
            "user_name": user.get("nickname", ""),
            "user_id": user.get("user_id", ""),
            "title": title,
            "content": title,
            "url": f"https://www.xiaohongshu.com/explore/{note_id}",
            "created_at": created_at,
            "fetched_at": datetime.now().isoformat(),
            "reposts_count": self._parse_count(interact.get("shared_count", "0")),
            "comments_count": self._parse_count(interact.get("comment_count", "0")),
            "likes_count": self._parse_count(interact.get("liked_count", "0")),
            "shares_count": self._parse_count(interact.get("shared_count", "0")),
            "extra": {
                "cover": cover,
                "type": note_card.get("type", ""),
            },
        }

    @staticmethod
    def _parse_time(raw) -> str:
        if not raw:
            return ""
        try:
            ts = int(raw)
            if ts > 1e12:
                ts = ts // 1000
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError, OSError):
            return str(raw)

    @staticmethod
    def _parse_relative_time(text: str) -> str:
        """解析'5小时前'、'3天前'等相对时间"""
        if not text:
            return ""
        import re
        from datetime import timedelta
        now = datetime.now()
        m = re.match(r"(\d+)\s*秒前", text)
        if m:
            return (now - timedelta(seconds=int(m.group(1)))).strftime("%Y-%m-%d %H:%M:%S")
        m = re.match(r"(\d+)\s*分钟前", text)
        if m:
            return (now - timedelta(minutes=int(m.group(1)))).strftime("%Y-%m-%d %H:%M:%S")
        m = re.match(r"(\d+)\s*小时前", text)
        if m:
            return (now - timedelta(hours=int(m.group(1)))).strftime("%Y-%m-%d %H:%M:%S")
        m = re.match(r"(\d+)\s*天前", text)
        if m:
            return (now - timedelta(days=int(m.group(1)))).strftime("%Y-%m-%d %H:%M:%S")
        m = re.match(r"(\d+)\s*周前", text)
        if m:
            return (now - timedelta(weeks=int(m.group(1)))).strftime("%Y-%m-%d %H:%M:%S")
        if "昨天" in text:
            return (now - timedelta(days=1)).strftime("%Y-%m-%d")
        if "前天" in text:
            return (now - timedelta(days=2)).strftime("%Y-%m-%d")
        return text

    @staticmethod
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

    def get_comments(self, post_id: str, max_count: int = 20) -> list[dict]:
        # 评论需要 x-s 签名，暂通过 Playwright 获取
        try:
            return self._run_async(
                self._get_comments_playwright(post_id, max_count)
            )
        except Exception as e:
            log.error("[XHS] 获取评论失败: %s", e)
            return []

    async def _get_comments_playwright(self, post_id: str, max_count: int) -> list[dict]:
        from playwright.async_api import async_playwright
        cookies_dict = self._get_cookies_for_playwright()
        if not cookies_dict.get("web_session"):
            return []

        pw = await async_playwright().start()
        browser = None
        try:
            browser = await pw.chromium.launch(
                executable_path="/usr/bin/google-chrome",
                headless=True, args=["--no-sandbox", "--disable-gpu"],
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800},
            )
            cookie_list = [
                {"name": k, "value": v, "domain": ".xiaohongshu.com", "path": "/"}
                for k, v in cookies_dict.items()
            ]
            await context.add_cookies(cookie_list)

            page = await context.new_page()
            comments_data = []

            async def on_response(resp):
                if "comment/page" in resp.url and resp.status == 200:
                    try:
                        body = await resp.text()
                        data = json.loads(body)
                        comments_data.extend(data.get("data", {}).get("comments", []))
                    except Exception:
                        pass

            page.on("response", lambda r: asyncio.ensure_future(on_response(r)))
            await page.goto(
                f"https://www.xiaohongshu.com/explore/{post_id}",
                wait_until="domcontentloaded", timeout=15000,
            )
            await page.wait_for_timeout(3000)

            # 滚动触发评论加载
            for _ in range(3):
                await page.evaluate("window.scrollBy(0, 500)")
                await page.wait_for_timeout(1000)

            result = []
            for c in comments_data[:max_count]:
                result.append({
                    "id": str(c.get("id", "")),
                    "post_id": post_id,
                    "user_name": c.get("user_info", {}).get("nickname", ""),
                    "content": c.get("content", ""),
                    "created_at": self._parse_time(c.get("create_time", "")),
                    "fetched_at": datetime.now().isoformat(),
                })
            return result

        finally:
            if browser:
                await browser.close()
            await pw.stop()
