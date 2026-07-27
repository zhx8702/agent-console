"""Authenticated, tenant-scoped admin API for channel connections."""

import re
from collections.abc import Callable
from typing import Annotated, Any, TypeVar

from fastapi import (
    APIRouter,
    Body,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from pydantic import BaseModel, ConfigDict, ValidationError

from app.admin.authorization import (
    AdminPermission,
    Principal,
    RoutePermission,
    build_admin_authorization_dependency,
)
from app.admin.mutation_ledger import (
    MutationAudit,
    MutationIdempotencyConflictError,
    hash_identifier,
)
from app.admin.route_permissions import declare_route_permission
from app.channel.adapters import ChannelAdapterCatalog, ChannelAdapterDescriptor
from app.channel.connections import (
    ChannelConnectionCheckResult,
    ChannelConnectionCreateRequest,
    ChannelConnectionDeleteResult,
    ChannelConnectionDocument,
    ChannelConnectionExistsError,
    ChannelConnectionMutationResult,
    ChannelConnectionNotFoundError,
    ChannelConnectionPayloadError,
    ChannelConnectionReadOnlyError,
    ChannelConnectionStateError,
    ChannelConnectionStore,
    ChannelConnectionUpdateRequest,
    ChannelConnectionVersionConflictError,
    generated_connection_id,
    legacy_wxbot_connection_from_settings,
)
from app.channel.identity import LEGACY_WXBOT_CONNECTION_ID
from app.common.config import Settings

_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_TENANT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
_T = TypeVar("_T", bound=BaseModel)
_TenantQuery = Annotated[
    str,
    Query(min_length=1, max_length=64, pattern=_TENANT_ID_PATTERN),
]


class ChannelAdapterCatalogDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ChannelAdapterDescriptor]


class ChannelConnectionPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    items: list[ChannelConnectionDocument]


def build_channel_admin_router(
    store: ChannelConnectionStore,
    settings: Settings | None = None,
    *,
    catalog: ChannelAdapterCatalog | None = None,
    authorization_dependency: Callable[..., Any] | None = None,
    legacy_settings: Any | None = None,
) -> APIRouter:
    """Build the generic channel catalog/connection control plane.

    ``catalog`` may contain plugin-contributed registrations.  Passing
    ``legacy_settings`` explicitly exposes the read-only WXBOT_* compatibility
    projection; ordinary deployments should migrate it to a durable connection.
    """

    router = APIRouter(prefix="/v1/admin", tags=["channel-connections"])
    adapter_catalog = catalog or store.catalog
    authorize = authorization_dependency or build_admin_authorization_dependency(settings)

    @router.get(
        "/channel-adapters",
        response_model=ChannelAdapterCatalogDocument,
    )
    @declare_route_permission(
        RoutePermission(
            method="GET",
            path="/v1/admin/channel-adapters",
            permission=AdminPermission.READ,
        )
    )
    async def list_adapters(
        tenant_id: _TenantQuery,
        principal: Annotated[Principal, Depends(authorize)],
        response: Response,
    ) -> ChannelAdapterCatalogDocument:
        _require_tenant_management(principal, tenant_id)
        _set_no_store(response)
        return ChannelAdapterCatalogDocument(
            items=list(adapter_catalog.list_descriptors())
        )

    @router.get(
        "/channel-connections",
        response_model=ChannelConnectionPage,
    )
    @declare_route_permission(
        RoutePermission(
            method="GET",
            path="/v1/admin/channel-connections",
            permission=AdminPermission.READ,
        )
    )
    async def list_connections(
        tenant_id: _TenantQuery,
        principal: Annotated[Principal, Depends(authorize)],
        response: Response,
    ) -> ChannelConnectionPage:
        _require_tenant_management(principal, tenant_id)
        items = await store.list(tenant_id)
        legacy = _legacy_for_tenant(legacy_settings, tenant_id)
        if legacy is not None and all(
            item.connection_id != LEGACY_WXBOT_CONNECTION_ID for item in items
        ):
            items.append(legacy)
        items.sort(key=lambda item: (item.priority, item.display_name, item.connection_id))
        _set_no_store(response)
        return ChannelConnectionPage(tenant_id=tenant_id, items=items)

    @router.post(
        "/channel-connections",
        response_model=ChannelConnectionDocument,
        status_code=status.HTTP_201_CREATED,
    )
    @declare_route_permission(
        RoutePermission(
            method="POST",
            path="/v1/admin/channel-connections",
            permission=AdminPermission.DANGER,
        )
    )
    async def create_connection(
        tenant_id: _TenantQuery,
        body: Annotated[dict[str, Any], Body(...)],
        request: Request,
        response: Response,
        principal: Annotated[Principal, Depends(authorize)],
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key", max_length=128)
        ] = None,
        change_reason: Annotated[
            str | None, Header(alias="X-Change-Reason", max_length=512)
        ] = None,
    ) -> ChannelConnectionDocument:
        _require_tenant_management(principal, tenant_id)
        operation_key = _required_idempotency_key(idempotency_key)
        create = _safe_request_model(ChannelConnectionCreateRequest, body)
        connection_id = create.connection_id or generated_connection_id(
            tenant_id, operation_key
        )
        try:
            outcome = await store.create(
                tenant_id,
                create,
                idempotency_key=operation_key,
                audit=_mutation_audit(
                    principal,
                    request,
                    connection_id=connection_id,
                    expected_version=0,
                    reason_code="channel_connection_create",
                    reason=change_reason,
                ),
            )
        except Exception as exc:
            raise _mutation_error(exc) from exc
        document = _as_document(outcome)
        _set_mutation_headers(response, outcome, document.version)
        return document

    @router.get(
        "/channel-connections/{connection_id}",
        response_model=ChannelConnectionDocument,
    )
    @declare_route_permission(
        RoutePermission(
            method="GET",
            path="/v1/admin/channel-connections/{connection_id}",
            permission=AdminPermission.READ,
        )
    )
    async def get_connection(
        tenant_id: _TenantQuery,
        connection_id: str,
        principal: Annotated[Principal, Depends(authorize)],
        response: Response,
    ) -> ChannelConnectionDocument:
        _require_tenant_management(principal, tenant_id)
        if connection_id == LEGACY_WXBOT_CONNECTION_ID:
            document = _legacy_for_tenant(legacy_settings, tenant_id)
            if document is None:
                raise _mutation_error(ChannelConnectionNotFoundError(connection_id))
        else:
            try:
                document = await store.get(tenant_id, connection_id)
            except Exception as exc:
                raise _mutation_error(exc) from exc
        _set_version_headers(response, document.version)
        return document

    @router.patch(
        "/channel-connections/{connection_id}",
        response_model=ChannelConnectionDocument,
    )
    @declare_route_permission(
        RoutePermission(
            method="PATCH",
            path="/v1/admin/channel-connections/{connection_id}",
            permission=AdminPermission.DANGER,
        )
    )
    async def patch_connection(
        tenant_id: _TenantQuery,
        connection_id: str,
        body: Annotated[dict[str, Any], Body(...)],
        request: Request,
        response: Response,
        principal: Annotated[Principal, Depends(authorize)],
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key", max_length=128)
        ] = None,
        change_reason: Annotated[
            str | None, Header(alias="X-Change-Reason", max_length=512)
        ] = None,
    ) -> ChannelConnectionDocument:
        update_body = _safe_request_model(ChannelConnectionUpdateRequest, body)
        outcome = await _call_mutation(
            store.update,
            tenant_id,
            connection_id,
            request=request,
            response=response,
            principal=principal,
            if_match=if_match,
            idempotency_key=idempotency_key,
            reason=change_reason,
            reason_code="channel_connection_update",
            call_kwargs={"request": update_body},
        )
        return _as_document(outcome)

    @router.delete(
        "/channel-connections/{connection_id}",
        response_model=ChannelConnectionDeleteResult,
    )
    @declare_route_permission(
        RoutePermission(
            method="DELETE",
            path="/v1/admin/channel-connections/{connection_id}",
            permission=AdminPermission.DANGER,
        )
    )
    async def delete_connection(
        tenant_id: _TenantQuery,
        connection_id: str,
        request: Request,
        response: Response,
        principal: Annotated[Principal, Depends(authorize)],
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key", max_length=128)
        ] = None,
        change_reason: Annotated[
            str | None, Header(alias="X-Change-Reason", max_length=512)
        ] = None,
    ) -> ChannelConnectionDeleteResult:
        outcome = await _call_mutation(
            store.delete,
            tenant_id,
            connection_id,
            request=request,
            response=response,
            principal=principal,
            if_match=if_match,
            idempotency_key=idempotency_key,
            reason=change_reason,
            reason_code="channel_connection_delete",
        )
        assert isinstance(outcome.value, ChannelConnectionDeleteResult)
        return outcome.value

    @router.post(
        "/channel-connections/{connection_id}/validate",
        response_model=ChannelConnectionCheckResult,
    )
    @declare_route_permission(
        RoutePermission(
            method="POST",
            path="/v1/admin/channel-connections/{connection_id}/validate",
            permission=AdminPermission.WRITE,
        )
    )
    async def validate_connection(
        tenant_id: _TenantQuery,
        connection_id: str,
        request: Request,
        response: Response,
        principal: Annotated[Principal, Depends(authorize)],
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key", max_length=128)
        ] = None,
    ) -> ChannelConnectionCheckResult:
        outcome = await _call_mutation(
            store.validate,
            tenant_id,
            connection_id,
            request=request,
            response=response,
            principal=principal,
            if_match=if_match,
            idempotency_key=idempotency_key,
            reason=None,
            reason_code="channel_connection_validate",
        )
        assert isinstance(outcome.value, ChannelConnectionCheckResult)
        return outcome.value

    @router.post(
        "/channel-connections/{connection_id}/probe",
        response_model=ChannelConnectionCheckResult,
    )
    @declare_route_permission(
        RoutePermission(
            method="POST",
            path="/v1/admin/channel-connections/{connection_id}/probe",
            permission=AdminPermission.DANGER,
        )
    )
    async def probe_connection(
        tenant_id: _TenantQuery,
        connection_id: str,
        request: Request,
        response: Response,
        principal: Annotated[Principal, Depends(authorize)],
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key", max_length=128)
        ] = None,
    ) -> ChannelConnectionCheckResult:
        outcome = await _call_mutation(
            store.probe,
            tenant_id,
            connection_id,
            request=request,
            response=response,
            principal=principal,
            if_match=if_match,
            idempotency_key=idempotency_key,
            reason=None,
            reason_code="channel_connection_probe",
        )
        assert isinstance(outcome.value, ChannelConnectionCheckResult)
        return outcome.value

    for action, enabled in (("enable", True), ("disable", False)):
        _register_desired_state_route(
            router,
            store,
            authorize,
            action=action,
            enabled=enabled,
        )

    return router


def _register_desired_state_route(
    router: APIRouter,
    store: ChannelConnectionStore,
    authorize: Callable[..., Any],
    *,
    action: str,
    enabled: bool,
) -> None:
    route_path = f"/channel-connections/{{connection_id}}/{action}"
    declared_path = f"/v1/admin{route_path}"

    async def change_desired_state(
        tenant_id: _TenantQuery,
        connection_id: str,
        request: Request,
        response: Response,
        principal: Annotated[Principal, Depends(authorize)],
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key", max_length=128)
        ] = None,
        change_reason: Annotated[
            str | None, Header(alias="X-Change-Reason", max_length=512)
        ] = None,
    ) -> ChannelConnectionDocument:
        outcome = await _call_mutation(
            store.set_desired_state,
            tenant_id,
            connection_id,
            request=request,
            response=response,
            principal=principal,
            if_match=if_match,
            idempotency_key=idempotency_key,
            reason=change_reason,
            reason_code=f"channel_connection_{action}",
            call_kwargs={"enabled": enabled},
        )
        return _as_document(outcome)

    change_desired_state.__name__ = f"{action}_channel_connection"
    declared = declare_route_permission(
        RoutePermission(
            method="POST",
            path=declared_path,
            permission=AdminPermission.DANGER,
        )
    )(change_desired_state)
    router.add_api_route(
        route_path,
        declared,
        methods=["POST"],
        response_model=ChannelConnectionDocument,
    )


async def _call_mutation(
    operation: Callable[..., Any],
    tenant_id: str,
    connection_id: str,
    *,
    request: Request,
    response: Response,
    principal: Principal,
    if_match: str | None,
    idempotency_key: str | None,
    reason: str | None,
    reason_code: str,
    call_kwargs: dict[str, Any] | None = None,
) -> ChannelConnectionMutationResult:
    _require_tenant_management(principal, tenant_id)
    expected_version = _required_if_match(if_match)
    operation_key = _required_idempotency_key(idempotency_key)
    try:
        outcome = await operation(
            tenant_id,
            connection_id,
            expected_version=expected_version,
            idempotency_key=operation_key,
            audit=_mutation_audit(
                principal,
                request,
                connection_id=connection_id,
                expected_version=expected_version,
                reason_code=reason_code,
                reason=reason,
            ),
            **dict(call_kwargs or {}),
        )
    except Exception as exc:
        raise _mutation_error(exc) from exc
    version = _outcome_version(outcome)
    _set_mutation_headers(response, outcome, version)
    return outcome


def _safe_request_model(model: type[_T], body: dict[str, Any]) -> _T:
    try:
        return model.model_validate(body)
    except ValidationError as exc:
        # Do not return Pydantic's input values: a caller may have accidentally
        # put plaintext credentials in a forbidden field.
        fields = sorted(
            {
                str(error.get("loc", ("request",))[-1])[:64]
                for error in exc.errors(include_input=False, include_url=False)
            }
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "invalid_request", "fields": fields},
        ) from None


def _required_if_match(value: str | None) -> int:
    if value is None or not value.strip():
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="if_match_required",
        )
    normalized = value.strip()
    if normalized.startswith("W/"):
        normalized = normalized[2:].strip()
    if len(normalized) >= 2 and normalized[0] == '"' and normalized[-1] == '"':
        normalized = normalized[1:-1]
    if not normalized.isdigit() or int(normalized) < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid_if_match",
        )
    return int(normalized)


def _required_idempotency_key(value: str | None) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="idempotency_key_required",
        )
    if not _IDEMPOTENCY_KEY.fullmatch(normalized):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid_idempotency_key",
        )
    return normalized


def _require_tenant_management(principal: Principal, tenant_id: str) -> None:
    normalized = str(tenant_id or "").strip()
    if not normalized or normalized != tenant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid_tenant_id",
        )
    if not principal.allows_tenant(normalized):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="tenant_scope_forbidden",
        )
    if principal.requires_explicit_group_scope:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="group_scope_forbidden",
        )


def _mutation_audit(
    principal: Principal,
    request: Request,
    *,
    connection_id: str,
    expected_version: int,
    reason_code: str,
    reason: str | None,
) -> MutationAudit:
    return MutationAudit(
        actor=principal.subject,
        actor_kind=principal.auth_kind,
        roles=principal.roles,
        scope={
            "connection_hash": hash_identifier(connection_id),
            "expected_version": expected_version,
        },
        reason_code=reason_code,
        reason=str(reason or ""),
        trace_id=str(
            request.headers.get("X-Trace-ID")
            or request.headers.get("X-Request-ID")
            or ""
        ).strip()[:128],
    )


def _mutation_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ChannelConnectionVersionConflictError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "version_conflict",
                "expected_version": exc.expected,
                "current_version": exc.current,
            },
            headers={"ETag": f'"{exc.current}"', "Cache-Control": "no-store"},
        )
    if isinstance(exc, MutationIdempotencyConflictError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "idempotency_key_conflict"},
        )
    if isinstance(exc, ChannelConnectionNotFoundError):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "channel_connection_not_found"},
        )
    if isinstance(exc, ChannelConnectionExistsError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "channel_connection_exists"},
        )
    if isinstance(exc, ChannelConnectionReadOnlyError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": str(exc)},
        )
    if isinstance(exc, ChannelConnectionStateError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code},
        )
    if isinstance(exc, ChannelConnectionPayloadError):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": exc.codes[0], "errors": list(exc.codes)},
        )
    if isinstance(exc, KeyError):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "adapter_not_registered"},
        )
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"code": "channel_connection_operation_failed"},
    )


def _set_no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _set_version_headers(response: Response, version: int) -> None:
    response.headers["ETag"] = f'"{version}"'
    _set_no_store(response)


def _set_mutation_headers(
    response: Response,
    outcome: ChannelConnectionMutationResult,
    version: int,
) -> None:
    response.status_code = outcome.status_code
    _set_version_headers(response, version)
    response.headers["X-Mutation-ID"] = outcome.mutation_id
    if outcome.replayed:
        response.headers["Idempotent-Replayed"] = "true"


def _outcome_version(outcome: ChannelConnectionMutationResult) -> int:
    value = outcome.value
    if isinstance(value, ChannelConnectionCheckResult):
        return value.connection.version
    return int(value.version)


def _as_document(outcome: ChannelConnectionMutationResult) -> ChannelConnectionDocument:
    assert isinstance(outcome.value, ChannelConnectionDocument)
    return outcome.value


def _legacy_for_tenant(
    legacy_settings: Any | None,
    tenant_id: str,
) -> ChannelConnectionDocument | None:
    if legacy_settings is None:
        return None
    if not str(getattr(legacy_settings, "wxbot_api_token", "") or "").strip():
        return None
    configured_tenant = str(
        getattr(legacy_settings, "wxbot_default_tenant_id", "") or "default"
    ).strip()
    if configured_tenant != str(tenant_id or "").strip():
        return None
    return legacy_wxbot_connection_from_settings(
        legacy_settings,
        tenant_id=configured_tenant,
    )


__all__ = [
    "ChannelAdapterCatalogDocument",
    "ChannelConnectionPage",
    "build_channel_admin_router",
]
