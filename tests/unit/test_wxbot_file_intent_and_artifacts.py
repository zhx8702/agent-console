from __future__ import annotations

import json
from pathlib import Path

import pytest

from plugins.wxbot.file_artifacts import (
    FileArtifactTooLarge,
    convert_file_bytes,
    stage_outbound_artifact,
)
from plugins.wxbot.file_intent import classify_file_intent


@pytest.mark.parametrize(
    ("text", "operation", "delivery"),
    [
        ("总结这个附件内容", "inspect_incoming", False),
        ("看一下这个文件", "inspect_incoming", False),
        ("把这个文件转成 csv 发我", "convert", True),
        ("把聊天记录整理成 json 文件发给我", "export_history", True),
        ("把聊天记录整理成文件但不要发给我", "export_history", False),
    ],
)
def test_file_intent_distinguishes_inspection_conversion_and_delivery(
    text: str,
    operation: str,
    delivery: bool,
) -> None:
    result = classify_file_intent(text, has_attachment="附件" in text or "这个文件" in text)
    assert result.operation == operation
    assert result.delivery_required is delivery


def test_file_upload_alone_does_not_trigger_agent_file_operation() -> None:
    result = classify_file_intent("[文件] report.pdf", has_attachment=True)
    assert result.operation == "none"
    assert result.delivery_required is False


def test_inbound_file_caption_is_not_mistaken_for_outbound_delivery() -> None:
    received = classify_file_intent("我发个文件给你", has_attachment=True)
    received_without_subject = classify_file_intent("给你发一个文件", has_attachment=True)
    received_with_task = classify_file_intent(
        "我发个文件给你，帮我看看",
        has_attachment=True,
    )
    assert received.operation == "none"
    assert received_without_subject.operation == "none"
    assert received_with_task.operation == "inspect_incoming"


def test_analysis_can_be_packaged_as_a_new_file() -> None:
    result = classify_file_intent("分析成文件发我", has_attachment=True)
    ambiguous = classify_file_intent("分析这个文件发我", has_attachment=True)
    ordinary_report = classify_file_intent("生成一份报告")
    assert result.operation == "generate"
    assert result.source == "incoming_attachment"
    assert result.delivery_required is True
    assert ambiguous.operation == "inspect_incoming"
    assert ordinary_report.operation == "none"




def test_file_delivery_negation_is_clause_scoped() -> None:
    denied = classify_file_intent("把文件转成 csv 但不要发送", has_attachment=True)
    later_affirmative = classify_file_intent(
        "把文件转成 csv 但不要发送，然后发我原文件",
        has_attachment=True,
    )
    assert denied.delivery_required is False
    assert denied.needs_confirmation is True
    assert later_affirmative.delivery_required is True


def test_json_can_be_converted_to_csv_and_markdown() -> None:
    source = json.dumps([{"name": "张三", "score": 3}], ensure_ascii=False).encode()
    csv_bytes = convert_file_bytes(source, source_name="scores.json", target_format="csv")
    assert csv_bytes.decode("utf-8-sig") == "name,score\n张三,3\n"
    markdown = convert_file_bytes(source, source_name="scores.json", target_format="md")
    assert "| name | score |" in markdown.decode()


def test_stage_outbound_artifact_is_idempotent_and_bounded(tmp_path: Path) -> None:
    first = stage_outbound_artifact(
        tmp_path,
        tenant_id="tenant",
        session_id="session",
        request_id="request",
        file_name="scores.csv",
        content=b"a,b\n1,2\n",
        file_format="csv",
        max_bytes=1024,
    )
    replay = stage_outbound_artifact(
        tmp_path,
        tenant_id="tenant",
        session_id="session",
        request_id="request",
        file_name="scores.csv",
        content=b"a,b\n1,2\n",
        file_format="csv",
        max_bytes=1024,
    )
    assert replay == first
    assert Path(str(first["file_path"])).read_bytes() == b"a,b\n1,2\n"
    with pytest.raises(FileArtifactTooLarge):
        stage_outbound_artifact(
            tmp_path,
            tenant_id="tenant",
            session_id="session",
            request_id="large",
            file_name="large.txt",
            content=b"x" * 1025,
            file_format="txt",
            max_bytes=1024,
        )
