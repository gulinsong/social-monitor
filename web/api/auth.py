"""登录管理 API — 扫码登录 + Cookie输入"""
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
    "weibo": "微博",
    "wechat": "微信",
    "maimai": "脉脉",
    "xiaohongshu": "小红书",
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
        return jsonify({"error": "无效的平台"}), 400
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
    """获取平台登录二维码（base64图片）"""
    if not _validate_platform(platform):
        return jsonify({"error": "无效的平台"}), 400
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
            return jsonify({"error": "不支持的平台"}), 400

        name = PLATFORM_NAMES.get(platform, platform)
        return jsonify({
            "qr_image": _url_to_qr_base64(url),
            "qrid": "",
            "message": f"请用{name}APP扫码登录，登录后在浏览器F12复制Cookie填入",
            "manual": True,
        })

    except Exception as e:
        log.error("获取二维码失败: %s", e, exc_info=True)
        return jsonify({"error": "获取二维码失败，请重试"}), 500


def _wechat_qrcode():
    """微信读书扫码登录"""
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
    """微博 Playwright 扫码登录"""
    try:
        from platforms.weibo.login import WeiboQRLogin
        login = WeiboQRLogin()
        result = login.get_qrcode()
        if "error" in result:
            return jsonify(result), 400
        _store_login_session("weibo", "browser", login)
        return jsonify(result)
    except ImportError:
        log.warning("Playwright 未安装，回退到手动模式")
        return jsonify({
            "qr_image": _url_to_qr_base64("https://passport.weibo.cn/signin/login"),
            "qrid": "",
            "message": "Playwright 未安装，请手动输入Cookie",
            "manual": True,
        })
    except Exception as e:
        log.error("微博二维码获取失败: %s", e, exc_info=True)
        return jsonify({"error": "获取二维码失败，请重试"}), 500


def _xhs_qrcode():
    """XHS Playwright 扫码登录"""
    try:
        from platforms.xiaohongshu.login import XhsQRLogin
        login = XhsQRLogin()
        result = login.get_qrcode()
        if "error" in result:
            return jsonify(result), 400
        _store_login_session("xiaohongshu", "browser", login)
        return jsonify(result)
    except ImportError:
        log.warning("Playwright 未安装，回退到手动模式")
        return jsonify({
            "qr_image": _url_to_qr_base64(LOGIN_URLS.get("xiaohongshu", "")),
            "qrid": "",
            "message": "Playwright 未安装，请手动输入Cookie",
            "manual": True,
        })
    except Exception as e:
        log.error("XHS 二维码获取失败: %s", e, exc_info=True)
        return jsonify({"error": "获取二维码失败，请重试"}), 500


def _maimai_qrcode():
    """MM 扫码登录"""
    try:
        from platforms.maimai.login import MaimaiQRLogin
        login = MaimaiQRLogin()
        result = login.get_qrcode()
        if "error" in result:
            return jsonify(result), 400
        _store_login_session("maimai", result.get("qrid", ""), login)
        return jsonify(result)
    except Exception as e:
        log.error("MM 二维码获取失败: %s", e, exc_info=True)
        return jsonify({"error": "获取二维码失败，请重试"}), 500


@bp.route("/api/auth/check/<platform>", methods=["POST"])
def check_login(platform):
    """轮询扫码登录状态"""
    if not _validate_platform(platform):
        return jsonify({"status": "error", "message": "无效的平台"}), 400
    data = request.get_json() or {}
    qrid = data.get("qrid", data.get("uuid", ""))

    try:
        login_obj = _get_login_session(platform, qrid)
        if platform == "wechat":
            if not login_obj:
                return jsonify({"status": "error", "message": "会话已过期，请重新获取二维码"})
            result = login_obj.check_login_status(qrid)
            if result.get("status") == "success":
                token = login_obj.load_token() or ""
                if token:
                    _save_platform_cookies(platform, f"weread_token={token}")
            return jsonify(result)

        if platform in ("weibo", "xiaohongshu", "maimai"):
            if not login_obj:
                return jsonify({"status": "error", "message": "会话已过期，请重新获取二维码"})
            result = login_obj.check_scan(qrid)
            if result.get("status") == "success" and result.get("cookies"):
                _save_platform_cookies(platform, result["cookies"])
                _cleanup_login_session(platform, qrid)
            return jsonify(result)

        return jsonify({"status": "waiting", "message": "该平台需手动输入Cookie"})

    except Exception as e:
        log.error("检查登录状态失败: %s", e, exc_info=True)
        return jsonify({"status": "error", "message": "检查状态失败，请重试"})


@bp.route("/api/auth/cookie/<platform>", methods=["POST"])
def save_cookie(platform):
    """手动保存 Cookie"""
    if not _validate_platform(platform):
        return jsonify({"error": "无效的平台"}), 400
    data = request.get_json()
    cookie_str = (data.get("cookies", "") or "").strip()
    if not cookie_str:
        return jsonify({"error": "Cookie 不能为空"}), 400
    if len(cookie_str) > 10000:
        return jsonify({"error": "Cookie 过长，请检查输入"}), 400
    _save_platform_cookies(platform, cookie_str)
    return jsonify({"status": "ok"})


@bp.route("/api/auth/verify/<platform>", methods=["POST"])
def verify_auth(platform):
    """验证平台 Cookie 是否有效"""
    if not _validate_platform(platform):
        return jsonify({"ok": False, "message": "无效的平台"}), 400
    try:
        from core.scheduler import _load_monitor_class
        from core.config_loader import get_platform_config
        MonitorClass = _load_monitor_class(platform)
        if not MonitorClass:
            return jsonify({"ok": False, "message": "平台模块未加载"})
        pcfg = get_platform_config(platform, _get_cfg())
        monitor = MonitorClass(pcfg, _get_db())
        ok = monitor.verify_auth()
        return jsonify({"ok": ok, "auth_status": "active" if ok else "expired"})
    except Exception as e:
        log.error("验证认证失败: %s", e, exc_info=True)
        return jsonify({"ok": False, "message": "验证失败"})


@bp.route("/api/auth/crawl/<platform>", methods=["POST"])
def crawl_once(platform):
    """手动触发一次爬取"""
    if not _validate_platform(platform):
        return jsonify({"ok": False, "message": "无效的平台"}), 400
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
            return jsonify({"ok": False, "message": "该平台未认证，请先登录"}), 400
    finally:
        conn.close()

    MonitorClass = _load_monitor_class(platform)
    if not MonitorClass:
        return jsonify({"ok": False, "message": "平台模块未加载"}), 400

    pcfg = get_platform_config(platform, cfg)
    default_kw = cfg.get("default_keywords", [])
    keywords = pcfg.get("keywords", default_kw)
    needs_keywords = pcfg.get("source", "") not in ("colleague_circle",)
    if not keywords and needs_keywords:
        return jsonify({"ok": False, "message": "未配置关键词"}), 400

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
        log.info("[手动爬取] %s 完成", platform)

    t = threading.Thread(target=_do_crawl, daemon=True)
    t.start()
    return jsonify({"ok": True, "message": f"已开始爬取 {PLATFORM_NAMES.get(platform, platform)}"})


# ── 辅助函数 ──

import threading
import time

_login_sessions = {}
_login_lock = threading.Lock()
_SESSION_TTL = 600  # 10 分钟过期


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
    """将 URL 转成二维码 base64 图片"""
    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


# ── 微信公众号订阅管理 ──

_ALLOWED_MP_DOMAINS = ("mp.weixin.qq.com", "weixin.qq.com")


def _get_weread_client():
    from platforms.wechat.weread_client import WeReadClient
    return WeReadClient(_get_db())


@bp.route("/api/auth/wechat/mp", methods=["GET"])
def list_mp():
    """获取微信公众号订阅列表"""
    client = _get_weread_client()
    subs = client.load_mp_subscriptions()
    return jsonify(subs)


@bp.route("/api/auth/wechat/mp", methods=["POST"])
def add_mp():
    """通过文章链接添加微信公众号订阅"""
    data = request.get_json() or {}
    article_url = data.get("article_url", "").strip()
    name = data.get("name", "").strip()
    mp_id = data.get("mpId", "").strip()

    if article_url:
        # SSRF 防护：只允许微信公众号域名
        from urllib.parse import urlparse
        parsed = urlparse(article_url)
        if parsed.scheme not in ("http", "https"):
            return jsonify({"error": "仅支持 http/https 链接"}), 400
        host = (parsed.hostname or "").lower()
        if not any(host == d or host.endswith("." + d) for d in _ALLOWED_MP_DOMAINS):
            return jsonify({"error": "仅支持微信公众号文章链接"}), 400
        mp_id, name = _extract_mp_from_url(article_url)
        if not mp_id:
            return jsonify({"error": "无法从链接中提取公众号信息，请确认链接格式"}), 400

    if not mp_id:
        return jsonify({"error": "请提供文章链接"}), 400

    client = _get_weread_client()
    subs = client.load_mp_subscriptions()
    if any(s.get("mpId") == mp_id for s in subs):
        return jsonify({"error": "该公众号已订阅"}), 400
    subs.append({"mpId": mp_id, "name": name or mp_id})
    client.save_mp_subscriptions(subs)
    return jsonify({"status": "ok"})


def _extract_mp_from_url(url: str) -> tuple[str, str]:
    """从微信公众号文章链接中提取公众号信息"""
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
        log.warning("请求文章页失败: %s", e)

    return biz, name


@bp.route("/api/auth/wechat/mp/<mp_id>", methods=["DELETE"])
def delete_mp(mp_id):
    """删除微信公众号订阅"""
    client = _get_weread_client()
    subs = client.load_mp_subscriptions()
    subs = [s for s in subs if s.get("mpId") != mp_id]
    client.save_mp_subscriptions(subs)
    return jsonify({"status": "ok"})
