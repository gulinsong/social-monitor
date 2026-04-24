#!/usr/bin/env python3
"""
双源数据合并去重模块
统一搜狗和微信读书两路数据，按发布时间排序
"""

import hashlib
import logging
from datetime import datetime

log = logging.getLogger(__name__)


def extract_url_key(url: str) -> str:
    """从微信文章 URL 提取唯一标识（sn 参数或 articleId）"""
    import urllib.parse

    # mp.weixin.qq.com 文章
    if "mp.weixin.qq.com" in url:
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)

        # 优先 sn 参数
        sn = params.get("sn", [None])[0]
        if sn:
            return sn

        # /s/{articleId} 格式
        path_parts = parsed.path.strip("/").split("/")
        if len(path_parts) >= 2 and path_parts[0] == "s":
            aid = path_parts[1]
            if aid and len(aid) > 5:
                return aid

    # 回退：URL 哈希
    return hashlib.md5(url.encode()).hexdigest()[:12]


def normalize_article(article: dict) -> dict:
    """统一文章数据格式"""
    return {
        "title": article.get("title", "").strip(),
        "url": article.get("url", "").strip(),
        "digest": article.get("digest", "")[:200],
        "account": article.get("account", "").strip(),
        "pub_time": article.get("pub_time", ""),
        "source": article.get("source", "unknown"),
        "found_at": article.get("found_at", datetime.now().isoformat()),
        "url_key": article.get("url_key", extract_url_key(article.get("url", ""))),
    }


def title_similarity(a: str, b: str) -> float:
    """简单的标题相似度计算（用于模糊去重）"""
    if not a or not b:
        return 0.0
    a, b = a.strip(), b.strip()
    if a == b:
        return 1.0
    # 完全包含
    if a in b or b in a:
        return 0.9
    # 共同字符比例
    common = set(a) & set(b)
    total = set(a) | set(b)
    if not total:
        return 0.0
    return len(common) / len(total)


def merge_results(
    existing: dict,
    sogou_articles: list[dict],
    weread_articles: list[dict],
    title_threshold: float = 0.85,
) -> tuple[dict, list[dict]]:
    """
    合并搜狗和微信读书的文章到已有数据中

    Args:
        existing: 已有文章 {url_key: article_dict}
        sogou_articles: 搜狗新抓取的文章列表
        weread_articles: 微信读书新抓取的文章列表
        title_threshold: 标题相似度阈值（超过则视为重复）

    Returns:
        (更新后的 articles dict, 本次新增的文章列表)
    """
    # 标记来源
    for a in sogou_articles:
        a["source"] = "sogou"
    for a in weread_articles:
        a["source"] = "weread"

    # 合并所有新文章
    all_new_raw = sogou_articles + weread_articles
    all_new = []

    for article in all_new_raw:
        normalized = normalize_article(article)
        url_key = normalized["url_key"]

        # 精确去重：url_key 已存在
        if url_key in existing:
            # 如果已有记录缺少信息，用新数据补充
            old = existing[url_key]
            updated = False
            for field in ["pub_time", "account", "digest"]:
                if not old.get(field) and normalized.get(field):
                    old[field] = normalized[field]
                    updated = True
            if old.get("source") == "unknown" and normalized.get("source") != "unknown":
                old["source"] = normalized["source"]
                updated = True
            if updated:
                existing[url_key] = old
            continue

        # 模糊去重：标题相似
        is_dup = False
        for key, existing_article in existing.items():
            sim = title_similarity(normalized["title"], existing_article.get("title", ""))
            if sim >= title_threshold:
                # 补充缺失信息
                for field in ["pub_time", "account", "digest"]:
                    if not existing_article.get(field) and normalized.get(field):
                        existing_article[field] = normalized[field]
                is_dup = True
                break

        if not is_dup:
            existing[url_key] = normalized
            all_new.append(normalized)

    log.info(
        "合并结果: 搜狗 %d + 微信读书 %d → 新增 %d 篇 (总计 %d 篇)",
        len(sogou_articles),
        len(weread_articles),
        len(all_new),
        len(existing),
    )

    return existing, all_new
