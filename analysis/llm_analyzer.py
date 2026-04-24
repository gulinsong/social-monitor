import json
import logging

import requests

log = logging.getLogger(__name__)

DEFAULT_PROMPT = """你是一个舆情分析专家。分析以下社交媒体文本，返回JSON格式结果：

文本：{text}

请返回：
{{
  "sentiment": "positive/negative/neutral",
  "score": 0.0到1.0之间的数值,
  "sarcastic": true/false（是否包含反讽），
  "topics": ["话题1", "话题2"],
  "summary": "一句话摘要",
  "risk_level": "low/medium/high"
}}

只返回JSON，不要其他内容。"""


class LLMAnalyzer:
    def __init__(self, config: dict):
        self.api_url = config.get("api_url", "")
        self.api_key = config.get("api_key", "")
        self.model = config.get("model", "gpt-3.5-turbo")
        self.enabled = bool(self.api_url and self.api_key)

    def analyze(self, text: str) -> dict | None:
        if not self.enabled:
            return None

        try:
            resp = requests.post(
                self.api_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "user", "content": DEFAULT_PROMPT.format(text=text)}
                    ],
                    "temperature": 0.1,
                    "max_tokens": 500,
                },
                timeout=30,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            # 尝试提取 JSON
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0]
            elif "```" in content:
                content = content.split("```")[1].split("```")[0]
            return json.loads(content.strip())
        except Exception as e:
            log.error("LLM 分析失败: %s", e)
            return None

    def analyze_batch(self, texts: list[str]) -> list[dict | None]:
        return [self.analyze(t) for t in texts]
