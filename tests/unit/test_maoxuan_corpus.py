from __future__ import annotations

from pathlib import Path

from scripts.maoxuan.corpus import (
    Article,
    clean_article_text,
    classify_genre,
    classify_period,
    parse_cn_year,
)
from scripts.maoxuan.kb import build_kb_document
from scripts.maoxuan.skill import SKILL_PROMPT_MAX_CHARS, compile_skill_prompt


def test_clean_article_drops_preface_and_notes() -> None:
    raw = """中国社会各阶级的分析
（一九二五年十二月一日）

毛泽东此文是为反对当时党内存在着的两种倾向而写的。

　　谁是我们的敌人？谁是我们的朋友？这个问题是革命的首要问题。

注释

[1] 国家主义派指中国青年党。
"""
    title, date_text, editorial, body = clean_article_text(raw)
    assert title == "中国社会各阶级的分析"
    assert date_text == "一九二五年十二月一日"
    assert "两种倾向" in editorial
    assert "谁是我们的敌人" in body
    assert "国家主义派" not in body


def test_star_preface_and_page_cite_are_stripped() -> None:
    raw = """中国人民站起来了
（一九四九年九月二十一日）

* 这是毛泽东同志在中国人民政治协商会议第一届全体会议上的开幕词

　　诸位代表先生们，全国人民所渴望的政治协商会议现在开幕了。

（《毛泽东选集》第五卷，人民出版社1977年4月第1版，第3—7页）
"""
    title, date_text, editorial, body = clean_article_text(raw)
    assert title == "中国人民站起来了"
    assert "一九四九年" in date_text
    assert "开幕词" in editorial
    assert "诸位代表先生们" in body
    assert "人民出版社" not in body


def test_year_and_period_and_genre() -> None:
    assert parse_cn_year("一九二五年十二月一日") == 1925
    assert parse_cn_year("1967年六月") == 1967
    assert classify_period(1938, "vol2/x.txt") == "resistance"
    assert classify_period(1967, "supplement_1968_wuhan/a.txt") == "late"
    assert classify_genre("反对党八股", "official") == "short"
    assert classify_genre("关于夏收的指示", "supplement") == "directive"
    assert classify_genre("谈话的主要精神的传达", "relay") == "relay"


def test_official_kb_document_keeps_title_and_body() -> None:
    article = Article(
        article_id="vol1/17_实践论.txt",
        title="实践论",
        date_text="一九三七年七月",
        year=1937,
        period="resistance",
        genre="treatise",
        source="official",
        trust=1.0,
        path="vol1/17_实践论.txt",
        editorial="这是一篇哲学论文。",
        body="认识从实践发生。",
        char_count=8,
    )
    document = build_kb_document(article)
    assert document["source"] == "maoxuan-official"
    assert document["title"] == "毛泽东选集第一卷 · 实践论"
    assert "认识从实践发生" in document["content"]
    assert document["metadata"]["article_id"] == "vol1/17_实践论.txt"
    assert document["metadata"]["volume"] == "vol1"


def test_compiled_skill_stays_bounded() -> None:
    stats = {
        "by_source": {"official": 159, "supplement": 900, "relay": 80, "variant": 12},
        "by_period": {
            "civil_war_1": {"articles": 18},
            "resistance": {"articles": 70},
            "liberation": {"articles": 70},
            "construction": {"articles": 70},
            "late": {"articles": 900},
        },
        "cadence_hits": {"总之": 10, "必须": 8},
    }
    prompt = compile_skill_prompt(stats)
    assert prompt.startswith("# 毛选人格")
    assert "PART A" in prompt
    assert "补编" in prompt
    assert len(prompt) <= SKILL_PROMPT_MAX_CHARS
