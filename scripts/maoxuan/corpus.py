"""Ingest and clean the full local 毛泽东选集 dump without mixing registers."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

_SKIP_NAME_RE = re.compile(r"(合订|_合并|INDEX|出版说明)")
_DATE_LINE_RE = re.compile(r"^（([一二三四五六七八九零〇○Ｏ0-9]{2,4}年[^）]*)）$")
_DATE_INLINE_RE = re.compile(r"（([一二三四五六七八九零〇○Ｏ0-9]{2,4}年[^）]*)）")
_YEAR_CN_RE = re.compile(r"([一二三四五六七八九零〇○Ｏ0-9]{2,4})年")
_PAGE_CITE_RE = re.compile(r"（《毛泽东选集》[^）]*）")
_NOTE_MARK_RE = re.compile(r"\[\d+\]")
_CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "○": 0,
    "Ｏ": 0,
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

_PERIODS = (
    ("civil_war_1", 0, 1937),
    ("resistance", 1937, 1945),
    ("liberation", 1945, 1949),
    ("construction", 1949, 1958),
    ("late", 1958, 3000),
)


@dataclass(frozen=True, slots=True)
class Article:
    article_id: str
    title: str
    date_text: str
    year: int | None
    period: str
    genre: str
    source: str
    trust: float
    path: str
    editorial: str
    body: str
    char_count: int

    def to_index_row(self) -> dict[str, Any]:
        row = asdict(self)
        row.pop("body")
        row.pop("editorial")
        return row


def iter_source_files(root: Path) -> Iterable[Path]:
    root = Path(root)
    for path in sorted(root.rglob("*.txt")):
        relative = path.relative_to(root).as_posix()
        if _SKIP_NAME_RE.search(path.name) or _SKIP_NAME_RE.search(relative):
            continue
        if relative.startswith("supplement_") or relative.startswith("vol"):
            yield path


def classify_source(relative: str) -> tuple[str, float]:
    posix = relative.replace("\\", "/")
    if "/extra_unabridged/" in posix:
        return "variant", 0.3
    if posix.startswith("supplement_"):
        if any(token in posix for token in ("传达", "转引", "转引自")):
            return "relay", 0.2
        return "supplement", 0.55
    return "official", 1.0


def classify_genre(title: str, source: str) -> str:
    text = title or ""
    rules = (
        ("relay", ("传达",)),
        ("survey", ("考察", "调查", "寻乌")),
        ("talk", ("谈话", "答记者", "和英国记者", "和中央社", "答问")),
        ("directive", ("指示", "批示", "批语", "批注", "命令", "贺电")),
        ("decree", ("声明", "布告", "通电")),
        ("short", ("纪念", "为人民服务", "反对自由主义", "反对党八股", "愚公移山")),
        ("treatise", ("论", "问题", "战略", "分析", "实践", "矛盾")),
    )
    for genre, tokens in rules:
        if any(token in text for token in tokens):
            return genre
    if source in {"supplement", "relay"}:
        return "directive"
    return "essay"


def classify_period(year: int | None, relative: str) -> str:
    if year is None:
        if relative.startswith("vol1"):
            return "civil_war_1"
        if relative.startswith("vol2") or relative.startswith("vol3"):
            return "resistance"
        if relative.startswith("vol4"):
            return "liberation"
        if relative.startswith("vol5"):
            return "construction"
        return "late"
    for name, start, end in _PERIODS:
        if start <= year < end:
            return name
    return "late"


def parse_cn_year(text: str) -> int | None:
    match = _YEAR_CN_RE.search(text or "")
    if not match:
        return None
    raw = match.group(1)
    if raw.isdigit():
        year = int(raw)
        return year if 1800 <= year <= 1999 else None
    if len(raw) != 4:
        return None
    digits: list[int] = []
    for char in raw:
        if char.isdigit():
            digits.append(int(char))
            continue
        if char not in _CN_DIGITS:
            return None
        digits.append(_CN_DIGITS[char])
    year = int("".join(str(item) for item in digits))
    return year if 1800 <= year <= 1999 else None


def clean_article_text(raw: str) -> tuple[str, str, str, str]:
    text = str(raw or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _PAGE_CITE_RE.sub("", text)
    lines = text.split("\n")
    nonempty = [index for index, line in enumerate(lines) if line.strip()]
    if not nonempty:
        return "", "", "", ""
    title = lines[nonempty[0]].strip()
    date_text = ""
    cursor = nonempty[0] + 1
    if cursor < len(lines):
        maybe_date = lines[cursor].strip()
        date_match = _DATE_LINE_RE.match(maybe_date)
        if date_match:
            date_text = date_match.group(1)
            cursor += 1
        else:
            inline = _DATE_INLINE_RE.search(title)
            if inline:
                date_text = inline.group(1)
    editorial: list[str] = []
    while cursor < len(lines):
        raw_line = lines[cursor]
        stripped = raw_line.strip()
        if not stripped:
            cursor += 1
            continue
        if stripped.startswith("注释"):
            break
        is_body = raw_line.startswith("　　") or raw_line.startswith("　")
        is_preface = (
            stripped.startswith("*")
            or stripped.startswith("这是毛泽东")
            or stripped.startswith("毛泽东此文")
            or stripped.startswith("毛泽东同志此文")
            or stripped.startswith("毛泽东同志在")
        )
        if is_preface or (not is_body and not editorial):
            editorial.append(stripped.lstrip("* ").strip())
            cursor += 1
            continue
        break
    body_lines = lines[cursor:]
    cut = len(body_lines)
    for index, line in enumerate(body_lines):
        if line.strip() == "注释":
            cut = index
            break
    body = "\n".join(body_lines[:cut]).strip()
    body = _NOTE_MARK_RE.sub("", body)
    return title, date_text, "\n".join(editorial).strip(), body


def load_article(path: Path, root: Path) -> Article | None:
    raw = path.read_text(encoding="utf-8", errors="replace")
    title, date_text, editorial, body = clean_article_text(raw)
    if not title or len(body) < 20:
        return None
    relative = path.relative_to(root).as_posix()
    source, trust = classify_source(relative)
    if source == "supplement" and any(token in (title + body[:80]) for token in ("传达", "转引")):
        source, trust = "relay", 0.2
    year = parse_cn_year(date_text) or parse_cn_year(title) or parse_cn_year(path.stem)
    return Article(
        article_id=relative,
        title=title,
        date_text=date_text,
        year=year,
        period=classify_period(year, relative),
        genre=classify_genre(title, source),
        source=source,
        trust=trust,
        path=str(path),
        editorial=editorial,
        body=body,
        char_count=len(body),
    )


def load_corpus(root: Path) -> list[Article]:
    articles: list[Article] = []
    seen: set[str] = set()
    for path in iter_source_files(root):
        article = load_article(path, root)
        if article is None or article.article_id in seen:
            continue
        seen.add(article.article_id)
        articles.append(article)
    return articles
