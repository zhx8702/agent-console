from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.persona_extract.offline import (
    OfflineBundleError,
    cleanup_expired_offline_exports,
    prepare_offline_bundle,
    read_offline_artifact,
)


def _job(checkpoint: dict | None = None) -> dict:
    return {
        "id": 41,
        "tenant_id": "demo",
        "session_id": "group@chatroom",
        "session_name": "测试群",
        "target_user_id": "wxid-alice",
        "target_name": "Alice",
        "checkpoint": checkpoint or {},
    }


def _write_payload(path: Path) -> list[dict[str, str]]:
    messages = [
        {
            "timestamp": "2026-07-01 10:00:00",
            "sender_name": "Alice",
            "text": "第一条",
        },
        {
            "timestamp": "2026-07-02 10:00:00",
            "sender_name": "Alice",
            "text": "第二条",
        },
    ]
    path.write_text(
        json.dumps({"status": "ok", "messages": messages}, ensure_ascii=False),
        encoding="utf-8",
    )
    return messages


def test_prepare_full_offline_bundle_streams_messages_and_contract(tmp_path: Path) -> None:
    raw = tmp_path / "raw.json"
    _write_payload(raw)
    archive = tmp_path / "bundle.zip"

    metadata = prepare_offline_bundle(
        raw_payload_path=raw,
        archive_path=archive,
        job=_job(),
        export_mode="full",
        slug="alice",
        baseline_artifact=None,
    )

    assert metadata["message_count"] == 2
    assert metadata["source_message_count"] == 2
    assert metadata["next_cursor"]["last_timestamp"] == "2026-07-02 10:00:00"
    with zipfile.ZipFile(archive) as bundle:
        assert {
            "AGENTS.md",
            "PROMPT.md",
            "manifest.json",
            "input/messages.jsonl",
            "output/SKILL.md",
            "output/persona.md",
            "output/work.md",
            "output/meta.json",
        }.issubset(bundle.namelist())
        lines = bundle.read("input/messages.jsonl").decode("utf-8").splitlines()
        assert [json.loads(line)["text"] for line in lines] == ["第一条", "第二条"]
        assert all(len(json.loads(line)["message_sha256"]) == 64 for line in lines)


def test_cleanup_removes_only_expired_persona_export_files(tmp_path: Path) -> None:
    expired = tmp_path / "persona-offline-7.zip"
    current = tmp_path / "persona-offline-8.zip"
    unrelated = tmp_path / "keep.zip"
    for path in (expired, current, unrelated):
        path.write_bytes(b"private")
    os.utime(expired, (1, 1))
    settings = SimpleNamespace(
        persona_extract_offline_export_dir=str(tmp_path),
        persona_extract_offline_retention_seconds=3_600,
    )

    removed = cleanup_expired_offline_exports(settings)

    assert removed == 1
    assert not expired.exists()
    assert current.exists()
    assert unrelated.exists()


def test_prepare_incremental_bundle_uses_timestamp_and_tail_hash_cursor(
    tmp_path: Path,
) -> None:
    raw = tmp_path / "raw.json"
    _write_payload(raw)
    first_archive = tmp_path / "full.zip"
    full = prepare_offline_bundle(
        raw_payload_path=raw,
        archive_path=first_archive,
        job=_job(),
        export_mode="full",
        slug="alice",
        baseline_artifact=None,
    )
    payload = json.loads(raw.read_text(encoding="utf-8"))
    payload["messages"].append(
        {
            "timestamp": "2026-07-01 12:00:00",
            "sender_name": "Alice",
            "text": "迟到但落在重叠窗口内",
        }
    )
    payload["messages"].append(
        {
            "timestamp": "2026-07-03 10:00:00",
            "sender_name": "Alice",
            "text": "第三条",
        }
    )
    raw.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    incremental_archive = tmp_path / "incremental.zip"
    job = _job({"baseline_cursor": full["next_cursor"]})

    incremental = prepare_offline_bundle(
        raw_payload_path=raw,
        archive_path=incremental_archive,
        job=job,
        export_mode="incremental",
        slug="alice",
        baseline_artifact={
            "files": {
                "SKILL.md": "# old skill",
                "persona.md": "# old persona",
                "work.md": "# old work",
            },
            "meta": {"impression": "old"},
        },
    )

    assert incremental["message_count"] == 2
    with zipfile.ZipFile(incremental_archive) as bundle:
        lines = bundle.read("input/messages.jsonl").decode("utf-8").splitlines()
        assert [json.loads(line)["text"] for line in lines] == [
            "迟到但落在重叠窗口内",
            "第三条",
        ]
        assert "baseline/SKILL.md" in bundle.namelist()


def test_read_offline_artifact_accepts_only_generated_output(tmp_path: Path) -> None:
    archive = tmp_path / "output.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("output/SKILL.md", "---\nname: colleague-alice\n---\n\n# Alice\n\n简洁。")
        bundle.writestr("output/persona.md", "# Alice 的表达风格参考\n\n直接。")
        bundle.writestr("output/work.md", "# Alice 的工作能力画像\n\n证据有限。")
        bundle.writestr(
            "output/meta.json",
            json.dumps(
                {
                    "slug": "alice",
                    "impression": "直接",
                    "profile": {"tone": "direct"},
                    "ignored_secret": "must not survive",
                },
                ensure_ascii=False,
            ),
        )

    result = read_offline_artifact(archive)

    assert result["skill_prompt"] == "# Alice\n\n简洁。"
    assert result["requested_slug"] == "alice"
    assert result["meta"] == {
        "impression": "直接",
        "profile": {"tone": "direct"},
    }


@pytest.mark.parametrize(
    "filename",
    [
        "../SKILL.md",
        "input/messages.jsonl",
        "output/extra.txt",
    ],
)
def test_read_offline_artifact_rejects_unsafe_or_raw_files(
    tmp_path: Path,
    filename: str,
) -> None:
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("output/SKILL.md", "# Alice")
        bundle.writestr("output/persona.md", "# Persona")
        bundle.writestr("output/work.md", "# Work")
        bundle.writestr("output/meta.json", "{}")
        bundle.writestr(filename, "private")

    with pytest.raises(OfflineBundleError):
        read_offline_artifact(archive)
