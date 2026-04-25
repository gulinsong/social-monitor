#!/usr/bin/env python3
"""
Dual-source data merge and deduplication module
Unifies Sogou and WeRead data, sorted by publish time
"""

import hashlib
import logging
from datetime import datetime

log = logging.getLogger(__name__)


def extract_url_key(url: str) -> str:
    """Extract unique identifier from WeChat article URL (sn parameter or articleId)"""
    import urllib.parse

    # mp.weixin.qq.com article
    if "mp.weixin.qq.com" in url:
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)

        # Prefer sn parameter
        sn = params.get("sn", [None])[0]
        if sn:
            return sn

        # /s/{articleId} format
        path_parts = parsed.path.strip("/").split("/")
        if len(path_parts) >= 2 and path_parts[0] == "s":
            aid = path_parts[1]
            if aid and len(aid) > 5:
                return aid

    # Fallback: URL hash
    return hashlib.md5(url.encode()).hexdigest()[:12]


def normalize_article(article: dict) -> dict:
    """Normalize article data format"""
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
    """Simple title similarity calculation (for fuzzy deduplication)"""
    if not a or not b:
        return 0.0
    a, b = a.strip(), b.strip()
    if a == b:
        return 1.0
    # Full containment
    if a in b or b in a:
        return 0.9
    # Common character ratio
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
    Merge Sogou and WeRead articles into existing data

    Args:
        existing: Existing articles {url_key: article_dict}
        sogou_articles: Newly fetched Sogou articles list
        weread_articles: Newly fetched WeRead articles list
        title_threshold: Title similarity threshold (above this is considered duplicate)

    Returns:
        (Updated articles dict, newly added articles list)
    """
    # Mark source
    for a in sogou_articles:
        a["source"] = "sogou"
    for a in weread_articles:
        a["source"] = "weread"

    # Merge all new articles
    all_new_raw = sogou_articles + weread_articles
    all_new = []

    for article in all_new_raw:
        normalized = normalize_article(article)
        url_key = normalized["url_key"]

        # Exact dedup: url_key already exists
        if url_key in existing:
            # If existing record is missing info, supplement with new data
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

        # Fuzzy dedup: similar titles
        is_dup = False
        for key, existing_article in existing.items():
            sim = title_similarity(normalized["title"], existing_article.get("title", ""))
            if sim >= title_threshold:
                # Supplement missing info
                for field in ["pub_time", "account", "digest"]:
                    if not existing_article.get(field) and normalized.get(field):
                        existing_article[field] = normalized[field]
                is_dup = True
                break

        if not is_dup:
            existing[url_key] = normalized
            all_new.append(normalized)

    log.info(
        "Merge result: Sogou %d + WeRead %d -> %d new articles (total %d articles)",
        len(sogou_articles),
        len(weread_articles),
        len(all_new),
        len(existing),
    )

    return existing, all_new
