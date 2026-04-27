"""Schedule Configuration API"""
import logging
from flask import Blueprint, jsonify, request, current_app
from core.config_loader import load_config, save_config, get_platform_config

log = logging.getLogger(__name__)
bp = Blueprint("config_api", __name__)


@bp.route("/api/config/platforms")
def get_platforms():
    cfg = current_app.config["MONITOR_CONFIG"]
    platforms = cfg.get("platforms", {})
    default_kw = cfg.get("default_keywords", [])
    result = {}
    for name, pcfg in platforms.items():
        result[name] = {
            "enabled": pcfg.get("enabled", False),
            "interval_hours": pcfg.get("interval_hours", 6),
            "keywords": pcfg.get("keywords", default_kw),
            "max_pages_per_keyword": pcfg.get("max_pages_per_keyword", 3),
            "max_comments_per_post": pcfg.get("max_comments_per_post", 20),
            "request_delay": pcfg.get("request_delay", {"min": 3.0, "max": 8.0}),
            "max_requests_per_hour": pcfg.get("max_requests_per_hour", 60),
            "source": pcfg.get("source", ""),
        }
    return jsonify(result)


@bp.route("/api/config/platforms/<platform>", methods=["PUT"])
def update_platform(platform):
    data = request.get_json()
    cfg = load_config()
    if platform not in cfg.get("platforms", {}):
        return jsonify({"error": f"未知平台: {platform}"}), 404

    pcfg = cfg["platforms"][platform]
    if "enabled" in data:
        pcfg["enabled"] = data["enabled"]
    if "interval_hours" in data:
        pcfg["interval_hours"] = data["interval_hours"]
    if "keywords" in data:
        pcfg["keywords"] = data["keywords"]
    if "request_delay" in data:
        pcfg["request_delay"] = data["request_delay"]
    if "source" in data:
        pcfg["source"] = data["source"]

    save_config(cfg)
    current_app.config["MONITOR_CONFIG"] = cfg

    # Hot-reload scheduler
    if hasattr(current_app, "scheduler") and current_app.scheduler:
        current_app.scheduler.reload_config()

    return jsonify({"status": "ok"})


@bp.route("/api/config/feishu", methods=["GET", "PUT"])
def feishu_config():
    cfg = load_config()
    if request.method == "PUT":
        data = request.get_json()
        fcfg = cfg.setdefault("feishu", {})
        if "enabled" in data:
            fcfg["enabled"] = data["enabled"]
        if "webhook_url" in data:
            fcfg["webhook_url"] = data["webhook_url"]
        if "sign_secret" in data:
            fcfg["sign_secret"] = data["sign_secret"]
        if "max_push_per_run" in data:
            fcfg["max_push_per_run"] = data["max_push_per_run"]
        if "bitable" in data:
            bcfg = fcfg.setdefault("bitable", {})
            for key in ("enabled", "app_id", "app_secret", "app_token", "table_id"):
                if key in data["bitable"]:
                    bcfg[key] = data["bitable"][key]
        save_config(cfg)
        current_app.config["MONITOR_CONFIG"] = cfg
        # Hot-reload scheduler
        if hasattr(current_app, "scheduler") and current_app.scheduler:
            current_app.scheduler.reload_config()
        return jsonify({"status": "ok"})
    # Mask app_secret in GET response
    result = dict(cfg.get("feishu", {}))
    if "bitable" in result and result["bitable"].get("app_secret"):
        result["bitable"]["app_secret"] = result["bitable"]["app_secret"][:4] + "****"
    return jsonify(result)


@bp.route("/api/config/keywords", methods=["GET", "PUT"])
def global_keywords():
    cfg = load_config()
    if request.method == "PUT":
        data = request.get_json()
        if "keywords" in data:
            cfg["default_keywords"] = data["keywords"]
            save_config(cfg)
            current_app.config["MONITOR_CONFIG"] = cfg
        return jsonify({"status": "ok"})
    return jsonify({"keywords": cfg.get("default_keywords", [])})
