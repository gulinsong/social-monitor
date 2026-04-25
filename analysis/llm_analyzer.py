import json
import logging

import requests

log = logging.getLogger(__name__)

DEFAULT_PROMPT = """You are a sentiment analysis expert. Analyze the following social media text and return the result in JSON format:

Text: {text}

Please return:
{{
  "sentiment": "positive/negative/neutral",
  "score": a value between 0.0 and 1.0,
  "sarcastic": true/false (whether it contains sarcasm),
  "topics": ["topic1", "topic2"],
  "summary": "a one-sentence summary",
  "risk_level": "low/medium/high"
}}

Return only JSON, no other content."""


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
            # Try to extract JSON
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0]
            elif "```" in content:
                content = content.split("```")[1].split("```")[0]
            return json.loads(content.strip())
        except Exception as e:
            log.error("LLM analysis failed: %s", e)
            return None

    def analyze_batch(self, texts: list[str]) -> list[dict | None]:
        return [self.analyze(t) for t in texts]
