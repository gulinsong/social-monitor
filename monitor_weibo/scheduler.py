#!/usr/bin/env python3
"""
定时调度器 - 按配置间隔执行爬取任务
也可以用系统 crontab 替代
"""

import signal
import sys
import time
from datetime import datetime

from weibo_monitor import WeiboMonitor, load_config


running = True


def signal_handler(sig, frame):
    global running
    print("\n[*] 收到停止信号，优雅退出...")
    running = False


def run_scheduler():
    global running

    config = load_config()
    interval = config["interval_hours"] * 3600

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    monitor = WeiboMonitor(config)
    print(f"[*] 调度器启动，间隔 {config['interval_hours']} 小时")
    print(f"[*] 监控关键词: {', '.join(config['keywords'])}")
    print(f"[*] 按 Ctrl+C 停止\n")

    while running:
        try:
            monitor.run_once()
        except Exception as e:
            print(f"[!] 爬取出错: {e}")

        next_time = datetime.now().timestamp() + interval
        next_str = datetime.fromtimestamp(next_time).strftime("%Y-%m-%d %H:%M:%S")
        print(f"[*] 下次爬取: {next_str}")

        # 可中断的等待
        for _ in range(int(interval)):
            if not running:
                break
            time.sleep(1)

    print("[*] 调度器已停止")


if __name__ == "__main__":
    run_scheduler()
