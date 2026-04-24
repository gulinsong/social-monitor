"""
XHS 扫码登录 — Playwright + Chrome 自动获取 Cookie

流程：
1. headless Chrome 打开登录页
2. 页面自动创建二维码并轮询扫码状态
3. 截取二维码图片返回给前端
4. 后台线程持续运行 event loop，保持 JS 轮询
5. 读取拦截到的 codeStatus 判断登录状态
"""

import asyncio
import base64
import io
import json
import logging
import threading
import time

log = logging.getLogger(__name__)

LOGIN_URL = "https://www.xiaohongshu.com/login"


class XhsQRLogin:
    def __init__(self):
        self.browser = None
        self.context = None
        self.page = None
        self._loop = None
        self._thread = None
        self._qr_id = ""
        self._qr_code = ""
        self._last_code_status = None
        self._logged_in_cookies = None
        self._ready = threading.Event()

    def _start_loop_thread(self):
        """启动后台线程持续运行 event loop"""
        self._loop = asyncio.new_event_loop()

        def _run():
            asyncio.set_event_loop(self._loop)
            self._loop.run_forever()

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

    def _submit(self, coro):
        """向后台 event loop 提交协程并等待结果"""
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
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        self.page = await self.context.new_page()

    def get_qrcode(self) -> dict:
        try:
            return self._submit(self._get_qrcode_async())
        except Exception as e:
            log.error("[XHS] Playwright 获取二维码失败: %s", e)
            self._submit(self._cleanup())
            return {"error": f"获取二维码失败: {e}"}

    def check_scan(self, qrid: str = "") -> dict:
        try:
            return self._submit(self._check_login_async())
        except Exception as e:
            log.error("[XHS] 检查扫码状态失败: %s", e)
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

        # 拦截 edith API 响应
        async def on_response(resp):
            url = resp.url
            try:
                if "qrcode/create" in url and resp.status == 200:
                    body = await resp.text()
                    data = json.loads(body)
                    self._qr_id = data.get("data", {}).get("qr_id", "")
                    self._qr_code = data.get("data", {}).get("code", "")
                    log.info("[XHS] qr_id=%s, code=%s", self._qr_id, self._qr_code)

                elif "qrcode/userinfo" in url and resp.status == 200:
                    body = await resp.text()
                    data = json.loads(body)
                    status = data.get("data", {}).get("codeStatus", 0)
                    self._last_code_status = status
                    log.info("[XHS] codeStatus=%s", status)

                    if status == 2:
                        # 登录成功，等待页面完成跳转并设置新 cookie
                        await self.page.wait_for_timeout(3000)
                        # 等待页面跳转到非登录页
                        try:
                            await self.page.wait_for_url(
                                lambda url: "login" not in url, timeout=10000
                            )
                        except Exception:
                            pass
                        await self.page.wait_for_timeout(2000)
                        cookies = await self.context.cookies()
                        self._logged_in_cookies = "; ".join(
                            f"{c['name']}={c['value']}" for c in cookies
                        )
            except Exception:
                pass

        self.page.on("response", lambda r: asyncio.ensure_future(on_response(r)))

        await self.page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=20000)
        await self.page.wait_for_timeout(4000)

        # 获取二维码图片
        qr_el = await self.page.query_selector("img.qrcode-img")
        if not qr_el:
            qr_el = await self.page.query_selector("div[class*='qrcode'] img")

        if qr_el:
            src = await qr_el.get_attribute("src") or ""
            if src.startswith("data:image"):
                raw_b64 = src.replace("data:image/png;base64,", "")
                raw = base64.b64decode(raw_b64)
                from PIL import Image
                img = Image.open(io.BytesIO(raw))
                if img.size[0] < 256:
                    img = img.resize((384, 384), Image.LANCZOS)
                    buf = io.BytesIO()
                    img.save(buf, format="PNG")
                    raw_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
                    return {"qr_image": f"data:image/png;base64,{raw_b64}", "qrid": "browser"}
                return {"qr_image": src, "qrid": "browser"}

            screenshot = await qr_el.screenshot(type="png")
            from PIL import Image
            img = Image.open(io.BytesIO(screenshot))
            if img.size[0] < 256:
                img = img.resize((384, 384), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            return {"qr_image": f"data:image/png;base64,{b64}", "qrid": "browser"}

        screenshot = await self.page.screenshot(type="png")
        b64 = base64.b64encode(screenshot).decode("ascii")
        return {"qr_image": f"data:image/png;base64,{b64}", "qrid": "browser"}

    async def _check_login_async(self) -> dict:
        if not self.page:
            return {"status": "error", "message": "浏览器未启动"}

        # 检查是否已拦截到登录成功
        if self._logged_in_cookies:
            cookies = self._logged_in_cookies
            await self._cleanup()
            return {"status": "success", "cookies": cookies}

        # 等待页面 JS 的下一次轮询（约 2-3 秒间隔）
        await asyncio.sleep(3)

        if self._logged_in_cookies:
            cookies = self._logged_in_cookies
            await self._cleanup()
            return {"status": "success", "cookies": cookies}

        if self._last_code_status == 1:
            return {"status": "scanned", "message": "已扫码，请在手机上确认..."}

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
        self.context = None
        self.page = None
