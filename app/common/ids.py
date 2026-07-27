from __future__ import annotations

import secrets
import time
import uuid


def new_uuid() -> str:
    return str(uuid.uuid4())


def new_trace_id() -> str:
    return "tr_" + secrets.token_hex(12)


def new_turn_id() -> str:
    return "tn_" + secrets.token_hex(10)


def new_session_id() -> str:
    return "se_" + secrets.token_hex(10)


def now_ms() -> int:
    return int(time.time() * 1000)
