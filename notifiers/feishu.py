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
    "weibo": "微博",
    "wechat": "微信",
    "maimai": "脉脉",
    "xiaohongshu": "小红书",
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
                log.warning("飞书推送被限流")
                return False
            resp.raise_for_status()
            result = resp.json()
            if result.get("code") != 0:
                log.warning("飞书推送失败: %s", result.get("msg"))
                return False
            return True
        except Exception as e:
            log.error("飞书推送异常: %s", e)
            return False

    def push_post(self, post: dict) -> bool:
        platform = post.get("platform", "unknown")
        color = PLATFORM_COLORS.get(platform, "blue")
        label = PLATFORM_LABELS.get(platform, platform)
        sentiment = post.get("sentiment", "")
        score = post.get("sentiment_score", "")

        sentiment_map = {"positive": "正面", "negative": "负面", "neutral": "中性"}
        sentiment_text = sentiment_map.get(sentiment, "未分析")

        title = post.get("title") or post.get("content", "")[:50]
        content = post.get("content", "")[:500]

        header = f"[{label}] {title}"
        elements = [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**作者**: {post.get('user_name', '未知')}\n**内容**: {content}",
                },
            },
            {"tag": "hr"},
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": f"关键词: {post.get('keyword', '')} | 情感: {sentiment_text} ({score})",
                    },
                    {
                        "tag": "plain_text",
                        "content": f"互动: {post.get('likes_count', 0)}赞 {post.get('comments_count', 0)}评 {post.get('reposts_count', 0)}转",
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
                        "text": {"tag": "plain_text", "content": "查看原文"},
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
            time.sleep(3)  # 遵守推送频率限制
        return success

    def push_summary(self, platform: str, new_count: int, sentiment_summary: dict = None) -> bool:
        label = PLATFORM_LABELS.get(platform, platform)
        color = PLATFORM_COLORS.get(platform, "blue")

        summary_text = f"本次 {label} 爬取完成，发现 {new_count} 条新内容。"
        if sentiment_summary:
            summary_text += f"\n正面: {sentiment_summary.get('positive', 0)} | 负面: {sentiment_summary.get('negative', 0)} | 中性: {sentiment_summary.get('neutral', 0)}"

        payload = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": f"[{label}] 爬取摘要"},
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
