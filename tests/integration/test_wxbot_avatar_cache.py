"""Real SQLCipher/WAL integration checks for the SDK cache repair helper."""
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

cipher = pytest.importorskip("sqlcipher3.dbapi2")
spec = importlib.util.spec_from_file_location(
    "refresh_avatar", Path(__file__).parents[2] / "scripts/refresh_wxbot_avatar_cache.py"
)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


@pytest.fixture
def account(tmp_path):
    source, cache, keys = [tmp_path / p for p in ("source.db", "cache.db", "keys.json")]
    key = "ab" * 32
    keys.write_text(json.dumps({"head_image/head_image.db": key}))
    native = cipher.connect(source)
    native.execute("PRAGMA key=\"x'" + key + "'\"")
    native.execute("PRAGMA journal_mode=WAL")
    native.execute("PRAGMA wal_autocheckpoint=0")
    native.execute("CREATE TABLE head_image(username TEXT, md5 TEXT, image_buffer BLOB, update_time INTEGER)")
    native.commit()
    native.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    native.execute("INSERT INTO head_image VALUES ('group@chatroom', 'real', ?, 200)", (b"real-avatar",))
    native.commit()
    with sqlite3.connect(cache) as old:
        old.execute("CREATE TABLE head_image(username TEXT, md5 TEXT, image_buffer BLOB, update_time INTEGER)")
        old.execute("INSERT INTO head_image VALUES ('old@chatroom', 'old', ?, 100)", (b"old-avatar",))
    yield source, cache, keys, native
    native.close()


def read_rows(path):
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        return db.execute("SELECT username, image_buffer FROM head_image").fetchall()


def test_refresh_includes_committed_wal_and_preserves_source_and_backup(account):
    source, cache, keys, native = account
    before = source.read_bytes(), Path(str(source) + "-wal").read_bytes()
    assert helper.refresh(source, cache, keys) == {"status": "refreshed", "rows": 1}
    assert read_rows(cache) == [("group@chatroom", b"real-avatar")]
    backup = cache.with_suffix(".before-avatar-refresh.db")
    assert read_rows(backup) == [("old@chatroom", b"old-avatar")]
    assert backup.stat().st_mode & 0o777 == 0o600
    assert before == (source.read_bytes(), Path(str(source) + "-wal").read_bytes())
    assert native.execute("SELECT count(*) FROM head_image").fetchone() == (1,)
    assert helper.refresh(source, cache, keys)["status"] == "unchanged"
    assert read_rows(backup) == [("old@chatroom", b"old-avatar")]


def test_invalid_key_keeps_previous_cache(account):
    source, cache, keys, _ = account
    keys.write_text(json.dumps({"head_image/head_image.db": "cd" * 32}))
    with pytest.raises(cipher.DatabaseError):
        helper.refresh(source, cache, keys)
    assert read_rows(cache) == [("old@chatroom", b"old-avatar")]
    assert not list(cache.parent.glob(".avatar-snapshot-*"))


def test_empty_source_keeps_previous_cache(account):
    source, cache, keys, native = account
    native.execute("DELETE FROM head_image")
    native.commit()
    with pytest.raises(ValueError, match="empty"):
        helper.refresh(source, cache, keys)
    assert read_rows(cache) == [("old@chatroom", b"old-avatar")]


def test_source_cannot_be_destination(account):
    source, _, keys, _ = account
    with pytest.raises(ValueError, match="differ"):
        helper.refresh(source, source, keys)
