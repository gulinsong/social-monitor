import hashlib
import hmac
import base64
import time
import logging

import requests

log = logging.getLogger(__name__)

PLATFORM_COLORS = {
    "weibo": "blue",
    "wechat": "green",
    "maimai": "orange",
    "xiaohongshu": "red",
}

PLATFORM_LABELS = {
    "weibo": "Weibo",
    "wechat": "WeChat",
    "maimai": "Maimai",
    "xiaohongshu": "Xiaohongshu",
}


class FeishuNotifier:
    def __init__(self, webhook_url: str, sign_secret: str = ""):
        self.webhook_url = webhook_url
        self.sign_secret = sign_secret

    def _gen_sign(self, timestamp: str) -> str:
        if not self.sign_secret:
            return ""
        string_to_sign = f"{timestamp}\n{self.sign_secret}"
        hmac_code = hmac.new(
            string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
        ).digest()
        return base64.b64encode(hmac_code).decode("utf-8")

    def _send(self, payload: dict) -> bool:
        try:
            resp = requests.post(
                self.webhook_url,
                json=payload,
                timeout=10,
            )
            if resp.status_code == 429:
                log.warning("Feishu push rate limited")
                return False
            resp.raise_for_status()
            result = resp.json()
            if result.get("code") != 0:
                log.warning("Feishu push failed: %s", result.get("msg"))
                return False
            return True
        except Exception as e:
            log.error("Feishu push error: %s", e)
            return False

    def push_post(self, post: dict) -> bool:
        platform = post.get("platform", "unknown")
        color = PLATFORM_COLORS.get(platform, "blue")
        label = PLATFORM_LABELS.get(platform, platform)
        sentiment = post.get("sentiment", "")
        score = post.get("sentiment_score", "")

        sentiment_map = {"positive": "Positive", "negative": "Negative", "neutral": "Neutral"}
        sentiment_text = sentiment_map.get(sentiment, "Not analyzed")

        title = post.get("title") or post.get("content", "")[:50]
        content = post.get("content", "")[:500]

        header = f"[{label}] {title}"
        elements = [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**Author**: {post.get('user_name', 'Unknown')}\n**Content**: {content}",
                },
            },
            {"tag": "hr"},
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": f"Keyword: {post.get('keyword', '')} | Sentiment: {sentiment_text} ({score})",
                    },
                    {
                        "tag": "plain_text",
                        "content": f"Engagement: {post.get('likes_count', 0)} likes, {post.get('comments_count', 0)} comments, {post.get('reposts_count', 0)} reposts",
                    },
                ],
            },
        ]

        url = post.get("url", "")
        if url:
            elements.append({
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "View original"},
                        "url": url,
                        "type": "primary",
                    }
                ],
            })

        timestamp = str(int(time.time()))
        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": header},
                    "template": color,
                },
                "elements": elements,
            },
        }

        if self.sign_secret:
            payload["timestamp"] = timestamp
            payload["sign"] = self._gen_sign(timestamp)

        success = self._send(payload)
        if success:
            time.sleep(3)  # Respect push rate limit
        return success

    def push_summary(self, platform: str, new_count: int, sentiment_summary: dict = None) -> bool:
        label = PLATFORM_LABELS.get(platform, platform)
        color = PLATFORM_COLORS.get(platform, "blue")

        summary_text = f"{label} crawl completed, found {new_count} new items."
        if sentiment_summary:
            summary_text += f"\nPositive: {sentiment_summary.get('positive', 0)} | Negative: {sentiment_summary.get('negative', 0)} | Neutral: {sentiment_summary.get('neutral', 0)}"

        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": f"[{label}] Crawl Summary"},
                    "template": color,
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {"tag": "plain_text", "content": summary_text},
                    }
                ],
            },
        }

        return self._send(payload)
