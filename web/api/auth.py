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
    "xiaohongshu": "https://passport.xiaohongshu.com/login",
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
               VALUES (?, ?, 'active', datetime('now'))
               ON CONFLICT(platform) DO UPDATE SET
                   cookies=excluded.cookies, auth_status='active', last_validated=datetime('now')""",
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
