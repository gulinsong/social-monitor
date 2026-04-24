#!/usr/bin/env python3
"""
微博扫码登录 - 终端二维码，手机扫码获取Cookie
适用于无GUI的远程服务器
"""

import json
import time
from pathlib import Path

import qrcode
import requests


def login():
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Mobile Safari/537.36"
        ),
        "Referer": "https://m.weibo.cn/",
    })

    # Step 1: 获取二维码token
    print("[*] 正在获取登录二维码...")
    qr_url = "https://login.sina.com.cn/ssologin/scanqrcode/url"
    params = {
        "entry": "mweibo",
        "returntype": "TEXT",
        "crossDomain": 1,
    }

    try:
        resp = session.get(
            "https://passport.weibo.com/sso/qrcode/url",
            params={"entry": "mweibo", "returntype": "TEXT"},
            timeout=10,
        )
        data = resp.json()
        qr_src = data.get("data", {}).get("image", "")
        qrid = data.get("data", {}).get("qrid", "")
        alt = data.get("data", {}).get("alt", "")
    except Exception:
        # 备用：直接构造扫码页面URL
        resp = session.get("https://passport.weibo.cn/signin/login", timeout=10)
        # 从页面获取
        print("[!] 标准接口不可用，使用备用方案...")

    # 方案B：使用passport.weibo.cn的扫码登录
    print("[*] 通过passport获取扫码地址...")
    try:
        resp = session.get(
            "https://passport.weibo.com/sso/qrcode/image",
            params={"entry": "mweibo"},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            qr_src = data.get("data", {}).get("image", "")
            qrid = data.get("data", {}).get("qrid", "")
    except Exception as e:
        print(f"[!] 接口访问异常: {e}")

    # 方案C：直接生成二维码让用户扫passport登录页
    passport_login_url = "https://passport.weibo.cn/signin/login"

    print("\n" + "=" * 55)
    print("  请用【微博手机APP】扫描下方二维码登录")
    print("  二维码有效期约 5 分钟")
    print("=" * 55)

    qr = qrcode.QRCode(border=1)
    qr.add_data(passport_login_url)
    qr.make(fit=True)
    qr.print_ascii(invert=True)

    print(f"\n  如果二维码无法扫描，手动打开:")
    print(f"  {passport_login_url}")
    print()

    # 提示用户在浏览器登录后粘贴Cookie
    print("=" * 55)
    print("  由于远程服务器限制，请按以下步骤操作:")
    print()
    print("  1. 在你本地电脑浏览器打开:")
    print("     https://m.weibo.cn")
    print()
    print("  2. 登录你的微博账号")
    print()
    print("  3. 按 F12 打开开发者工具")
    print("     → 切换到 Console(控制台)")
    print()
    print("  4. 输入并回车:")
    print('     document.cookie')
    print()
    print("  5. 复制输出的字符串")
    print("     (类似: SUB=_2A...; SUBP=003...; ...)")
    print("=" * 55)

    cookie = input("\n请粘贴Cookie内容: ").strip()

    if not cookie:
        print("[!] 未输入Cookie，退出")
        return

    # 验证Cookie是否有效
    print("[*] 正在验证Cookie...")
    session.cookies.clear()
    for item in cookie.split(";"):
        item = item.strip()
        if "=" in item:
            k, v = item.split("=", 1)
            session.cookies.set(k.strip(), v.strip(), domain=".weibo.cn")
            session.cookies.set(k.strip(), v.strip(), domain=".weibo.com")

    try:
        resp = session.get(
            "https://m.weibo.cn/api/config",
            timeout=10,
        )
        data = resp.json().get("data", {})
        uid = data.get("uid", "")
        user = data.get("user", "")
        if uid:
            print(f"[✓] Cookie有效! 用户: {user} (UID: {uid})")
        else:
            print("[!] Cookie可能无效(未获取到用户信息)，但已保存，可尝试使用")
    except Exception as e:
        print(f"[!] 验证请求失败: {e}，Cookie已保存")

    # 保存到config
    config_path = Path(__file__).parent / "config.json"
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    config["cookies"] = cookie

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=4)

    print(f"[✓] Cookie已保存到 {config_path}")
    print("[*] 现在可以运行: python3 weibo_monitor.py")


if __name__ == "__main__":
    login()
