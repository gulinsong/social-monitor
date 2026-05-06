import json
import logging
import re

import jieba
import jieba.analyse

log = logging.getLogger(__name__)

_custom_dict_loaded = False

# Topic tag rules: keyword → tag label
TAG_RULES = {
    "加班": "#加班", "996": "#加班", "007": "#加班",
    "薪资": "#薪资", "工资": "#薪资", "降薪": "#薪资", "涨薪": "#薪资", "收入": "#薪资",
    "离职": "#离职", "辞职": "#离职", "跑路": "#离职",
    "裁员": "#裁员", "优化": "#裁员", "毕业": "#裁员", "n+1": "#裁员",
    "内卷": "#内卷", "卷": "#内卷",
    "摸鱼": "#摸鱼", "划水": "#摸鱼",
    "吐槽": "#吐槽", "抱怨": "#吐槽",
    "福利": "#福利", "补贴": "#福利", "年终奖": "#福利",
    "绩效": "#绩效", "考核": "#绩效", "kpi": "#绩效",
    "晋升": "#晋升", "升职": "#晋升", "职级": "#晋升",
    "续签": "#续签", "合同": "#续签",
    "比亚迪": "#比亚迪", "byd": "#比亚迪", "迪子": "#比亚迪", "迪厂": "#比亚迪",
    "领导": "#管理", "经理": "#管理", "主管": "#管理", "pua": "#管理",
    "歧视": "#歧视", "性别": "#歧视", "年龄": "#歧视",
    "举报": "#举报", "投诉": "#举报",
}

# High-risk keywords that elevate risk level
RISK_KEYWORDS = {"裁员", "降薪", "举报", "投诉", "歧视", "性骚扰", "PUA", "pua",
                 "违法", "仲裁", "维权", "集体", "罢工", "抗议"}


def _ensure_custom_dict(path: str = None):
    global _custom_dict_loaded
    if _custom_dict_loaded:
        return
    if path:
        try:
            jieba.load_userdict(path)
        except Exception as e:
            log.warning("Failed to load custom dictionary: %s", e)
    jieba.add_word("比亚迪")
    _custom_dict_loaded = True


class SentimentAnalyzer:
    def __init__(self, custom_dict_path: str = None):
        _ensure_custom_dict(custom_dict_path)
        try:
            from snownlp import SnowNLP
            self._SnowNLP = SnowNLP
        except ImportError:
            log.warning("SnowNLP not installed, sentiment analysis unavailable")
            self._SnowNLP = None

    def analyze(self, text: str) -> dict:
        if not text or not text.strip():
            return {"sentiment": "neutral", "score": 0.5, "keywords": [], "intensity": 0.0}

        keywords = jieba.analyse.extract_tags(text, topK=5)

        if self._SnowNLP:
            try:
                s = self._SnowNLP(text)
                score = s.sentiments
            except Exception:
                score = 0.5
        else:
            score = 0.5

        if score >= 0.6:
            sentiment = "positive"
        elif score <= 0.4:
            sentiment = "negative"
        else:
            sentiment = "neutral"

        intensity = abs(score - 0.5) * 2

        return {
            "sentiment": sentiment,
            "score": round(score, 4),
            "keywords": keywords,
            "intensity": round(intensity, 4),
        }

    def analyze_batch(self, texts: list[str]) -> list[dict]:
        return [self.analyze(t) for t in texts]

    def extract_tags(self, text: str) -> list[str]:
        """Extract topic tags (#标签) based on keyword matching."""
        if not text or not text.strip():
            return []
        text_lower = text.lower()
        tags = set()
        for keyword, tag in TAG_RULES.items():
            if keyword in text_lower:
                tags.add(tag)
        # Deduplicate and sort
        return sorted(tags)

    def generate_summary(self, text: str, max_len: int = 100) -> str:
        """Generate a lightweight summary (first N chars, stripped of whitespace)."""
        if not text or not text.strip():
            return ""
        # Remove excessive whitespace/newlines
        cleaned = re.sub(r'\s+', ' ', text.strip())
        if len(cleaned) <= max_len:
            return cleaned
        return cleaned[:max_len] + "..."

    def assess_risk(self, text: str, sentiment_score: float) -> str:
        """Assess risk level based on sentiment score and keyword matching."""
        if not text or not text.strip():
            return "low"

        text_lower = text.lower()
        risk_hits = sum(1 for kw in RISK_KEYWORDS if kw in text_lower)

        # Strongly negative + risk keywords → high
        if sentiment_score <= 0.2 and risk_hits >= 2:
            return "high"
        if sentiment_score <= 0.3 and risk_hits >= 1:
            return "high"

        # Negative + risk keywords → medium
        if sentiment_score <= 0.4 and risk_hits >= 1:
            return "medium"
        if sentiment_score <= 0.2:
            return "medium"

        return "low"
