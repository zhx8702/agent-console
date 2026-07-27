from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.common.logging import get_logger
from app.infra.db import get_engine
from app.infra.runtime_schema import verify_runtime_schema

logger = get_logger(__name__)

_DEFAULT_CONFIG = {
    "admin_user_ids_text": "",
    "user_commands_text": "",
    "admin_commands_text": "",
}
_LEGACY_ENABLED_WXBOT_ADMIN_COMMANDS = {
    "/ban",
    "/禁言",
    "/unban",
    "/解禁",
    "/banlist",
    "/禁言列表",
}


class CommandConfigVersionConflictError(RuntimeError):
    def __init__(self, *, expected: int, current: int) -> None:
        super().__init__(f"expected version {expected}, current version {current}")
        self.expected = expected
        self.current = current


@dataclass(frozen=True, slots=True)
class CommandConfigMutation:
    before: dict[str, Any]
    after: dict[str, Any]


async def _exec(sql: str, params: dict | None = None) -> list[dict[str, Any]]:
    engine = get_engine()
    async with engine.begin() as conn:
        result = await conn.execute(text(sql), params or {})
        if result.returns_rows:
            return [dict(row._mapping) for row in result.fetchall()]
        return []


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
        token = item if item.startswith("/") else f"/{item}"
        token = token.strip().lower()
        if not token or token in seen:
            continue
        seen.add(token)
        commands.append(token)
    return "\n".join(commands), commands


def _build_defaults(catalog: list[dict[str, object]]) -> dict[str, object]:
    user_commands: list[str] = []
    admin_commands: list[str] = []
    seen_user: set[str] = set()
    seen_admin: set[str] = set()
    for item in catalog:
        tokens = [str(item.get("command") or "").strip()]
        tokens.extend(str(alias or "").strip() for alias in (item.get("aliases") or []))
        target = admin_commands if bool(item.get("admin_only")) else user_commands
        target_seen = seen_admin if bool(item.get("admin_only")) else seen_user
        for token in tokens:
            normalized = token.lower()
            if not normalized or normalized in target_seen:
                continue
            target_seen.add(normalized)
            target.append(normalized)
    return {
        "admin_user_ids_text": "",
        "user_commands_text": "\n".join(user_commands),
        "admin_commands_text": "\n".join(admin_commands),
        "available_user_commands": user_commands,
        "available_admin_commands": admin_commands,
    }


def _merge_enabled_default_aliases(
    saved_commands: list[str],
    catalog: list[dict[str, object]],
    *,
    admin_only: bool,
) -> list[str]:
    effective = list(saved_commands)
    seen = set(effective)
    for item in catalog:
        if bool(item.get("admin_only")) != admin_only:
            continue
        tokens = [str(item.get("command") or "").strip()]
        tokens.extend(str(alias or "").strip() for alias in (item.get("aliases") or []))
        normalized = _normalize_command_list(tokens)[1]
        if not normalized or normalized[0] not in seen:
            continue
        for token in normalized:
            if token in seen:
                continue
            seen.add(token)
            effective.append(token)
    return effective


def _legacy_enabled_wxbot_admin_commands(catalog: list[dict[str, object]]) -> list[str]:
    commands: list[str] = []
    seen: set[str] = set()
    for item in catalog:
        if not bool(item.get("admin_only")):
            continue
        if str(item.get("plugin_name") or item.get("owner") or "").strip() != "wxbot":
            continue
        tokens = [str(item.get("command") or "").strip()]
        tokens.extend(str(alias or "").strip() for alias in (item.get("aliases") or []))
        for token in _normalize_command_list(tokens)[1]:
            if token not in _LEGACY_ENABLED_WXBOT_ADMIN_COMMANDS or token in seen:
                continue
            seen.add(token)
            commands.append(token)
    return commands


def _normalize_config(
    row: dict[str, Any] | None,
    tenant_id: str,
    catalog: list[dict[str, object]],
) -> dict[str, Any]:
    defaults = _build_defaults(catalog)
    cfg = dict(_DEFAULT_CONFIG)
    cfg.update(defaults)
    if row:
        cfg.update({k: v for k, v in row.items() if v is not None})
    cfg["tenant_id"] = tenant_id
    cfg["version"] = int(cfg.get("version") or 0) if row else 0
    cfg["catalog"] = catalog
    cfg["admin_user_ids_text"], cfg["admin_user_ids"] = _normalize_text_list(
        cfg.get("admin_user_ids_text", "")
    )
    cfg["user_commands_text"], cfg["user_commands"] = _normalize_command_list(
        cfg.get("user_commands_text", defaults["user_commands_text"])
    )
    cfg["admin_commands_text"], cfg["admin_commands"] = _normalize_command_list(
        cfg.get("admin_commands_text", defaults["admin_commands_text"])
    )
    cfg["user_commands"] = _merge_enabled_default_aliases(
        cfg["user_commands"],
        catalog,
        admin_only=False,
    )
    cfg["admin_commands"] = _merge_enabled_default_aliases(
        cfg["admin_commands"],
        catalog,
        admin_only=True,
    )
    legacy_wxbot_admin_commands = _legacy_enabled_wxbot_admin_commands(catalog)
    missing_legacy_wxbot_admin_commands = [
        command for command in legacy_wxbot_admin_commands if command not in cfg["admin_commands"]
    ]
    if row and missing_legacy_wxbot_admin_commands:
        cfg["admin_commands"].extend(missing_legacy_wxbot_admin_commands)
        logger.info(
            "commands.legacy_wxbot_admin_commands_enabled",
            tenant_id=tenant_id,
            commands=missing_legacy_wxbot_admin_commands,
        )
    cfg["available_user_commands"] = list(defaults["available_user_commands"])
    cfg["available_admin_commands"] = list(defaults["available_admin_commands"])
    return cfg


class CommandStore:
    def __init__(self, settings: Any) -> None:
        self.settings = settings

    async def ensure_tables(self) -> None:
        await verify_runtime_schema(get_engine(), component="commands store")

    async def get_config(
        self,
        tenant_id: str,
        *,
        catalog: list[dict[str, object]],
    ) -> dict[str, Any]:
        rows = await _exec(
            """
            SELECT tenant_id, admin_user_ids_text, user_commands_text,
                   admin_commands_text, version, updated_at
            FROM plugin_command_center_config
            WHERE tenant_id = :tenant_id
            """,
            {"tenant_id": tenant_id},
        )
        return _normalize_config(rows[0] if rows else None, tenant_id, catalog)

    async def set_config(
        self,
        tenant_id: str,
        *,
        expected_version: int,
        catalog: list[dict[str, object]],
        **updates: Any,
    ) -> CommandConfigMutation:
        normalized: dict[str, Any] = {}
        if "admin_user_ids_text" in updates:
            normalized["admin_user_ids_text"] = _normalize_text_list(
                updates["admin_user_ids_text"]
            )[0]
        if "user_commands_text" in updates:
            normalized["user_commands_text"] = _normalize_command_list(
                updates["user_commands_text"]
            )[0]
        if "admin_commands_text" in updates:
            normalized["admin_commands_text"] = _normalize_command_list(
                updates["admin_commands_text"]
            )[0]

        engine = get_engine()
        async with engine.begin() as conn:
            row = await _config_row(conn, tenant_id, for_update=True)
            before = _normalize_config(row, tenant_id, catalog)
            current_version = int(before["version"])
            if current_version != expected_version:
                raise CommandConfigVersionConflictError(
                    expected=expected_version,
                    current=current_version,
                )

            next_config = dict(before)
            next_config.update(normalized)
            params = {
                "tenant_id": tenant_id,
                "admin_user_ids_text": str(next_config["admin_user_ids_text"]),
                "user_commands_text": str(next_config["user_commands_text"]),
                "admin_commands_text": str(next_config["admin_commands_text"]),
                "expected_version": expected_version,
            }
            if row is None:
                result = await conn.execute(
                    text(
                        """
                        INSERT INTO plugin_command_center_config
                            (tenant_id, admin_user_ids_text, user_commands_text,
                             admin_commands_text, version, updated_at)
                        VALUES
                            (:tenant_id, :admin_user_ids_text, :user_commands_text,
                             :admin_commands_text, 1, NOW())
                        ON CONFLICT (tenant_id) DO NOTHING
                        RETURNING tenant_id, admin_user_ids_text, user_commands_text,
                                  admin_commands_text, version, updated_at
                        """
                    ),
                    params,
                )
            else:
                result = await conn.execute(
                    text(
                        """
                        UPDATE plugin_command_center_config
                        SET admin_user_ids_text = :admin_user_ids_text,
                            user_commands_text = :user_commands_text,
                            admin_commands_text = :admin_commands_text,
                            version = version + 1,
                            updated_at = NOW()
                        WHERE tenant_id = :tenant_id
                          AND version = :expected_version
                        RETURNING tenant_id, admin_user_ids_text, user_commands_text,
                                  admin_commands_text, version, updated_at
                        """
                    ),
                    params,
                )
            written = result.mappings().first()
            if written is None:
                current = await _config_row(conn, tenant_id, for_update=True)
                raise CommandConfigVersionConflictError(
                    expected=expected_version,
                    current=int((current or {}).get("version") or 0),
                )
            after = _normalize_config(dict(written), tenant_id, catalog)
        return CommandConfigMutation(before=before, after=after)


async def _config_row(
    conn: AsyncConnection,
    tenant_id: str,
    *,
    for_update: bool,
) -> dict[str, Any] | None:
    suffix = " FOR UPDATE" if for_update else ""
    result = await conn.execute(
        text(
            "SELECT tenant_id, admin_user_ids_text, user_commands_text, "
            "admin_commands_text, version, updated_at "
            "FROM plugin_command_center_config WHERE tenant_id = :tenant_id"
            f"{suffix}"
        ),
        {"tenant_id": tenant_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None
