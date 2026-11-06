"""Build session-scoped knowledge-base documents from official 毛选 articles."""

from __future__ import annotations

from typing import Any

from scripts.maoxuan.corpus import Article

_VOLUME_LABELS = {
    "vol1": "第一卷",
    "vol2": "第二卷",
    "vol3": "第三卷",
    "vol4": "第四卷",
    "vol5": "第五卷",
}

KB_SOURCE = "maoxuan-official"


def volume_from_article_id(article_id: str) -> str:
    return str(article_id or "").replace("\\", "/").split("/", 1)[0]


def volume_label(article_id: str) -> str:
    return _VOLUME_LABELS.get(volume_from_article_id(article_id), "")


def build_kb_document(article: Article) -> dict[str, Any]:
    volume = volume_from_article_id(article.article_id)
    label = volume_label(article.article_id)
    volume_title = f"毛泽东选集{label}" if label else "毛泽东选集"
    lines = [article.title]
    if label:
        lines.append(f"出处：{volume_title}")
    if article.date_text:
        lines.append(f"（{article.date_text}）")
    if article.editorial:
        lines.extend(["", article.editorial])
    lines.extend(["", article.body])
    return {
        "title": f"{volume_title} · {article.title}" if label else article.title,
        "content": "\n".join(lines).strip(),
        "source": KB_SOURCE,
        "url": None,
        "metadata": {
            "article_id": article.article_id,
            "year": article.year,
            "period": article.period,
            "genre": article.genre,
            "volume": volume,
            "trust": article.trust,
            "corpus": "毛泽东选集正编",
        },
    }
