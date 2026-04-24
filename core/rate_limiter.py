import random
import time
import logging

log = logging.getLogger(__name__)


class CircuitBreakerError(Exception):
    pass


class RateLimiter:
    def __init__(self, config: dict):
        delay_cfg = config.get("request_delay", {})
        self.min_delay = delay_cfg.get("min", 3.0)
        self.max_delay = delay_cfg.get("max", 8.0)
        self.max_per_hour = config.get("max_requests_per_hour", 60)
        self.request_times: list[float] = []
        self.consecutive_failures = 0
        self.max_failures = 5

    def wait(self):
        delay = random.uniform(self.min_delay, self.max_delay)
        delay *= random.gauss(1.0, 0.2)
        delay = max(delay, 1.0)

        if self.consecutive_failures > 0:
            delay *= 2 ** self.consecutive_failures
            log.warning("连续失败 %d 次，退避延迟 %.1f 秒", self.consecutive_failures, delay)

        # 每小时限额（滑动窗口）
        now = time.time()
        self.request_times = [t for t in self.request_times if now - t < 3600]
        if len(self.request_times) >= self.max_per_hour:
            wait_until = self.request_times[0] + 3600
            sleep_seconds = wait_until - now
            log.warning("已达每小时请求上限 %d，等待 %.0f 秒", self.max_per_hour, sleep_seconds)
            time.sleep(sleep_seconds)
            self.request_times = self.request_times[1:]

        time.sleep(delay)
        self.request_times.append(time.time())

    def record_success(self):
        self.consecutive_failures = 0

    def record_failure(self):
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.max_failures:
            raise CircuitBreakerError(
                f"连续失败 {self.consecutive_failures} 次，触发熔断"
            )
