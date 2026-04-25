"""
Weibo QR Login - Automated cookie retrieval via Playwright + Chrome

Flow:
1. Backend launches headless Chrome and opens the Weibo login page
2. Captures QR code image and returns to frontend
3. Frontend displays QR code for user to scan with Weibo APP
4. Backend polls for successful login and extracts cookies
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

        # Desktop view shows QR code by default, wait for it to appear
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
            # Scale up screenshot for clearer QR code
            box = await qr_el.bounding_box()
            if box and box["width"] < 200:
                scale = 3
                new_w = int(box["width"] * scale)
                new_h = int(box["height"] * scale)
                screenshot = await qr_el.screenshot(type="png", scale="device")
                # Scale up with Pillow
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
            return {"status": "error", "message": "Browser not started"}

        current_url = self.page.url

        # After successful login, browser redirects to weibo.com or weibo.cn
        if ("weibo.com" in current_url or "weibo.cn" in current_url) and "passport" not in current_url:
            cookies = await self.context.cookies()
            cookie_str = "; ".join(
                f"{c['name']}={c['value']}" for c in cookies
            )
            sub = next((c["value"] for c in cookies if c["name"] == "SUB"), "")
            if sub:
                await self._cleanup()
                return {"status": "success", "cookies": cookie_str}
            return {"status": "scanned", "message": "Scanned, waiting for redirect..."}

        # Check if page shows a "scan successful" indicator
        try:
            el = await self.page.query_selector(".success, .scanned, [class*='success']")
            if el:
                text = await el.text_content()
                if text and "success" in text.lower():
                    return {"status": "scanned", "message": "Scanned, please confirm..."}
        except Exception:
            pass

        return {"status": "waiting", "message": "Waiting for scan..."}

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
            log.error("[Weibo] Failed to get QR code via Playwright: %s", e)
            self._run(self._cleanup())
            return {"error": f"Failed to get QR code: {e}"}

    def check_scan(self, qrid: str = "") -> dict:
        try:
            return self._run(self._check_login_async())
        except Exception as e:
            log.error("[Weibo] Failed to check scan status: %s", e)
            return {"status": "error", "message": str(e)}

    def close(self):
        try:
            self._run(self._cleanup())
        except Exception:
            pass
