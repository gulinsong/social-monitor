"""
微博关键词监控 — 改造自 legacy/monitor_weibo/weibo_monitor.py
"""

import logging
import re
from datetime import datetime
from urllib.parse import quote

from core.base_monitor import BaseMonitor, CrawlResult

log = logging.getLogger(__name__)


class Monitor(BaseMonitor):
    PLATFORM_NAME = "weibo"

    SEARCH_URL = "https://m.weibo.cn/api/container/getIndex"
    COMMENTS_URL = "https://m.weibo.cn/api/comments/show"

    def _configure_session(self):
        super()._configure_session()
        self.session.headers.update({
            "Referer": "https://m.weibo.cn/",
            "X-Requested-With": "XMLHttpRequest",
        })

    def verify_auth(self) -> bool:
        try:
            resp = self._safe_request("https://m.weibo.cn/api/config")
            if not resp:
                return False
            data = resp.get("data", {})
            uid = data.get("uid", "")
            if uid:
                conn = self._get_auth_conn()
                conn.execute(
                    "UPDATE platform_auth SET auth_status='active', last_validated=datetime('now','localtime') WHERE platform=?",
                    (self.PLATFORM_NAME,),
                )
                conn.commit()
                conn.close()
                log.info("[微博] 认证有效, UID: %s****", uid[:4])
                return True
            return False
        except Exception as e:
            log.warning("[微博] 认证检查失败: %s", e)
            return False

    def _get_auth_conn(self):
        from db.schema import get_connection
        return get_connection(self.db_path)

    def crawl(self, keyword: str, max_pages: int = 3) -> CrawlResult:
        result = CrawlResult()
        all_posts = []

        for page in range(1, max_pages + 1):
            posts = self._search_keyword(keyword, page)
            if not posts:
                break
            all_posts.extend(posts)
            result.posts_scanned += len(posts)

        # 去重
        seen = set()
        unique = []
        for p in all_posts:
            if p["id"] not in seen:
                seen.add(p["id"])
                unique.append(p)

        result.new_posts = unique

        # 爬取评论
        max_comments = self.config.get("max_comments_per_post", 20)
        for post in unique:
            if post.get("comments_count", 0) > 0:
                comments = self.get_comments(post["id"], max_count=max_comments)
                if comments:
                    result.new_comments.extend(comments)

        return result

    def _search_keyword(self, keyword: str, page: int = 1) -> list[dict]:
        containerid = f"100103type=1&q={quote(keyword)}"
        params = {
            "containerid": containerid,
            "page_type": "searchall",
            "page": page,
        }
        data = self._safe_request(self.SEARCH_URL, params=params)
        if not data:
            return []

        cards = data.get("data", {}).get("cards", [])
        posts = []
        for card in cards:
            card_group = card.get("card_group", [])
            if not card_group:
                card_group = [card]
            for item in card_group:
                mblog = item.get("mblog")
                if not mblog:
                    continue
                posts.append(self._parse_post(mblog, keyword))
        return posts

    def _parse_post(self, mblog: dict, keyword: str = "") -> dict:
        text = self._clean_html(mblog.get("text", ""))
        created_at = self._parse_time(mblog.get("created_at", ""))
        return {
            "id": mblog.get("mid", mblog.get("id", "")),
            "keyword": keyword,
            "user_name": mblog.get("user", {}).get("screen_name", ""),
            "user_id": str(mblog.get("user", {}).get("id", "")),
            "title": "",
            "content": text,
            "url": f"https://m.weibo.cn/detail/{mblog.get('mid', mblog.get('id', ''))}",
            "created_at": created_at,
            "fetched_at": datetime.now().isoformat(),
            "reposts_count": mblog.get("reposts_count", 0),
            "comments_count": mblog.get("comments_count", 0),
            "likes_count": mblog.get("attitudes_count", 0),
            "shares_count": 0,
            "extra": {
                "is_original": not mblog.get("retweeted_status"),
            },
        }

    @staticmethod
    def _parse_time(raw: str) -> str:
        """将微博时间格式转为 ISO 格式"""
        if not raw:
            return ""
        try:
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(raw)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return raw

    def get_comments(self, post_id: str, max_count: int = 20) -> list[dict]:
        comments = []
        page = 1
        while len(comments) < max_count:
            params = {"id": post_id, "page": page}
            data = self._safe_request(self.COMMENTS_URL, params=params)
            if not data:
                break

            hotflow = data.get("data", {})
            raw = hotflow.get("data", hotflow.get("comments", []))
            if not raw:
                break

            for c in raw:
                text = self._clean_html(c.get("text", ""))
                comments.append({
                    "id": str(c.get("id", "")),
                    "post_id": post_id,
                    "user_name": c.get("user", {}).get("screen_name", ""),
                    "content": text,
                    "created_at": self._parse_time(c.get("created_at", "")),
                    "fetched_at": datetime.now().isoformat(),
                })

            page += 1
            if page > 5:
                break

        return comments[:max_count]

    def get_login_qrcode(self) -> dict:
        return {
            "qr_url": "https://passport.weibo.cn/signin/login",
            "uuid": "manual",
            "message": "请用微博 APP 扫码登录，或手动输入 Cookie",
        }

    def check_login_status(self, uuid: str) -> dict:
        return {"status": "manual_required", "message": "微博需手动提供 Cookie"}
