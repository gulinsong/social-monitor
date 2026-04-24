#!/usr/bin/env python3
"""
微信读书(WeRead) API 客户端 - 通过 wewe-rss 代理获取公众号文章
用于实时监控微信公众号更新
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

# ── 配置 ──────────────────────────────────────────────
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


# ── 凭据加密存储 ───────────────────────────────────────

def _get_machine_key() -> bytes:
    """用机器特征码生成加密密钥"""
    # 尝试读取 machine-id
    for path in ["/etc/machine-id", "/var/lib/dbus/machine-id"]:
        try:
            mid = Path(path).read_text().strip()
            if mid:
                return hashlib.sha256(f"weread:{mid}".encode()).digest()[:32]
        except (FileNotFoundError, PermissionError):
            continue
    # 回退：用用户名 + 主目录
    import getpass
    user = getpass.getuser()
    home = str(Path.home())
    return hashlib.sha256(f"weread:{user}:{home}".encode()).digest()[:32]


def _xor_encrypt(data: bytes, key: bytes) -> bytes:
    """简单 XOR 加密（无需额外依赖）"""
    key = key[: len(data)] if len(key) >= len(data) else (key * (len(data) // len(key) + 1))[: len(data)]
    return bytes(a ^ b for a, b in zip(data, key))


def save_token(token: str):
    """加密存储 token"""
    key = _get_machine_key()
    encrypted = _xor_encrypt(token.encode("utf-8"), key)
    encoded = base64.b64encode(encrypted).decode("ascii")
    TOKEN_FILE.write_text(encoded, encoding="utf-8")
    # 设置文件权限为仅所有者可读写
    os.chmod(TOKEN_FILE, stat.S_IRUSR | stat.S_IWUSR)


def load_token() -> str | None:
    """解密加载 token"""
    if not TOKEN_FILE.exists():
        return None
    try:
        encoded = TOKEN_FILE.read_text(encoding="utf-8").strip()
        encrypted = base64.b64decode(encoded)
        key = _get_machine_key()
        return _xor_encrypt(encrypted, key).decode("utf-8")
    except Exception:
        log.warning("token 解密失败，可能需要重新登录")
        return None


def save_account(account_id: str, token: str):
    """保存账号信息"""
    save_token(token)
    data = {"id": account_id, "saved_at": datetime.now().isoformat()}
    ACCOUNT_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.chmod(ACCOUNT_FILE, stat.S_IRUSR | stat.S_IWUSR)


def load_account() -> dict | None:
    """加载账号信息"""
    token = load_token()
    if not token:
        return None
    try:
        data = json.loads(ACCOUNT_FILE.read_text(encoding="utf-8"))
        data["token"] = token
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return None


# ── MP 订阅管理 ────────────────────────────────────────

def load_mp_subscriptions() -> list[dict]:
    """加载已订阅的公众号列表 [{mpId, name, cover}]"""
    try:
        return json.loads(MP_SUBS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_mp_subscriptions(subs: list[dict]):
    """保存公众号订阅列表"""
    MP_SUBS_FILE.write_text(json.dumps(subs, ensure_ascii=False, indent=2), encoding="utf-8")


# ── API 客户端 ─────────────────────────────────────────

class WeReadClient:
    """微信读书 API 客户端（通过 wewe-rss 代理）"""

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
        """统一请求方法，带认证和错误处理"""
        url = f"{PROXY_URL}{path}"
        kwargs.setdefault("timeout", (5, 15))

        # 添加认证头
        if self.account:
            headers = kwargs.pop("headers", {})
            headers["Authorization"] = f"Bearer {self.account['token']}"
            headers["xid"] = self.account["id"]
            kwargs["headers"] = headers

        try:
            resp = self.session.request(method, url, **kwargs)
            if resp.status_code == 401:
                log.warning("WeRead token 已过期，需要重新登录")
                self.account = None
                return None
            if resp.status_code == 429:
                log.warning("WeRead 请求频率过高，被限流")
                return None
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            log.error("WeRead API 请求失败: %s %s - %s", method, path, e)
            return None

    # ── 登录 ───────────────────────────────────────────

    def login(self) -> bool:
        """交互式扫码登录"""
        log.info("正在获取微信扫码登录二维码...")

        # 1. 获取登录 URL
        result = self._request_no_auth("GET", "/api/v2/login/platform")
        if not result:
            log.error("获取登录二维码失败")
            return False

        uuid = result.get("uuid", "")
        scan_url = result.get("scanUrl", "")

        if not scan_url:
            log.error("未获取到扫码 URL")
            return False

        # 2. 生成二维码图片
        qr_path = DATA_DIR / "login_qr.png"
        try:
            import qrcode
            qr = qrcode.make(scan_url)
            qr.save(str(qr_path), format="PNG")
            print(f"\n二维码已保存到: {qr_path}")
            print("请用微信扫描该二维码图片登录微信读书")
        except ImportError:
            print("\n请将以下链接转成二维码后用微信扫描：")
            print(f"  {scan_url}\n")

        # 3. 轮询登录状态
        log.info("等待扫码登录（最长等待 120 秒）...")
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
                    log.info("登录成功！vid=%s", vid)
                    return True
            elif "waiting" in message.lower() or "scanned" in message.lower():
                log.info("登录状态: %s", message)
            else:
                log.info("登录状态: %s", message)

        log.error("登录超时")
        return False

    def _request_no_auth(self, method: str, path: str, **kwargs) -> dict | None:
        """无认证的请求（用于登录流程）"""
        url = f"{PROXY_URL}{path}"
        kwargs.setdefault("timeout", (5, 120))
        try:
            resp = self.session.request(method, url, **kwargs)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            log.error("请求失败: %s %s - %s", method, path, e)
            return None

    def ensure_login(self) -> bool:
        """确保已登录，未登录则引导登录"""
        if self.account and self.account.get("token"):
            return True
        log.info("未登录或 token 已过期，开始登录流程...")
        return self.login()

    # ── 公众号操作 ──────────────────────────────────────

    def get_mp_info(self, article_url: str) -> dict | None:
        """通过微信公众号文章 URL 获取公众号信息"""
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
        """获取公众号文章列表"""
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

        log.info("WeRead 获取公众号 %s 第%d页 %d 篇文章", mp_id, page, len(articles))
        return articles

    def fetch_all_subscribed(self, keyword: str = "") -> list[dict]:
        """获取所有已订阅公众号的最新文章，可选按关键词过滤"""
        if not self.ensure_login():
            log.warning("WeRead 未登录，跳过")
            return []

        subs = load_mp_subscriptions()
        if not subs:
            log.info("WeRead: 暂无订阅的公众号")
            return []

        all_articles = []
        for sub in subs:
            mp_id = sub.get("mpId", "")
            mp_name = sub.get("name", "未知")
            if not mp_id:
                continue

            articles = self.get_mp_articles(mp_id, page=1)
            for a in articles:
                a["account"] = mp_name

            if keyword:
                articles = [a for a in articles if keyword in a.get("title", "") or keyword in a.get("digest", "")]

            all_articles.extend(articles)
            # 礼貌延迟
            time.sleep(random.uniform(2, 4))

        log.info("WeRead 共获取 %d 篇文章 (关键词: '%s')", len(all_articles), keyword or "全部")
        return all_articles


# ── 命令行工具 ─────────────────────────────────────────

def cmd_login():
    """交互式登录"""
    client = WeReadClient()
    if client.login():
        print("登录成功！")
    else:
        print("登录失败")


def cmd_add_mp(article_url: str):
    """添加公众号订阅"""
    client = WeReadClient()
    if not client.ensure_login():
        print("请先登录")
        return

    mp_info = client.get_mp_info(article_url)
    if not mp_info:
        print("未能获取公众号信息，请检查文章 URL")
        return

    subs = load_mp_subscriptions()
    # 检查是否已订阅
    for s in subs:
        if s.get("mpId") == mp_info["mpId"]:
            print(f"已订阅过此公众号: {mp_info['name']}")
            return

    subs.append(mp_info)
    save_mp_subscriptions(subs)
    print(f"已添加订阅: {mp_info['name']} (ID: {mp_info['mpId']})")


def cmd_list_subs():
    """列出已订阅公众号"""
    subs = load_mp_subscriptions()
    if not subs:
        print("暂无订阅")
        return
    for i, s in enumerate(subs, 1):
        print(f"  {i}. {s.get('name', '未知')} (ID: {s.get('mpId', '')})")


def cmd_remove_mp(index: int):
    """移除公众号订阅"""
    subs = load_mp_subscriptions()
    if 1 <= index <= len(subs):
        removed = subs.pop(index - 1)
        save_mp_subscriptions(subs)
        print(f"已移除: {removed.get('name', '未知')}")
    else:
        print(f"无效序号，当前共 {len(subs)} 个订阅")


def main():
    """命令行入口"""
    import sys

    if len(sys.argv) < 2:
        print("用法:")
        print("  python weread_client.py login              # 扫码登录")
        print("  python weread_client.py add <文章URL>       # 添加公众号订阅")
        print("  python weread_client.py list                # 列出订阅")
        print("  python weread_client.py remove <序号>       # 移除订阅")
        print("  python weread_client.py fetch [关键词]      # 获取文章")
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
        print("未知命令")


if __name__ == "__main__":
    main()
