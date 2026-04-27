"""
Flask Web Application
"""

import functools
import logging
import time
from collections import defaultdict
from datetime import datetime

from flask import Flask, render_template, request, redirect, url_for, session, jsonify

from core.config_loader import load_config, save_config, get_platform_config
from db.schema import get_connection

log = logging.getLogger(__name__)


class InMemoryRateLimiter:
    """Simple sliding-window rate limiter per IP"""

    def __init__(self, max_requests: int = 60, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, list[float]] = defaultdict(list)

    def is_allowed(self, key: str) -> bool:
        now = time.time()
        cutoff = now - self.window_seconds
        hits = self._hits[key]
        # prune old hits
        while hits and hits[0] < cutoff:
            hits.pop(0)
        if len(hits) >= self.max_requests:
            return False
        hits.append(now)
        return True


def create_app(config: dict = None) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(__file__).replace("app.py", "") + "templates",
        static_folder=str(__file__).replace("app.py", "") + "static",
    )
    cfg = config or load_config()
    app.secret_key = cfg.get("app", {}).get("secret_key", "change-me")
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["PERMANENT_SESSION_LIFETIME"] = 86400  # 24 hours
    app.config["MONITOR_CONFIG"] = cfg
    app.config["DB_PATH"] = cfg.get("app", {}).get("db_path", "db/monitor.db")
    app.scheduler = None

    _api_limiter = InMemoryRateLimiter(max_requests=60, window_seconds=60)

    # Register blueprints
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

    @app.route("/health")
    def health():
        db_ok = False
        try:
            conn = get_connection(app.config["DB_PATH"])
            conn.execute("SELECT 1")
            conn.close()
            db_ok = True
        except Exception:
            pass
        return jsonify({"status": "ok" if db_ok else "degraded", "db": db_ok}), 200 if db_ok else 503

    # Login verification + rate limiting
    @app.before_request
    def check_login():
        # Rate limit API endpoints
        if request.path.startswith("/api/"):
            client_ip = request.headers.get("X-Forwarded-For", request.remote_addr)
            if not _api_limiter.is_allowed(client_ip):
                return jsonify({"error": "Too many requests"}), 429
        if request.endpoint in ("login_page", "login_submit", "static", "health"):
            return None
        if not session.get("logged_in"):
            password = cfg.get("app", {}).get("password", "")
            if not password:
                return None  # No password set, login not required
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

    @app.route("/logout", methods=["POST"])
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
