"""
Maimai QR Login - Playwright opens login page, captures real QR code

Flow:
1. headless Chrome opens maimai.cn/platform/login
2. Intercept get-qr-code API to get qr_code token
3. Capture the QR code image on the page and return to frontend
4. Background thread keeps browser running, polls login-yrcode API to check scan status
5. Extract cookies after successful login
"""

import asyncio
import base64
import io
import json
import logging
import threading

import requests

log = logging.getLogger(__name__)

LOGIN_URL = "https://maimai.cn/platform/login"
QR_API = "https://maimai.cn/sdk/webs/platform/get-qr-code"
POLL_API = "https://maimai.cn/sdk/webs/platform/login-qrcode"

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class MaimaiQRLogin:
    def __init__(self):
        self.browser = None
        self.context = None
        self.page = None
        self._pw = None
        self._loop = None
        self._thread = None
        self._qr_code = ""
        self._logged_in_cookies = None
        self._last_rcode = None

    def _start_loop_thread(self):
        self._loop = asyncio.new_event_loop()

        def _run():
            asyncio.set_event_loop(self._loop)
            self._loop.run_forever()

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

    def _submit(self, coro):
        if self._loop is None or not self._loop.is_running():
            self._start_loop_thread()
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=60)

    async def _init_browser(self):
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.launch(
            executable_path="/usr/bin/google-chrome",
            headless=True,
            args=["--no-sandbox", "--disable-gpu"],
        )
        self.context = await self.browser.new_context(
            user_agent=UA,
            viewport={"width": 1280, "height": 800},
        )
        self.page = await self.context.new_page()

    def get_qrcode(self) -> dict:
        try:
            return self._submit(self._get_qrcode_async())
        except Exception as e:
            log.error("[MM] Failed to get QR code via Playwright: %s", e)
            self._submit(self._cleanup())
            return {"error": f"Failed to get QR code: {e}"}

    def check_scan(self, qrid: str = "") -> dict:
        try:
            return self._submit(self._check_login_async())
        except Exception as e:
            log.error("[MM] Failed to check scan status: %s", e)
            return {"status": "error", "message": str(e)}

    def close(self):
        try:
            if self._loop and self._loop.is_running():
                self._submit(self._cleanup())
                self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass

    async def _get_qrcode_async(self) -> dict:
        await self._init_browser()

        # Intercept API responses
        async def on_response(resp):
            url = resp.url
            try:
                if "get-qr-code" in url and resp.status == 200:
                    body = await resp.text()
                    data = json.loads(body)
                    if data.get("result") == "ok":
                        self._qr_code = data.get("qr_code", "")
                        log.info("[MM] qr_code=%s", self._qr_code)

                elif "login-qrcode" in url and resp.status == 200:
                    body = await resp.text()
                    data = json.loads(body)
                    rcode = data.get("rcode", 0)
                    self._last_rcode = rcode
                    log.info("[MM] login-yrcode rcode=%s", rcode)

                    if data.get("result") == "ok":
                        # Login successful
                        await self.page.wait_for_timeout(2000)
                        cookies = await self.context.cookies()
                        self._logged_in_cookies = "; ".join(
                            f"{c['name']}={c['value']}" for c in cookies
                        )
                        log.info("[MM] Login successful, cookies obtained")
            except Exception:
                pass

        self.page.on("response", lambda r: asyncio.ensure_future(on_response(r)))

        await self.page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=20000)
        # Wait for QR code to load
        await self.page.wait_for_timeout(4000)

        # Try clicking to switch to QR code mode (if currently in username/password mode)
        qr_switch = await self.page.query_selector(".p-login-switch.qrcode")
        if qr_switch:
            await qr_switch.click()
            await self.page.wait_for_timeout(2000)

        # Try to capture the QR code image on the page
        qr_el = (
            await self.page.query_selector("canvas")
            or await self.page.query_selector("img[src*='qr']")
            or await self.page.query_selector(".p-login-qrcode img")
            or await self.page.query_selector(".p-login-qrcode canvas")
        )

        if qr_el:
            screenshot = await qr_el.screenshot(type="png")
            b64 = base64.b64encode(screenshot).decode("ascii")
            return {"qr_image": f"data:image/png;base64,{b64}", "qrid": self._qr_code}

        # Fallback: screenshot the entire login area
        login_box = await self.page.query_selector(".p-login-qrcode-box")
        if login_box:
            screenshot = await login_box.screenshot(type="png")
            b64 = base64.b64encode(screenshot).decode("ascii")
            return {"qr_image": f"data:image/png;base64,{b64}", "qrid": self._qr_code}

        # Final fallback: if qr_code token was obtained, use requests to poll
        if self._qr_code:
            log.warning("[MM] QR code image not captured, but qr_code token available")
            return {"qrid": self._qr_code, "message": "QR code retrieval failed, please retry"}

        return {"error": "QR code element not found"}

    async def _check_login_async(self) -> dict:
        # 1. Interceptor has captured successful login
        if self._logged_in_cookies:
            cookies = self._logged_in_cookies
            await self._cleanup()
            return {"status": "success", "cookies": cookies}

        # 2. Check if page has navigated away from login page (browser auto-redirects after login)
        if self.page and "/login" not in self.page.url:
            await asyncio.sleep(2)
            try:
                cookies = await self.context.cookies()
                if cookies:
                    cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
                    log.info("[MM] Page redirected, cookies extracted successfully")
                    await self._cleanup()
                    return {"status": "success", "cookies": cookie_str}
            except Exception:
                pass

        # 3. Wait for the next JS poll cycle
        await asyncio.sleep(3)

        # Check once more
        if self._logged_in_cookies:
            cookies = self._logged_in_cookies
            await self._cleanup()
            return {"status": "success", "cookies": cookies}

        if self.page and "/login" not in self.page.url:
            await asyncio.sleep(1)
            try:
                cookies = await self.context.cookies()
                if cookies:
                    cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
                    await self._cleanup()
                    return {"status": "success", "cookies": cookie_str}
            except Exception:
                pass

        # 4. Status code check
        if self._last_rcode == -11060004:
            await self._cleanup()
            return {"status": "expired", "message": "QR code expired, please get a new one"}

        if self._last_rcode == -11060006:
            return {"status": "scanned", "message": "Scanned, please confirm on your phone..."}

        return {"status": "waiting", "message": "Waiting for scan..."}

    async def _cleanup(self):
        try:
            if self.browser:
                await self.browser.close()
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self.browser = None
        self.context = None
        self.page = None
