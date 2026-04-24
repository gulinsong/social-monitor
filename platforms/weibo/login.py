"""
微博扫码登录 — 通过 Playwright + Chrome 自动获取 Cookie

流程：
1. 后端启动 headless Chrome 打开微博登录页
2. 截取二维码图片返回给前端
3. 前端展示二维码，用户用微博 APP 扫码
4. 后端轮询检测登录成功后提取 Cookie
"""

import asyncio
import base64
import logging
import time

log = logging.getLogger(__name__)

LOGIN_URL = "https://passport.weibo.com/sso/signin?entry=miniblog&source=miniblog&url=https%3A%2F%2Fwww.weibo.com%2F"


class WeiboQRLogin:
    def __init__(self):
        self.browser = None
        self.page = None
        self._loop = None

    def _run(self, coro):
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
        return self._loop.run_until_complete(coro)

    async def _start_browser(self):
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.launch(
            executable_path="/usr/bin/google-chrome",
            headless=True,
            args=["--no-sandbox", "--disable-gpu"],
        )
        self.context = await self.browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        self.page = await self.context.new_page()

    async def _get_qrcode_async(self) -> dict:
        await self._start_browser()
        await self.page.goto(LOGIN_URL, wait_until="networkidle", timeout=20000)

        # 桌面端默认显示二维码，直接等待出现
        qr_el = None
        for selector in [
            "img[src*='qr.weibo.cn']",
            "img[src*='qrcode']",
            "img[src*='qr']",
            "canvas",
        ]:
            try:
                await self.page.wait_for_selector(selector, timeout=8000)
                qr_el = await self.page.query_selector(selector)
                if qr_el:
                    break
            except Exception:
                continue

        if qr_el:
            # 放大截图使二维码更清晰
            box = await qr_el.bounding_box()
            if box and box["width"] < 200:
                scale = 3
                new_w = int(box["width"] * scale)
                new_h = int(box["height"] * scale)
                screenshot = await qr_el.screenshot(type="png", scale="device")
                # 用 Pillow 放大
                import io
                from PIL import Image
                img = Image.open(io.BytesIO(screenshot))
                img = img.resize((new_w, new_h), Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                screenshot = buf.getvalue()
            else:
                screenshot = await qr_el.screenshot(type="png")
        else:
            screenshot = await self.page.screenshot(type="png")

        b64 = base64.b64encode(screenshot).decode("ascii")
        return {"qr_image": f"data:image/png;base64,{b64}", "qrid": "browser"}

    async def _check_login_async(self) -> dict:
        if not self.page:
            return {"status": "error", "message": "浏览器未启动"}

        current_url = self.page.url

        # 登录成功后会跳转到 weibo.com 或 weibo.cn
        if ("weibo.com" in current_url or "weibo.cn" in current_url) and "passport" not in current_url:
            cookies = await self.context.cookies()
            cookie_str = "; ".join(
                f"{c['name']}={c['value']}" for c in cookies
            )
            sub = next((c["value"] for c in cookies if c["name"] == "SUB"), "")
            if sub:
                await self._cleanup()
                return {"status": "success", "cookies": cookie_str}
            return {"status": "scanned", "message": "已扫码，等待跳转..."}

        # 检查页面是否有"扫码成功"提示
        try:
            el = await self.page.query_selector(".success, .scanned, [class*='success']")
            if el:
                text = await el.text_content()
                if text and "成功" in text:
                    return {"status": "scanned", "message": "已扫码，请确认..."}
        except Exception:
            pass

        return {"status": "waiting", "message": "等待扫码..."}

    async def _cleanup(self):
        try:
            if self.browser:
                await self.browser.close()
            if hasattr(self, "_pw") and self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self.browser = None
        self.page = None

    def get_qrcode(self) -> dict:
        try:
            return self._run(self._get_qrcode_async())
        except Exception as e:
            log.error("[微博] Playwright 获取二维码失败: %s", e)
            self._run(self._cleanup())
            return {"error": f"获取二维码失败: {e}"}

    def check_scan(self, qrid: str = "") -> dict:
        try:
            return self._run(self._check_login_async())
        except Exception as e:
            log.error("[微博] 检查扫码状态失败: %s", e)
            return {"status": "error", "message": str(e)}

    def close(self):
        try:
            self._run(self._cleanup())
        except Exception:
            pass
