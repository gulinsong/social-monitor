#!/usr/bin/env python3
"""
WeChat Official Account Article Monitor - Dual data source (Sogou + WeRead)
Keyword: target keyword
Sogou frequency: every 6 hours
WeRead: higher frequency possible (official API)
"""

import json
import time
import random
import hashlib
import os
import re
import stat
import logging
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# -- Configuration ───────────────────────────────────────────
KEYWORD = "target-keyword"
BASE_URL = "https://weixin.sogou.com/weixin"
DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATA_FILE = DATA_DIR / "articles.json"
LOG_FILE = DATA_DIR / "monitor.log"
USER_AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# -- Security: Input Validation ────────────────────────────────

def validate_keyword(keyword: str) -> str:
    """Validate and sanitize search keyword"""
    keyword = keyword.strip()
    if not keyword:
        raise ValueError("Keyword cannot be empty")
    if len(keyword) > 50:
        raise ValueError(f"Keyword too long: {len(keyword)} > 50")
    # Remove potentially dangerous characters
    keyword = re.sub(r'[<>"\']', "", keyword)
    return keyword


def validate_url(url: str) -> bool:
    """URL whitelist validation"""
    ALLOWED_HOSTS = ("mp.weixin.qq.com", "weread.qq.com", "weixin.sogou.com")
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc.lower()
        return any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)
    except Exception:
        return False


def set_secure_permissions():
    """Set secure permissions for data directory and files"""
    os.chmod(DATA_DIR, stat.S_IRWXU)  # 0700 - owner only
    for f in DATA_DIR.iterdir():
        if f.name.startswith("."):
            os.chmod(f, stat.S_IRUSR | stat.S_IWUSR)  # 0600


def ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not DATA_FILE.exists():
        DATA_FILE.write_text("{}", encoding="utf-8")


def load_articles() -> dict:
    """Load saved article records {url_hash: {title, url, date, digest}}"""
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, FileNotFoundError):
        return {}


def save_articles(articles: dict):
    # Sort by pub_time or found_at before saving, newest first
    def sort_key(item):
        _, a = item
        # Prefer pub_time, fall back to found_at
        t = a.get("pub_time") or a.get("found_at") or ""
        return t

    sorted_articles = dict(sorted(articles.items(), key=sort_key, reverse=True))
    DATA_FILE.write_text(
        json.dumps(sorted_articles, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "Referer": "https://weixin.sogou.com/",
        }
    )
    # SUGER (Sogou anti-crawl cookie) - auto-generated
    s.cookies.set("SUID", hashlib.md5(str(time.time()).encode()).hexdigest()[:16], domain=".sogou.com")
    return s


def search_sogou(session: requests.Session, keyword: str, page: int = 1) -> list[dict]:
    """Get article list from Sogou WeChat search"""
    params = {
        "type": "2",          # 2 = search articles
        "query": keyword,
        "page": str(page),
        "ie": "utf8",
    }

    try:
        resp = session.get(BASE_URL, params=params, timeout=15)
        resp.raise_for_status()
        resp.encoding = "utf-8"
    except requests.RequestException as e:
        log.error("Sogou request failed: %s", e)
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    results = []

    # Sogou WeChat search result page structure
    for news_box in soup.select("div.news-box, ul.news-list li, div.news-list li"):
        title_tag = news_box.select_one("h3 a, div.txt-box h3 a")
        if not title_tag:
            continue

        title = title_tag.get_text(strip=True)
        url = title_tag.get("href", "")

        # Digest
        digest_tag = news_box.select_one("p.txt-info, div.txt-info")
        digest = digest_tag.get_text(strip=True) if digest_tag else ""

        # Source account and time
        account_tag = news_box.select_one("a.account, span.s2 a, div.s-p a")
        account = account_tag.get_text(strip=True) if account_tag else ""

        # Try multiple selectors to extract publish time
        pub_time = ""
        # Prefer extracting timestamp from script tag (Sogou uses document.write(timeConvert('timestamp')))
        script_tag = news_box.select_one("div.s-p script")
        if script_tag and script_tag.string:
            m = re.search(r"timeConvert\('(\d+)'\)", script_tag.string)
            if m:
                ts = int(m.group(1))
                pub_time = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")

        # If script didn't work, try other selectors
        if not pub_time:
            for selector in [
                "span.s2",            # Sogou legacy time label
                "span.time",          # Generic time label
                "div.s-p span",       # Sogou footer time
                "span.wx-time",       # WeChat time
            ]:
                time_tag = news_box.select_one(selector)
                if time_tag:
                    text = time_tag.get_text(strip=True)
                    if text:
                        pub_time = text
                        break

        # If still no time, try regex matching dates from the entire news_box text
        if not pub_time:
            box_text = news_box.get_text()
            m = re.search(r"(\d{4}[-年]\d{1,2}[-月]\d{1,2}日?)", box_text)  # Match Chinese date formats
            if m:
                pub_time = m.group(1)
            else:
                m = re.search(r"(\d+[天小时分钟秒]+前)", box_text)  # Match relative time in Chinese
                if m:
                    pub_time = m.group(1)
                else:
                    m = re.search(r"(昨天|前天|今天|\d+天前)", box_text)  # Match yesterday/today in Chinese
                    if m:
                        pub_time = m.group(1)

        if title and url:
            results.append(
                {
                    "title": title,
                    "url": url,
                    "digest": digest[:200],
                    "account": account,
                    "pub_time": pub_time,
                }
            )

    log.info("Sogou search '%s' page %d got %d results", keyword, page, len(results))
    return results


def dedupe_url(url: str) -> str:
    """Extract key part of URL for deduplication"""
    if "mp.weixin.qq.com" in url:
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        sn = params.get("sn", [None])[0]
        if sn:
            return sn
    return hashlib.md5(url.encode()).hexdigest()[:12]


def run_sogou(keyword: str = KEYWORD, max_page: int = 5) -> list[dict]:
    """Sogou data source: search articles by keyword"""
    keyword = validate_keyword(keyword)
    log.info("[Sogou] Starting search keyword: %s, pages: %d", keyword, max_page)

    session = make_session()
    sogou_articles = []

    for page in range(1, max_page + 1):
        results = search_sogou(session, keyword, page)
        for article in results:
            article["url_key"] = dedupe_url(article.get("url", ""))
            article["found_at"] = datetime.now().isoformat()
            # URL validation
            url = article.get("url", "")
            if url and not validate_url(url) and "weixin.sogou.com" not in url:
                # Sogou links redirect to WeChat, keep them
                article["url_validated"] = False
            else:
                article["url_validated"] = True
            sogou_articles.append(article)
        # Polite delay
        if page < max_page:
            time.sleep(random.uniform(3, 6))

    log.info("[Sogou] Got %d articles total", len(sogou_articles))
    return sogou_articles


def run_weread(keyword: str = KEYWORD) -> list[dict]:
    """WeRead data source: get articles from subscribed accounts (non-interactive, requires prior login)"""
    try:
        from weread_client import WeReadClient, load_account
    except ImportError:
        log.warning("[WeRead] Cannot import weread_client, skipping WeRead source")
        return []

    # Check if there is a valid token, skip if none (avoid blocking for QR scan)
    account = load_account()
    if not account or not account.get("token"):
        log.info("[WeRead] Not logged in, skipping. Run 'python3 weread_client.py login' to log in via QR code")
        return []

    client = WeReadClient()
    try:
        articles = client.fetch_all_subscribed(keyword=keyword)
    except Exception as e:
        log.error("[WeRead] Failed to fetch articles: %s", e)
        return []

    # Mark found_at
    for a in articles:
        a["found_at"] = datetime.now().isoformat()
        a["url_key"] = dedupe_url(a.get("url", ""))

    log.info("[WeRead] Got %d articles total", len(articles))
    return articles


def run():
    """Main run function: dual-source crawl + merge"""
    log.info("=" * 50)
    log.info("Starting dual-source WeChat article crawl, keyword: %s", KEYWORD)
    ensure_data_dir()
    set_secure_permissions()

    existing = load_articles()

    # 1. Sogou source
    sogou_articles = run_sogou(KEYWORD)

    # 2. WeRead source
    weread_articles = run_weread(KEYWORD)

    # 3. Merge and deduplicate
    from merge import merge_results
    existing, all_new = merge_results(existing, sogou_articles, weread_articles)

    save_articles(existing)

    log.info("Found %d new articles (total %d articles)", len(all_new), len(existing))
    if all_new:
        for a in all_new:
            source_tag = a.get("source", "unknown")
            log.info("  [New|%s] %s - %s", source_tag, a.get("account", "Unknown"), a["title"])

    return all_new


if __name__ == "__main__":
    run()
