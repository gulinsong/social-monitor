#!/usr/bin/env python3
"""
WeRead API Client - Fetch official account articles via wewe-rss proxy
Used for real-time monitoring of WeChat official account updates
"""

import json
import time
import random
import hashlib
import base64
import os
import stat
import logging
import re
from pathlib import Path
from datetime import datetime

import requests

# -- Configuration ───────────────────────────────────────────
PROXY_URL = "https://weread.111965.xyz"
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
TOKEN_FILE = DATA_DIR / ".weread_token"
ACCOUNT_FILE = DATA_DIR / ".weread_account.json"
MP_SUBS_FILE = DATA_DIR / "mp_subscriptions.json"

log = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]


# -- Credential Encryption Storage ────────────────────────────────

def _get_machine_key() -> bytes:
    """Generate encryption key from machine fingerprint"""
    # Try reading machine-id
    for path in ["/etc/machine-id", "/var/lib/dbus/machine-id"]:
        try:
            mid = Path(path).read_text().strip()
            if mid:
                return hashlib.sha256(f"weread:{mid}".encode()).digest()[:32]
        except (FileNotFoundError, PermissionError):
            continue
    # Fallback: use username + home directory
    import getpass
    user = getpass.getuser()
    home = str(Path.home())
    return hashlib.sha256(f"weread:{user}:{home}".encode()).digest()[:32]


def _xor_encrypt(data: bytes, key: bytes) -> bytes:
    """Simple XOR encryption (no extra dependencies needed)"""
    key = key[: len(data)] if len(key) >= len(data) else (key * (len(data) // len(key) + 1))[: len(data)]
    return bytes(a ^ b for a, b in zip(data, key))


def save_token(token: str):
    """Encrypt and save token"""
    key = _get_machine_key()
    encrypted = _xor_encrypt(token.encode("utf-8"), key)
    encoded = base64.b64encode(encrypted).decode("ascii")
    TOKEN_FILE.write_text(encoded, encoding="utf-8")
    # Set file permissions to owner read/write only
    os.chmod(TOKEN_FILE, stat.S_IRUSR | stat.S_IWUSR)


def load_token() -> str | None:
    """Decrypt and load token"""
    if not TOKEN_FILE.exists():
        return None
    try:
        encoded = TOKEN_FILE.read_text(encoding="utf-8").strip()
        encrypted = base64.b64decode(encoded)
        key = _get_machine_key()
        return _xor_encrypt(encrypted, key).decode("utf-8")
    except Exception:
        log.warning("Token decryption failed, may need to re-login")
        return None


def save_account(account_id: str, token: str):
    """Save account information"""
    save_token(token)
    data = {"id": account_id, "saved_at": datetime.now().isoformat()}
    ACCOUNT_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.chmod(ACCOUNT_FILE, stat.S_IRUSR | stat.S_IWUSR)


def load_account() -> dict | None:
    """Load account information"""
    token = load_token()
    if not token:
        return None
    try:
        data = json.loads(ACCOUNT_FILE.read_text(encoding="utf-8"))
        data["token"] = token
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return None


# -- MP Subscription Management ──────────────────────────────────

def load_mp_subscriptions() -> list[dict]:
    """Load subscribed official accounts list [{mpId, name, cover}]"""
    try:
        return json.loads(MP_SUBS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_mp_subscriptions(subs: list[dict]):
    """Save official account subscription list"""
    MP_SUBS_FILE.write_text(json.dumps(subs, ensure_ascii=False, indent=2), encoding="utf-8")


# -- API Client ──────────────────────────────────────────────

class WeReadClient:
    """WeRead API Client (via wewe-rss proxy)"""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": random.choice(USER_AGENTS),
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
        )
        self.account = load_account()

    def _request(self, method: str, path: str, **kwargs) -> dict | list | None:
        """Unified request method with authentication and error handling"""
        url = f"{PROXY_URL}{path}"
        kwargs.setdefault("timeout", (5, 15))

        # Add authentication headers
        if self.account:
            headers = kwargs.pop("headers", {})
            headers["Authorization"] = f"Bearer {self.account['token']}"
            headers["xid"] = self.account["id"]
            kwargs["headers"] = headers

        try:
            resp = self.session.request(method, url, **kwargs)
            if resp.status_code == 401:
                log.warning("WeRead token expired, need to re-login")
                self.account = None
                return None
            if resp.status_code == 429:
                log.warning("WeRead request rate too high, rate limited")
                return None
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            log.error("WeRead API request failed: %s %s - %s", method, path, e)
            return None

    # -- Login ────────────────────────────────────────────────

    def login(self) -> bool:
        """Interactive QR code login"""
        log.info("Getting WeChat QR code login...")

        # 1. Get login URL
        result = self._request_no_auth("GET", "/api/v2/login/platform")
        if not result:
            log.error("Failed to get login QR code")
            return False

        uuid = result.get("uuid", "")
        scan_url = result.get("scanUrl", "")

        if not scan_url:
            log.error("Failed to get scan URL")
            return False

        # 2. Generate QR code image
        qr_path = DATA_DIR / "login_qr.png"
        try:
            import qrcode
            qr = qrcode.make(scan_url)
            qr.save(str(qr_path), format="PNG")
            print(f"\nQR code saved to: {qr_path}")
            print("Please scan the QR code image with WeChat to log in to WeRead")
        except ImportError:
            print("\nPlease convert the following link to a QR code and scan with WeChat:")
            print(f"  {scan_url}\n")

        # 3. Poll login status
        log.info("Waiting for QR code scan (max 120 seconds)...")
        start = time.time()
        while time.time() - start < 120:
            time.sleep(3)
            login_result = self._request_no_auth("GET", f"/api/v2/login/platform/{uuid}")
            if not login_result:
                continue

            message = login_result.get("message", "")
            if message == "OK" or login_result.get("token"):
                vid = login_result.get("vid", "")
                token = login_result.get("token", "")
                if token:
                    save_account(str(vid), token)
                    self.account = {"id": str(vid), "token": token}
                    log.info("Login successful! vid=%s", vid)
                    return True
            elif "waiting" in message.lower() or "scanned" in message.lower():
                log.info("Login status: %s", message)
            else:
                log.info("Login status: %s", message)

        log.error("Login timed out")
        return False

    def _request_no_auth(self, method: str, path: str, **kwargs) -> dict | None:
        """Unauthenticated request (for login flow)"""
        url = f"{PROXY_URL}{path}"
        kwargs.setdefault("timeout", (5, 120))
        try:
            resp = self.session.request(method, url, **kwargs)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            log.error("Request failed: %s %s - %s", method, path, e)
            return None

    def ensure_login(self) -> bool:
        """Ensure logged in, guide login if not"""
        if self.account and self.account.get("token"):
            return True
        log.info("Not logged in or token expired, starting login flow...")
        return self.login()

    # -- Official Account Operations ────────────────────────────

    def get_mp_info(self, article_url: str) -> dict | None:
        """Get official account info via WeChat article URL"""
        result = self._request("POST", "/api/v2/platform/wxs2mp", json={"url": article_url.strip()})
        if isinstance(result, list) and len(result) > 0:
            mp = result[0]
            return {
                "mpId": mp.get("id", ""),
                "name": mp.get("name", ""),
                "cover": mp.get("cover", ""),
                "intro": mp.get("intro", ""),
                "updateTime": mp.get("updateTime", 0),
            }
        return None

    def get_mp_articles(self, mp_id: str, page: int = 1) -> list[dict]:
        """Get official account article list"""
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

            articles.append(
                {
                    "title": item.get("title", ""),
                    "url": f"https://mp.weixin.qq.com/s/{item.get('id', '')}",
                    "digest": "",
                    "account": "",
                    "pub_time": pub_time,
                    "source": "weread",
                    "mp_id": mp_id,
                }
            )

        log.info("WeRead got %d articles for account %s page %d", len(articles), mp_id, page)
        return articles

    def fetch_all_subscribed(self, keyword: str = "") -> list[dict]:
        """Get latest articles from all subscribed accounts, optionally filtered by keyword"""
        if not self.ensure_login():
            log.warning("WeRead not logged in, skipping")
            return []

        subs = load_mp_subscriptions()
        if not subs:
            log.info("WeRead: No subscribed official accounts")
            return []

        all_articles = []
        for sub in subs:
            mp_id = sub.get("mpId", "")
            mp_name = sub.get("name", "Unknown")
            if not mp_id:
                continue

            articles = self.get_mp_articles(mp_id, page=1)
            for a in articles:
                a["account"] = mp_name

            if keyword:
                articles = [a for a in articles if keyword in a.get("title", "") or keyword in a.get("digest", "")]

            all_articles.extend(articles)
            # Polite delay
            time.sleep(random.uniform(2, 4))

        log.info("WeRead got %d articles total (keyword: '%s')", len(all_articles), keyword or "all")
        return all_articles


# -- CLI Tools ───────────────────────────────────────────────

def cmd_login():
    """Interactive login"""
    client = WeReadClient()
    if client.login():
        print("Login successful!")
    else:
        print("Login failed")


def cmd_add_mp(article_url: str):
    """Add official account subscription"""
    client = WeReadClient()
    if not client.ensure_login():
        print("Please log in first")
        return

    mp_info = client.get_mp_info(article_url)
    if not mp_info:
        print("Failed to get official account info, please check the article URL")
        return

    subs = load_mp_subscriptions()
    # Check if already subscribed
    for s in subs:
        if s.get("mpId") == mp_info["mpId"]:
            print(f"Already subscribed to this account: {mp_info['name']}")
            return

    subs.append(mp_info)
    save_mp_subscriptions(subs)
    print(f"Added subscription: {mp_info['name']} (ID: {mp_info['mpId']})")


def cmd_list_subs():
    """List subscribed official accounts"""
    subs = load_mp_subscriptions()
    if not subs:
        print("No subscriptions")
        return
    for i, s in enumerate(subs, 1):
        print(f"  {i}. {s.get('name', 'Unknown')} (ID: {s.get('mpId', '')})")


def cmd_remove_mp(index: int):
    """Remove official account subscription"""
    subs = load_mp_subscriptions()
    if 1 <= index <= len(subs):
        removed = subs.pop(index - 1)
        save_mp_subscriptions(subs)
        print(f"Removed: {removed.get('name', 'Unknown')}")
    else:
        print(f"Invalid index, currently {len(subs)} subscriptions")


def main():
    """CLI entry point"""
    import sys

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python weread_client.py login              # QR code login")
        print("  python weread_client.py add <article URL>  # Add official account subscription")
        print("  python weread_client.py list               # List subscriptions")
        print("  python weread_client.py remove <index>     # Remove subscription")
        print("  python weread_client.py fetch [keyword]    # Fetch articles")
        return

    cmd = sys.argv[1]
    if cmd == "login":
        cmd_login()
    elif cmd == "add" and len(sys.argv) >= 3:
        cmd_add_mp(sys.argv[2])
    elif cmd == "list":
        cmd_list_subs()
    elif cmd == "remove" and len(sys.argv) >= 3:
        cmd_remove_mp(int(sys.argv[2]))
    elif cmd == "fetch":
        keyword = sys.argv[2] if len(sys.argv) >= 3 else ""
        client = WeReadClient()
        articles = client.fetch_all_subscribed(keyword)
        for a in articles:
            print(f"  [{a.get('pub_time', '')}] {a.get('account', '')} - {a['title']}")
    else:
        print("Unknown command")


if __name__ == "__main__":
    main()
