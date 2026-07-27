"""Scan decrypted WeChat DB for new messages and store in local queue.

Supports text (local_type=1) and image (local_type=3) messages.
Images are decrypted from V2 .dat format and stored locally.
"""
import datetime
import hashlib
import os
import re
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import queue_store as qs
import zstandard

import config
from sealed_core.runtime import require_capability

_zstd_dctx = zstandard.ZstdDecompressor()

MSG_DB = os.path.join(config.DECRYPTED_DIR, "message", "message_0.db")
CONTACT_DB = os.path.join(config.DECRYPTED_DIR, "contact", "contact.db")
_IMAGES_DIR = os.path.join(os.path.dirname(config.DECRYPTED_DIR), "images")

_GROUP_PREFIX_RE = re.compile(r"^([a-zA-Z0-9_@]+):\n(.*)$", re.DOTALL)
_LEADING_MENTION_RUN_RE = re.compile(
    r"^\s*(?:@\S+(?:[\s\u2005\u00a0,，.:：；;!！?？]+|$))+",
    re.IGNORECASE,
)
_MENTION_SEPARATOR_RE = r"(?=[\s\u2005\u00a0,，.:：；;!！?？]|$)"
_ATUSERLIST_RE = re.compile(
    r"<atuserlist(?:\s[^>]*)?>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</atuserlist>",
    re.IGNORECASE | re.DOTALL,
)
_IMAGE_STATUS_PENDING = "pending"
_IMAGE_STATUS_THUMBNAIL = "thumbnail"
_IMAGE_STATUS_READY = "ready"
_IMAGE_STATUS_FAILED = "failed"

_IDENTITY_LOCK = threading.Lock()
_IDENTITY_STATE = {
    "ready": False,
    "self_wxid": "",
    "self_rowid": None,
    "reason": "identity_not_checked",
    "checked_at": 0,
}


# ── Image helpers ──

def _parse_image_xml(xml_text):
    try:
        root = ET.fromstring(xml_text)
        img = root.find(".//img")
        if img is None:
            return {}
        return dict(img.attrib)
    except Exception:
        return {}


def _dat_variant(path):
    name = Path(path).name.lower()
    if name.endswith("_t.dat"):
        return "thumbnail"
    if name.endswith("_b.dat"):
        return "thumbnail"
    return "original"


def _find_dat_file(session_id, create_time, md5_val, *, include_thumbnail=False):
    data_dir = config.WECHAT_DATA_DIR
    if not data_dir:
        return None
    chat_hash = hashlib.md5(session_id.encode()).hexdigest()
    dt = datetime.datetime.fromtimestamp(create_time)
    month = dt.strftime("%Y-%m")

    attach_base = Path(data_dir) / "msg" / "attach"
    if md5_val:
        for name in (f"{md5_val}.dat", f"{md5_val}_h.dat"):
            candidate = attach_base / chat_hash / month / "Img" / name
            if candidate.exists():
                return str(candidate)
        if include_thumbnail:
            for name in (f"{md5_val}_t.dat", f"{md5_val}_b.dat"):
                candidate = attach_base / chat_hash / month / "Img" / name
                if candidate.exists():
                    return str(candidate)

    img_dir = attach_base / chat_hash / month / "Img"
    if img_dir.exists():
        dats = sorted(img_dir.glob("*.dat"), key=lambda p: p.stat().st_mtime)
        ts_lo, ts_hi = create_time - 60, create_time + 60
        for dat in dats:
            if ts_lo <= dat.stat().st_mtime <= ts_hi:
                if not dat.name.endswith(("_t.dat", "_b.dat")):
                    return str(dat)
        if include_thumbnail:
            for dat in dats:
                if ts_lo <= dat.stat().st_mtime <= ts_hi:
                    if dat.name.endswith(("_t.dat", "_b.dat")):
                        return str(dat)
    return None


def _decrypt_image_if_possible(dat_path, session_id, local_id, *, variant="original"):
    try:
        from image_decrypt import V2_SIG, decrypt_dat, detect_ext, load_image_key
    except ImportError:
        return "", "image_decrypt_import_failed"
    key_hex = load_image_key(config.DECRYPTED_DIR)
    if not key_hex:
        return "", "image_key_missing"
    try:
        data = Path(dat_path).read_bytes()
        if data[:6] != V2_SIG:
            return "", "invalid_v2_dat"
        result = decrypt_dat(data, key_hex.encode("ascii"))
        if result is None:
            return "", "decrypt_failed"
        ext = detect_ext(result)
        out_dir = Path(_IMAGES_DIR) / hashlib.md5(session_id.encode()).hexdigest()
        out_dir.mkdir(parents=True, exist_ok=True)
        suffix = "_thumb" if variant == "thumbnail" else ""
        out_path = out_dir / f"{local_id}{suffix}{ext}"
        out_path.write_bytes(result)
        return str(out_path), ""
    except Exception as exc:
        return "", f"decrypt_exception:{exc}"


def _resolve_image(session_id, create_time, local_id, xml_body):
    attrs = _parse_image_xml(xml_body)
    md5_val = attrs.get("md5", "")

    original = _find_dat_file(session_id, create_time, md5_val, include_thumbnail=False)
    if original:
        image_path, failure = _decrypt_image_if_possible(
            original,
            session_id,
            local_id,
            variant="original",
        )
        if image_path:
            return {
                "image_path": image_path,
                "media_status": _IMAGE_STATUS_READY,
                "media_variant": "original",
                "image_failure_reason": "",
            }
        return {
            "image_path": "",
            "media_status": _IMAGE_STATUS_FAILED,
            "media_variant": "original",
            "image_failure_reason": failure or "decrypt_failed",
        }

    if getattr(config, "IMAGE_DECRYPT_THUMBNAIL_FALLBACK", True):
        thumb = _find_dat_file(session_id, create_time, md5_val, include_thumbnail=True)
        if thumb:
            image_path, failure = _decrypt_image_if_possible(
                thumb,
                session_id,
                local_id,
                variant="thumbnail",
            )
            if image_path:
                return {
                    "image_path": image_path,
                    "media_status": _IMAGE_STATUS_THUMBNAIL,
                    "media_variant": _dat_variant(thumb),
                    "image_failure_reason": "",
                }
            return {
                "image_path": "",
                "media_status": _IMAGE_STATUS_FAILED,
                "media_variant": _dat_variant(thumb),
                "image_failure_reason": failure or "thumbnail_decrypt_failed",
            }

    return {
        "image_path": "",
        "media_status": _IMAGE_STATUS_PENDING,
        "media_variant": "",
        "image_failure_reason": "original_dat_not_found",
    }


def _try_resolve_image(session_id, create_time, local_id, xml_body):
    result = _resolve_image(session_id, create_time, local_id, xml_body)
    return result["image_path"] or None


def _queue_image(row, result):
    image_path = result.get("image_path") or ""
    media_status = result.get("media_status") or _IMAGE_STATUS_PENDING
    media_variant = result.get("media_variant") or ""
    image_failure_reason = result.get("image_failure_reason") or ""
    inserted = qs.push_inbound(
        msg_svr_id=row["msg_svr_id"],
        session_id=row["session_id"],
        session_name=row["session_name"],
        sender_wxid=row["sender_wxid"],
        sender_name=row["sender_name"],
        msg_text="[图片]",
        recv_ts=row["recv_ts"],
        msg_type="image",
        image_path=image_path,
        media_status=media_status,
        image_failure_reason=image_failure_reason,
        media_variant=media_variant,
        metadata=row.get("metadata") or {
            key: row.get(key)
            for key in (
                "mentioned_me",
                "mention_mode",
                "is_self_sent",
                "at_wxids",
                "quote",
                "bot_mentioned",
                "bot_addressed",
                "bot_mention_position",
                "bot_mention_names",
                "bot_normalized_content",
                "bot_wxid",
                "identity_resolved",
                "self_wxid",
                "self_rowid",
                "capture_allowed",
                "capture_reason",
            )
            if row.get(key) is not None
        },
    )
    if inserted:
        return "inserted"
    if qs.update_inbound_image_path(
        row["msg_svr_id"],
        image_path,
        media_status=media_status,
        image_failure_reason=image_failure_reason,
        media_variant=media_variant,
    ):
        return "updated"
    return "duplicate"


def _retry_pending_media(identity=None):
    ready = 0
    for row in qs.pull_pending_media(limit=100):
        result = _resolve_image(
            row["session_id"],
            row["recv_ts"],
            row["local_id"],
            row["xml_body"] or "",
        )
        status = result["media_status"]
        if status == _IMAGE_STATUS_PENDING:
            qs.save_pending_media(
                **{key: row[key] for key in (
                    "msg_svr_id", "table_name", "local_id", "session_id",
                    "session_name", "sender_wxid", "sender_name", "xml_body", "recv_ts"
                )},
                image_path=result["image_path"],
                media_status=status,
                image_failure_reason=result["image_failure_reason"],
                media_variant=result["media_variant"],
            )
            continue
        queued_row = dict(row)
        metadata = dict(queued_row.get("metadata") or {})
        if isinstance(identity, dict) and identity.get("ready") is True:
            metadata.update(_identity_metadata(identity))
        queued_row["metadata"] = metadata
        action = _queue_image(queued_row, result)
        if status == _IMAGE_STATUS_READY:
            qs.delete_pending_media(row["msg_svr_id"])
        elif status == _IMAGE_STATUS_THUMBNAIL:
            qs.save_pending_media(
                **{key: row[key] for key in (
                    "msg_svr_id", "table_name", "local_id", "session_id",
                    "session_name", "sender_wxid", "sender_name", "xml_body", "recv_ts"
                )},
                image_path=result["image_path"],
                media_status=status,
                image_failure_reason=result["image_failure_reason"],
                media_variant=result["media_variant"],
            )
        else:
            qs.delete_pending_media(row["msg_svr_id"])
        ready += 1
        print(
            f"[ingest] media {status} msg={row['msg_svr_id']} "
            f"result={action} variant={result['media_variant']}"
        )
    return ready


def _find_message_row_by_server_id(mc, table, msg_svr_id):
    try:
        return mc.execute(
            f"SELECT local_id, server_id, create_time, message_content, "
            f"       WCDB_CT_message_content AS ct "
            f"FROM {table} WHERE CAST(server_id AS TEXT)=? LIMIT 1",
            (str(msg_svr_id),),
        ).fetchone()
    except Exception:
        return None


def _repair_unready_inbound_images(mc, sessions):
    repaired = 0
    for row in qs.list_inbound_unready_images(limit=100):
        table = ""
        for candidate_table, info in sessions.items():
            if info["session_id"] == row["session_id"]:
                table = candidate_table
                break
        if not table:
            continue

        msg_row = _find_message_row_by_server_id(mc, table, row["msg_svr_id"])
        if not msg_row:
            continue

        content = decode_content(msg_row["message_content"], msg_row["ct"] or 0)
        if sessions[table]["kind"] == "group":
            _, body = parse_group_message(content)
        else:
            body = content

        result = _resolve_image(
            row["session_id"],
            msg_row["create_time"] or row["recv_ts"],
            msg_row["local_id"],
            body,
        )
        if result["media_status"] == _IMAGE_STATUS_PENDING:
            continue
        if qs.update_inbound_image_path(
            row["msg_svr_id"],
            result["image_path"],
            media_status=result["media_status"],
            image_failure_reason=result["image_failure_reason"],
            media_variant=result["media_variant"],
        ):
            repaired += 1
            print(
                f"[ingest] repaired image msg={row['msg_svr_id']} "
                f"status={result['media_status']} variant={result['media_variant']}"
            )
    return repaired


# ── DB helpers ──

def _connect_ro(path):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    return c


def build_session_mapping():
    identity = resolve_self_identity()
    if identity.get("ready") is not True:
        return {}
    self_wxid = str(identity.get("self_wxid") or "")
    overrides = config.SESSION_NAME_OVERRIDES
    with _connect_ro(MSG_DB) as mc:
        rows = mc.execute(
            "SELECT user_name FROM Name2Id "
            "WHERE user_name NOT IN (?, 'filehelper', '') "
            "AND user_name NOT LIKE 'gh_%' "
            "AND user_name NOT LIKE '%@openim'",
            (self_wxid,)
        ).fetchall()
    session_ids = [r["user_name"] for r in rows]

    name_map = {}
    try:
        with _connect_ro(CONTACT_DB) as cc:
            for r in cc.execute("SELECT username, nick_name, remark FROM contact"):
                name_map[r["username"]] = r["remark"] or r["nick_name"] or ""
    except Exception:
        pass

    out = {}
    for sid in session_ids:
        kind = "group" if sid.endswith("@chatroom") else "private"
        display = overrides.get(sid) or name_map.get(sid) or sid
        table = "Msg_" + hashlib.md5(sid.encode()).hexdigest()
        out[table] = {"session_id": sid, "session_name": display, "kind": kind}
    return out


def _self_rowid(mc):
    rows = mc.execute(
        "SELECT rowid FROM Name2Id WHERE user_name = ? LIMIT 2",
        (str(getattr(config, "SELF_WXID", "") or "").strip(),),
    ).fetchall()
    if len(rows) != 1:
        return -1
    try:
        rowid = int(rows[0]["rowid"])
    except (TypeError, ValueError):
        return -1
    return rowid if rowid > 0 else -1


def _set_identity_state(*, ready, self_wxid="", self_rowid=None, reason=""):
    snapshot = {
        "ready": bool(ready),
        "self_wxid": str(self_wxid or "").strip() if ready else "",
        "self_rowid": int(self_rowid) if ready and self_rowid is not None else None,
        "reason": "" if ready else str(reason or "self_identity_unavailable")[:64],
        "checked_at": int(time.time()),
    }
    with _IDENTITY_LOCK:
        _IDENTITY_STATE.clear()
        _IDENTITY_STATE.update(snapshot)
    return dict(snapshot)


def resolve_self_identity():
    """Resolve the SDK account to one unambiguous Name2Id row."""

    self_wxid = str(getattr(config, "SELF_WXID", "") or "").strip()
    if not self_wxid or self_wxid.lower() == "auto":
        return _set_identity_state(
            ready=False,
            reason="self_wxid_missing",
        )
    if not Path(MSG_DB).is_file():
        return _set_identity_state(
            ready=False,
            reason="message_database_missing",
        )
    try:
        with _connect_ro(MSG_DB) as mc:
            rows = mc.execute(
                "SELECT rowid, user_name FROM Name2Id "
                "WHERE user_name = ? LIMIT 2",
                (self_wxid,),
            ).fetchall()
    except Exception:
        return _set_identity_state(
            ready=False,
            reason="self_identity_lookup_failed",
        )
    if not rows:
        return _set_identity_state(
            ready=False,
            reason="self_rowid_missing",
        )
    if len(rows) != 1:
        return _set_identity_state(
            ready=False,
            reason="self_identity_ambiguous",
        )
    try:
        self_rowid = int(rows[0]["rowid"])
    except (TypeError, ValueError):
        self_rowid = -1
    if self_rowid <= 0 or str(rows[0]["user_name"] or "").strip() != self_wxid:
        return _set_identity_state(
            ready=False,
            reason="self_rowid_invalid",
        )
    return _set_identity_state(
        ready=True,
        self_wxid=self_wxid,
        self_rowid=self_rowid,
    )


def identity_status(refresh=False):
    if refresh:
        return resolve_self_identity()
    with _IDENTITY_LOCK:
        return dict(_IDENTITY_STATE)


def _identity_metadata(identity):
    return {
        "identity_resolved": True,
        "self_wxid": str(identity.get("self_wxid") or ""),
        "self_rowid": int(identity.get("self_rowid")),
        "bot_wxid": str(identity.get("self_wxid") or ""),
    }


def resolve_contact_name(wxid):
    if not wxid:
        return ""
    try:
        with _connect_ro(CONTACT_DB) as cc:
            row = cc.execute(
                "SELECT nick_name, remark FROM contact WHERE username=?", (wxid,)
            ).fetchone()
            if row:
                return row["remark"] or row["nick_name"] or wxid
    except Exception:
        pass
    return wxid


def _parse_at_wxids(source):
    """Return (wxids, present) from WeChat's msgsource atuserlist field."""

    raw = str(source or "").strip()
    if not raw:
        return [], False
    value = None
    try:
        root = ET.fromstring(raw)
        node = root.find(".//atuserlist")
        if node is not None:
            value = node.text or ""
    except ET.ParseError:
        match = _ATUSERLIST_RE.search(raw)
        if match:
            value = match.group(1)
    if value is None:
        return [], False
    wxids = []
    seen = set()
    for item in re.split(r"[,;，；\s]+", value):
        wxid = str(item or "").strip()
        if wxid and wxid not in seen:
            seen.add(wxid)
            wxids.append(wxid)
    return wxids, True


def _analyze_bot_mentions(
    body,
    *,
    self_display_name="",
    structured_at_wxids=None,
):
    text = str(body or "")
    # ``my_names`` remains useful for stable aliases, but the account's actual
    # WeChat display name is authoritative for the common ``@当前昵称`` case.
    # Resolve it from the verified self wxid once per scan so a nickname change
    # cannot silently make direct mentions disappear until config is edited.
    bot_names = [
        str(name).strip()
        for name in (*config.MY_NAMES, self_display_name)
        if str(name).strip()
    ]
    matches = []
    matched_names = []
    for name in sorted(set(bot_names), key=len, reverse=True):
        pattern = re.compile(rf"@{re.escape(name)}{_MENTION_SEPARATOR_RE}", re.IGNORECASE)
        name_matches = list(pattern.finditer(text))
        if not name_matches:
            continue
        matches.extend(name_matches)
        matched_names.append(name)

    bot_wxid = str(getattr(config, "SELF_WXID", "") or "").strip()
    structured_mention = structured_at_wxids is not None
    actual_at_wxids = []
    if structured_mention:
        seen_wxids = set()
        for item in structured_at_wxids:
            wxid = str(item or "").strip()
            if wxid and wxid not in seen_wxids:
                seen_wxids.add(wxid)
                actual_at_wxids.append(wxid)
        mentioned_me = bool(bot_wxid and bot_wxid in seen_wxids)
        # A structured list explicitly pointing elsewhere is authoritative.
        # Do not let a duplicate display name turn @another member into @bot.
        if not mentioned_me:
            matches = []
            matched_names = []
    else:
        mentioned_me = bool(matches)
    leading_run = _LEADING_MENTION_RUN_RE.match(text)
    leading_end = leading_run.end() if leading_run else 0
    bot_addressed = mentioned_me and any(match.start() < leading_end for match in matches)
    if not bot_addressed:
        bot_addressed = mentioned_me and any(
            match.start() == len(text) - len(text.lstrip()) for match in matches
        )
    if mentioned_me and structured_mention and not matches and leading_end > 0:
        # atuserlist preserves mention order. This fallback handles a verified
        # self wxid even when the visible nickname is absent from local aliases.
        leading_mention_count = len(re.findall(r"@\S+", text[:leading_end]))
        bot_addressed = actual_at_wxids.index(bot_wxid) < leading_mention_count

    normalized = text
    if bot_addressed and leading_end > 0:
        prefix = text[:leading_end]
        suffix = text[leading_end:]
        for name in sorted(set(matched_names), key=len, reverse=True):
            prefix = re.sub(
                rf"@{re.escape(name)}(?:[\s\u2005\u00a0,，.:：；;!！?？]+|$)",
                "",
                prefix,
                flags=re.IGNORECASE,
            )
        normalized = f"{prefix.strip()} {suffix.lstrip()}".strip()

    return {
        "mentioned_me": mentioned_me,
        "mention_mode": (
            "metadata"
            if structured_mention
            else "text_name_match" if mentioned_me else ""
        ),
        "is_self_sent": False,
        "at_wxids": (
            actual_at_wxids
            if structured_mention
            else [bot_wxid] if mentioned_me and bot_wxid else []
        ),
        "quote": None,
        "bot_mentioned": mentioned_me,
        "bot_addressed": bot_addressed,
        "bot_mention_position": (
            "leading" if bot_addressed else "inline" if mentioned_me else ""
        ),
        "bot_mention_names": matched_names,
        "bot_normalized_content": normalized if bot_addressed else "",
        "bot_wxid": bot_wxid,
    }


def _is_at_me(body):
    return bool(_analyze_bot_mentions(body)["mentioned_me"])


def _is_command_body(body):
    return str(body or "").strip().startswith("/")


def _should_ingest_group_body(body, *, mentioned_me=None):
    if _is_command_body(body):
        return True
    if not getattr(config, "GROUP_REQUIRE_AT_ME", True):
        return True
    if mentioned_me is None:
        mentioned_me = _is_at_me(body)
    return bool(mentioned_me)


def decode_content(raw, ct_flag):
    if raw is None:
        return ""
    if ct_flag == 4 and isinstance(raw, (bytes, bytearray)):
        try:
            return _zstd_dctx.decompress(bytes(raw)).decode("utf-8", errors="replace")
        except Exception:
            return ""
    if isinstance(raw, (bytes, bytearray)):
        return raw.decode("utf-8", errors="replace")
    return raw


def parse_group_message(message_content):
    m = _GROUP_PREFIX_RE.match(message_content or "")
    if m:
        return m.group(1), m.group(2)
    return None, message_content or ""


def scan_once():
    require_capability("fetch_messages")
    identity = resolve_self_identity()
    if identity.get("ready") is not True:
        return 0
    sessions = build_session_mapping()
    if not sessions:
        return 0
    self_display_name = resolve_contact_name(str(identity.get("self_wxid") or ""))
    enqueued = 0
    with _connect_ro(MSG_DB) as mc:
        self_rowid = _self_rowid(mc)
        if self_rowid != int(identity.get("self_rowid") or -1):
            _set_identity_state(
                ready=False,
                reason="self_identity_changed",
            )
            return 0
        enqueued += _retry_pending_media(identity)
        enqueued += _repair_unready_inbound_images(mc, sessions)
        for table, info in sessions.items():
            exists = mc.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists:
                continue

            table_columns = {
                str(row["name"] or "")
                for row in mc.execute(f"PRAGMA table_info({table})").fetchall()
            }
            source_expr = "source" if "source" in table_columns else "NULL"
            source_ct_expr = (
                "WCDB_CT_source" if "WCDB_CT_source" in table_columns else "0"
            )

            cursor = qs.get_ingest_cursor(table)
            if cursor is None:
                row = mc.execute(f"SELECT COALESCE(MAX(local_id), 0) AS m FROM {table}").fetchone()
                qs.set_ingest_cursor(table, row["m"])
                continue

            rows = mc.execute(
                f"SELECT local_id, server_id, real_sender_id, create_time, local_type, "
                f"       message_content, WCDB_CT_message_content AS ct, "
                f"       {source_expr} AS source, {source_ct_expr} AS source_ct "
                f"FROM {table} WHERE local_id > ? AND local_type IN (1, 3) ORDER BY local_id",
                (cursor,),
            ).fetchall()
            if not rows:
                continue

            new_max = cursor
            for r in rows:
                new_max = max(new_max, r["local_id"])
                content = decode_content(r["message_content"], r["ct"] or 0)
                source = decode_content(r["source"], r["source_ct"] or 0)
                recv_ts = r["create_time"] or int(time.time())
                msg_svr_id = str(r["server_id"]) if r["server_id"] else f"{table}:{r['local_id']}"
                is_image = r["local_type"] == 3

                is_self_sent = (self_rowid >= 0 and r["real_sender_id"] == self_rowid)

                if info["kind"] == "group":
                    sender_wxid, body = parse_group_message(content)

                    if is_self_sent:
                        continue

                    if not sender_wxid:
                        continue

                    sname = resolve_contact_name(sender_wxid)
                    at_wxids, atuserlist_present = _parse_at_wxids(source)
                    message_metadata = _analyze_bot_mentions(
                        body,
                        self_display_name=self_display_name,
                        structured_at_wxids=(at_wxids if atuserlist_present else None),
                    )
                    message_metadata.update(_identity_metadata(identity))
                    if _is_command_body(body):
                        message_metadata["capture_reason"] = "command"
                    elif message_metadata["mentioned_me"]:
                        message_metadata["capture_reason"] = "bot_mention"
                    else:
                        message_metadata["capture_reason"] = "all_group_messages"
                    message_metadata["capture_allowed"] = True

                    if is_image:
                        image_result = _resolve_image(
                            info["session_id"], recv_ts, r["local_id"], body)
                        if not _should_ingest_group_body(
                            body,
                            mentioned_me=message_metadata["mentioned_me"],
                        ):
                            continue
                        if image_result["media_status"] in {
                            _IMAGE_STATUS_PENDING,
                            _IMAGE_STATUS_THUMBNAIL,
                        }:
                            qs.save_pending_media(
                                msg_svr_id=msg_svr_id,
                                table_name=table,
                                local_id=r["local_id"],
                                session_id=info["session_id"],
                                session_name=info["session_name"],
                                sender_wxid=sender_wxid,
                                sender_name=sname,
                                xml_body=body,
                                recv_ts=recv_ts,
                                image_path=image_result["image_path"],
                                media_status=image_result["media_status"],
                                image_failure_reason=image_result["image_failure_reason"],
                                media_variant=image_result["media_variant"],
                                metadata=message_metadata,
                            )
                            print(
                                f"[ingest] image {image_result['media_status']} "
                                f"msg={msg_svr_id} "
                                f"variant={image_result['media_variant'] or 'N/A'}"
                            )
                        image_path = image_result["image_path"]
                        media_status = image_result["media_status"]
                        image_failure_reason = image_result["image_failure_reason"]
                        media_variant = image_result["media_variant"]
                        body = "[图片]"
                    else:
                        image_path = None
                        media_status = ""
                        image_failure_reason = ""
                        media_variant = ""
                        if not _should_ingest_group_body(
                            body,
                            mentioned_me=message_metadata["mentioned_me"],
                        ):
                            continue
                else:
                    sender_wxid = info["session_id"]
                    sname = info["session_name"]
                    body = content
                    message_metadata = {
                        "mentioned_me": False,
                        "mention_mode": "",
                        "is_self_sent": False,
                        "at_wxids": [],
                        "quote": None,
                        "bot_mentioned": False,
                        "bot_addressed": False,
                        "bot_mention_position": "",
                        "bot_mention_names": [],
                        "bot_normalized_content": "",
                        "bot_wxid": str(getattr(config, "SELF_WXID", "") or "").strip(),
                        "capture_allowed": True,
                        "capture_reason": "private_message",
                    }
                    message_metadata.update(_identity_metadata(identity))

                    if is_self_sent:
                        continue

                    if is_image:
                        image_result = _resolve_image(
                            info["session_id"], recv_ts, r["local_id"], body)
                        if image_result["media_status"] in {
                            _IMAGE_STATUS_PENDING,
                            _IMAGE_STATUS_THUMBNAIL,
                        }:
                            qs.save_pending_media(
                                msg_svr_id=msg_svr_id,
                                table_name=table,
                                local_id=r["local_id"],
                                session_id=info["session_id"],
                                session_name=info["session_name"],
                                sender_wxid=sender_wxid,
                                sender_name=sname,
                                xml_body=body,
                                recv_ts=recv_ts,
                                image_path=image_result["image_path"],
                                media_status=image_result["media_status"],
                                image_failure_reason=image_result["image_failure_reason"],
                                media_variant=image_result["media_variant"],
                                metadata=message_metadata,
                            )
                            print(
                                f"[ingest] image {image_result['media_status']} "
                                f"msg={msg_svr_id} "
                                f"variant={image_result['media_variant'] or 'N/A'}"
                            )
                        image_path = image_result["image_path"]
                        media_status = image_result["media_status"]
                        image_failure_reason = image_result["image_failure_reason"]
                        media_variant = image_result["media_variant"]
                        body = "[图片]"
                    else:
                        image_path = None
                        media_status = ""
                        image_failure_reason = ""
                        media_variant = ""

                inserted = qs.push_inbound(
                    msg_svr_id=msg_svr_id,
                    session_id=info["session_id"],
                    session_name=info["session_name"],
                    sender_wxid=sender_wxid,
                    sender_name=sname,
                    msg_text=body,
                    recv_ts=recv_ts,
                    msg_type="image" if is_image else "text",
                    image_path=image_path,
                    media_status=media_status,
                    image_failure_reason=image_failure_reason,
                    media_variant=media_variant,
                    metadata=message_metadata,
                )
                if inserted:
                    enqueued += 1
                    print(
                        f"[ingest] id={inserted} kind={info['kind']} "
                        f"media={'image' if is_image else 'text'}"
                    )

            qs.set_ingest_cursor(table, new_max)
    return enqueued


def run_forever():
    import events
    print(f"[ingest] started interval={config.INGEST_INTERVAL}s")
    while True:
        try:
            n = scan_once()
            if n:
                print(f"[ingest] +{n} new messages queued")
                events.send_wakeup.set()
        except Exception as e:
            print(f"[ingest] error type={e.__class__.__name__}")
        events.wait_or_timeout(events.ingest_wakeup, config.INGEST_INTERVAL)
