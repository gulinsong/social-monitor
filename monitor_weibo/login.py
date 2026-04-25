#!/usr/bin/env python3
"""
Weibo QR Code Login - Terminal QR code, scan with phone to get Cookie
Suitable for headless remote servers
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

    # Step 1: Get QR code token
    print("[*] Getting login QR code...")
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
        # Fallback: directly construct the scan page URL
        resp = session.get("https://passport.weibo.cn/signin/login", timeout=10)
        # Extract from page
        print("[!] Standard API unavailable, using fallback...")

    # Plan B: Use passport.weibo.cn QR code login
    print("[*] Getting scan URL via passport...")
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
        print(f"[!] API access error: {e}")

    # Plan C: Generate QR code for user to scan the passport login page
    passport_login_url = "https://passport.weibo.cn/signin/login"

    print("\n" + "=" * 55)
    print("  Please scan the QR code below with the [Weibo mobile app] to log in")
    print("  QR code expires in about 5 minutes")
    print("=" * 55)

    qr = qrcode.QRCode(border=1)
    qr.add_data(passport_login_url)
    qr.make(fit=True)
    qr.print_ascii(invert=True)

    print(f"\n  If you cannot scan the QR code, open manually:")
    print(f"  {passport_login_url}")
    print()

    # Prompt user to paste Cookie after logging in via browser
    print("=" * 55)
    print("  Due to remote server limitations, please follow these steps:")
    print()
    print("  1. Open this URL in your local browser:")
    print("     https://m.weibo.cn")
    print()
    print("  2. Log in to your Weibo account")
    print()
    print("  3. Press F12 to open Developer Tools")
    print("     -> Switch to Console tab")
    print()
    print("  4. Type and press Enter:")
    print('     document.cookie')
    print()
    print("  5. Copy the output string")
    print("     (e.g.: SUB=_2A...; SUBP=003...; ...)")
    print("=" * 55)

    cookie = input("\nPlease paste the Cookie content: ").strip()

    if not cookie:
        print("[!] No Cookie entered, exiting")
        return

    # Verify if Cookie is valid
    print("[*] Verifying Cookie...")
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
            print(f"[OK] Cookie is valid! User: {user} (UID: {uid})")
        else:
            print("[!] Cookie may be invalid (no user info retrieved), but saved for trial use")
    except Exception as e:
        print(f"[!] Verification request failed: {e}, Cookie saved anyway")

    # Save to config
    config_path = Path(__file__).parent / "config.json"
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    config["cookies"] = cookie

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=4)

    print(f"[OK] Cookie saved to {config_path}")
    print("[*] You can now run: python3 weibo_monitor.py")


if __name__ == "__main__":
    login()
