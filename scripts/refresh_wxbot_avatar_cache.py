"""Refresh the SDK's avatar snapshot from the logged-in account's real database.

Requires sqlcipher3-binary==0.6.0. Never writes to the WeChat source database,
downloads replacement avatars, or changes the sender's identity checks.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sqlite3
import tempfile
from pathlib import Path


def refresh(source: Path, cache: Path, keys: Path) -> dict:
    from sqlcipher3 import dbapi2 as cipher

    source, cache, keys = source.resolve(), cache.resolve(), keys.resolve()
    if not source.is_file() or not cache.is_file() or not keys.is_file():
        raise ValueError("source, existing cache and account keys are required")
    if source == cache:
        raise ValueError("source must differ from SDK cache")
    lock_path = cache.with_suffix(".refresh.lock")
    with lock_path.open("a") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        key = json.loads(keys.read_text())["head_image/head_image.db"]
        if len(key) != 64 or any(c not in "0123456789abcdefABCDEF" for c in key):
            raise ValueError("invalid account key format")
        fd, name = tempfile.mkstemp(prefix=".avatar-snapshot-", suffix=".db", dir=cache.parent)
        os.close(fd)
        snapshot = Path(name)
        db = None
        try:
            db = cipher.connect(source.as_uri() + "?mode=ro", uri=True, timeout=10)
            db.execute("PRAGMA key=\"x'" + key + "'\"")
            db.execute("ATTACH DATABASE ? AS snapshot KEY ''", (str(snapshot),))
            db.execute("BEGIN")
            if db.execute("PRAGMA main.integrity_check").fetchall() != [("ok",)]:
                raise ValueError("source integrity check failed")
            db.execute("SELECT sqlcipher_export('snapshot')").fetchone()
            db.commit()
            db.execute("DETACH DATABASE snapshot")
            db.close()
            db = None
            with sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True) as fresh:
                if fresh.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError("snapshot integrity check failed")
                rows = fresh.execute("SELECT username, md5, image_buffer, update_time FROM head_image ORDER BY username").fetchall()
                if not rows:
                    raise ValueError("refusing empty avatar snapshot")
                with sqlite3.connect(cache.as_uri() + "?mode=ro", uri=True) as old:
                    prior = old.execute("SELECT username, md5, image_buffer, update_time FROM head_image ORDER BY username").fetchall()
                    if prior == rows:
                        return {"status": "unchanged", "rows": len(rows)}
                    # Keep one original rollback snapshot; SQLite backup includes WAL.
                    backup = cache.with_suffix(".before-avatar-refresh.db")
                    if not backup.exists():
                        backup.touch(mode=0o600, exist_ok=False)
                        with sqlite3.connect(backup) as rollback:
                            old.backup(rollback)
                # SQLite backup commits atomically and respects active SDK readers.
                with sqlite3.connect(cache, timeout=10) as destination:
                    fresh.backup(destination)
            return {"status": "refreshed", "rows": len(rows)}
        finally:
            if db is not None:
                db.close()
            for suffix in ("", "-journal", "-wal", "-shm"):
                Path(str(snapshot) + suffix).unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--keys", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        print(json.dumps(refresh(args.source, args.cache, args.keys)))
    except Exception as exc:
        # Avoid including database keys, SQL text or private contact data in logs.
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
