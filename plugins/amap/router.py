from __future__ import annotations

import hashlib
import importlib
import math
import os
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse

from app.admin.audit import set_admin_audit_context
from app.admin.auth_router import authenticate_admin_request
from app.admin.mutation_ledger import MutationAudit, MutationIdentity
from app.agent.scopes import GROUP_PERSONAL_MAP_SCOPE
from app.common import runtime_config as runtime_config_helpers
from app.common.config import Settings, get_settings
from app.common.request_models import StrictRequestModel
from app.common.runtime_config import (
    runtime_config_writes_allowed,
    runtime_env_file_path,
    serialize_env_value,
    write_env_overrides_atomic,
)
from plugins.amap.config_mutations import (
    AMapConfigIdempotencyConflictError,
    AMapConfigMutationIndeterminateError,
    AMapConfigMutationResult,
    AMapConfigMutationStore,
    AMapConfigPreparation,
)

_ENV_ASSIGNMENT_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")
_MUTABLE_ENV_KEYS = frozenset({"AMAP_API_TIMEOUT_SECONDS", "AMAP_STORAGE_DIR"})
_MUTATION_MARKER_PREFIX = " # agent-console-amap-mutation="
_AMAP_CONFIG_THREAD_LOCK = threading.RLock()
_QR_FILE_RE = re.compile(r"^amap-[^/\\]+\.png$", re.IGNORECASE)


class AMapConfigUpdate(StrictRequestModel):
    amap_api_key: str | None = None
    clear_amap_api_key: bool = False
    timeout_seconds: float | None = None
    storage_dir: str | None = None


class _ConfigVersionConflict(RuntimeError):
    def __init__(self, current_etag: str) -> None:
        super().__init__(f"AMap config version changed to {current_etag}")
        self.current_etag = current_etag


class _ConfigRecoveryIndeterminate(RuntimeError):
    """A prepared mutation no longer matches either attributable file state."""


def build_amap_router(
    settings: Settings,
    mutation_store: AMapConfigMutationStore | None = None,
) -> APIRouter:
    router = APIRouter()
    durable_mutations = mutation_store or AMapConfigMutationStore()

    @router.get("/admin/config")
    async def get_config(request: Request, response: Response) -> dict[str, Any]:
        _require_admin(request, settings)
        current, etag = _load_config_snapshot(settings)
        _set_config_headers(response, etag)
        return _config_payload(current)

    @router.post("/admin/config")
    async def set_config(
        body: AMapConfigUpdate,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        _require_admin(request, settings)
        if not runtime_config_writes_allowed(settings):
            raise HTTPException(
                status_code=409,
                detail="runtime_config_read_only_in_production",
            )
        operation_key = _required_idempotency_key(
            request.headers.get("idempotency-key")
        )
        update_payload = body.model_dump(exclude_unset=True)
        expected_etag = _required_if_match(request.headers.get("if-match"))
        if "amap_api_key" in update_payload or body.clear_amap_api_key:
            raise HTTPException(
                status_code=409,
                detail="amap_api_key_managed_by_secret_provider",
            )

        if body.timeout_seconds is None and body.storage_dir is None:
            raise HTTPException(status_code=400, detail="no_mutable_fields")

        env_updates: dict[str, Any] = {}
        if body.timeout_seconds is not None:
            if not math.isfinite(body.timeout_seconds) or body.timeout_seconds <= 0:
                raise HTTPException(
                    status_code=400, detail="timeout_seconds must be greater than 0"
                )
            env_updates["AMAP_API_TIMEOUT_SECONDS"] = float(body.timeout_seconds)
        if body.storage_dir is not None:
            cleaned_dir = str(body.storage_dir or "").strip()
            if not cleaned_dir:
                raise HTTPException(status_code=400, detail="storage_dir cannot be empty")
            env_updates["AMAP_STORAGE_DIR"] = cleaned_dir

        env_path = _env_file_path(settings)
        identity = MutationIdentity(
            tenant_id="__platform__",
            plugin_name="amap",
            operation="amap.runtime_config.update",
            # The generic ledger persists only a hash of this path.  Binding
            # the intent prevents a prepared mutation from moving to another
            # dotenv file after a deployment/configuration change.
            resource_key=str(
                Path(env_path).expanduser().resolve(strict=False)
            ),
            idempotency_key=operation_key,
            request_payload={
                "expected_etag": expected_etag,
                "updates": env_updates,
            },
        )
        mutation_marker = hashlib.sha256(operation_key.encode("utf-8")).hexdigest()
        try:
            claim = await durable_mutations.lookup(identity=identity)
        except AMapConfigIdempotencyConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "idempotency_key_conflict"},
                headers={"Cache-Control": "no-store"},
            ) from exc
        except AMapConfigMutationIndeterminateError as exc:
            raise _indeterminate_http_error(exc.mutation_id) from exc
        except Exception as exc:
            raise _ledger_unavailable_http_error() from exc

        if claim is None:
            _reject_externally_managed_fields(body)
            current = _load_runtime_settings()
            next_timeout = float(current.amap_api_timeout_seconds or 30.0)
            if body.timeout_seconds is not None:
                next_timeout = float(body.timeout_seconds)
            next_storage_dir = str(current.amap_storage_dir or "").strip()
            if body.storage_dir is not None:
                next_storage_dir = str(body.storage_dir).strip()
            env_path = _env_file_path(current)
            next_settings = current.model_copy(
                update={
                    "amap_api_timeout_seconds": next_timeout,
                    "amap_storage_dir": next_storage_dir,
                }
            )
            preparation = AMapConfigPreparation(
                expected_etag=expected_etag,
                target_etag=_planned_config_etag(
                    env_path,
                    env_updates,
                    mutation_marker=mutation_marker,
                ),
                response=_config_payload(next_settings, restart_required=True),
                before_state=_audit_config_state(current),
                after_state=_audit_config_state(next_settings),
                scope={
                    "timeout_changed": body.timeout_seconds is not None,
                    "storage_dir_changed": body.storage_dir is not None,
                },
            )
            try:
                claim = await durable_mutations.claim(
                    identity=identity,
                    audit=_mutation_audit(request),
                    preparation=preparation,
                )
            except AMapConfigIdempotencyConflictError as exc:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "idempotency_key_conflict"},
                    headers={"Cache-Control": "no-store"},
                ) from exc
            except AMapConfigMutationIndeterminateError as exc:
                raise _indeterminate_http_error(exc.mutation_id) from exc
            except Exception as exc:
                raise _ledger_unavailable_http_error() from exc

        if claim.completed is not None:
            _set_mutation_result(response, claim.completed)
            _set_semantic_audit_context(request, claim.completed)
            return claim.completed.response

        stored_preparation = claim.preparation
        if stored_preparation is None:
            raise _ledger_unavailable_http_error()
        try:
            applied_etag = _apply_prepared_config_mutation(
                env_path,
                env_updates,
                mutation_marker=mutation_marker,
                preparation=stored_preparation,
                is_new=claim.is_new,
            )
        except _ConfigVersionConflict as exc:
            failure_payload = {
                "detail": {
                    "code": "amap_config_version_conflict",
                    "expected_etag": stored_preparation.expected_etag,
                    "current_etag": exc.current_etag,
                }
            }
            try:
                result = await durable_mutations.complete_failure(
                    claim.mutation_id,
                    status_code=409,
                    response=failure_payload,
                    resource_version=exc.current_etag,
                )
            except Exception as completion_exc:
                raise _ledger_unavailable_http_error() from completion_exc
            _set_mutation_result(response, result)
            return result.response
        except _ConfigRecoveryIndeterminate as exc:
            try:
                await durable_mutations.mark_indeterminate(claim.mutation_id)
            except Exception as ledger_exc:
                raise _ledger_unavailable_http_error() from ledger_exc
            raise _indeterminate_http_error(claim.mutation_id) from exc
        except Exception as exc:
            # The atomic replace may or may not have happened.  Leave the
            # durable row prepared so a retry can resolve it from the marker.
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "amap_config_mutation_pending",
                    "mutation_id": claim.mutation_id,
                },
                headers={"Cache-Control": "no-store"},
            ) from exc

        try:
            result = await durable_mutations.complete_success(claim.mutation_id)
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "amap_config_mutation_pending",
                    "mutation_id": claim.mutation_id,
                },
                headers={
                    "Cache-Control": "no-store",
                    "ETag": applied_etag,
                    "X-Mutation-ID": claim.mutation_id,
                },
            ) from exc

        _set_mutation_result(response, result)
        _set_semantic_audit_context(request, result)
        return result.response

    @router.get("/files/{file_name:path}")
    async def get_amap_file(file_name: str):
        if not _QR_FILE_RE.fullmatch(str(file_name or "")):
            raise HTTPException(status_code=404, detail="file not found")
        storage_dir = Path(str(_load_runtime_settings().amap_storage_dir or "")).resolve()
        path = (storage_dir / file_name).resolve()
        if not _is_relative_to(path, storage_dir) or not path.is_file():
            raise HTTPException(status_code=404, detail="file not found")
        return FileResponse(path)

    return router


def _require_admin(request: Request, settings: Settings) -> None:
    authenticate_admin_request(request, settings)


def _env_file_path(settings: Settings) -> str:
    return runtime_env_file_path(settings)


def _write_env_overrides(
    env_path: str,
    values: dict[str, Any],
    *,
    process_env_values: dict[str, Any] | None = None,
) -> None:
    write_env_overrides_atomic(
        env_path,
        values,
        process_env_values=process_env_values,
    )


def _write_env_overrides_if_match(
    env_path: str,
    values: dict[str, Any],
    *,
    expected_etag: str,
) -> str:
    """Apply one AMap dotenv update under a cross-process optimistic lock."""

    path = Path(env_path).expanduser().resolve(strict=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.amap-config.lock")
    with _AMAP_CONFIG_THREAD_LOCK, _process_file_lock(lock_path):
        current_etag = _config_etag(str(path))
        if expected_etag != current_etag:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "amap_config_version_conflict",
                    "expected_etag": expected_etag,
                    "current_etag": current_etag,
                },
                headers={"ETag": current_etag, "Cache-Control": "no-store"},
            )
        _write_env_overrides(str(path), values)
        return _config_etag(str(path))


def _apply_prepared_config_mutation(
    env_path: str,
    values: dict[str, Any],
    *,
    mutation_marker: str,
    preparation: AMapConfigPreparation,
    is_new: bool,
) -> str:
    """Apply or recover one prepared dotenv mutation under the CAS lock."""

    path = Path(env_path).expanduser().resolve(strict=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.amap-config.lock")
    with _AMAP_CONFIG_THREAD_LOCK, _process_file_lock(lock_path):
        current_etag = _config_etag(str(path))
        marker_present = _has_config_mutation_marker(str(path), mutation_marker)
        if marker_present:
            return preparation.target_etag
        if current_etag != preparation.expected_etag:
            if is_new:
                raise _ConfigVersionConflict(current_etag)
            raise _ConfigRecoveryIndeterminate(
                f"prepared AMap mutation observed {current_etag}"
            )

        _write_env_overrides_with_marker(
            str(path),
            values,
            mutation_marker=mutation_marker,
        )
        next_etag = _config_etag(str(path))
        if (
            next_etag != preparation.target_etag
            or not _has_config_mutation_marker(str(path), mutation_marker)
        ):
            raise _ConfigRecoveryIndeterminate(
                "AMap dotenv write could not be attributed to its prepared intent"
            )
        return next_etag


def _planned_config_etag(
    env_path: str,
    values: dict[str, Any],
    *,
    mutation_marker: str,
) -> str:
    """Predict the ETag produced by the shared atomic dotenv merge helper."""

    path = Path(env_path).expanduser().resolve(strict=False)
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except FileNotFoundError:
        lines = []
    rendered = _merge_env_lines_for_plan(
        lines,
        values,
        mutation_marker=mutation_marker,
    )
    return _config_etag_for_lines(rendered)


def _merge_env_lines_for_plan(
    lines: list[str],
    values: dict[str, Any],
    *,
    mutation_marker: str = "",
) -> list[str]:
    remaining = dict(values)
    updated_keys: set[str] = set()
    rendered: list[str] = []
    for raw_line in lines:
        match = _ENV_ASSIGNMENT_RE.match(raw_line)
        if not match:
            rendered.append(raw_line)
            continue
        key = match.group(1)
        if key not in values:
            rendered.append(raw_line)
            continue
        if key in updated_keys:
            continue
        rendered.append(f"{key}={serialize_env_value(values[key])}\n")
        updated_keys.add(key)
        remaining.pop(key, None)

    if remaining and rendered and not rendered[-1].endswith(("\n", "\r")):
        rendered[-1] += "\n"
    for key, value in remaining.items():
        rendered.append(f"{key}={serialize_env_value(value)}\n")
    if mutation_marker:
        for index, raw_line in enumerate(rendered):
            match = _ENV_ASSIGNMENT_RE.match(raw_line)
            if match and match.group(1) in values:
                rendered[index] = (
                    raw_line.rstrip("\r\n")
                    + _MUTATION_MARKER_PREFIX
                    + mutation_marker
                    + "\n"
                )
                break
    return rendered


def _write_env_overrides_with_marker(
    env_path: str,
    values: dict[str, Any],
    *,
    mutation_marker: str,
) -> None:
    """Atomically merge values and an inline recovery marker in one replace."""

    path = Path(env_path).expanduser().resolve(strict=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    shared_thread_lock = runtime_config_helpers._thread_lock(path)
    shared_lock_path = path.with_name(f".{path.name}.lock")
    with shared_thread_lock, runtime_config_helpers._process_file_lock(shared_lock_path):
        try:
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        except FileNotFoundError:
            lines = []
        rendered = _merge_env_lines_for_plan(
            lines,
            values,
            mutation_marker=mutation_marker,
        )
        runtime_config_helpers._atomic_replace_text(path, "".join(rendered))


def _has_config_mutation_marker(env_path: str, mutation_marker: str) -> bool:
    path = Path(env_path).expanduser().resolve(strict=False)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return False
    suffix = _MUTATION_MARKER_PREFIX + mutation_marker
    return any(raw_line.rstrip().endswith(suffix) for raw_line in lines)


def _load_config_snapshot(settings: Settings) -> tuple[Settings, str]:
    if not runtime_config_writes_allowed(settings):
        return _load_runtime_settings(), '"amap-config-read-only"'

    path = Path(_env_file_path(settings)).expanduser().resolve(strict=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.amap-config.lock")
    with _AMAP_CONFIG_THREAD_LOCK, _process_file_lock(lock_path):
        current = _load_runtime_settings()
        return current, _config_etag(str(path))


def _required_if_match(value: str | None) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise HTTPException(
            status_code=428,
            detail="if_match_required",
            headers={"Cache-Control": "no-store"},
        )
    if normalized == "*" or not (
        normalized.startswith('"amap-config-') and normalized.endswith('"')
    ):
        raise HTTPException(status_code=400, detail="invalid_if_match")
    return normalized


def _required_idempotency_key(value: str | None) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise HTTPException(
            status_code=428,
            detail={"code": "idempotency_key_required"},
            headers={"Cache-Control": "no-store"},
        )
    if len(normalized) > 128 or not normalized.isprintable():
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_idempotency_key"},
            headers={"Cache-Control": "no-store"},
        )
    return normalized


def _config_etag(env_path: str) -> str:
    path = Path(env_path).expanduser().resolve(strict=False)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    return _config_etag_for_lines(lines)


def _config_etag_for_lines(lines: list[str]) -> str:
    selected: list[str] = []
    for raw_line in lines:
        match = _ENV_ASSIGNMENT_RE.match(raw_line)
        if match and match.group(1).upper() in _MUTABLE_ENV_KEYS:
            selected.append(raw_line.strip())
    canonical = "\0".join(selected).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()[:24]
    return f'"amap-config-{digest}"'


def _set_config_headers(response: Response, etag: str) -> None:
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-store"


def _set_mutation_result(
    response: Response,
    result: AMapConfigMutationResult,
) -> None:
    response.status_code = int(result.status_code)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Mutation-ID"] = result.mutation_id
    if result.resource_version:
        response.headers["ETag"] = result.resource_version
    if result.replayed:
        response.headers["Idempotent-Replayed"] = "true"


def _mutation_audit(request: Request) -> MutationAudit:
    principal = getattr(request.state, "admin_principal", None)
    return MutationAudit(
        actor=str(getattr(principal, "subject", "") or "unknown")[:128],
        actor_kind=str(getattr(principal, "auth_kind", "") or "unknown")[:32],
        roles=tuple(
            str(role)[:64]
            for role in (getattr(principal, "roles", ()) or ())
        ),
        reason_code="amap_runtime_config_update",
        trace_id=_request_trace_id(request),
    )


def _set_semantic_audit_context(
    request: Request,
    result: AMapConfigMutationResult,
) -> None:
    if result.status_code >= 400 or not result.after_state:
        return
    set_admin_audit_context(
        request,
        target_type="plugin:amap:runtime_config",
        before_state=dict(result.before_state),
        after_state=dict(result.after_state),
        trace_id=_request_trace_id(request),
        reason=(
            "amap_runtime_config_idempotent_replay"
            if result.replayed
            else "amap_runtime_config_updated"
        ),
    )


def _request_trace_id(request: Request) -> str:
    return str(
        request.headers.get("X-Trace-ID")
        or request.headers.get("X-Request-ID")
        or getattr(request.state, "admin_request_id", "")
    ).strip()[:128]


def _ledger_unavailable_http_error() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={"code": "amap_config_mutation_ledger_unavailable"},
        headers={"Cache-Control": "no-store"},
    )


def _indeterminate_http_error(mutation_id: str) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": "amap_config_mutation_indeterminate",
            "mutation_id": str(mutation_id),
        },
        headers={
            "Cache-Control": "no-store",
            "X-Mutation-ID": str(mutation_id),
        },
    )


def _reject_externally_managed_fields(body: AMapConfigUpdate) -> None:
    requested_keys: list[str] = []
    if body.timeout_seconds is not None:
        requested_keys.append("AMAP_API_TIMEOUT_SECONDS")
    if body.storage_dir is not None:
        requested_keys.append("AMAP_STORAGE_DIR")
    externally_managed = sorted(key for key in requested_keys if key in os.environ)
    if externally_managed:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "amap_config_field_managed_externally",
                "fields": externally_managed,
            },
        )


def _audit_config_state(settings: Settings) -> dict[str, object]:
    return {
        "api_key_configured": bool(str(settings.amap_api_key or "").strip()),
        "timeout_seconds": float(settings.amap_api_timeout_seconds or 30.0),
        # Do not persist a workstation/user path in the audit trail.
        "storage_dir_configured": bool(str(settings.amap_storage_dir or "").strip()),
    }


@contextmanager
def _process_file_lock(path: Path) -> Iterator[None]:
    """Serialize AMap compare-and-set across development server processes."""

    with path.open("a+b") as stream:
        if os.name == "nt":
            module = importlib.import_module("msvcrt")
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            module.locking(stream.fileno(), module.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                module.locking(stream.fileno(), module.LK_UNLCK, 1)
            return

        module = importlib.import_module("fcntl")
        module.flock(stream.fileno(), module.LOCK_EX)
        try:
            yield
        finally:
            module.flock(stream.fileno(), module.LOCK_UN)


def _load_runtime_settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()


def _config_payload(settings: Settings, *, restart_required: bool = False) -> dict[str, Any]:
    storage_dir = Path(str(getattr(settings, "amap_storage_dir", "") or ""))
    payload = {
        "api_key_configured": bool(str(getattr(settings, "amap_api_key", "") or "").strip()),
        "api_key_mutable_via_api": False,
        "api_key_source": "environment_or_file_secret",
        "runtime_config_mutable": runtime_config_writes_allowed(settings),
        "timeout_seconds": float(getattr(settings, "amap_api_timeout_seconds", 15.0) or 15.0),
        "storage_dir": str(storage_dir),
        "storage_dir_exists": storage_dir.exists(),
        "storage_dir_writable": _is_writable_dir(storage_dir),
        "agent_scope": GROUP_PERSONAL_MAP_SCOPE,
        "tools": [
            "amap_geo",
            "amap_text_search",
            "amap_regeo",
            "amap_place_detail",
            "amap_input_tips",
            "amap_around_search",
            "amap_route_plan",
            "amap_distance",
            "amap_weather",
            "amap_district",
            "amap_static_map",
            "amap_coordinate_convert",
            "amap_traffic_status",
            "amap_bus_info",
            "amap_create_personal_map",
        ],
        "restart_required": restart_required,
    }
    return payload


def _is_writable_dir(path: Path) -> bool:
    return path.is_dir() and os.access(path, os.W_OK)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
