import base64
import hashlib
import json
import logging
import random
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

import requests

from core.rate_limiter import RateLimiter, CircuitBreakerError
from db.schema import get_connection

log = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
]

# 响应头随机排列顺序（模拟浏览器波动）
ACCEPT_LANGUAGES = [
    "zh-CN,zh;q=0.9,en;q=0.8",
    "zh-CN,zh;q=0.9",
    "zh-CN,zh;q=0.8,en-US;q=0.7,en;q=0.6",
    "en,zh-CN;q=0.9,zh;q=0.8",
]


@dataclass
class CrawlResult:
    new_posts: list[dict] = field(default_factory=list)
    new_comments: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    posts_scanned: int = 0


def _get_machine_key() -> bytes:
    for path in ["/etc/machine-id", "/var/lib/dbus/machine-id"]:
        try:
            mid = Path(path).read_text().strip()
            if mid:
                return hashlib.sha256(f"monitor:{mid}".encode()).digest()[:32]
        except (FileNotFoundError, PermissionError):
            continue
    import getpass
    user = getpass.getuser()
    home = str(Path.home())
    return hashlib.sha256(f"monitor:{user}:{home}".encode()).digest()[:32]


def encrypt_cookie(plaintext: str) -> str:
    key = _get_machine_key()
    data = plaintext.encode("utf-8")
    key_repeated = (key * (len(data) // len(key) + 1))[:len(data)]
    encrypted = bytes(a ^ b for a, b in zip(data, key_repeated))
    return base64.b64encode(encrypted).decode("ascii")


def decrypt_cookie(ciphertext: str) -> str:
    key = _get_machine_key()
    data = base64.b64decode(ciphertext)
    key_repeated = (key * (len(data) // len(key) + 1))[:len(data)]
    return bytes(a ^ b for a, b in zip(data, key_repeated)).decode("utf-8")


class BaseMonitor(ABC):
    PLATFORM_NAME: str = ""
    REQUIRED_COOKIES: list[str] = []

    def __init__(self, platform_config: dict, db_path: str = None):
        self.config = platform_config
        self.db_path = db_path
        self.rate_limiter = RateLimiter(platform_config)
        self.session = requests.Session()
        self._request_count = 0
        self._session_rebuild_interval = 20
        self._configure_session()

    def _configure_session(self):
        self.session.headers.update({
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "application/json, text/html, text/plain, */*",
            "Accept-Language": random.choice(ACCEPT_LANGUAGES),
            "Accept-Encoding": "gzip, deflate, br",
        })
        self._load_cookies()

    def _rebuild_session(self):
        self.session.close()
        self.session = requests.Session()
        self._configure_session()
        log.debug("[%s] Session 已重建", self.PLATFORM_NAME)

    def _load_cookies(self):
        conn = get_connection(self.db_path)
        try:
            row = conn.execute(
                "SELECT cookies, auth_status FROM platform_auth WHERE platform = ?",
                (self.PLATFORM_NAME,),
            ).fetchone()
            if row and row["cookies"] and row["auth_status"] == "active":
                try:
                    cookie_str = decrypt_cookie(row["cookies"])
                except Exception:
                    cookie_str = row["cookies"]
                for item in cookie_str.split(";"):
                    item = item.strip()
                    if "=" in item:
                        k, v = item.split("=", 1)
                        self.session.cookies.set(k.strip(), v.strip())
        finally:
            conn.close()

    def _save_cookies(self, cookie_str: str):
        conn = get_connection(self.db_path)
        try:
            encrypted = encrypt_cookie(cookie_str)
            conn.execute(
                """INSERT INTO platform_auth (platform, cookies, auth_status, last_validated)
                   VALUES (?, ?, 'active', datetime('now'))
                   ON CONFLICT(platform) DO UPDATE SET
                       cookies = excluded.cookies,
                       auth_status = excluded.auth_status,
                       last_validated = excluded.last_validated""",
                (self.PLATFORM_NAME, encrypted),
            )
            conn.commit()
        finally:
            conn.close()

    def _mark_auth_expired(self):
        conn = get_connection(self.db_path)
        try:
            conn.execute(
                "UPDATE platform_auth SET auth_status = 'expired' WHERE platform = ?",
                (self.PLATFORM_NAME,),
            )
            conn.commit()
        finally:
            conn.close()

    def _safe_request(self, url, params=None, retries=2, method="GET", **kwargs) -> dict | None:
        for attempt in range(retries):
            try:
                self._request_count += 1
                if self._request_count % self._session_rebuild_interval == 0:
                    self._rebuild_session()

                # 每次请求随机化部分 headers
                self.session.headers["User-Agent"] = random.choice(USER_AGENTS)
                self.session.headers["Accept-Language"] = random.choice(ACCEPT_LANGUAGES)

                self.rate_limiter.wait()

                resp = self.session.request(method, url, params=params, timeout=15, **kwargs)
                resp.raise_for_status()

                # 检测登录失效
                if resp.status_code == 403:
                    log.error("[%s] 被限流(403)，停止请求", self.PLATFORM_NAME)
                    self._mark_auth_expired()
                    self.rate_limiter.record_failure()
                    return None

                if "登录" in resp.text and "密码" in resp.text:
                    log.error("[%s] Cookie 已失效", self.PLATFORM_NAME)
                    self._mark_auth_expired()
                    self.rate_limiter.record_failure()
                    return None

                self.rate_limiter.record_success()

                content_type = resp.headers.get("Content-Type", "")
                if "json" in content_type:
                    return resp.json()
                return {"text": resp.text, "status_code": resp.status_code}

            except CircuitBreakerError:
                raise
            except requests.exceptions.RequestException as e:
                log.warning("[%s] 请求失败 (%d/%d): %s", self.PLATFORM_NAME, attempt + 1, retries, e)
                self.rate_limiter.record_failure()
                if attempt < retries - 1:
                    time.sleep(10 * (attempt + 1))
        return None

    @staticmethod
    def _clean_html(text: str) -> str:
        return re.sub(r"<[^>]+>", "", text).strip()

    def save_posts(self, posts: list[dict]):
        if not posts:
            return
        conn = get_connection(self.db_path)
        try:
            for p in posts:
                conn.execute(
                    """INSERT OR IGNORE INTO posts
                       (id, platform, keyword, user_name, user_id, title, content, url,
                        created_at, fetched_at, reposts_count, comments_count,
                        likes_count, shares_count, extra)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        p["id"],
                        self.PLATFORM_NAME,
                        p.get("keyword", ""),
                        p.get("user_name", ""),
                        p.get("user_id", ""),
                        p.get("title", ""),
                        p.get("content", ""),
                        p.get("url", ""),
                        p.get("created_at", ""),
                        p.get("fetched_at", ""),
                        p.get("reposts_count", 0),
                        p.get("comments_count", 0),
                        p.get("likes_count", 0),
                        p.get("shares_count", 0),
                        json.dumps(p.get("extra", {}), ensure_ascii=False) if isinstance(p.get("extra"), dict) else p.get("extra"),
                    ),
                )
            conn.commit()
            log.info("[%s] 保存 %d 条帖子", self.PLATFORM_NAME, len(posts))
        finally:
            conn.close()

    def save_comments(self, comments: list[dict]):
        if not comments:
            return
        conn = get_connection(self.db_path)
        try:
            for c in comments:
                conn.execute(
                    """INSERT OR IGNORE INTO comments
                       (id, post_id, platform, user_name, content, created_at, fetched_at, extra)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        c["id"],
                        c["post_id"],
                        self.PLATFORM_NAME,
                        c.get("user_name", ""),
                        c.get("content", ""),
                        c.get("created_at", ""),
                        c.get("fetched_at", ""),
                        json.dumps(c.get("extra", {}), ensure_ascii=False) if isinstance(c.get("extra"), dict) else c.get("extra"),
                    ),
                )
            conn.commit()
            log.info("[%s] 保存 %d 条评论", self.PLATFORM_NAME, len(comments))
        finally:
            conn.close()

    @abstractmethod
    def crawl(self, keyword: str, max_pages: int = 3) -> CrawlResult:
        pass

    @abstractmethod
    def verify_auth(self) -> bool:
        pass

    @abstractmethod
    def get_comments(self, post_id: str, max_count: int = 20) -> list[dict]:
        pass

    def get_login_qrcode(self) -> dict:
        return {"error": f"{self.PLATFORM_NAME} 暂不支持扫码登录"}

    def check_login_status(self, uuid: str) -> dict:
        return {"status": "unsupported"}

    def get_auth_status(self) -> dict:
        conn = get_connection(self.db_path)
        try:
            row = conn.execute(
                "SELECT auth_status, last_validated FROM platform_auth WHERE platform = ?",
                (self.PLATFORM_NAME,),
            ).fetchone()
            if row:
                return {"status": row["auth_status"], "last_validated": row["last_validated"]}
            return {"status": "inactive", "last_validated": None}
        finally:
            conn.close()
