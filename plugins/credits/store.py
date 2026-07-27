"""
Credit balance persistence and read models for the credits plugin.

Uses the shared PostgreSQL database via SQLAlchemy async engine.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.billing.catalog import DRAW_QUALITY_COSTS
from app.common.logging import get_logger
from app.infra.db import get_engine
from app.infra.runtime_schema import verify_runtime_schema

logger = get_logger(__name__)


class CreditIdempotencyConflict(ValueError):
    """Raised when a management idempotency key is reused with another payload."""


class CreditConfigVersionConflict(ValueError):
    """Raised when a credits configuration write uses a stale ETag."""

    def __init__(self, expected: str, current: str) -> None:
        super().__init__("credits config version conflict")
        self.expected = expected
        self.current = current


def _payload_fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _management_ledger_key(
    operation: str,
    tenant_id: str,
    session_id: str,
    idempotency_key: str,
    *,
    part: str = "",
) -> str:
    identity = "\0".join((operation, tenant_id, session_id, idempotency_key, part))
    return f"credits:{operation}:{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"


def _adjust_result_reference(fingerprint: str, balance: int | None) -> str:
    result = "pending" if balance is None else str(balance)
    return f"credits-adjust-v1:{fingerprint}:{result}"


def _transfer_result_reference(
    fingerprint: str,
    from_balance: int | None,
    to_balance: int | None,
) -> str:
    if from_balance is None or to_balance is None:
        result = "pending"
    else:
        result = f"{from_balance}:{to_balance}"
    return f"credits-transfer-v1:{fingerprint}:{result}"


def _replayed_adjust_balance(reference: object, fingerprint: str) -> int:
    prefix = f"credits-adjust-v1:{fingerprint}:"
    value = str(reference or "")
    if not value.startswith(prefix):
        raise CreditIdempotencyConflict(
            "idempotency key was already used for a different adjustment"
        )
    result = value.removeprefix(prefix)
    if result == "pending":
        raise RuntimeError("credit adjustment idempotency record is incomplete")
    return int(result)


def _replayed_transfer_balances(reference: object, fingerprint: str) -> dict[str, int]:
    prefix = f"credits-transfer-v1:{fingerprint}:"
    value = str(reference or "")
    if not value.startswith(prefix):
        raise CreditIdempotencyConflict(
            "idempotency key was already used for a different transfer"
        )
    result = value.removeprefix(prefix)
    if result == "pending":
        raise RuntimeError("credit transfer idempotency record is incomplete")
    from_balance, to_balance = result.split(":", 1)
    return {
        "from_balance": int(from_balance),
        "to_balance": int(to_balance),
    }


CHECKIN_MODE_COMMAND = 1
CHECKIN_MODE_SILENT_ANY = 2
CHECKIN_MODE_MENTION_ONLY = 3

_VALID_CHECKIN_MODES = {
    CHECKIN_MODE_COMMAND,
    CHECKIN_MODE_SILENT_ANY,
    CHECKIN_MODE_MENTION_ONLY,
}

_CHECKIN_MODE_LABELS = {
    CHECKIN_MODE_COMMAND: "命令签到",
    CHECKIN_MODE_SILENT_ANY: "当前发言签到（静默）",
    CHECKIN_MODE_MENTION_ONLY: "@ 机器人时静默签到",
}

_DEFAULT_CONFIG = {
    "enabled": False,
    "credit_name": "积分",
    "cost_per_chat": 0,
    "command_costs_text": "",
    "draw_quality_costs_text": "\n".join(
        f"{quality}={amount}" for quality, amount in DRAW_QUALITY_COSTS.items()
    ),
    "amap_search_credit_cost": 2,
    "amap_map_credit_cost": 8,
    "amap_route_map_credit_cost": 12,
    "initial_credits": 100,
    "daily_checkin": 10,
    "streak_bonus": 5,
    "streak_cap": 50,
    "checkin_mode": CHECKIN_MODE_COMMAND,
    "admin_user_ids_text": "",
    "user_commands_text": "/签到\n/checkin\n/余额\n/balance\n/榜单\n/top\n/rank\n/积分排名\n/转账\n/transfer",
    "admin_commands_text": "/赠送\n/grant\n/sign-in\n/signin\n/签到模式",
}

try:
    _CN_TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover - stdlib zoneinfo fallback
    _CN_TZ = timezone(timedelta(hours=8))


async def _exec(sql: str, params: dict | None = None) -> list[dict[str, Any]]:
    engine = get_engine()
    async with engine.begin() as conn:
        result = await conn.execute(text(sql), params or {})
        if result.returns_rows:
            return [dict(row._mapping) for row in result.fetchall()]
        return []


def normalize_checkin_mode(mode: int | str | None) -> int:
    try:
        normalized = int(mode or CHECKIN_MODE_COMMAND)
    except (TypeError, ValueError) as exc:
        raise ValueError("签到模式只能是 1、2、3") from exc
    if normalized not in _VALID_CHECKIN_MODES:
        raise ValueError("签到模式只能是 1、2、3")
    return normalized


def checkin_mode_label(mode: int | str | None) -> str:
    normalized = normalize_checkin_mode(mode)
    return _CHECKIN_MODE_LABELS.get(normalized, f"未知模式({normalized})")


def _today_cn() -> date:
    return datetime.now(_CN_TZ).date()


def _date_value(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _require_user_id(user_id: object) -> str:
    value = str(user_id or "").strip()
    if not value:
        raise ValueError("user_id is required")
    return value


def _normalize_text_list(value: Any) -> tuple[str, list[str]]:
    if isinstance(value, str):
        raw = value
    elif isinstance(value, (list, tuple, set)):
        raw = "\n".join(str(item or "").strip() for item in value)
    else:
        raw = str(value or "")
    items: list[str] = []
    seen: set[str] = set()
    for chunk in raw.replace(",", "\n").splitlines():
        item = str(chunk or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        items.append(item)
    return "\n".join(items), items


def _normalize_command_list(value: Any) -> tuple[str, list[str]]:
    _, items = _normalize_text_list(value)
    commands: list[str] = []
    seen: set[str] = set()
    for item in items:
        normalized = item if item.startswith("/") else f"/{item}"
        normalized = normalized.strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        commands.append(normalized)
    return "\n".join(commands), commands


def _normalize_command_costs(value: Any) -> tuple[str, dict[str, int]]:
    if isinstance(value, str):
        raw = value
    elif isinstance(value, dict):
        raw = "\n".join(f"{key}={val}" for key, val in value.items())
    else:
        raw = str(value or "")

    items: list[str] = []
    costs: dict[str, int] = {}
    for line in raw.replace(",", "\n").splitlines():
        entry = str(line or "").strip()
        if not entry:
            continue
        if "=" in entry:
            command_raw, amount_raw = entry.split("=", 1)
        elif ":" in entry:
            command_raw, amount_raw = entry.split(":", 1)
        else:
            raise ValueError("命令积分规则格式应为 /command=10")
        command = command_raw.strip()
        if not command:
            raise ValueError("命令积分规则缺少命令名")
        command = command if command.startswith("/") else f"/{command}"
        command = command.lower()
        try:
            amount = int(str(amount_raw or "").strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"命令积分必须是整数: {command}") from exc
        if amount < 0:
            raise ValueError(f"命令积分不能小于 0: {command}")
        costs[command] = amount
    for command, amount in sorted(costs.items()):
        items.append(f"{command}={amount}")
    return "\n".join(items), costs


def _normalize_draw_quality_costs(value: Any) -> tuple[str, dict[str, int]]:
    if isinstance(value, str):
        raw = value
    elif isinstance(value, dict):
        raw = "\n".join(f"{key}={val}" for key, val in value.items())
    else:
        raw = str(value or "")

    costs = dict(DRAW_QUALITY_COSTS)
    for line in raw.replace(",", "\n").splitlines():
        entry = str(line or "").strip()
        if not entry:
            continue
        if "=" in entry:
            quality_raw, amount_raw = entry.split("=", 1)
        elif ":" in entry:
            quality_raw, amount_raw = entry.split(":", 1)
        else:
            raise ValueError("画图质量积分规则格式应为 low=5")
        quality = quality_raw.strip().lower()
        if quality not in DRAW_QUALITY_COSTS:
            raise ValueError("画图质量只能是 low、medium、high")
        try:
            amount = int(str(amount_raw or "").strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"画图质量积分必须是整数: {quality}") from exc
        if amount < 0:
            raise ValueError(f"画图质量积分不能小于 0: {quality}")
        costs[quality] = amount
    items = [f"{quality}={costs[quality]}" for quality in DRAW_QUALITY_COSTS]
    return "\n".join(items), costs


def command_cost_for_config(cfg: dict[str, Any], command: str) -> int:
    token = str(command or "").strip().lower()
    if not token:
        return 0
    if not token.startswith("/"):
        token = f"/{token}"
    mapping = cfg.get("command_costs") or {}
    try:
        return max(0, int(mapping.get(token) or 0))
    except (TypeError, ValueError):
        return 0


def draw_quality_cost_for_config(cfg: dict[str, Any], quality: str) -> int:
    token = str(quality or "low").strip().lower()
    mapping = cfg.get("draw_quality_costs") or {}
    try:
        return max(0, int(mapping.get(token) or DRAW_QUALITY_COSTS.get(token, 0)))
    except (TypeError, ValueError):
        return max(0, int(DRAW_QUALITY_COSTS.get(token, 0)))


def _non_negative_int(value: Any, field_name: str) -> int:
    try:
        normalized = int(value or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 必须是整数") from exc
    if normalized < 0:
        raise ValueError(f"{field_name} 不能小于 0")
    return normalized


def _normalize_config(
    row: dict[str, Any] | None, tenant_id: str, session_id: str
) -> dict[str, Any]:
    cfg = dict(_DEFAULT_CONFIG)
    if row:
        cfg.update({k: v for k, v in row.items() if v is not None})
    cfg["tenant_id"] = tenant_id
    cfg["session_id"] = session_id
    enabled = cfg.get("enabled", False)
    cfg["enabled"] = (
        enabled.strip().lower() in {"1", "true", "yes", "on"}
        if isinstance(enabled, str)
        else bool(enabled)
    )
    cfg["checkin_mode"] = normalize_checkin_mode(cfg.get("checkin_mode"))
    cfg["checkin_mode_label"] = checkin_mode_label(cfg["checkin_mode"])
    cfg["command_costs_text"], cfg["command_costs"] = _normalize_command_costs(
        cfg.get("command_costs_text", _DEFAULT_CONFIG["command_costs_text"])
    )
    cfg["draw_quality_costs_text"], cfg["draw_quality_costs"] = _normalize_draw_quality_costs(
        cfg.get("draw_quality_costs_text", _DEFAULT_CONFIG["draw_quality_costs_text"])
    )
    cfg["amap_search_credit_cost"] = _non_negative_int(
        cfg.get("amap_search_credit_cost", _DEFAULT_CONFIG["amap_search_credit_cost"]),
        "高德普通查询积分",
    )
    cfg["amap_map_credit_cost"] = _non_negative_int(
        cfg.get("amap_map_credit_cost", _DEFAULT_CONFIG["amap_map_credit_cost"]),
        "高德地图二维码积分",
    )
    cfg["amap_route_map_credit_cost"] = _non_negative_int(
        cfg.get("amap_route_map_credit_cost", _DEFAULT_CONFIG["amap_route_map_credit_cost"]),
        "高德复杂路线地图积分",
    )
    cfg["admin_user_ids_text"], cfg["admin_user_ids"] = _normalize_text_list(
        cfg.get("admin_user_ids_text", "")
    )
    cfg["user_commands_text"], cfg["user_commands"] = _normalize_command_list(
        cfg.get("user_commands_text", _DEFAULT_CONFIG["user_commands_text"])
    )
    cfg["admin_commands_text"], cfg["admin_commands"] = _normalize_command_list(
        cfg.get("admin_commands_text", _DEFAULT_CONFIG["admin_commands_text"])
    )
    return cfg


def _config_version_value(value: object) -> str:
    if isinstance(value, datetime):
        normalized = value
        if normalized.tzinfo is not None:
            normalized = normalized.astimezone(UTC).replace(tzinfo=None)
        return normalized.isoformat(timespec="microseconds")
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed.isoformat(timespec="microseconds")


def _config_etag(
    tenant_id: str,
    session_id: str,
    row: dict[str, Any] | None,
) -> str:
    version = "missing" if row is None else f"present:{_config_version_value(row.get('updated_at'))}"
    identity = "\0".join(("credits-config-v1", tenant_id, session_id, version))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f'"credits-config-{digest}"'


def _next_config_updated_at(current: object) -> datetime:
    # plugin_credits_config.updated_at is a TIMESTAMP without time zone.  Keep
    # writes naive for asyncpg and force a strictly newer value so optimistic
    # comparison remains sound even on coarse or skewed clocks.
    now = datetime.now(UTC).replace(tzinfo=None)
    version = _config_version_value(current)
    if not version:
        return now
    try:
        previous = datetime.fromisoformat(version)
    except ValueError:
        return now
    if now <= previous:
        return previous + timedelta(microseconds=1)
    return now


def _normalize_config_updates(kwargs: dict[str, Any]) -> dict[str, Any]:
    unknown = set(kwargs).difference(_DEFAULT_CONFIG)
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"unsupported credits config field(s): {names}")
    updates = {key: value for key, value in kwargs.items() if value is not None}
    if "checkin_mode" in updates:
        updates["checkin_mode"] = normalize_checkin_mode(updates["checkin_mode"])
    if "command_costs_text" in updates:
        updates["command_costs_text"] = _normalize_command_costs(updates["command_costs_text"])[0]
    if "draw_quality_costs_text" in updates:
        updates["draw_quality_costs_text"] = _normalize_draw_quality_costs(
            updates["draw_quality_costs_text"]
        )[0]
    for key, label in (
        ("cost_per_chat", "每次对话积分"),
        ("initial_credits", "初始积分"),
        ("daily_checkin", "每日签到积分"),
        ("streak_bonus", "连续签到奖励"),
        ("streak_cap", "连续签到奖励上限"),
        ("amap_search_credit_cost", "高德普通查询积分"),
        ("amap_map_credit_cost", "高德地图二维码积分"),
        ("amap_route_map_credit_cost", "高德复杂路线地图积分"),
    ):
        if key in updates:
            updates[key] = _non_negative_int(updates[key], label)
    if "admin_user_ids_text" in updates:
        updates["admin_user_ids_text"] = _normalize_text_list(updates["admin_user_ids_text"])[0]
    if "user_commands_text" in updates:
        updates["user_commands_text"] = _normalize_command_list(updates["user_commands_text"])[0]
    if "admin_commands_text" in updates:
        updates["admin_commands_text"] = _normalize_command_list(
            updates["admin_commands_text"]
        )[0]
    return updates


class CreditStore:
    def __init__(self, settings: Any) -> None:
        self.settings = settings

    async def _financial_checkpoint(self, step: str) -> None:
        """Test seam for proving that financial statements roll back atomically."""
        _ = step

    async def ensure_tables(self) -> None:
        await verify_runtime_schema(get_engine(), component="credits store")
        logger.info("credits.schema_verified")

    async def get_config(self, tenant_id: str, session_id: str) -> dict[str, Any]:
        config, _ = await self.get_config_versioned(tenant_id, session_id)
        return config

    async def get_config_versioned(
        self,
        tenant_id: str,
        session_id: str,
    ) -> tuple[dict[str, Any], str]:
        rows = await _exec(
            "SELECT * FROM plugin_credits_config WHERE tenant_id = :tid AND session_id = :sid",
            {"tid": tenant_id, "sid": session_id},
        )
        row = rows[0] if rows else None
        return (
            _normalize_config(row, tenant_id, session_id),
            _config_etag(tenant_id, session_id, row),
        )

    async def set_config(self, tenant_id: str, session_id: str, **kwargs: Any) -> dict[str, Any]:
        config, _ = await self._set_config(
            tenant_id,
            session_id,
            expected_etag=None,
            updates=_normalize_config_updates(kwargs),
        )
        return config

    async def set_config_versioned(
        self,
        tenant_id: str,
        session_id: str,
        *,
        expected_etag: str,
        **kwargs: Any,
    ) -> tuple[dict[str, Any], str]:
        return await self._set_config(
            tenant_id,
            session_id,
            expected_etag=str(expected_etag or "").strip(),
            updates=_normalize_config_updates(kwargs),
        )

    async def _set_config(
        self,
        tenant_id: str,
        session_id: str,
        *,
        expected_etag: str | None,
        updates: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        params: dict[str, Any] = {"tid": tenant_id, "sid": session_id}
        engine = get_engine()
        async with engine.begin() as conn:
            lock_suffix = " FOR UPDATE" if engine.dialect.name == "postgresql" else ""
            current_result = await conn.execute(
                text(
                    "SELECT * FROM plugin_credits_config "
                    "WHERE tenant_id = :tid AND session_id = :sid" + lock_suffix
                ),
                params,
            )
            current_mapping = current_result.mappings().first()
            current = dict(current_mapping) if current_mapping is not None else None
            current_etag = _config_etag(tenant_id, session_id, current)
            if expected_etag is not None and expected_etag != current_etag:
                raise CreditConfigVersionConflict(expected_etag, current_etag)
            if not updates:
                return _normalize_config(current, tenant_id, session_id), current_etag

            updated_at = _next_config_updated_at(
                current.get("updated_at") if current is not None else None
            )
            mutation_params = {**params, **updates, "updated_at": updated_at}
            if current is None:
                columns = ["tenant_id", "session_id", *updates, "updated_at"]
                values = [":tid", ":sid", *(f":{key}" for key in updates), ":updated_at"]
                inserted = await conn.execute(
                    text(
                        f"INSERT INTO plugin_credits_config ({', '.join(columns)}) "
                        f"VALUES ({', '.join(values)}) "
                        "ON CONFLICT (tenant_id, session_id) DO NOTHING RETURNING *"
                    ),
                    mutation_params,
                )
                written_mapping = inserted.mappings().first()
            else:
                assignments = [*(f"{key} = :{key}" for key in updates), "updated_at = :updated_at"]
                version_guard = ""
                if expected_etag is not None:
                    version_guard = (
                        " AND ((updated_at = :expected_updated_at) "
                        "OR (updated_at IS NULL AND :expected_updated_at IS NULL))"
                    )
                    mutation_params["expected_updated_at"] = current.get("updated_at")
                updated = await conn.execute(
                    text(
                        f"UPDATE plugin_credits_config SET {', '.join(assignments)} "
                        "WHERE tenant_id = :tid AND session_id = :sid"
                        f"{version_guard} RETURNING *"
                    ),
                    mutation_params,
                )
                written_mapping = updated.mappings().first()

            if written_mapping is None:
                latest_result = await conn.execute(
                    text(
                        "SELECT * FROM plugin_credits_config "
                        "WHERE tenant_id = :tid AND session_id = :sid"
                    ),
                    params,
                )
                latest_mapping = latest_result.mappings().first()
                latest = dict(latest_mapping) if latest_mapping is not None else None
                raise CreditConfigVersionConflict(
                    expected_etag or current_etag,
                    _config_etag(tenant_id, session_id, latest),
                )

            written = dict(written_mapping)
            return (
                _normalize_config(written, tenant_id, session_id),
                _config_etag(tenant_id, session_id, written),
            )

    async def prepare_command_charge(
        self,
        tenant_id: str,
        session_id: str,
        user_id: str,
        command: str,
        *,
        display_name: str = "",
    ) -> dict[str, Any]:
        cfg = await self.get_config(tenant_id, session_id)
        if not cfg.get("enabled"):
            return {"enabled": False, "cost": 0, "command": command}
        cost = command_cost_for_config(cfg, command)
        if cost <= 0:
            return {
                "enabled": True,
                "cost": 0,
                "command": command,
                "credit_name": str(cfg.get("credit_name") or "积分"),
            }
        balance = await self.peek_balance(
            tenant_id,
            session_id,
            user_id,
            display_name=display_name,
        )
        if balance < cost:
            credit_name = str(cfg.get("credit_name") or "积分")
            raise ValueError(
                f"你的{credit_name}不足（余额 {balance}，需要 {cost}），无法执行 {command}。"
            )
        reservation = await self.reserve_charge(
            tenant_id,
            session_id,
            user_id,
            cost,
            reason="command_cost",
            reference=command,
            display_name=display_name,
            metadata={"command": command},
        )
        return {
            "enabled": True,
            "cost": cost,
            "command": command,
            "credit_name": str(cfg.get("credit_name") or "积分"),
            "balance": reservation.get("balance", balance),
            "reservation_id": reservation.get("reservation_id", ""),
        }

    async def settle_command_charge(
        self,
        tenant_id: str,
        session_id: str,
        user_id: str,
        plan: dict[str, Any],
        *,
        trace_id: str = "",
        display_name: str = "",
    ) -> int | None:
        cost = int(plan.get("cost") or 0)
        if cost <= 0:
            return None
        reservation_id = str(plan.get("reservation_id") or "").strip()
        if reservation_id:
            captured = await self.capture_reservation(
                reservation_id,
                amount=cost,
                reference=trace_id or str(plan.get("command") or ""),
                display_name=display_name,
            )
            return int(captured.get("balance") or 0) if captured is not None else None
        command = str(plan.get("command") or "").strip()
        reference = f"{command}|{trace_id}" if trace_id else command
        return await self.adjust(
            tenant_id,
            session_id,
            user_id,
            -cost,
            "command_cost",
            actor="system",
            reference=reference[:255],
            display_name=display_name,
        )

    async def get_balance_record(
        self, tenant_id: str, session_id: str, user_id: str
    ) -> dict[str, Any] | None:
        rows = await _exec(
            "SELECT tenant_id, session_id, user_id, display_name, credits, updated_at "
            "FROM plugin_credits_balance "
            "WHERE tenant_id = :tid AND session_id = :sid AND user_id = :uid",
            {"tid": tenant_id, "sid": session_id, "uid": user_id},
        )
        return rows[0] if rows else None

    async def _update_display_name(
        self,
        tenant_id: str,
        session_id: str,
        user_id: str,
        display_name: str = "",
    ) -> None:
        if not display_name.strip():
            return
        await _exec(
            "UPDATE plugin_credits_balance "
            "SET display_name = :display_name "
            "WHERE tenant_id = :tid AND session_id = :sid AND user_id = :uid "
            "AND COALESCE(display_name, '') <> :display_name",
            {
                "tid": tenant_id,
                "sid": session_id,
                "uid": user_id,
                "display_name": display_name.strip(),
            },
        )

    async def get_balance(
        self,
        tenant_id: str,
        session_id: str,
        user_id: str,
        *,
        display_name: str = "",
    ) -> int:
        user_id = _require_user_id(user_id)
        row = await self.get_balance_record(tenant_id, session_id, user_id)
        if row:
            await self._update_display_name(tenant_id, session_id, user_id, display_name)
            return int(row["credits"] or 0)

        cfg = await self.get_config(tenant_id, session_id)
        initial = int(cfg.get("initial_credits", _DEFAULT_CONFIG["initial_credits"]))
        await _exec(
            "INSERT INTO plugin_credits_balance "
            "(tenant_id, session_id, user_id, display_name, credits) "
            "VALUES (:tid, :sid, :uid, :display_name, :credits) "
            "ON CONFLICT (tenant_id, session_id, user_id) DO NOTHING",
            {
                "tid": tenant_id,
                "sid": session_id,
                "uid": user_id,
                "display_name": display_name.strip(),
                "credits": initial,
            },
        )
        return initial

    async def peek_balance(
        self,
        tenant_id: str,
        session_id: str,
        user_id: str,
        *,
        display_name: str = "",
    ) -> int:
        user_id = _require_user_id(user_id)
        row = await self.get_balance_record(tenant_id, session_id, user_id)
        if row:
            await self._update_display_name(tenant_id, session_id, user_id, display_name)
            return int(row["credits"] or 0)
        return 0

    async def reserve_charge(
        self,
        tenant_id: str,
        session_id: str,
        user_id: str,
        amount: int,
        *,
        reason: str,
        reference: str = "",
        display_name: str = "",
        metadata: dict[str, Any] | None = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        user_id = _require_user_id(user_id)
        amount = int(amount or 0)
        if amount <= 0:
            raise ValueError("reservation amount must be positive")
        idempotency_key = str(idempotency_key or "").strip()[:256]
        reservation_id = f"cr_{uuid.uuid4().hex}"
        params = {
            "reservation_id": reservation_id,
            "tid": tenant_id,
            "sid": session_id,
            # Keep config lookup binds distinct from INSERT target binds.
            # asyncpg/PostgreSQL can otherwise infer incompatible types for
            # one reused placeholder before ON CONFLICT is evaluated.
            "config_tid": tenant_id,
            "config_sid": session_id,
            "uid": user_id,
            "amount": amount,
            "reason": str(reason or "reserved_cost")[:64],
            "reference": str(reference or "")[:255],
            "metadata_json": json.dumps(metadata or {}, ensure_ascii=False),
            "idempotency_key": idempotency_key,
            "display_name": display_name.strip()[:128],
        }

        engine = get_engine()
        async with engine.begin() as conn:
            inserted = await conn.execute(
                text(
                    "INSERT INTO plugin_credits_reservation "
                    "(reservation_id, tenant_id, session_id, user_id, amount, reason, "
                    "reference, metadata_json, idempotency_key) "
                    "VALUES (:reservation_id, :tid, :sid, :uid, :amount, :reason, "
                    ":reference, :metadata_json, :idempotency_key) "
                    "ON CONFLICT DO NOTHING RETURNING reservation_id"
                ),
                params,
            )
            inserted_row = inserted.mappings().first()
            if inserted_row is None:
                if not idempotency_key:
                    raise RuntimeError("credit reservation identifier collision")
                existing_result = await conn.execute(
                    text(
                        "SELECT reservation_id, amount, captured_amount, reason, status "
                        "FROM plugin_credits_reservation "
                        "WHERE tenant_id = :tid AND session_id = :sid AND user_id = :uid "
                        "AND idempotency_key = :idempotency_key"
                    ),
                    params,
                )
                existing = existing_result.mappings().first()
                if existing is None:
                    raise RuntimeError("credit reservation idempotency conflict was lost")
                if int(existing["amount"] or 0) != amount or str(existing["reason"] or "") != str(
                    params["reason"]
                ):
                    raise ValueError("idempotency key was already used for a different reservation")
                balance_result = await conn.execute(
                    text(
                        "SELECT credits FROM plugin_credits_balance "
                        "WHERE tenant_id = :tid AND session_id = :sid AND user_id = :uid"
                    ),
                    params,
                )
                balance_row = balance_result.mappings().first()
                status = str(existing["status"] or "")
                settled_amount = (
                    int(existing["captured_amount"] or 0)
                    if status == "captured" and existing["captured_amount"] is not None
                    else int(existing["amount"] or 0)
                )
                return {
                    "reservation_id": str(existing["reservation_id"]),
                    "amount": settled_amount,
                    "balance": int((balance_row or {}).get("credits", 0)),
                    "status": status,
                }

            await self._financial_checkpoint("reserve_reservation")
            await conn.execute(
                text(
                    "INSERT INTO plugin_credits_balance "
                    "(tenant_id, session_id, user_id, display_name, credits) "
                    "VALUES (:tid, :sid, :uid, :display_name, "
                    "COALESCE((SELECT initial_credits FROM plugin_credits_config "
                    "WHERE tenant_id = CAST(:config_tid AS VARCHAR(64)) "
                    "AND session_id = CAST(:config_sid AS VARCHAR(256))), 100)) "
                    "ON CONFLICT (tenant_id, session_id, user_id) DO NOTHING"
                ),
                params,
            )
            await self._financial_checkpoint("reserve_balance_initialized")
            debited = await conn.execute(
                text(
                    "UPDATE plugin_credits_balance "
                    "SET credits = credits - :amount, "
                    "display_name = CASE WHEN :display_name = '' THEN display_name ELSE :display_name END, "
                    "updated_at = CURRENT_TIMESTAMP "
                    "WHERE tenant_id = :tid AND session_id = :sid AND user_id = :uid "
                    "AND credits >= :amount RETURNING credits"
                ),
                params,
            )
            debit_row = debited.mappings().first()
            if debit_row is None:
                current_result = await conn.execute(
                    text(
                        "SELECT credits FROM plugin_credits_balance "
                        "WHERE tenant_id = :tid AND session_id = :sid AND user_id = :uid"
                    ),
                    params,
                )
                current_row = current_result.mappings().first()
                current = int((current_row or {}).get("credits", 0))
                raise ValueError(f"余额不足 ({current} < {amount})")
            await self._financial_checkpoint("reserve_balance_debit")
            return {
                "reservation_id": reservation_id,
                "amount": amount,
                "balance": int(debit_row["credits"] or 0),
                "status": "reserved",
            }

    async def capture_reservation(
        self,
        reservation_id: str,
        *,
        amount: int | None = None,
        reference: str = "",
        display_name: str = "",
    ) -> dict[str, Any] | None:
        requested_amount = None if amount is None else int(amount)
        if requested_amount is not None and requested_amount < 0:
            raise ValueError("capture amount exceeds reservation")
        params = {
            "reservation_id": str(reservation_id or "").strip(),
            "capture_amount": requested_amount,
            "reference": str(reference or "")[:255],
            "display_name": display_name.strip()[:128],
        }
        if not params["reservation_id"]:
            return None

        capture_expression = "amount" if requested_amount is None else ":capture_amount"
        amount_condition = "" if requested_amount is None else "AND :capture_amount <= amount "
        engine = get_engine()
        async with engine.begin() as conn:
            claimed = await conn.execute(
                text(
                    "UPDATE plugin_credits_reservation "
                    f"SET status = 'captured', captured_amount = {capture_expression}, "
                    "reference = CASE WHEN :reference = '' THEN reference ELSE :reference END, "
                    "updated_at = CURRENT_TIMESTAMP, captured_at = CURRENT_TIMESTAMP "
                    "WHERE reservation_id = :reservation_id AND status = 'reserved' "
                    f"{amount_condition}"
                    "RETURNING tenant_id, session_id, user_id, amount AS reserved_amount, "
                    "captured_amount, reason, reference"
                ),
                params,
            )
            claimed_row = claimed.mappings().first()
            if claimed_row is None:
                existing_result = await conn.execute(
                    text(
                        "SELECT tenant_id, session_id, user_id, amount, captured_amount, status "
                        "FROM plugin_credits_reservation WHERE reservation_id = :reservation_id"
                    ),
                    params,
                )
                existing = existing_result.mappings().first()
                if existing is None:
                    return None
                status = str(existing["status"] or "")
                if status == "reserved":
                    raise ValueError("capture amount exceeds reservation")
                if status != "captured":
                    return None
                captured_amount = int(
                    existing["captured_amount"]
                    if existing["captured_amount"] is not None
                    else existing["amount"] or 0
                )
                if requested_amount is not None and requested_amount != captured_amount:
                    raise ValueError("capture amount conflicts with completed reservation")
                balance_result = await conn.execute(
                    text(
                        "SELECT credits FROM plugin_credits_balance "
                        "WHERE tenant_id = :tid AND session_id = :sid AND user_id = :uid"
                    ),
                    {
                        "tid": existing["tenant_id"],
                        "sid": existing["session_id"],
                        "uid": existing["user_id"],
                    },
                )
                balance_row = balance_result.mappings().first()
                return {
                    "reservation_id": params["reservation_id"],
                    "amount": captured_amount,
                    "balance": int((balance_row or {}).get("credits", 0)),
                    "status": "captured",
                }

            await self._financial_checkpoint("capture_status")
            reserved_amount = int(claimed_row["reserved_amount"] or 0)
            captured_amount = int(claimed_row["captured_amount"] or 0)
            refund = reserved_amount - captured_amount
            subject_params = {
                "tid": claimed_row["tenant_id"],
                "sid": claimed_row["session_id"],
                "uid": claimed_row["user_id"],
                "refund": refund,
                "display_name": params["display_name"],
            }
            if refund > 0:
                refunded = await conn.execute(
                    text(
                        "UPDATE plugin_credits_balance "
                        "SET credits = credits + :refund, "
                        "display_name = CASE WHEN :display_name = '' THEN display_name ELSE :display_name END, "
                        "updated_at = CURRENT_TIMESTAMP "
                        "WHERE tenant_id = :tid AND session_id = :sid AND user_id = :uid "
                        "RETURNING credits"
                    ),
                    subject_params,
                )
                if refunded.mappings().first() is None:
                    raise RuntimeError("credit balance missing for reservation capture")
                await self._financial_checkpoint("capture_refund")
            elif params["display_name"]:
                await conn.execute(
                    text(
                        "UPDATE plugin_credits_balance SET display_name = :display_name "
                        "WHERE tenant_id = :tid AND session_id = :sid AND user_id = :uid"
                    ),
                    subject_params,
                )

            if captured_amount > 0:
                ledger_key = f"credits:capture:{params['reservation_id']}"
                ledger = await conn.execute(
                    text(
                        "INSERT INTO plugin_credits_ledger "
                        "(tenant_id, session_id, user_id, delta, reason, actor, reference, "
                        "idempotency_key) "
                        "VALUES (:tid, :sid, :uid, :delta, :reason, 'system', :reference, "
                        ":idempotency_key) ON CONFLICT DO NOTHING RETURNING id"
                    ),
                    {
                        **subject_params,
                        "delta": -captured_amount,
                        "reason": str(claimed_row["reason"] or "reserved_cost")[:64],
                        "reference": str(claimed_row["reference"] or params["reservation_id"])[
                            :255
                        ],
                        "idempotency_key": ledger_key,
                    },
                )
                if ledger.mappings().first() is None:
                    raise RuntimeError("credit capture ledger idempotency conflict")
                await self._financial_checkpoint("capture_ledger")

            balance_result = await conn.execute(
                text(
                    "SELECT credits FROM plugin_credits_balance "
                    "WHERE tenant_id = :tid AND session_id = :sid AND user_id = :uid"
                ),
                subject_params,
            )
            balance_row = balance_result.mappings().first()
            if balance_row is None:
                raise RuntimeError("credit balance missing for reservation capture")
            return {
                "reservation_id": params["reservation_id"],
                "amount": captured_amount,
                "balance": int(balance_row["credits"] or 0),
                "status": "captured",
            }

    async def release_reservation(self, reservation_id: str) -> None:
        reservation_id = str(reservation_id or "").strip()
        if not reservation_id:
            return
        engine = get_engine()
        async with engine.begin() as conn:
            released = await conn.execute(
                text(
                    "UPDATE plugin_credits_reservation "
                    "SET status = 'released', updated_at = CURRENT_TIMESTAMP, "
                    "released_at = CURRENT_TIMESTAMP "
                    "WHERE reservation_id = :reservation_id AND status = 'reserved' "
                    "RETURNING tenant_id, session_id, user_id, amount"
                ),
                {"reservation_id": reservation_id},
            )
            row = released.mappings().first()
            if row is None:
                return
            await self._financial_checkpoint("release_status")
            refunded = await conn.execute(
                text(
                    "UPDATE plugin_credits_balance "
                    "SET credits = credits + :amount, updated_at = CURRENT_TIMESTAMP "
                    "WHERE tenant_id = :tid AND session_id = :sid AND user_id = :uid "
                    "RETURNING credits"
                ),
                {
                    "tid": row["tenant_id"],
                    "sid": row["session_id"],
                    "uid": row["user_id"],
                    "amount": int(row["amount"] or 0),
                },
            )
            if refunded.mappings().first() is None:
                raise RuntimeError("credit balance missing for reservation release")
            await self._financial_checkpoint("release_refund")

    async def adjust(
        self,
        tenant_id: str,
        session_id: str,
        user_id: str,
        delta: int,
        reason: str,
        *,
        actor: str = "",
        reference: str = "",
        display_name: str = "",
        idempotency_key: str = "",
    ) -> int:
        user_id = _require_user_id(user_id)
        return await self._mutate_balance(
            tenant_id,
            session_id,
            user_id,
            delta=int(delta),
            target_amount=None,
            reason=reason,
            actor=actor,
            reference=reference,
            display_name=display_name,
            idempotency_key=idempotency_key,
        )

    async def set_balance(
        self,
        tenant_id: str,
        session_id: str,
        user_id: str,
        amount: int,
        reason: str,
        *,
        actor: str = "",
        reference: str = "",
        display_name: str = "",
        idempotency_key: str = "",
    ) -> int:
        user_id = _require_user_id(user_id)
        if amount < 0:
            raise ValueError("余额不能小于 0")
        return await self._mutate_balance(
            tenant_id,
            session_id,
            user_id,
            delta=None,
            target_amount=int(amount),
            reason=reason,
            actor=actor,
            reference=reference,
            display_name=display_name,
            idempotency_key=idempotency_key,
        )

    async def _mutate_balance(
        self,
        tenant_id: str,
        session_id: str,
        user_id: str,
        *,
        delta: int | None,
        target_amount: int | None,
        reason: str,
        actor: str,
        reference: str,
        display_name: str,
        idempotency_key: str,
    ) -> int:
        mode = "set" if target_amount is not None else "delta"
        requested_value = int(target_amount if target_amount is not None else delta or 0)
        normalized_key = str(idempotency_key or "").strip()
        fingerprint = _payload_fingerprint(
            {
                "operation": "adjust",
                "tenant_id": tenant_id,
                "session_id": session_id,
                "user_id": user_id,
                "mode": mode,
                "value": requested_value,
                "reason": str(reason or ""),
                "actor": str(actor or ""),
                "reference": str(reference or ""),
                "display_name": str(display_name or ""),
            }
        )
        ledger_key = (
            _management_ledger_key("adjust", tenant_id, session_id, normalized_key)
            if normalized_key
            else ""
        )
        params = {
            "tid": tenant_id,
            "sid": session_id,
            "config_tid": tenant_id,
            "config_sid": session_id,
            "uid": user_id,
            "requested_value": requested_value,
            "reason": str(reason or "admin_adjust")[:64],
            "actor": str(actor or "")[:128],
            "reference": str(reference or "")[:256],
            "display_name": str(display_name or "").strip()[:128],
            "idempotency_key": ledger_key,
            "claim_reference": _adjust_result_reference(fingerprint, None),
        }

        engine = get_engine()
        async with engine.begin() as conn:
            claim_id: int | None = None
            if ledger_key:
                claimed = await conn.execute(
                    text(
                        "INSERT INTO plugin_credits_ledger "
                        "(tenant_id, session_id, user_id, delta, reason, actor, reference, "
                        "idempotency_key) VALUES "
                        "(:tid, :sid, :uid, 0, 'idempotency_claim', :actor, "
                        ":claim_reference, :idempotency_key) "
                        "ON CONFLICT DO NOTHING RETURNING id"
                    ),
                    params,
                )
                claim_row = claimed.mappings().first()
                if claim_row is None:
                    replayed = await conn.execute(
                        text(
                            "SELECT reference FROM plugin_credits_ledger "
                            "WHERE idempotency_key = :idempotency_key"
                        ),
                        params,
                    )
                    replayed_row = replayed.mappings().first()
                    if replayed_row is None:
                        raise RuntimeError("credit adjustment idempotency conflict was lost")
                    return _replayed_adjust_balance(replayed_row["reference"], fingerprint)
                claim_id = int(claim_row["id"])
                await self._financial_checkpoint("adjust_claim")

            await conn.execute(
                text(
                    "INSERT INTO plugin_credits_balance "
                    "(tenant_id, session_id, user_id, display_name, credits) "
                    "VALUES (:tid, :sid, :uid, :display_name, "
                    "COALESCE((SELECT initial_credits FROM plugin_credits_config "
                    "WHERE tenant_id = CAST(:config_tid AS VARCHAR(64)) "
                    "AND session_id = CAST(:config_sid AS VARCHAR(256))), 100)) "
                    "ON CONFLICT (tenant_id, session_id, user_id) DO NOTHING"
                ),
                params,
            )
            await self._financial_checkpoint("adjust_balance_initialized")

            if target_amount is None:
                mutation_sql = (
                    "UPDATE plugin_credits_balance "
                    "SET credits = credits + :requested_value, "
                    "display_name = CASE WHEN :display_name = '' THEN display_name "
                    "ELSE :display_name END, updated_at = CURRENT_TIMESTAMP "
                    "WHERE tenant_id = :tid AND session_id = :sid AND user_id = :uid "
                    "AND credits + :requested_value >= 0 RETURNING credits"
                )
            else:
                mutation_sql = (
                    "UPDATE plugin_credits_balance "
                    "SET credits = :requested_value, "
                    "display_name = CASE WHEN :display_name = '' THEN display_name "
                    "ELSE :display_name END, updated_at = CURRENT_TIMESTAMP "
                    "WHERE tenant_id = :tid AND session_id = :sid AND user_id = :uid "
                    "RETURNING credits"
                )
            lock_suffix = " FOR UPDATE" if engine.dialect.name == "postgresql" else ""
            current_result = await conn.execute(
                text(
                    "SELECT credits FROM plugin_credits_balance "
                    "WHERE tenant_id = :tid AND session_id = :sid AND user_id = :uid"
                    + lock_suffix
                ),
                params,
            )
            current_row = current_result.mappings().first()
            current = int(current_row["credits"] or 0) if current_row is not None else 0
            mutated = await conn.execute(text(mutation_sql), params)
            mutated_row = mutated.mappings().first()
            if mutated_row is None:
                raise ValueError(
                    f"调整后余额不能小于 0 ({current} -> {current + requested_value})"
                )
            balance = int(mutated_row["credits"] or 0)
            actual_delta = balance - current
            await self._financial_checkpoint("adjust_balance_mutated")

            result_reference = _adjust_result_reference(fingerprint, balance)
            if claim_id is not None:
                await conn.execute(
                    text(
                        "UPDATE plugin_credits_ledger SET delta = :actual_delta, "
                        "reason = :reason, actor = :actor, reference = :result_reference "
                        "WHERE id = :claim_id"
                    ),
                    {
                        **params,
                        "claim_id": claim_id,
                        "actual_delta": actual_delta,
                        "result_reference": result_reference,
                    },
                )
            elif actual_delta != 0:
                await conn.execute(
                    text(
                        "INSERT INTO plugin_credits_ledger "
                        "(tenant_id, session_id, user_id, delta, reason, actor, reference) "
                        "VALUES (:tid, :sid, :uid, :actual_delta, :reason, :actor, :reference)"
                    ),
                    {**params, "actual_delta": actual_delta},
                )
            await self._financial_checkpoint("adjust_ledger")
            return balance

    async def transfer(
        self,
        tenant_id: str,
        session_id: str,
        from_user_id: str,
        to_user_id: str,
        amount: int,
        *,
        actor: str = "",
        reference: str = "",
        idempotency_key: str = "",
    ) -> dict[str, int]:
        from_user_id = _require_user_id(from_user_id)
        to_user_id = _require_user_id(to_user_id)
        if amount <= 0:
            raise ValueError("amount must be positive")
        if from_user_id == to_user_id:
            raise ValueError("cannot transfer to the same user")

        normalized_key = str(idempotency_key or "").strip()
        fingerprint = _payload_fingerprint(
            {
                "operation": "transfer",
                "tenant_id": tenant_id,
                "session_id": session_id,
                "from_user_id": from_user_id,
                "to_user_id": to_user_id,
                "amount": amount,
                "actor": str(actor or ""),
                "reference": str(reference or ""),
            }
        )
        out_key = (
            _management_ledger_key("transfer", tenant_id, session_id, normalized_key, part="out")
            if normalized_key
            else ""
        )
        in_key = (
            _management_ledger_key("transfer", tenant_id, session_id, normalized_key, part="in")
            if normalized_key
            else ""
        )
        params = {
            "tid": tenant_id,
            "sid": session_id,
            "config_tid": tenant_id,
            "config_sid": session_id,
            "from_uid": from_user_id,
            "to_uid": to_user_id,
            "amount": amount,
            "actor": str(actor or "")[:128],
            "out_key": out_key,
            "in_key": in_key,
            "claim_reference": _transfer_result_reference(fingerprint, None, None),
        }

        engine = get_engine()
        async with engine.begin() as conn:
            claim_id: int | None = None
            if out_key:
                claimed = await conn.execute(
                    text(
                        "INSERT INTO plugin_credits_ledger "
                        "(tenant_id, session_id, user_id, delta, reason, actor, reference, "
                        "idempotency_key) VALUES "
                        "(:tid, :sid, :from_uid, 0, 'idempotency_claim', :actor, "
                        ":claim_reference, :out_key) "
                        "ON CONFLICT DO NOTHING RETURNING id"
                    ),
                    params,
                )
                claim_row = claimed.mappings().first()
                if claim_row is None:
                    replayed = await conn.execute(
                        text(
                            "SELECT reference FROM plugin_credits_ledger "
                            "WHERE idempotency_key = :out_key"
                        ),
                        params,
                    )
                    replayed_row = replayed.mappings().first()
                    if replayed_row is None:
                        raise RuntimeError("credit transfer idempotency conflict was lost")
                    return _replayed_transfer_balances(replayed_row["reference"], fingerprint)
                claim_id = int(claim_row["id"])
                await self._financial_checkpoint("transfer_claim")

            await conn.execute(
                text(
                    "INSERT INTO plugin_credits_balance "
                    "(tenant_id, session_id, user_id, credits) "
                    "VALUES (:tid, :sid, :from_uid, "
                    "COALESCE((SELECT initial_credits FROM plugin_credits_config "
                    "WHERE tenant_id = CAST(:config_tid AS VARCHAR(64)) "
                    "AND session_id = CAST(:config_sid AS VARCHAR(256))), 100)), "
                    "(:tid, :sid, :to_uid, "
                    "COALESCE((SELECT initial_credits FROM plugin_credits_config "
                    "WHERE tenant_id = CAST(:config_tid AS VARCHAR(64)) "
                    "AND session_id = CAST(:config_sid AS VARCHAR(256))), 100)) "
                    "ON CONFLICT (tenant_id, session_id, user_id) DO NOTHING"
                ),
                params,
            )
            await self._financial_checkpoint("transfer_balances_initialized")

            lock_suffix = " FOR UPDATE" if engine.dialect.name == "postgresql" else ""
            await conn.execute(
                text(
                    "SELECT user_id FROM plugin_credits_balance "
                    "WHERE tenant_id = :tid AND session_id = :sid "
                    "AND user_id IN (:from_uid, :to_uid) ORDER BY user_id" + lock_suffix
                ),
                params,
            )
            debited = await conn.execute(
                text(
                    "UPDATE plugin_credits_balance "
                    "SET credits = credits - :amount, updated_at = CURRENT_TIMESTAMP "
                    "WHERE tenant_id = :tid AND session_id = :sid AND user_id = :from_uid "
                    "AND credits >= :amount RETURNING credits"
                ),
                params,
            )
            sender_row = debited.mappings().first()
            if sender_row is None:
                raise ValueError("insufficient credits")
            sender_balance = int(sender_row["credits"] or 0)
            await self._financial_checkpoint("transfer_sender_debit")

            credited = await conn.execute(
                text(
                    "UPDATE plugin_credits_balance "
                    "SET credits = credits + :amount, updated_at = CURRENT_TIMESTAMP "
                    "WHERE tenant_id = :tid AND session_id = :sid AND user_id = :to_uid "
                    "RETURNING credits"
                ),
                params,
            )
            recipient_row = credited.mappings().first()
            if recipient_row is None:
                raise RuntimeError("credit transfer recipient balance is missing")
            recipient_balance = int(recipient_row["credits"] or 0)
            await self._financial_checkpoint("transfer_recipient_credit")

            result_reference = _transfer_result_reference(
                fingerprint,
                sender_balance,
                recipient_balance,
            )
            if claim_id is not None:
                await conn.execute(
                    text(
                        "UPDATE plugin_credits_ledger "
                        "SET delta = -CAST(:amount AS INTEGER), "
                        "reason = 'transfer_out', actor = :actor, "
                        "reference = :result_reference WHERE id = :claim_id"
                    ),
                    {**params, "claim_id": claim_id, "result_reference": result_reference},
                )
            else:
                await conn.execute(
                    text(
                        "INSERT INTO plugin_credits_ledger "
                        "(tenant_id, session_id, user_id, delta, reason, actor, reference) "
                        "VALUES (:tid, :sid, :from_uid, -CAST(:amount AS INTEGER), "
                        "'transfer_out', :actor, "
                        ":to_uid)"
                    ),
                    params,
                )
            await self._financial_checkpoint("transfer_ledger_out")

            incoming = await conn.execute(
                text(
                    "INSERT INTO plugin_credits_ledger "
                    "(tenant_id, session_id, user_id, delta, reason, actor, reference, "
                    "idempotency_key) VALUES "
                    "(:tid, :sid, :to_uid, :amount, 'transfer_in', :actor, :from_uid, "
                    ":in_key) ON CONFLICT DO NOTHING RETURNING id"
                ),
                params,
            )
            if incoming.mappings().first() is None:
                raise RuntimeError("credit transfer incoming ledger idempotency conflict")
            await self._financial_checkpoint("transfer_ledger_in")
            return {
                "from_balance": sender_balance,
                "to_balance": recipient_balance,
            }

    async def get_checkin_status(
        self,
        tenant_id: str,
        session_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        user_id = _require_user_id(user_id)
        today = _today_cn()
        cfg = await self.get_config(tenant_id, session_id)
        rows = await _exec(
            "SELECT checkin_date, streak, reward, created_at "
            "FROM plugin_credits_checkin "
            "WHERE tenant_id = :tid AND session_id = :sid AND user_id = :uid "
            "ORDER BY checkin_date DESC, created_at DESC",
            {"tid": tenant_id, "sid": session_id, "uid": user_id},
        )

        latest = rows[0] if rows else None
        today_row = next(
            (row for row in rows if _date_value(row.get("checkin_date")) == today),
            None,
        )
        total_checkins = len(rows)
        latest_date = _date_value(latest.get("checkin_date")) if latest else None
        current_streak = int(latest["streak"] or 0) if latest else 0
        if latest_date and latest_date < today - timedelta(days=1):
            current_streak = 0

        next_streak = int(today_row["streak"] or 0) if today_row else current_streak + 1
        base = int(cfg.get("daily_checkin", _DEFAULT_CONFIG["daily_checkin"]))
        bonus_per_week = int(cfg.get("streak_bonus", _DEFAULT_CONFIG["streak_bonus"]))
        bonus_cap = int(cfg.get("streak_cap", _DEFAULT_CONFIG["streak_cap"]))
        next_bonus = min((next_streak // 7) * bonus_per_week, bonus_cap)

        return {
            "today": today.isoformat(),
            "checked_in_today": today_row is not None,
            "today_reward": int(today_row["reward"] or 0) if today_row else 0,
            "today_streak": int(today_row["streak"] or 0) if today_row else 0,
            "current_streak": current_streak,
            "total_checkins": total_checkins,
            "last_checkin_date": latest_date.isoformat() if latest_date else None,
            "last_reward": int(latest["reward"] or 0) if latest else 0,
            "next_reward": base + next_bonus,
            "checkin_mode": cfg["checkin_mode"],
            "checkin_mode_label": cfg["checkin_mode_label"],
        }

    async def checkin(
        self,
        tenant_id: str,
        session_id: str,
        user_id: str,
        *,
        display_name: str = "",
    ) -> dict[str, Any]:
        user_id = _require_user_id(user_id)
        today = _today_cn()
        yesterday = today - timedelta(days=1)
        params: dict[str, Any] = {
            "tid": tenant_id,
            "sid": session_id,
            "uid": user_id,
            "today": today,
            "yesterday": yesterday,
            "display_name": str(display_name or "").strip()[:128],
        }
        engine = get_engine()
        async with engine.begin() as conn:
            config_lock = " FOR SHARE" if engine.dialect.name == "postgresql" else ""
            config_result = await conn.execute(
                text(
                    "SELECT initial_credits, daily_checkin, streak_bonus, streak_cap "
                    "FROM plugin_credits_config "
                    "WHERE tenant_id = :tid AND session_id = :sid" + config_lock
                ),
                params,
            )
            config_mapping = config_result.mappings().first()
            config = dict(config_mapping) if config_mapping is not None else {}
            initial_credits = _non_negative_int(
                config.get("initial_credits", _DEFAULT_CONFIG["initial_credits"]),
                "初始积分",
            )
            base = _non_negative_int(
                config.get("daily_checkin", _DEFAULT_CONFIG["daily_checkin"]),
                "每日签到积分",
            )
            bonus_per_week = _non_negative_int(
                config.get("streak_bonus", _DEFAULT_CONFIG["streak_bonus"]),
                "连续签到奖励",
            )
            bonus_cap = _non_negative_int(
                config.get("streak_cap", _DEFAULT_CONFIG["streak_cap"]),
                "连续签到奖励上限",
            )
            params["initial_credits"] = initial_credits

            await conn.execute(
                text(
                    "INSERT INTO plugin_credits_balance "
                    "(tenant_id, session_id, user_id, display_name, credits) "
                    "VALUES (:tid, :sid, :uid, :display_name, :initial_credits) "
                    "ON CONFLICT (tenant_id, session_id, user_id) DO NOTHING"
                ),
                params,
            )
            await self._financial_checkpoint("checkin_balance_initialized")

            previous_result = await conn.execute(
                text(
                    "SELECT streak FROM plugin_credits_checkin "
                    "WHERE tenant_id = :tid AND session_id = :sid AND user_id = :uid "
                    "AND checkin_date = :yesterday"
                ),
                params,
            )
            previous = previous_result.mappings().first()
            previous_streak = int(previous["streak"] or 0) if previous is not None else 0
            streak = previous_streak + 1
            streak_bonus = min((streak // 7) * bonus_per_week, bonus_cap)
            reward = base + streak_bonus
            params.update(
                {
                    "streak": streak,
                    "reward": reward,
                    "ledger_key": _management_ledger_key(
                        "checkin",
                        tenant_id,
                        session_id,
                        _payload_fingerprint(
                            {"user_id": user_id, "checkin_date": today.isoformat()}
                        ),
                    ),
                    "ledger_reference": (
                        f"date:{today.isoformat()};base:{base};bonus:{streak_bonus}"
                    ),
                }
            )
            inserted_result = await conn.execute(
                text(
                    "INSERT INTO plugin_credits_checkin "
                    "(tenant_id, session_id, user_id, checkin_date, streak, reward) "
                    "VALUES (:tid, :sid, :uid, :today, :streak, :reward) "
                    "ON CONFLICT (tenant_id, session_id, user_id, checkin_date) "
                    "DO NOTHING RETURNING streak, reward"
                ),
                params,
            )
            inserted = inserted_result.mappings().first()
            if inserted is None:
                existing_result = await conn.execute(
                    text(
                        "SELECT streak, reward FROM plugin_credits_checkin "
                        "WHERE tenant_id = :tid AND session_id = :sid AND user_id = :uid "
                        "AND checkin_date = :today"
                    ),
                    params,
                )
                existing = existing_result.mappings().first()
                if existing is None:
                    raise RuntimeError("credit checkin idempotency conflict was lost")
                balance_result = await conn.execute(
                    text(
                        "UPDATE plugin_credits_balance SET "
                        "display_name = CASE WHEN :display_name = '' THEN display_name "
                        "ELSE :display_name END "
                        "WHERE tenant_id = :tid AND session_id = :sid AND user_id = :uid "
                        "RETURNING credits"
                    ),
                    params,
                )
                balance_row = balance_result.mappings().first()
                if balance_row is None:
                    raise RuntimeError("credit checkin balance is missing")
                result: dict[str, Any] = {
                    "checked_in": False,
                    "already_checked_in": True,
                    "reward": int(existing["reward"] or 0),
                    "streak": int(existing["streak"] or 0),
                    "balance": int(balance_row["credits"] or 0),
                }
            else:
                await self._financial_checkpoint("checkin_record")
                credited_result = await conn.execute(
                    text(
                        "UPDATE plugin_credits_balance "
                        "SET credits = credits + :reward, "
                        "display_name = CASE WHEN :display_name = '' THEN display_name "
                        "ELSE :display_name END, updated_at = CURRENT_TIMESTAMP "
                        "WHERE tenant_id = :tid AND session_id = :sid AND user_id = :uid "
                        "RETURNING credits"
                    ),
                    params,
                )
                credited = credited_result.mappings().first()
                if credited is None:
                    raise RuntimeError("credit checkin balance is missing")
                balance = int(credited["credits"] or 0)
                await self._financial_checkpoint("checkin_balance_reward")

                ledger_result = await conn.execute(
                    text(
                        "INSERT INTO plugin_credits_ledger "
                        "(tenant_id, session_id, user_id, delta, reason, actor, reference, "
                        "idempotency_key) VALUES "
                        "(:tid, :sid, :uid, :reward, 'checkin', 'system', "
                        ":ledger_reference, :ledger_key) "
                        "ON CONFLICT DO NOTHING RETURNING id"
                    ),
                    params,
                )
                if ledger_result.mappings().first() is None:
                    raise RuntimeError("credit checkin ledger idempotency conflict")
                await self._financial_checkpoint("checkin_ledger")
                result = {
                    "checked_in": True,
                    "already_checked_in": False,
                    "reward": reward,
                    "streak": streak,
                    "balance": balance,
                    "bonus": streak_bonus,
                }

        result["checkin_status"] = await self.get_checkin_status(
            tenant_id,
            session_id,
            user_id,
        )
        return result

    async def list_members(
        self,
        tenant_id: str,
        session_id: str,
        *,
        limit: int = 200,
        query: str = "",
    ) -> dict[str, Any]:
        today = _today_cn()
        query_value = query.strip()
        params = {
            "tid": tenant_id,
            "sid": session_id,
            "today": today,
            "lim": max(1, min(limit, 500)),
            "query": query_value,
            "query_like": f"%{query_value}%",
        }
        items = await _exec(
            """
            WITH ranked AS (
                SELECT
                    b.user_id,
                    b.display_name,
                    b.credits,
                    b.updated_at,
                    ROW_NUMBER() OVER (
                        ORDER BY b.credits DESC, b.updated_at DESC NULLS LAST, b.user_id ASC
                    ) AS rank
                FROM plugin_credits_balance b
                WHERE b.tenant_id = :tid AND b.session_id = :sid
                  AND COALESCE(BTRIM(b.user_id), '') <> ''
            ),
            today_checkin AS (
                SELECT user_id, streak, reward
                FROM plugin_credits_checkin
                WHERE tenant_id = :tid AND session_id = :sid AND checkin_date = :today
            ),
            last_checkin AS (
                SELECT DISTINCT ON (user_id)
                    user_id, checkin_date, streak, reward, created_at
                FROM plugin_credits_checkin
                WHERE tenant_id = :tid AND session_id = :sid
                ORDER BY user_id, checkin_date DESC, created_at DESC
            )
            SELECT
                ranked.user_id,
                ranked.display_name,
                ranked.credits,
                ranked.updated_at,
                ranked.rank,
                (today_checkin.user_id IS NOT NULL) AS checked_in_today,
                COALESCE(today_checkin.reward, 0) AS today_reward,
                COALESCE(today_checkin.streak, 0) AS today_streak,
                last_checkin.checkin_date AS last_checkin_date,
                COALESCE(last_checkin.reward, 0) AS last_reward,
                COALESCE(last_checkin.streak, 0) AS last_streak
            FROM ranked
            LEFT JOIN today_checkin ON today_checkin.user_id = ranked.user_id
            LEFT JOIN last_checkin ON last_checkin.user_id = ranked.user_id
            WHERE (
                :query = ''
                OR ranked.user_id ILIKE :query_like
                OR COALESCE(ranked.display_name, '') ILIKE :query_like
            )
            ORDER BY ranked.rank
            LIMIT :lim
            """,
            params,
        )
        summary_rows = await _exec(
            """
            SELECT
                COUNT(*) AS member_count,
                COALESCE(SUM(credits), 0) AS total_credits
            FROM plugin_credits_balance
            WHERE tenant_id = :tid AND session_id = :sid
              AND COALESCE(BTRIM(user_id), '') <> ''
            """,
            params,
        )
        checked_in_rows = await _exec(
            """
            SELECT COUNT(DISTINCT user_id) AS checked_in_today_count
            FROM plugin_credits_checkin
            WHERE tenant_id = :tid AND session_id = :sid AND checkin_date = :today
            """,
            params,
        )
        filtered_rows = await _exec(
            """
            SELECT COUNT(*) AS filtered_count
            FROM plugin_credits_balance
            WHERE tenant_id = :tid AND session_id = :sid
              AND COALESCE(BTRIM(user_id), '') <> ''
              AND (
                :query = ''
                OR user_id ILIKE :query_like
                OR COALESCE(display_name, '') ILIKE :query_like
              )
            """,
            params,
        )
        summary = summary_rows[0] if summary_rows else {}
        checked = checked_in_rows[0] if checked_in_rows else {}
        filtered = filtered_rows[0] if filtered_rows else {}
        return {
            "items": items,
            "count": int(filtered.get("filtered_count") or 0),
            "summary": {
                "member_count": int(summary.get("member_count") or 0),
                "checked_in_today_count": int(checked.get("checked_in_today_count") or 0),
                "total_credits": int(summary.get("total_credits") or 0),
                "today": today.isoformat(),
            },
        }

    async def get_member_detail(
        self,
        tenant_id: str,
        session_id: str,
        user_id: str,
        *,
        ledger_limit: int = 20,
    ) -> dict[str, Any]:
        user_id = _require_user_id(user_id)
        cfg = await self.get_config(tenant_id, session_id)
        balance_row = await self.get_balance_record(tenant_id, session_id, user_id)
        rank_rows = await _exec(
            """
            SELECT rank
            FROM (
                SELECT
                    user_id,
                    ROW_NUMBER() OVER (
                        ORDER BY credits DESC, updated_at DESC NULLS LAST, user_id ASC
                    ) AS rank
                FROM plugin_credits_balance
                WHERE tenant_id = :tid AND session_id = :sid
            ) ranked
            WHERE ranked.user_id = :uid
            """,
            {"tid": tenant_id, "sid": session_id, "uid": user_id},
        )
        ledger = await self.get_ledger(
            tenant_id,
            session_id,
            limit=max(1, min(ledger_limit, 100)),
            user_id=user_id,
        )
        checkin_status = await self.get_checkin_status(tenant_id, session_id, user_id)

        return {
            "tenant_id": tenant_id,
            "session_id": session_id,
            "user_id": user_id,
            "display_name": (balance_row or {}).get("display_name") or "",
            "credits": int((balance_row or {}).get("credits") or 0),
            "updated_at": (balance_row or {}).get("updated_at"),
            "has_balance_record": balance_row is not None,
            "rank": int(rank_rows[0]["rank"]) if rank_rows else None,
            "config": {
                "credit_name": cfg["credit_name"],
                "initial_credits": cfg["initial_credits"],
                "checkin_mode": cfg["checkin_mode"],
                "checkin_mode_label": cfg["checkin_mode_label"],
            },
            "checkin_status": checkin_status,
            "recent_ledger": ledger["items"],
        }

    async def get_top(
        self,
        tenant_id: str,
        session_id: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        data = await self.list_members(tenant_id, session_id, limit=limit)
        return data["items"]

    async def get_ledger(
        self,
        tenant_id: str,
        session_id: str,
        *,
        limit: int = 50,
        user_id: str = "",
    ) -> dict[str, Any]:
        params = {
            "tid": tenant_id,
            "sid": session_id,
            "uid": user_id.strip(),
            "lim": max(1, min(limit, 200)),
        }
        rows = await _exec(
            """
            SELECT
                l.id,
                l.tenant_id,
                l.session_id,
                l.user_id,
                COALESCE(b.display_name, '') AS display_name,
                l.delta,
                l.reason,
                l.actor,
                l.reference,
                l.created_at
            FROM plugin_credits_ledger l
            LEFT JOIN plugin_credits_balance b
              ON b.tenant_id = l.tenant_id
             AND b.session_id = l.session_id
             AND b.user_id = l.user_id
            WHERE l.tenant_id = :tid
              AND l.session_id = :sid
              AND (:uid = '' OR l.user_id = :uid)
            ORDER BY l.created_at DESC, l.id DESC
            LIMIT :lim
            """,
            params,
        )
        return {"items": rows, "count": len(rows)}
