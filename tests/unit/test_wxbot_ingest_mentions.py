from __future__ import annotations

import importlib
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace


def _load_ingest(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "wechat_data_dir": str(tmp_path / "wechat"),
                "self_wxid": "wxid_bot",
                "decrypted_dir": str(tmp_path / "decrypted"),
                "my_names": ["机器人", "小助手"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("WXBOT_CONFIG", str(config_path))
    monkeypatch.syspath_prepend(str(Path(__file__).parents[2] / "wxbot_client"))
    monkeypatch.setitem(
        sys.modules,
        "zstandard",
        SimpleNamespace(ZstdDecompressor=lambda: SimpleNamespace()),
    )
    for name in ("config", "queue_store", "sealed_core.ingest"):
        sys.modules.pop(name, None)
    return importlib.import_module("sealed_core.ingest")


def test_bot_mention_analysis_preserves_other_leading_mentions(
    monkeypatch,
    tmp_path,
) -> None:
    ingest = _load_ingest(monkeypatch, tmp_path)

    result = ingest._analyze_bot_mentions("@机器人\u2005@张三 你怎么看")

    assert result["mentioned_me"] is True
    assert result["bot_addressed"] is True
    assert result["bot_mention_position"] == "leading"
    assert result["bot_mention_names"] == ["机器人"]
    assert result["at_wxids"] == ["wxid_bot"]
    assert result["bot_normalized_content"] == "@张三 你怎么看"


def test_inline_bot_mention_is_not_treated_as_direct_address(
    monkeypatch,
    tmp_path,
) -> None:
    ingest = _load_ingest(monkeypatch, tmp_path)

    result = ingest._analyze_bot_mentions("你觉得@机器人 刚才说得对吗")

    assert result["mentioned_me"] is True
    assert result["bot_addressed"] is False
    assert result["bot_mention_position"] == "inline"
    assert result["bot_normalized_content"] == ""


def test_account_display_name_recognizes_real_u2005_direct_mention(
    monkeypatch,
    tmp_path,
) -> None:
    ingest = _load_ingest(monkeypatch, tmp_path)

    result = ingest._analyze_bot_mentions(
        "@zzz\u2005你知道吗",
        self_display_name="zzz",
    )

    assert "zzz" not in ingest.config.MY_NAMES
    assert result["mentioned_me"] is True
    assert result["bot_addressed"] is True
    assert result["mention_mode"] == "text_name_match"
    assert result["at_wxids"] == ["wxid_bot"]
    assert result["bot_mention_names"] == ["zzz"]
    assert result["bot_normalized_content"] == "你知道吗"


def test_account_display_name_does_not_match_a_different_member(
    monkeypatch,
    tmp_path,
) -> None:
    ingest = _load_ingest(monkeypatch, tmp_path)

    result = ingest._analyze_bot_mentions(
        "@张三\u2005你知道吗",
        self_display_name="zzz",
    )

    assert result["mentioned_me"] is False
    assert result["at_wxids"] == []


def test_structured_atuserlist_is_authoritative_for_self_wxid(
    monkeypatch,
    tmp_path,
) -> None:
    ingest = _load_ingest(monkeypatch, tmp_path)
    at_wxids, present = ingest._parse_at_wxids(
        "<msgsource><atuserlist><![CDATA[wxid_other,wxid_bot]]>"
        "</atuserlist></msgsource>"
    )

    result = ingest._analyze_bot_mentions(
        "@张三\u2005@改过昵称的机器人\u2005帮我看看",
        structured_at_wxids=at_wxids,
    )

    assert present is True
    assert result["mentioned_me"] is True
    assert result["bot_addressed"] is True
    assert result["mention_mode"] == "metadata"
    assert result["at_wxids"] == ["wxid_other", "wxid_bot"]


def test_structured_atuserlist_pointing_elsewhere_blocks_alias_fallback(
    monkeypatch,
    tmp_path,
) -> None:
    ingest = _load_ingest(monkeypatch, tmp_path)

    result = ingest._analyze_bot_mentions(
        "@机器人\u2005你知道吗",
        self_display_name="机器人",
        structured_at_wxids=["wxid_other"],
    )

    assert result["mentioned_me"] is False
    assert result["bot_addressed"] is False
    assert result["mention_mode"] == "metadata"
    assert result["at_wxids"] == ["wxid_other"]
    assert result["bot_normalized_content"] == ""


def test_empty_structured_atuserlist_is_present_and_authoritative(
    monkeypatch,
    tmp_path,
) -> None:
    ingest = _load_ingest(monkeypatch, tmp_path)

    at_wxids, present = ingest._parse_at_wxids(
        "<msgsource><atuserlist></atuserlist></msgsource>"
    )

    assert present is True
    assert at_wxids == []


def test_scan_uses_msgsource_atuserlist_before_nickname_fallback(
    monkeypatch,
    tmp_path,
) -> None:
    ingest = _load_ingest(monkeypatch, tmp_path)
    _create_message_db(ingest, ["wxid_bot", "wxid_sender"])
    table = "Msg_structured_mention"
    with sqlite3.connect(ingest.MSG_DB) as db:
        db.execute(
            f"CREATE TABLE {table} ("
            "local_id INTEGER, server_id INTEGER, real_sender_id INTEGER, "
            "create_time INTEGER, local_type INTEGER, message_content TEXT, "
            "WCDB_CT_message_content INTEGER, source TEXT, WCDB_CT_source INTEGER)"
        )
        db.execute(
            f"INSERT INTO {table} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                1,
                8900,
                2,
                1_700_000_000,
                1,
                "wxid_sender:\n@改过昵称的机器人\u2005你知道吗",
                0,
                "<msgsource><atuserlist><![CDATA[wxid_bot]]>"
                "</atuserlist></msgsource>",
                0,
            ),
        )

    captured = []
    ingest.require_capability = lambda _capability: None
    ingest.build_session_mapping = lambda: {
        table: {
            "session_id": "room@chatroom",
            "session_name": "测试群",
            "kind": "group",
        }
    }
    ingest.qs.get_ingest_cursor = lambda _table: 0
    ingest.qs.set_ingest_cursor = lambda _table, _value: None
    ingest.qs.pull_pending_media = lambda limit=100: []
    ingest.qs.list_inbound_unready_images = lambda limit=100: []

    def capture(**kwargs):
        captured.append(kwargs)
        return True

    ingest.qs.push_inbound = capture

    count = ingest.scan_once()

    assert count == 1
    assert captured[0]["metadata"]["mentioned_me"] is True
    assert captured[0]["metadata"]["mention_mode"] == "metadata"
    assert captured[0]["metadata"]["at_wxids"] == ["wxid_bot"]


def test_new_client_config_captures_all_group_messages_by_default(
    monkeypatch,
    tmp_path,
) -> None:
    ingest = _load_ingest(monkeypatch, tmp_path)

    assert ingest.config.GROUP_REQUIRE_AT_ME is False
    assert ingest._should_ingest_group_body("普通群消息", mentioned_me=False) is True


def _create_message_db(ingest, rows: list[str]) -> None:
    db_path = Path(ingest.MSG_DB)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE Name2Id (user_name TEXT NOT NULL)")
        db.executemany(
            "INSERT INTO Name2Id (user_name) VALUES (?)",
            [(value,) for value in rows],
        )


def test_self_identity_requires_one_resolved_name2id_row(
    monkeypatch,
    tmp_path,
) -> None:
    ingest = _load_ingest(monkeypatch, tmp_path)
    _create_message_db(ingest, ["wxid_bot", "room@chatroom"])

    identity = ingest.resolve_self_identity()

    assert identity["ready"] is True
    assert identity["self_wxid"] == "wxid_bot"
    assert identity["self_rowid"] == 1
    assert identity["reason"] == ""
    assert ingest.identity_status()["self_rowid"] == 1


def test_scan_fails_closed_before_capture_when_self_rowid_is_missing(
    monkeypatch,
    tmp_path,
) -> None:
    ingest = _load_ingest(monkeypatch, tmp_path)
    _create_message_db(ingest, ["room@chatroom"])
    ingest.require_capability = lambda _capability: None

    def unexpected_capture(*args, **kwargs):
        _ = args, kwargs
        raise AssertionError("capture must not run without verified self identity")

    ingest.qs.push_inbound = unexpected_capture
    ingest.qs.set_ingest_cursor = unexpected_capture

    count = ingest.scan_once()

    assert count == 0
    identity = ingest.identity_status()
    assert identity["ready"] is False
    assert identity["reason"] == "self_rowid_missing"
