"""Scope-bound keyset pagination for the knowledge review console."""
from __future__ import annotations

import base64
import json
from datetime import datetime
from uuid import UUID

from app.jev.models import fingerprint
from app.jev.store import execute

_TABLES = {"jobs": "jev_knowledge_job", "candidates": "jev_knowledge_candidate", "findings": "jev_quality_finding"}


def decode_cursor(cursor: str, scope: str) -> tuple[datetime, str]:
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        if value['scope'] != scope:
            raise ValueError('cursor_scope_changed')
        timestamp = datetime.fromisoformat(value['created_at'])
        if timestamp.tzinfo is None:
            raise ValueError('cursor_timezone_missing')
        return timestamp, str(UUID(value['id']))
    except (ValueError, KeyError, TypeError, UnicodeError) as exc:
        raise ValueError('invalid_knowledge_cursor') from exc


async def read_page(kind: str, tenant_id: str, session_id: str, *, status: str = '', cursor: str = '', limit: int = 100):
    table = _TABLES[kind]
    scope = fingerprint([kind, tenant_id, session_id, status])
    params = {'tid': tenant_id, 'sid': session_id, 'status': status, 'limit': limit+1}
    where = "tenant_id=:tid AND (:sid='' OR session_id=:sid) AND (:status='' OR status=:status)"
    if cursor:
        stamp, item_id = decode_cursor(cursor, scope)
        params.update(stamp=stamp, item_id=item_id)
        where += ' AND (created_at,id)<(:stamp,:item_id)'
    rows = await execute(f'SELECT * FROM {table} WHERE '+where+' ORDER BY created_at DESC,id DESC LIMIT :limit',params)
    more = len(rows)>limit
    rows = rows[:limit]
    next_cursor = ''
    if more:
        last = rows[-1]
        next_cursor = base64.urlsafe_b64encode(json.dumps({'scope':scope,'created_at':last['created_at'].isoformat(),'id':last['id']}).encode()).decode()
    for row in rows:
        row.pop('lease_token',None)
    return rows, next_cursor
