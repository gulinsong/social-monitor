"""登录管理 API"""
import logging
from flask import Blueprint, jsonify, request, current_app
from core.base_monitor import encrypt_cookie
from db.schema import get_connection

log = logging.getLogger(__name__)
bp = Blueprint("auth", __name__)


@bp.route("/api/auth/status/<platform>")
def auth_status(platform):
    db = current_app.config["DB_PATH"]
    conn = get_connection(db)
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
    monitor = _create_monitor(platform)
    if not monitor:
        return jsonify({"error": f"未知平台: {platform}"}), 400
    return jsonify(monitor.get_login_qrcode())


@bp.route("/api/auth/check/<platform>/<uuid>")
def check_login(platform, uuid):
    monitor = _create_monitor(platform)
    if not monitor:
        return jsonify({"status": "error"}), 400
    return jsonify(monitor.check_login_status(uuid))


@bp.route("/api/auth/cookie/<platform>", methods=["POST"])
def save_cookie(platform):
    data = request.get_json()
    cookie_str = data.get("cookies", "")
    if not cookie_str:
        return jsonify({"error": "Cookie 不能为空"}), 400

    db = current_app.config["DB_PATH"]
    conn = get_connection(db)
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
    return jsonify({"status": "ok"})


def _create_monitor(platform: str):
    try:
        from core.scheduler import _load_monitor_class
        from core.config_loader import get_platform_config
        cfg = current_app.config["MONITOR_CONFIG"]
        MonitorClass = _load_monitor_class(platform)
        if MonitorClass:
            return MonitorClass(get_platform_config(platform, cfg), current_app.config["DB_PATH"])
    except Exception as e:
        log.error("创建监控器失败: %s", e)
    return None
