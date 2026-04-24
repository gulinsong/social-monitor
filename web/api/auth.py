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


@bp.route("/api/auth/status/<platform>")
def auth_status(platform):
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
    try:
        # WeChat WeRead 支持自动扫码登录
        if platform == "wechat":
            return _wechat_qrcode()

        # Weibo 通过 Playwright 自动获取 Cookie
        if platform == "weibo":
            return _weibo_qrcode()

        # XHS 通过 Playwright 扫码登录
        if platform == "xiaohongshu":
            return _xhs_qrcode()

        # 其他平台：生成登录页二维码 + 手动输入Cookie提示
        url = LOGIN_URLS.get(platform)
        if not url:
            return jsonify({"error": f"不支持的平台: {platform}"}), 400

        name = PLATFORM_NAMES.get(platform, platform)
        return jsonify({
            "qr_image": _url_to_qr_base64(url),
            "qrid": "",
            "message": f"请用{name}APP扫码登录，登录后在浏览器F12复制Cookie填入",
            "manual": True,
        })

    except Exception as e:
        log.error("获取二维码失败: %s", e)
        return jsonify({"error": str(e)}), 500


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
        log.error("微博二维码获取失败: %s", e)
        return jsonify({"error": str(e)}), 500


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
        log.error("XHS 二维码获取失败: %s", e)
        return jsonify({"error": str(e)}), 500


@bp.route("/api/auth/check/<platform>", methods=["POST"])
def check_login(platform):
    """轮询扫码登录状态"""
    data = request.get_json() or {}
    qrid = data.get("qrid", data.get("uuid", ""))

    try:
        if platform == "wechat":
            client_obj = _get_login_session(platform, qrid)
            if not client_obj:
                return jsonify({"status": "error", "message": "会话已过期，请重新获取二维码"})
            result = client_obj.check_login_status(qrid)
            if result.get("status") == "success":
                token = client_obj.load_token() or ""
                if token:
                    _save_platform_cookies(platform, f"weread_token={token}")
            return jsonify(result)

        if platform == "weibo":
            login_obj = _get_login_session(platform, qrid)
            if not login_obj:
                return jsonify({"status": "error", "message": "会话已过期，请重新获取二维码"})
            result = login_obj.check_scan(qrid)
            if result.get("status") == "success" and result.get("cookies"):
                _save_platform_cookies(platform, result["cookies"])
            return jsonify(result)

        if platform == "xiaohongshu":
            login_obj = _get_login_session(platform, qrid)
            if not login_obj:
                return jsonify({"status": "error", "message": "会话已过期，请重新获取二维码"})
            result = login_obj.check_scan(qrid)
            if result.get("status") == "success" and result.get("cookies"):
                _save_platform_cookies(platform, result["cookies"])
            return jsonify(result)

        return jsonify({"status": "waiting", "message": "该平台需手动输入Cookie"})

    except Exception as e:
        log.error("检查登录状态失败: %s", e)
        return jsonify({"status": "error", "message": str(e)})


@bp.route("/api/auth/cookie/<platform>", methods=["POST"])
def save_cookie(platform):
    """手动保存 Cookie"""
    data = request.get_json()
    cookie_str = data.get("cookies", "")
    if not cookie_str:
        return jsonify({"error": "Cookie 不能为空"}), 400
    _save_platform_cookies(platform, cookie_str)
    return jsonify({"status": "ok"})


@bp.route("/api/auth/verify/<platform>", methods=["POST"])
def verify_auth(platform):
    """验证平台 Cookie 是否有效"""
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
        return jsonify({"ok": False, "message": str(e)})


@bp.route("/api/auth/crawl/<platform>", methods=["POST"])
def crawl_once(platform):
    """手动触发一次爬取"""
    import threading
    from core.scheduler import _load_monitor_class, UnifiedScheduler
    from core.config_loader import get_platform_config

    cfg = _get_cfg()
    db_path = _get_db()

    # 检查平台是否已认证
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
    if not keywords:
        return jsonify({"ok": False, "message": "未配置关键词"}), 400

    # 后台线程执行爬取
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
_login_sessions = {}
_login_lock = threading.Lock()


def _store_login_session(platform: str, key: str, obj):
    with _login_lock:
        _login_sessions[f"{platform}:{key}"] = obj


def _get_login_session(platform: str, key: str):
    with _login_lock:
        return _login_sessions.get(f"{platform}:{key}")


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

    # 方式1: 通过文章链接提取
    if article_url:
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

    # 先从 URL 参数提取 __biz
    m = re.search(r'__biz=([A-Za-z0-9_+=]+)', url)
    if m:
        biz = m.group(1)

    # 请求文章页面提取更多信息
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

        # 从最终 URL 提取 __biz（短链接跳转后）
        if not biz:
            m = re.search(r'__biz=([A-Za-z0-9_+=]+)', final_url)
            if m:
                biz = m.group(1)

        # 从 HTML 中提取 __biz
        if not biz:
            m = re.search(r'__biz\s*=\s*["\']?\s*([A-Za-z0-9_+=]+)', html)
            if m:
                biz = m.group(1)

        # 提取公众号名称 — 多种模式
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
