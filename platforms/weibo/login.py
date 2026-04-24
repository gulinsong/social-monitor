"""
微博登录 — 生成登录页二维码 + 手动输入Cookie

微博 passport API 需要浏览器级JS执行，纯HTTP请求难以自动获取Cookie。
策略：生成 weibo.com 登录页面的二维码，用户扫码登录后，通过浏览器F12复制Cookie。
"""

import logging

log = logging.getLogger(__name__)

LOGIN_URL = "https://weibo.com/login.php"


class WeiboQRLogin:
    """微博登录辅助类 — 返回登录页二维码供用户扫码"""

    def get_qrcode(self) -> dict:
        return {
            "qr_url": LOGIN_URL,
            "manual": True,
            "message": "请用微博APP扫码登录，登录后在浏览器F12复制Cookie填入",
        }

    def check_scan(self, qrid: str) -> dict:
        return {"status": "waiting", "message": "该平台需手动输入Cookie"}
