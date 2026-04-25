"""
脉脉扫码登录 — Playwright 打开登录页，截取真实二维码

流程：
1. headless Chrome 打开 maimai.cn/platform/login
2. 拦截 get-qr-code API 获取 qr_code token
3. 截取页面上的二维码图片返回给前端
4. 后台线程保持浏览器运行，轮询 login-yrcode API 检查扫码状态
5. 登录成功后提取 Cookie
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
            log.error("[MM] Playwright 获取二维码失败: %s", e)
            self._submit(self._cleanup())
            return {"error": f"获取二维码失败: {e}"}

    def check_scan(self, qrid: str = "") -> dict:
        try:
            return self._submit(self._check_login_async())
        except Exception as e:
            log.error("[MM] 检查扫码状态失败: %s", e)
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

        # 拦截 API 响应
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
                        # 登录成功
                        await self.page.wait_for_timeout(2000)
                        cookies = await self.context.cookies()
                        self._logged_in_cookies = "; ".join(
                            f"{c['name']}={c['value']}" for c in cookies
                        )
                        log.info("[MM] 登录成功，获取到 Cookie")
            except Exception:
                pass

        self.page.on("response", lambda r: asyncio.ensure_future(on_response(r)))

        await self.page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=20000)
        # 等待二维码加载
        await self.page.wait_for_timeout(4000)

        # 先尝试点击切换到二维码模式（如果当前是账号密码模式）
        qr_switch = await self.page.query_selector(".p-login-switch.qrcode")
        if qr_switch:
            await qr_switch.click()
            await self.page.wait_for_timeout(2000)

        # 尝试截取页面上的二维码图片
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

        # fallback: 截取整个登录区域
        login_box = await self.page.query_selector(".p-login-qrcode-box")
        if login_box:
            screenshot = await login_box.screenshot(type="png")
            b64 = base64.b64encode(screenshot).decode("ascii")
            return {"qr_image": f"data:image/png;base64,{b64}", "qrid": self._qr_code}

        # 最终 fallback: 如果拿到了 qr_code token，用 requests 帮轮询
        if self._qr_code:
            log.warning("[MM] 未截取到二维码图片，但有 qr_code token")
            return {"qrid": self._qr_code, "message": "二维码获取异常，请重试"}

        return {"error": "未找到二维码元素"}

    async def _check_login_async(self) -> dict:
        # 1. 拦截器已捕获登录成功
        if self._logged_in_cookies:
            cookies = self._logged_in_cookies
            await self._cleanup()
            return {"status": "success", "cookies": cookies}

        # 2. 检查页面是否已跳转离开登录页（登录成功后浏览器自动跳转）
        if self.page and "/login" not in self.page.url:
            await asyncio.sleep(2)
            try:
                cookies = await self.context.cookies()
                if cookies:
                    cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
                    log.info("[MM] 页面已跳转，提取 Cookie 成功")
                    await self._cleanup()
                    return {"status": "success", "cookies": cookie_str}
            except Exception:
                pass

        # 3. 等待页面 JS 下一次轮询
        await asyncio.sleep(3)

        # 再检查一次
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

        # 4. 状态码判断
        if self._last_rcode == -11060004:
            await self._cleanup()
            return {"status": "expired", "message": "二维码已过期，请重新获取"}

        if self._last_rcode == -11060006:
            return {"status": "scanned", "message": "已扫码，请在手机上确认..."}

        return {"status": "waiting", "message": "等待扫码..."}

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
