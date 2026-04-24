"""
小红书监控 — 占位实现
Cookie 到位后实现具体爬取逻辑
"""

import logging

from core.base_monitor import BaseMonitor, CrawlResult

log = logging.getLogger(__name__)


class Monitor(BaseMonitor):
    PLATFORM_NAME = "xiaohongshu"

    def crawl(self, keyword: str, max_pages: int = 3) -> CrawlResult:
        log.info("[小红书] 暂未实现，跳过。请配置 Cookie 后再启用。")
        return CrawlResult()

    def verify_auth(self) -> bool:
        return False

    def get_comments(self, post_id: str, max_count: int = 20) -> list[dict]:
        return []

    def get_login_qrcode(self) -> dict:
        return {
            "qr_url": "https://passport.xiaohongshu.com/login",
            "uuid": "",
            "message": "请手动登录小红书后提供 Cookie (a1, webId, web_session)",
        }

    def check_login_status(self, uuid: str) -> dict:
        return {"status": "unsupported"}
