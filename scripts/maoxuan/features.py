"""Deterministic style signals from cleaned 毛选 articles."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any

from scripts.maoxuan.corpus import Article

_SENTENCE_RE = re.compile(r"[^。！？!?；;]+[。！？!?；;]?")
_QUESTION_RE = re.compile(r"[？?]|谁是|什么是|怎样|为什么|难道")
_METHOD_PHRASES = (
    "调查研究",
    "没有调查",
    "实事求是",
    "一分为二",
    "对立统一",
    "主要矛盾",
    "群众路线",
    "从群众中来",
    "具体问题具体分析",
    "谁是我们的敌人",
    "谁是我们的朋友",
)
_CADENCE_PHRASES = (
    "总之",
    "总而言之",
    "一方面",
    "另一方面",
    "不但",
    "而且",
    "不是",
    "而是",
    "必须",
    "应当",
    "我们主张",
    "我们反对",
)
_VERNACULAR = (
    "岂不是",
    "罢了",
    "啊",
    "呢",
    "老鼠过街",
    "懒婆娘",
    "到什么山上唱什么歌",
    "看菜吃饭",
)


def _sentences(text: str) -> list[str]:
    return [item.strip() for item in _SENTENCE_RE.findall(text or "") if item.strip()]


def article_features(article: Article) -> dict[str, Any]:
    sentences = _sentences(article.body)
    lengths = [len(item) for item in sentences] or [0]
    questions = len(_QUESTION_RE.findall(article.body))
    method_hits = {phrase: article.body.count(phrase) for phrase in _METHOD_PHRASES if article.body.count(phrase)}
    cadence_hits = {phrase: article.body.count(phrase) for phrase in _CADENCE_PHRASES if article.body.count(phrase)}
    vernacular_hits = {phrase: article.body.count(phrase) for phrase in _VERNACULAR if article.body.count(phrase)}
    return {
        "article_id": article.article_id,
        "period": article.period,
        "genre": article.genre,
        "source": article.source,
        "trust": article.trust,
        "char_count": article.char_count,
        "sentence_count": len(sentences),
        "avg_sentence_len": round(sum(lengths) / max(1, len(lengths)), 1),
        "question_count": questions,
        "question_rate": round(questions / max(1, len(sentences)), 3),
        "method_hits": method_hits,
        "cadence_hits": cadence_hits,
        "vernacular_hits": vernacular_hits,
    }


def aggregate_features(articles: list[Article]) -> dict[str, Any]:
    rows = [article_features(article) for article in articles]
    by_period: dict[str, dict[str, Any]] = {}
    by_genre: Counter[str] = Counter()
    by_source: Counter[str] = Counter()
    method = Counter()
    cadence = Counter()
    vernacular = Counter()
    weighted_chars = 0.0
    official_chars = 0
    for article, row in zip(articles, rows, strict=True):
        weight = article.trust * article.char_count
        weighted_chars += weight
        by_genre[article.genre] += 1
        by_source[article.source] += 1
        if article.source == "official":
            official_chars += article.char_count
        bucket = by_period.setdefault(
            article.period,
            {
                "articles": 0,
                "chars": 0,
                "weighted_chars": 0.0,
                "questions": 0,
                "sentences": 0,
            },
        )
        bucket["articles"] += 1
        bucket["chars"] += article.char_count
        bucket["weighted_chars"] += weight
        bucket["questions"] += row["question_count"]
        bucket["sentences"] += row["sentence_count"]
        method.update({key: value * article.trust for key, value in row["method_hits"].items()})
        cadence.update({key: value * article.trust for key, value in row["cadence_hits"].items()})
        vernacular.update({key: value * article.trust for key, value in row["vernacular_hits"].items()})
    period_summary = {}
    for name, bucket in by_period.items():
        period_summary[name] = {
            **bucket,
            "question_rate": round(bucket["questions"] / max(1, bucket["sentences"]), 3),
        }
    return {
        "article_count": len(articles),
        "official_chars": official_chars,
        "weighted_chars": int(weighted_chars),
        "by_period": period_summary,
        "by_genre": dict(by_genre),
        "by_source": dict(by_source),
        "method_hits": dict(method.most_common(16)),
        "cadence_hits": dict(cadence.most_common(16)),
        "vernacular_hits": dict(vernacular.most_common(12)),
        "rows": rows,
    }


def pick_representatives(articles: list[Article], *, limit: int = 24) -> list[dict[str, str]]:
    wanted = {
        ("official", "survey"),
        ("official", "treatise"),
        ("official", "short"),
        ("official", "talk"),
        ("official", "essay"),
        ("official", "directive"),
        ("supplement", "directive"),
        ("supplement", "talk"),
        ("relay", "relay"),
    }
    picked: list[dict[str, str]] = []
    seen: set[str] = set()
    grouped: dict[tuple[str, str], list[Article]] = defaultdict(list)
    for article in articles:
        grouped[(article.source, article.genre)].append(article)
    for key in wanted:
        candidates = sorted(grouped.get(key, []), key=lambda item: item.char_count, reverse=True)
        for article in candidates[:3]:
            if article.article_id in seen:
                continue
            seen.add(article.article_id)
            picked.append(
                {
                    "title": article.title,
                    "period": article.period,
                    "genre": article.genre,
                    "source": article.source,
                    "year": str(article.year or ""),
                }
            )
            if len(picked) >= limit:
                return picked
    return picked
