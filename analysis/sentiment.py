import json
import logging

import jieba
import jieba.analyse

log = logging.getLogger(__name__)

_custom_dict_loaded = False


def _ensure_custom_dict(path: str = None):
    global _custom_dict_loaded
    if _custom_dict_loaded:
        return
    if path:
        try:
            jieba.load_userdict(path)
        except Exception as e:
            log.warning("加载自定义词典失败: %s", e)
    jieba.add_word("迪子")
    jieba.add_word("比亚迪")
    _custom_dict_loaded = True


class SentimentAnalyzer:
    def __init__(self, custom_dict_path: str = None):
        _ensure_custom_dict(custom_dict_path)
        try:
            from snownlp import SnowNLP
            self._SnowNLP = SnowNLP
        except ImportError:
            log.warning("SnowNLP 未安装，情感分析不可用")
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
