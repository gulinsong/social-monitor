"""Login Management API — QR Login + Cookie Input"""
import json
import logging

import qrcode
import io
import base64

from flask import Blueprint, jsonify, request, current_app
from core.base_monitor import encrypt_cookie
from db.schema import get_connection

log = logging.getLogger(__name__)
bp = Blueprint("auth", __name__)

PLATFORM_NAMES = {
    "weibo": "Weibo",
    "wechat": "WeChat",
    "maimai": "Maimai",
    "xiaohongshu": "Xiaohongshu",
}

VALID_PLATFORMS = set(PLATFORM_NAMES.keys())

LOGIN_URLS = {
    "weibo": "https://weibo.com/login.php",
    "wechat": "https://weread.qq.com/",
    "maimai": "https://maimai.cn/login",
    "xiaohongshu": "https://www.xiaohongshu.com/login",
}


def _get_db():
    return current_app.config["DB_PATH"]


def _get_cfg():
    return current_app.config["MONITOR_CONFIG"]


def _validate_platform(platform: str) -> bool:
    return platform in VALID_PLATFORMS


@bp.route("/api/auth/status/<platform>")
def auth_status(platform):
    if not _validate_platform(platform):
        return jsonify({"error": "Invalid platform"}), 400
    conn = get_connection(_get_db())
    try:
        row = conn.execute(
            "SELECT platform, auth_status, last_validated FROM platform_auth WHERE platform=?",
            (platform,),
        ).fetchone()
        if row:
            return jsonify(dict(row))
        return jsonify({"platform": platform, "auth_status": "inactive", "last_validated": None})
    finally:
        conn.close()


@bp.route("/api/auth/qrcode/<platform>")
def login_qrcode(platform):
    """Get platform login QR code (base64 image)"""
    if not _validate_platform(platform):
        return jsonify({"error": "Invalid platform"}), 400
    try:
        if platform == "wechat":
            return _wechat_qrcode()
        if platform == "weibo":
            return _weibo_qrcode()
        if platform == "xiaohongshu":
            return _xhs_qrcode()
        if platform == "maimai":
            return _maimai_qrcode()

        url = LOGIN_URLS.get(platform)
        if not url:
            return jsonify({"error": "Unsupported platform"}), 400

        name = PLATFORM_NAMES.get(platform, platform)
        return jsonify({
            "qr_image": _url_to_qr_base64(url),
            "qrid": "",
            "message": f"Scan QR code with {name} app to login, then copy Cookie from browser F12 and paste it here",
            "manual": True,
        })

    except Exception as e:
        log.error("Failed to get QR code: %s", e, exc_info=True)
        return jsonify({"error": "Failed to get QR code, please retry"}), 500


def _wechat_qrcode():
    """WeRead QR code login"""
    from platforms.wechat.weread_client import WeReadClient
    client = WeReadClient(_get_db())
    result = client.get_login_qrcode()
    if "error" in result:
        return jsonify(result), 400
    qr_url = result.get("qr_url", "")
    if qr_url and not result.get("qr_image"):
        result["qr_image"] = _url_to_qr_base64(qr_url)
    _store_login_session("wechat", result.get("uuid", ""), client)
    return jsonify(result)


def _weibo_qrcode():
    """Weibo Playwright QR code login"""
    try:
        from platforms.weibo.login import WeiboQRLogin
        login = WeiboQRLogin()
        result = login.get_qrcode()
        if "error" in result:
            return jsonify(result), 400
        _store_login_session("weibo", "browser", login)
        return jsonify(result)
    except ImportError:
        log.warning("Playwright not installed, falling back to manual mode")
        return jsonify({
            "qr_image": _url_to_qr_base64("https://passport.weibo.cn/signin/login"),
            "qrid": "",
            "message": "Playwright not installed, please enter Cookie manually",
            "manual": True,
        })
    except Exception as e:
        log.error("Failed to get Weibo QR code: %s", e, exc_info=True)
        return jsonify({"error": "Failed to get QR code, please retry"}), 500


def _xhs_qrcode():
    """XHS Playwright QR code login"""
    try:
        from platforms.xiaohongshu.login import XhsQRLogin
        login = XhsQRLogin()
        result = login.get_qrcode()
        if "error" in result:
            return jsonify(result), 400
        _store_login_session("xiaohongshu", "browser", login)
        return jsonify(result)
    except ImportError:
        log.warning("Playwright not installed, falling back to manual mode")
        return jsonify({
            "qr_image": _url_to_qr_base64(LOGIN_URLS.get("xiaohongshu", "")),
            "qrid": "",
            "message": "Playwright not installed, please enter Cookie manually",
            "manual": True,
        })
    except Exception as e:
        log.error("Failed to get XHS QR code: %s", e, exc_info=True)
        return jsonify({"error": "Failed to get QR code, please retry"}), 500


def _maimai_qrcode():
    """Maimai QR code login"""
    try:
        from platforms.maimai.login import MaimaiQRLogin
        login = MaimaiQRLogin()
        result = login.get_qrcode()
        if "error" in result:
            return jsonify(result), 400
        _store_login_session("maimai", result.get("qrid", ""), login)
        return jsonify(result)
    except Exception as e:
        log.error("Failed to get Maimai QR code: %s", e, exc_info=True)
        return jsonify({"error": "Failed to get QR code, please retry"}), 500


@bp.route("/api/auth/check/<platform>", methods=["POST"])
def check_login(platform):
    """Poll QR code login status"""
    if not _validate_platform(platform):
        return jsonify({"status": "error", "message": "Invalid platform"}), 400
    data = request.get_json() or {}
    qrid = data.get("qrid", data.get("uuid", ""))

    try:
        login_obj = _get_login_session(platform, qrid)
        if platform == "wechat":
            if not login_obj:
                return jsonify({"status": "error", "message": "Session expired, please get a new QR code"})
            result = login_obj.check_login_status(qrid)
            if result.get("status") == "success":
                token = login_obj.load_token() or ""
                if token:
                    _save_platform_cookies(platform, f"weread_token={token}")
            return jsonify(result)

        if platform in ("weibo", "xiaohongshu", "maimai"):
            if not login_obj:
                return jsonify({"status": "error", "message": "Session expired, please get a new QR code"})
            result = login_obj.check_scan(qrid)
            if result.get("status") == "success" and result.get("cookies"):
                _save_platform_cookies(platform, result["cookies"])
                _cleanup_login_session(platform, qrid)
            return jsonify(result)

        return jsonify({"status": "waiting", "message": "This platform requires manual Cookie input"})

    except Exception as e:
        log.error("Failed to check login status: %s", e, exc_info=True)
        return jsonify({"status": "error", "message": "Failed to check status, please retry"})


@bp.route("/api/auth/cookie/<platform>", methods=["POST"])
def save_cookie(platform):
    """Manually save Cookie"""
    if not _validate_platform(platform):
        return jsonify({"error": "Invalid platform"}), 400
    data = request.get_json()
    cookie_str = (data.get("cookies", "") or "").strip()
    if not cookie_str:
        return jsonify({"error": "Cookie cannot be empty"}), 400
    if len(cookie_str) > 10000:
        return jsonify({"error": "Cookie too long, please check input"}), 400
    _save_platform_cookies(platform, cookie_str)
    return jsonify({"status": "ok"})


@bp.route("/api/auth/verify/<platform>", methods=["POST"])
def verify_auth(platform):
    """Verify platform Cookie validity"""
    if not _validate_platform(platform):
        return jsonify({"ok": False, "message": "Invalid platform"}), 400
    try:
        from core.scheduler import _load_monitor_class
        from core.config_loader import get_platform_config
        MonitorClass = _load_monitor_class(platform)
        if not MonitorClass:
            return jsonify({"ok": False, "message": "Platform module not loaded"})
        pcfg = get_platform_config(platform, _get_cfg())
        monitor = MonitorClass(pcfg, _get_db())
        ok = monitor.verify_auth()
        return jsonify({"ok": ok, "auth_status": "active" if ok else "expired"})
    except Exception as e:
        log.error("Failed to verify auth: %s", e, exc_info=True)
        return jsonify({"ok": False, "message": "Verification failed"})


@bp.route("/api/auth/crawl/<platform>", methods=["POST"])
def crawl_once(platform):
    """Manually trigger a single crawl"""
    if not _validate_platform(platform):
        return jsonify({"ok": False, "message": "Invalid platform"}), 400
    import threading
    from core.scheduler import _load_monitor_class, UnifiedScheduler
    from core.config_loader import get_platform_config

    cfg = _get_cfg()
    db_path = _get_db()

    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT auth_status FROM platform_auth WHERE platform=?", (platform,)
        ).fetchone()
        if not row or row["auth_status"] != "active":
            return jsonify({"ok": False, "message": "Platform not authenticated, please login first"}), 400
    finally:
        conn.close()

    MonitorClass = _load_monitor_class(platform)
    if not MonitorClass:
        return jsonify({"ok": False, "message": "Platform module not loaded"}), 400

    pcfg = get_platform_config(platform, cfg)
    default_kw = cfg.get("default_keywords", [])
    keywords = pcfg.get("keywords", default_kw)
    needs_keywords = pcfg.get("source", "") not in ("colleague_circle",)
    if not keywords and needs_keywords:
        return jsonify({"ok": False, "message": "No keywords configured"}), 400

    def _do_crawl():
        scheduler = UnifiedScheduler.__new__(UnifiedScheduler)
        scheduler.config = cfg
        scheduler.db_path = db_path
        scheduler.jobs = []
        scheduler._lock = threading.Lock()
        scheduler._sentiment_analyzer = None
        scheduler._feishu_notifier = None
        scheduler._init_analyzer()
        scheduler._init_notifier()

        from core.scheduler import ScheduledJob
        job = ScheduledJob(platform, 999999, keywords, True)
        scheduler._execute_job(job)
        log.info("[Manual crawl] %s completed", platform)

    t = threading.Thread(target=_do_crawl, daemon=True)
    t.start()
    return jsonify({"ok": True, "message": f"Crawl started for {PLATFORM_NAMES.get(platform, platform)}"})


# -- Helper functions --

import threading
import time

_login_sessions = {}
_login_lock = threading.Lock()
_SESSION_TTL = 600  # 10 min expiry


def _store_login_session(platform: str, key: str, obj):
    with _login_lock:
        _cleanup_expired_sessions()
        _login_sessions[f"{platform}:{key}"] = {
            "obj": obj,
            "created_at": time.time(),
        }


def _get_login_session(platform: str, key: str):
    with _login_lock:
        entry = _login_sessions.get(f"{platform}:{key}")
        if entry and time.time() - entry["created_at"] > _SESSION_TTL:
            _cleanup_entry(f"{platform}:{key}", entry)
            return None
        return entry["obj"] if entry else None


def _cleanup_login_session(platform: str, key: str):
    with _login_lock:
        entry = _login_sessions.pop(f"{platform}:{key}", None)
        if entry and hasattr(entry["obj"], "close"):
            try:
                entry["obj"].close()
            except Exception:
                pass


def _cleanup_entry(k: str, entry: dict):
    _login_sessions.pop(k, None)
    if hasattr(entry["obj"], "close"):
        try:
            entry["obj"].close()
        except Exception:
            pass


def _cleanup_expired_sessions():
    now = time.time()
    expired = [k for k, v in _login_sessions.items()
               if now - v["created_at"] > _SESSION_TTL]
    for k in expired:
        _cleanup_entry(k, _login_sessions[k])


def _save_platform_cookies(platform: str, cookie_str: str):
    conn = get_connection(_get_db())
    try:
        encrypted = encrypt_cookie(cookie_str)
        conn.execute(
            """INSERT INTO platform_auth (platform, cookies, auth_status, last_validated)
               VALUES (?, ?, 'active', datetime('now','localtime'))
               ON CONFLICT(platform) DO UPDATE SET
                   cookies=excluded.cookies, auth_status='active', last_validated=datetime('now','localtime')""",
            (platform, encrypted),
        )
        conn.commit()
    finally:
        conn.close()


def _url_to_qr_base64(url: str) -> str:
    """Convert URL to QR code base64 image"""
    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


# -- WeChat MP subscription management --

_ALLOWED_MP_DOMAINS = ("mp.weixin.qq.com", "weixin.qq.com")


def _get_weread_client():
    from platforms.wechat.weread_client import WeReadClient
    return WeReadClient(_get_db())


@bp.route("/api/auth/wechat/mp", methods=["GET"])
def list_mp():
    """Get WeChat MP subscription list"""
    client = _get_weread_client()
    subs = client.load_mp_subscriptions()
    return jsonify(subs)


@bp.route("/api/auth/wechat/mp", methods=["POST"])
def add_mp():
    """Add WeChat MP subscription via article URL"""
    data = request.get_json() or {}
    article_url = data.get("article_url", "").strip()
    name = data.get("name", "").strip()
    mp_id = data.get("mpId", "").strip()

    if article_url:
        # SSRF protection: only allow WeChat MP domains
        from urllib.parse import urlparse
        parsed = urlparse(article_url)
        if parsed.scheme not in ("http", "https"):
            return jsonify({"error": "Only http/https links are supported"}), 400
        host = (parsed.hostname or "").lower()
        if not any(host == d or host.endswith("." + d) for d in _ALLOWED_MP_DOMAINS):
            return jsonify({"error": "Only WeChat official account article links are supported"}), 400
        mp_id, name = _extract_mp_from_url(article_url)
        if not mp_id:
            return jsonify({"error": "Cannot extract official account info from URL, please verify the link format"}), 400

    if not mp_id:
        return jsonify({"error": "Please provide an article link"}), 400

    client = _get_weread_client()
    subs = client.load_mp_subscriptions()
    if any(s.get("mpId") == mp_id for s in subs):
        return jsonify({"error": "This official account is already subscribed"}), 400
    subs.append({"mpId": mp_id, "name": name or mp_id})
    client.save_mp_subscriptions(subs)
    return jsonify({"status": "ok"})


def _extract_mp_from_url(url: str) -> tuple[str, str]:
    """Extract official account info from WeChat article URL"""
    import re
    import requests as req

    biz = ""
    name = ""

    m = re.search(r'__biz=([A-Za-z0-9_+=]+)', url)
    if m:
        biz = m.group(1)

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        resp = req.get(url, headers=headers, timeout=10, allow_redirects=True)
        html = resp.text
        final_url = resp.url

        if not biz:
            m = re.search(r'__biz=([A-Za-z0-9_+=]+)', final_url)
            if m:
                biz = m.group(1)

        if not biz:
            m = re.search(r'__biz\s*=\s*["\']?\s*([A-Za-z0-9_+=]+)', html)
            if m:
                biz = m.group(1)

        for pattern in [
            r'var\s+nickname\s*=\s*["\']([^"\']+)["\']',
            r'<strong\s+class="profile_nickname">([^<]+)</strong>',
            r'<span\s+class="profile_nickname">([^<]+)</span>',
            r'"nickname"\s*:\s*"([^"]+)"',
        ]:
            m = re.search(pattern, html)
            if m:
                name = m.group(1).strip()
                break

    except Exception as e:
        log.warning("Failed to fetch article page: %s", e)

    return biz, name


@bp.route("/api/auth/wechat/mp/<mp_id>", methods=["DELETE"])
def delete_mp(mp_id):
    """Delete WeChat MP subscription"""
    client = _get_weread_client()
    subs = client.load_mp_subscriptions()
    subs = [s for s in subs if s.get("mpId") != mp_id]
    client.save_mp_subscriptions(subs)
    return jsonify({"status": "ok"})
