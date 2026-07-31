from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path

import pytest

from plugins.wxbot.message_exports import (
    InvalidMessageExportPath,
    MessageExportConflict,
    MessageExportTooLarge,
    build_message_export_summary,
    cleanup_message_exports,
    stage_message_export,
)


def _stage(
    root: Path,
    *,
    request_id: str = "request-1",
    summary_text: str = "今天确认了交付时间。",
) -> dict[str, object]:
    return stage_message_export(
        root,
        tenant_id="tenant/customer-a",
        session_id="project-room@chatroom",
        request_id=request_id,
        session_name="项目群",
        period="2026-07-29",
        summary_text=summary_text,
        messages=[
            {
                "timestamp": "2026-07-29 09:00:00",
                "sender_name": "张三",
                "text": "周五交付",
                "msg_type": "text",
            },
            {
                "timestamp": "2026-07-29 09:01:00",
                "sender_wxid": "wxid-lisi",
                "text": "",
                "msg_type": "image",
            },
            {
                "timestamp": "2026-07-29 09:02:00",
                "sender_name": "机器人",
                "text": "这是机器人回复",
                "is_self_sent": True,
            },
        ],
    )


def test_stage_message_export_writes_deterministic_bom_txt_and_metadata(tmp_path: Path) -> None:
    result = _stage(tmp_path)
    file_path = Path(str(result["file_path"]))
    content = file_path.read_bytes()

    assert file_path.is_absolute()
    assert file_path.is_relative_to(tmp_path)
    assert file_path.name == "message-export.txt"
    assert "tenant/customer-a" not in str(file_path)
    assert "project-room@chatroom" not in str(file_path)
    relative_parts = file_path.relative_to(tmp_path).parts
    assert re.fullmatch(r"tenant-[0-9a-f]{24}", relative_parts[0])
    assert re.fullmatch(r"session-[0-9a-f]{24}", relative_parts[1])
    assert re.fullmatch(r"request-[0-9a-f]{24}", relative_parts[2])
    assert content.startswith(b"\xef\xbb\xbf")
    decoded = content.decode("utf-8-sig")
    assert "会话：项目群" in decoded
    assert "时间范围：2026-07-29" in decoded
    assert "消息数量：2" in decoded
    assert "[2026-07-29 09:00:00] 张三: 周五交付" in decoded
    assert "[2026-07-29 09:01:00] wxid-lisi: [图片]" in decoded
    assert "这是机器人回复" not in decoded
    assert result["file_name"] == "消息汇总-项目群-2026-07-29.txt"
    assert result["file_size"] == len(content)
    assert result["file_md5"] == hashlib.md5(content).hexdigest()
    assert result["file_sha256"] == hashlib.sha256(content).hexdigest()
    assert result["message_count"] == 2


def test_stage_message_export_is_idempotent_and_rejects_content_conflicts(
    tmp_path: Path,
) -> None:
    first = _stage(tmp_path)
    first_path = Path(str(first["file_path"]))
    first_mtime = first_path.stat().st_mtime_ns

    replay = _stage(tmp_path)

    assert replay == first
    assert first_path.stat().st_mtime_ns == first_mtime
    with pytest.raises(MessageExportConflict, match="different content"):
        _stage(tmp_path, summary_text="同一个请求不得改写为另一份汇总。")
    assert first_path.read_bytes().decode("utf-8-sig").find("今天确认了交付时间。") >= 0


def test_stage_message_export_sanitizes_display_name_and_validates_root(
    tmp_path: Path,
) -> None:
    result = stage_message_export(
        tmp_path,
        tenant_id="tenant",
        session_id="session",
        request_id="request",
        session_name='  项目<>:"/\\|?*\n群. ',
        period="../../7月\r\n汇总",
        summary_text="摘要",
        messages=[],
    )

    filename = str(result["file_name"])
    assert filename.endswith(".txt")
    assert not any(character in filename for character in '<>:"/\\|?*\r\n')
    assert len(filename.encode("utf-8")) <= 220
    assert Path(str(result["file_path"])).is_relative_to(tmp_path)

    with pytest.raises(InvalidMessageExportPath, match="must be absolute"):
        stage_message_export(
            "relative/outbox",
            "tenant",
            "session",
            "request",
            "会话",
            "今天",
            "摘要",
            [],
        )
    with pytest.raises(ValueError, match="tenant_id"):
        stage_message_export(tmp_path, "", "session", "request", "会话", "今天", "摘要", [])


def test_stage_message_export_enforces_encoded_byte_limit_without_partial_file(
    tmp_path: Path,
) -> None:
    with pytest.raises(MessageExportTooLarge, match="limit is 64 bytes"):
        stage_message_export(
            tmp_path,
            "tenant",
            "session",
            "request",
            "会话",
            "今天",
            "超长摘要" * 100,
            [],
            max_bytes=64,
        )

    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode and gid semantics")
def test_stage_message_export_is_group_readable_but_not_group_writable(tmp_path: Path) -> None:
    result = _stage(tmp_path)
    file_path = Path(str(result["file_path"]))
    relative = file_path.relative_to(tmp_path)
    current = tmp_path
    for part in relative.parts[:-1]:
        current = current / part
        assert stat.S_IMODE(current.stat().st_mode) == 0o750
        assert current.stat().st_gid == tmp_path.stat().st_gid

    assert stat.S_IMODE(file_path.stat().st_mode) == 0o640
    assert file_path.stat().st_gid == tmp_path.stat().st_gid


@pytest.mark.skipif(os.name != "posix", reason="POSIX setgid semantics")
def test_stage_message_export_preserves_setgid_for_nested_group_inheritance(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o2770)

    result = _stage(tmp_path)

    file_path = Path(str(result["file_path"]))
    current = tmp_path
    for part in file_path.relative_to(tmp_path).parts[:-1]:
        current = current / part
        assert stat.S_IMODE(current.stat().st_mode) == 0o2750
        assert current.stat().st_gid == tmp_path.stat().st_gid
    assert stat.S_IMODE(file_path.stat().st_mode) == 0o640
    assert file_path.stat().st_gid == tmp_path.stat().st_gid


def test_cleanup_preserves_protected_files_and_expires_only_owned_regular_files(
    tmp_path: Path,
) -> None:
    old_protected = Path(str(_stage(tmp_path, request_id="protected-old")["file_path"]))
    expired = Path(str(_stage(tmp_path, request_id="expired")["file_path"]))
    within_grace = Path(str(_stage(tmp_path, request_id="within-grace")["file_path"]))
    fresh = Path(str(_stage(tmp_path, request_id="fresh")["file_path"]))
    unknown = tmp_path / "do-not-delete.txt"
    unknown.write_text("owned by somebody else", encoding="utf-8")
    os.utime(old_protected, (1, 1))
    os.utime(expired, (8_800, 8_800))
    os.utime(within_grace, (9_050, 9_050))
    os.utime(fresh, (9_500, 9_500))
    os.utime(unknown, (1, 1))

    result = cleanup_message_exports(
        tmp_path,
        protected_paths=[old_protected],
        retention_seconds=900,
        cleanup_grace_seconds=100,
        now=10_000,
    )

    assert result["removed_count"] == 1
    assert result["removed_paths"] == [str(expired)]
    assert result["retained_count"] == 3
    assert result["errors"] == []
    assert old_protected.exists()
    assert not expired.exists()
    assert within_grace.exists()
    assert fresh.exists()
    assert unknown.read_text(encoding="utf-8") == "owned by somebody else"


def test_cleanup_rejects_protected_paths_outside_staging_root(tmp_path: Path) -> None:
    artifact = Path(str(_stage(tmp_path)["file_path"]))
    outside = tmp_path.parent / "outside-message-export.txt"

    with pytest.raises(InvalidMessageExportPath, match="escapes"):
        cleanup_message_exports(tmp_path, protected_paths=[outside])
    assert artifact.exists()


def test_build_message_export_summary_calculates_daily_statistics_deterministically() -> None:
    summary = build_message_export_summary(
        "项目群",
        "2026-07-29",
        [
            {
                "timestamp": "2026-07-29 09:01:00",
                "sender_wxid": "wxid-zhang",
                "sender_name": "张三",
                "msg_type": "text",
            },
            {
                "timestamp": "2026-07-29 09:20:00",
                "sender_wxid": "wxid-zhang",
                "sender_name": "张三",
                "msg_type": "image",
            },
            {
                "timestamp": "2026-07-29 09:30:00",
                "sender_wxid": "wxid-wang",
                "sender_name": "王五",
                "msg_type": "file",
            },
            {
                "timestamp": "2026-07-29 10:00:00",
                "sender_wxid": "wxid-li",
                "sender_name": "李四",
                "msg_type": "text",
            },
            {
                "timestamp": "2026-07-29 10:30:00",
                "sender_wxid": "wxid-li",
                "sender_name": "李四",
                "msg_type": "audio",
            },
            {
                "timestamp": "2026-07-29 11:00:00",
                "sender_name": "机器人",
                "msg_type": "text",
                "is_self_sent": True,
            },
        ],
    )

    assert summary.startswith("[项目群] 日报 · 2026-07-29")
    assert "消息总数：5 条" in summary
    assert "参与人数：3 人" in summary
    assert "消息类型：文字 2 条、图片 1 条、语音 1 条、文件 1 条" in summary
    assert "高峰时段：09:00 - 10:00（3 条）" in summary
    assert "1. 李四 — 2 条" in summary
    assert "2. 张三 — 2 条" in summary
    assert "3. 王五 — 1 条" in summary
    assert "机器人" not in summary


def test_build_message_export_summary_monthly_has_no_unbacked_comparison_or_peak() -> None:
    summary = build_message_export_summary(
        "项目群",
        "2026-07",
        [
            {
                "timestamp": "2026-07-01 09:00:00",
                "sender_name": "张三",
                "msg_type": "video",
            },
            {
                "timestamp": "2026-07-20 18:00:00",
                "sender_name": "张三",
                "msg_type": "custom",
            },
        ],
        report_type="monthly",
    )

    assert summary.startswith("[项目群] 月报 · 2026-07")
    assert "消息总数：2 条" in summary
    assert "参与人数：1 人" in summary
    assert "消息类型：视频 1 条、custom 1 条" in summary
    assert "高峰时段" not in summary
    assert "上月" not in summary
    assert "vs" not in summary.lower()


def test_build_message_export_summary_empty_payload_is_explicit() -> None:
    summary = build_message_export_summary(
        "私聊",
        "2026-07-29",
        [{"sender_name": "机器人", "text": "回复", "is_self_sent": True}],
    )

    assert summary == "[私聊] 日报 · 2026-07-29\n\n暂无可汇总的消息记录。"
