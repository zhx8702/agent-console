from __future__ import annotations

from app.postprocessing.outbound_desensitize import desensitize_outbound_text


def test_desensitize_masks_political_and_sexual_terms() -> None:
    text = "别拿习近平和做爱开玩笑，操作流程还是按文档走。"
    sanitized, matched = desensitize_outbound_text(text)

    assert "习近平" not in sanitized
    assert "做爱" not in sanitized
    assert "操作" in sanitized
    assert "***" in sanitized
    assert "**" in sanitized
    assert "习近平" in matched
    assert "做爱" in matched


def test_desensitize_keeps_everyday_cao_compounds() -> None:
    text = "先操作一遍，再操心进度。"
    sanitized, matched = desensitize_outbound_text(text)

    assert sanitized == text
    assert matched == ()


def test_desensitize_masks_standalone_vulgar_cao() -> None:
    sanitized, matched = desensitize_outbound_text("操，这也太猛了")

    assert sanitized.startswith("*，")
    assert "操" in matched


def test_desensitize_masks_english_terms_case_insensitively() -> None:
    sanitized, matched = desensitize_outbound_text("This Porn clip and Xi Jinping joke")

    assert "Porn" not in sanitized
    assert "Xi Jinping" not in sanitized
    assert "****" in sanitized
    assert any(item.lower() == "porn" for item in matched)
    assert any(item.lower() == "xi jinping" for item in matched)
