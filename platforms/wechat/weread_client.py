"""
微信读书 (WeRead) API 客户端 — 改造自 legacy
通过 wewe-rss 代理获取公众号文章
"""

import base64
import hashlib
import json
import logging
import os
import random
import stat
import time
from datetime import datetime
from pathlib import Path

import requests

log = logging.getLogger(__name__)

PROXY_URL = "https://weread.111965.xyz"

USER_AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]


def _get_machine_key() -> bytes:
    for path in ["/etc/machine-id", "/var/lib/dbus/machine-id"]:
        try:
            mid = Path(path).read_text().strip()
            if mid:
                return hashlib.sha256(f"weread:{mid}".encode()).digest()[:32]
        except (FileNotFoundError, PermissionError):
            continue
    import getpass
    return hashlib.sha256(f"weread:{getpass.getuser()}:{Path.home()}".encode()).digest()[:32]


def _xor_crypt(data: bytes, key: bytes) -> bytes:
    key_ext = (key * (len(data) // len(key) + 1))[:len(data)]
    return bytes(a ^ b for a, b in zip(data, key_ext))


class WeReadClient:
    def __init__(self, db_path: str = None):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        self.db_path = db_path
        self.account = self.load_account()

    def _data_dir(self) -> Path:
        return Path(self.db_path).parent if self.db_path else Path("db")

    def _token_file(self) -> Path:
        return self._data_dir() / ".weread_token"

    def _account_file(self) -> Path:
        return self._data_dir() / ".weread_account.json"

    def _subs_file(self) -> Path:
        return self._data_dir() / "mp_subscriptions.json"

    def save_token(self, token: str):
        key = _get_machine_key()
        encrypted = _xor_crypt(token.encode("utf-8"), key)
        path = self._token_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(base64.b64encode(encrypted).decode("ascii"))
        os.chmod(str(path), stat.S_IRUSR | stat.S_IWUSR)

    def load_token(self) -> str | None:
        path = self._token_file()
        if not path.exists():
            return None
        try:
            encrypted = base64.b64decode(path.read_text().strip())
            return _xor_crypt(encrypted, _get_machine_key()).decode("utf-8")
        except Exception:
            return None

    def save_account(self, account_id: str, token: str):
        self.save_token(token)
        data = {"id": account_id, "saved_at": datetime.now().isoformat()}
        path = self._account_file()
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.chmod(str(path), stat.S_IRUSR | stat.S_IWUSR)

    def load_account(self) -> dict | None:
        token = self.load_token()
        if not token:
            return None
        try:
            data = json.loads(self._account_file().read_text(encoding="utf-8"))
            data["token"] = token
            return data
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def load_mp_subscriptions(self) -> list[dict]:
        try:
            return json.loads(self._subs_file().read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def save_mp_subscriptions(self, subs: list[dict]):
        self._subs_file().write_text(
            json.dumps(subs, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _request(self, method: str, path: str, **kwargs) -> dict | list | None:
        url = f"{PROXY_URL}{path}"
        kwargs.setdefault("timeout", (5, 15))
        if self.account:
            headers = kwargs.pop("headers", {})
            headers["Authorization"] = f"Bearer {self.account['token']}"
            headers["xid"] = self.account["id"]
            kwargs["headers"] = headers
        try:
            resp = self.session.request(method, url, **kwargs)
            if resp.status_code == 401:
                log.warning("[WeRead] Token 过期")
                self.account = None
                return None
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            log.error("[WeRead] 请求失败: %s", e)
            return None

    def _request_no_auth(self, method: str, path: str, **kwargs) -> dict | None:
        url = f"{PROXY_URL}{path}"
        kwargs.setdefault("timeout", (5, 120))
        try:
            resp = self.session.request(method, url, **kwargs)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            log.error("[WeRead] 请求失败: %s", e)
            return None

    def get_mp_articles(self, mp_id: str, page: int = 1) -> list[dict]:
        result = self._request("GET", f"/api/v2/platform/mps/{mp_id}/articles", params={"page": page})
        if not isinstance(result, list):
            return []
        articles = []
        for item in result:
            pub_time = ""
            pt = item.get("publishTime", 0)
            if pt:
                try:
                    pub_time = datetime.fromtimestamp(pt).strftime("%Y-%m-%d %H:%M")
                except (OSError, ValueError):
                    pass
            articles.append({
                "title": item.get("title", ""),
                "url": f"https://mp.weixin.qq.com/s/{item.get('id', '')}",
                "digest": "",
                "account": "",
                "pub_time": pub_time,
            })
        return articles

    def fetch_all_subscribed(self, keyword: str = "") -> list[dict]:
        if not self.account:
            return []
        subs = self.load_mp_subscriptions()
        if not subs:
            return []
        all_articles = []
        for sub in subs:
            mp_id = sub.get("mpId", "")
            mp_name = sub.get("name", "")
            if not mp_id:
                continue
            articles = self.get_mp_articles(mp_id, page=1)
            for a in articles:
                a["account"] = mp_name
            if keyword:
                articles = [a for a in articles if keyword in a.get("title", "")]
            all_articles.extend(articles)
            time.sleep(random.uniform(2, 4))
        return all_articles

    def get_login_qrcode(self) -> dict:
        result = self._request_no_auth("GET", "/api/v2/login/platform")
        if result:
            return {
                "qr_url": result.get("scanUrl", ""),
                "uuid": result.get("uuid", ""),
            }
        return {"qr_url": PROXY_URL, "uuid": "", "message": "获取二维码失败"}

    def check_login_status(self, uuid: str) -> dict:
        if not uuid:
            return {"status": "error"}
        result = self._request_no_auth("GET", f"/api/v2/login/platform/{uuid}")
        if not result:
            return {"status": "waiting"}
        message = result.get("message", "")
        if message == "OK" or result.get("token"):
            vid = str(result.get("vid", ""))
            token = result.get("token", "")
            if token:
                self.save_account(vid, token)
                self.account = {"id": vid, "token": token}
                return {"status": "success", "vid": vid}
        return {"status": "waiting", "message": message}
