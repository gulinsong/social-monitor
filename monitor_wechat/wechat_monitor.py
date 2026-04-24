#!/usr/bin/env python3
"""
微信公众号文章监控 - 双数据源（搜狗 + 微信读书）
关键词: 迪子
搜狗频率: 每6小时一次
微信读书: 可更高频率（官方API）
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

# ── 配置 ──────────────────────────────────────────────
KEYWORD = "迪子"
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

# ── 安全：输入验证 ─────────────────────────────────────

def validate_keyword(keyword: str) -> str:
    """验证并清理搜索关键词"""
    keyword = keyword.strip()
    if not keyword:
        raise ValueError("关键词不能为空")
    if len(keyword) > 50:
        raise ValueError(f"关键词过长: {len(keyword)} > 50")
    # 移除潜在危险字符
    keyword = re.sub(r'[<>"\']', "", keyword)
    return keyword


def validate_url(url: str) -> bool:
    """URL 白名单校验"""
    ALLOWED_HOSTS = ("mp.weixin.qq.com", "weread.qq.com", "weixin.sogou.com")
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc.lower()
        return any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)
    except Exception:
        return False


def set_secure_permissions():
    """设置数据目录和文件的安全权限"""
    os.chmod(DATA_DIR, stat.S_IRWXU)  # 0700 - 仅所有者
    for f in DATA_DIR.iterdir():
        if f.name.startswith("."):
            os.chmod(f, stat.S_IRUSR | stat.S_IWUSR)  # 0600


def ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not DATA_FILE.exists():
        DATA_FILE.write_text("{}", encoding="utf-8")


def load_articles() -> dict:
    """加载已保存的文章记录 {url_hash: {title, url, date, digest}}"""
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, FileNotFoundError):
        return {}


def save_articles(articles: dict):
    # 按 pub_time 或 found_at 排序后保存，最新的在最前面
    def sort_key(item):
        _, a = item
        # 优先用 pub_time，否则用 found_at
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
    # SUGER (搜狗反爬cookie) — 自动处理
    s.cookies.set("SUID", hashlib.md5(str(time.time()).encode()).hexdigest()[:16], domain=".sogou.com")
    return s


def search_sogou(session: requests.Session, keyword: str, page: int = 1) -> list[dict]:
    """从搜狗微信搜索获取文章列表"""
    params = {
        "type": "2",          # 2=搜文章
        "query": keyword,
        "page": str(page),
        "ie": "utf8",
    }

    try:
        resp = session.get(BASE_URL, params=params, timeout=15)
        resp.raise_for_status()
        resp.encoding = "utf-8"
    except requests.RequestException as e:
        log.error("请求搜狗失败: %s", e)
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    results = []

    # 搜狗微信搜索结果页面结构
    for news_box in soup.select("div.news-box, ul.news-list li, div.news-list li"):
        title_tag = news_box.select_one("h3 a, div.txt-box h3 a")
        if not title_tag:
            continue

        title = title_tag.get_text(strip=True)
        url = title_tag.get("href", "")

        # 摘要
        digest_tag = news_box.select_one("p.txt-info, div.txt-info")
        digest = digest_tag.get_text(strip=True) if digest_tag else ""

        # 来源公众号和时间
        account_tag = news_box.select_one("a.account, span.s2 a, div.s-p a")
        account = account_tag.get_text(strip=True) if account_tag else ""

        # 尝试多种选择器提取发布时间
        pub_time = ""
        # 优先从 script 标签提取时间戳（搜狗用 document.write(timeConvert('timestamp'))）
        script_tag = news_box.select_one("div.s-p script")
        if script_tag and script_tag.string:
            m = re.search(r"timeConvert\('(\d+)'\)", script_tag.string)
            if m:
                ts = int(m.group(1))
                pub_time = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")

        # script 没拿到就尝试其他选择器
        if not pub_time:
            for selector in [
                "span.s2",            # 搜狗旧版时间标签
                "span.time",          # 通用时间标签
                "div.s-p span",       # 搜狗底栏时间
                "span.wx-time",       # 微信时间
            ]:
                time_tag = news_box.select_one(selector)
                if time_tag:
                    text = time_tag.get_text(strip=True)
                    if text:
                        pub_time = text
                        break

        # 如果还没拿到时间，尝试从整个 news_box 文本中正则匹配日期
        if not pub_time:
            box_text = news_box.get_text()
            m = re.search(r"(\d{4}[-年]\d{1,2}[-月]\d{1,2}日?)", box_text)
            if m:
                pub_time = m.group(1)
            else:
                m = re.search(r"(\d+[天小时分钟秒]+前)", box_text)
                if m:
                    pub_time = m.group(1)
                else:
                    m = re.search(r"(昨天|前天|今天|\d+天前)", box_text)
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

    log.info("搜狗搜索 '%s' 第%d页获取到 %d 条结果", keyword, page, len(results))
    return results


def dedupe_url(url: str) -> str:
    """提取URL中的关键部分用于去重"""
    if "mp.weixin.qq.com" in url:
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        sn = params.get("sn", [None])[0]
        if sn:
            return sn
    return hashlib.md5(url.encode()).hexdigest()[:12]


def run_sogou(keyword: str = KEYWORD, max_page: int = 5) -> list[dict]:
    """搜狗数据源：按关键词搜索文章"""
    keyword = validate_keyword(keyword)
    log.info("[搜狗] 开始搜索关键词: %s, 页数: %d", keyword, max_page)

    session = make_session()
    sogou_articles = []

    for page in range(1, max_page + 1):
        results = search_sogou(session, keyword, page)
        for article in results:
            article["url_key"] = dedupe_url(article.get("url", ""))
            article["found_at"] = datetime.now().isoformat()
            # URL 校验
            url = article.get("url", "")
            if url and not validate_url(url) and "weixin.sogou.com" not in url:
                # 搜狗链接会跳转到微信，保留
                article["url_validated"] = False
            else:
                article["url_validated"] = True
            sogou_articles.append(article)
        # 礼貌延迟
        if page < max_page:
            time.sleep(random.uniform(3, 6))

    log.info("[搜狗] 共获取 %d 篇文章", len(sogou_articles))
    return sogou_articles


def run_weread(keyword: str = KEYWORD) -> list[dict]:
    """微信读书数据源：从已订阅公众号获取文章（非交互式，需提前登录）"""
    try:
        from weread_client import WeReadClient, load_account
    except ImportError:
        log.warning("[WeRead] 无法导入 weread_client，跳过微信读书源")
        return []

    # 检查是否已有有效 token，无 token 则跳过（避免阻塞等待扫码）
    account = load_account()
    if not account or not account.get("token"):
        log.info("[WeRead] 未登录，跳过。请运行 'python3 weread_client.py login' 扫码登录")
        return []

    client = WeReadClient()
    try:
        articles = client.fetch_all_subscribed(keyword=keyword)
    except Exception as e:
        log.error("[WeRead] 获取文章失败: %s", e)
        return []

    # 标记 found_at
    for a in articles:
        a["found_at"] = datetime.now().isoformat()
        a["url_key"] = dedupe_url(a.get("url", ""))

    log.info("[WeRead] 共获取 %d 篇文章", len(articles))
    return articles


def run():
    """主运行函数：双源抓取 + 合并"""
    log.info("=" * 50)
    log.info("开始双源抓取微信公众号文章, 关键词: %s", KEYWORD)
    ensure_data_dir()
    set_secure_permissions()

    existing = load_articles()

    # 1. 搜狗源
    sogou_articles = run_sogou(KEYWORD)

    # 2. 微信读书源
    weread_articles = run_weread(KEYWORD)

    # 3. 合并去重
    from merge import merge_results
    existing, all_new = merge_results(existing, sogou_articles, weread_articles)

    save_articles(existing)

    log.info("本次发现 %d 篇新文章 (总计 %d 篇)", len(all_new), len(existing))
    if all_new:
        for a in all_new:
            source_tag = a.get("source", "unknown")
            log.info("  [新|%s] %s — %s", source_tag, a.get("account", "未知"), a["title"])

    return all_new


if __name__ == "__main__":
    run()
