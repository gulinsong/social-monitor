"""
Flask Web 应用
"""

import functools
import logging
from datetime import datetime

from flask import Flask, render_template, request, redirect, url_for, session, jsonify

from core.config_loader import load_config, save_config, get_platform_config
from db.schema import get_connection

log = logging.getLogger(__name__)


def create_app(config: dict = None) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(__file__).replace("app.py", "") + "templates",
        static_folder=str(__file__).replace("app.py", "") + "static",
    )
    cfg = config or load_config()
    app.secret_key = cfg.get("app", {}).get("secret_key", "change-me")
    app.config["MONITOR_CONFIG"] = cfg
    app.config["DB_PATH"] = cfg.get("app", {}).get("db_path", "db/monitor.db")
    app.scheduler = None

    # 注册蓝图
    from web.api.dashboard import bp as dashboard_bp
    from web.api.auth import bp as auth_bp
    from web.api.data import bp as data_bp
    from web.api.analysis import bp as analysis_bp
    from web.api.config_api import bp as config_bp

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(data_bp)
    app.register_blueprint(analysis_bp)
    app.register_blueprint(config_bp)

    # 登录验证
    @app.before_request
    def check_login():
        if request.endpoint in ("login_page", "login_submit", "static"):
            return None
        if not session.get("logged_in"):
            password = cfg.get("app", {}).get("password", "")
            if not password:
                return None  # 未设密码则不需要登录
            return redirect(url_for("login_page"))

    @app.route("/login", methods=["GET", "POST"])
    def login_page():
        if request.method == "POST":
            pwd = request.form.get("password", "")
            cfg_password = cfg.get("app", {}).get("password", "")
            if pwd == cfg_password:
                session["logged_in"] = True
                session.permanent = True
                if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                    return jsonify({"ok": True})
                return redirect(url_for("index"))
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return jsonify({"ok": False, "error": "密码错误"}), 401
            return render_template("login.html", error="密码错误")
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.pop("logged_in", None)
        return redirect(url_for("login_page"))

    @app.route("/")
    def index():
        return render_template("dashboard.html")

    @app.route("/login-manage")
    def login_manage():
        return render_template("login_manage.html")

    @app.route("/schedule")
    def schedule_page():
        return render_template("schedule.html")

    @app.route("/data")
    def data_page():
        return render_template("data.html")

    @app.route("/analysis")
    def analysis_page():
        return render_template("analysis.html")

    return app
