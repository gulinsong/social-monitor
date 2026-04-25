#!/usr/bin/env python3
"""
Social Media Monitoring System - Entry Point
"""

import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from core.config_loader import load_config
from db.schema import init_db


def setup_logging(log_dir: str):
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(Path(log_dir) / "monitor.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def run_scheduler_only(config: dict):
    from core.scheduler import UnifiedScheduler
    scheduler = UnifiedScheduler(config)

    def stop(sig, frame):
        scheduler.stop()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    scheduler.run()


def run_web_only(config: dict):
    from web.app import create_app
    app = create_app(config)
    app.run(
        host=config.get("app", {}).get("host", "0.0.0.0"),
        port=config.get("app", {}).get("port", 5000),
        debug=False,
    )


def run_all(config: dict):
    from core.scheduler import UnifiedScheduler
    from web.app import create_app

    scheduler = UnifiedScheduler(config)
    scheduler_thread = threading.Thread(target=scheduler.run, daemon=True)
    scheduler_thread.start()

    app = create_app(config)
    app.scheduler = scheduler

    def stop(sig, frame):
        scheduler.stop()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    app.run(
        host=config.get("app", {}).get("host", "0.0.0.0"),
        port=config.get("app", {}).get("port", 5000),
        debug=False,
    )


def run_test(config: dict):
    """Quick test: run all enabled platform crawls once"""
    from core.scheduler import UnifiedScheduler
    scheduler = UnifiedScheduler(config)
    for job in scheduler.jobs:
        scheduler._execute_job(job)
    print("[Test completed]")


def main():
    parser = argparse.ArgumentParser(description="Social Media Monitoring System")
    parser.add_argument("--config", default=None, help="Config file path")
    parser.add_argument("--web", action="store_true", help="Start web UI only")
    parser.add_argument("--scheduler", action="store_true", help="Start scheduler only")
    parser.add_argument("--test", action="store_true", help="Test mode: run once")
    parser.add_argument("--migrate", action="store_true", help="Run database migration")
    args = parser.parse_args()

    config = load_config(args.config)
    log_dir = config.get("app", {}).get("log_dir", "logs")
    setup_logging(log_dir)

    db_path = config.get("app", {}).get("db_path", "db/monitor.db")
    init_db(db_path)

    if args.migrate:
        from db.migrate import run_migration
        run_migration()
        return

    if args.test:
        run_test(config)
    elif args.web:
        run_web_only(config)
    elif args.scheduler:
        run_scheduler_only(config)
    else:
        run_all(config)


if __name__ == "__main__":
    main()
