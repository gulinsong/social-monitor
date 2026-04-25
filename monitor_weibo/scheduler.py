#!/usr/bin/env python3
"""
Scheduled Task Runner - Executes crawl tasks at configured intervals
Can also be replaced by system crontab
"""

import signal
import sys
import time
from datetime import datetime

from weibo_monitor import WeiboMonitor, load_config


running = True


def signal_handler(sig, frame):
    global running
    print("\n[*] Stop signal received, graceful shutdown...")
    running = False


def run_scheduler():
    global running

    config = load_config()
    interval = config["interval_hours"] * 3600

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    monitor = WeiboMonitor(config)
    print(f"[*] Scheduler started, interval: {config['interval_hours']} hours")
    print(f"[*] Monitoring keywords: {', '.join(config['keywords'])}")
    print(f"[*] Press Ctrl+C to stop\n")

    while running:
        try:
            monitor.run_once()
        except Exception as e:
            print(f"[!] Crawl error: {e}")

        next_time = datetime.now().timestamp() + interval
        next_str = datetime.fromtimestamp(next_time).strftime("%Y-%m-%d %H:%M:%S")
        print(f"[*] Next crawl: {next_str}")

        # Interruptible wait
        for _ in range(int(interval)):
            if not running:
                break
            time.sleep(1)

    print("[*] Scheduler stopped")


if __name__ == "__main__":
    run_scheduler()
