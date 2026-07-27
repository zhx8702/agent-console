"""Exact control-plane route permission registry and startup validation.

The registry is intentionally a committed manifest, rather than a permission
inference rule.  Adding or changing an admin/plugin route therefore requires a
reviewed declaration; an undeclared route prevents application startup.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, TypeVar

from fastapi import FastAPI
from fastapi.routing import APIRoute

from app.admin.authorization import (
    ROUTE_PERMISSIONS_ATTR,
    AdminPermission,
    RoutePermission,
    RoutePermissionEnforcement,
)

_CONTROL_PLANE_PREFIXES = ("/v1/admin", "/plugins/")
_ENDPOINT_ROUTE_PERMISSIONS_ATTR = "_agent_console_endpoint_route_permissions"
_Endpoint = TypeVar("_Endpoint", bound=Callable[..., Any])


def declare_route_permission(
    declaration: RoutePermission,
) -> Callable[[_Endpoint], _Endpoint]:
    """Attach an exact typed permission declaration to a route endpoint.

    This is the extension hook for new routers.  Put it below the FastAPI
    decorator so the same endpoint object carries the declaration through
    nested/lazy ``include_router`` calls::

        @router.get("/widgets")
        @declare_route_permission(
            RoutePermission("GET", "/v1/admin/widgets", AdminPermission.READ)
        )
        async def list_widgets(): ...

    The startup validator checks that method and final mounted path still match,
    so moving the route without updating the declaration fails closed.
    """

    def decorate(endpoint: _Endpoint) -> _Endpoint:
        existing = tuple(
            getattr(endpoint, _ENDPOINT_ROUTE_PERMISSIONS_ATTR, ()) or ()
        )
        setattr(
            endpoint,
            _ENDPOINT_ROUTE_PERMISSIONS_ATTR,
            (*existing, declaration),
        )
        return endpoint

    return decorate


class RoutePermissionRegistry:
    """Validated exact declarations keyed by ``(HTTP method, route template)``."""

    def __init__(self, declarations: Iterable[RoutePermission]) -> None:
        by_key: dict[tuple[str, str], RoutePermission] = {}
        duplicates: list[tuple[str, str]] = []
        for declaration in declarations:
            key = declaration.key
            if key in by_key:
                duplicates.append(key)
            by_key[key] = declaration
        if duplicates:
            rendered = ", ".join(_render_key(key) for key in sorted(set(duplicates)))
            raise ValueError(f"duplicate route permission declarations: {rendered}")
        self._by_key = by_key

    @property
    def declarations(self) -> tuple[RoutePermission, ...]:
        return tuple(self._by_key[key] for key in sorted(self._by_key))

    def get(self, method: str, path: str) -> RoutePermission | None:
        return self._by_key.get(_route_key(method, path))

    def bind_and_validate(self, app: FastAPI) -> None:
        """Bind declarations to routes and fail startup on any missing policy."""

        # Strict mode is set before validation so even a caller which catches a
        # startup failure cannot accidentally serve an undeclared guarded route.
        app.state.route_permissions_strict = True

        routes_by_key: dict[tuple[str, str], APIRoute] = {}
        effective_declarations = dict(self._by_key)
        duplicate_routes: list[tuple[str, str]] = []
        invalid_endpoint_declarations: list[str] = []
        route_records = tuple(_iter_effective_api_routes(app))
        for route, mounted_path in route_records:
            if not _is_control_plane_path(mounted_path):
                continue
            for method in sorted(route.methods or ()):
                key = _route_key(method, mounted_path)
                if key in routes_by_key:
                    duplicate_routes.append(key)
                routes_by_key[key] = route

            endpoint_declarations = tuple(
                getattr(route.endpoint, _ENDPOINT_ROUTE_PERMISSIONS_ATTR, ()) or ()
            )
            route_keys = {
                _route_key(method, mounted_path)
                for method in route.methods or ()
            }
            for declaration in endpoint_declarations:
                if not isinstance(declaration, RoutePermission):
                    invalid_endpoint_declarations.append(
                        f"{mounted_path}:non-RoutePermission"
                    )
                    continue
                key = declaration.key
                if key not in route_keys:
                    invalid_endpoint_declarations.append(
                        f"{_render_key(key)}!=mounted {','.join(_render_key(item) for item in sorted(route_keys))}"
                    )
                    continue
                existing = effective_declarations.get(key)
                if existing is not None and existing != declaration:
                    invalid_endpoint_declarations.append(
                        f"{_render_key(key)}:conflicting declarations"
                    )
                    continue
                effective_declarations[key] = declaration

        undeclared = sorted(set(routes_by_key).difference(effective_declarations))
        if duplicate_routes or undeclared or invalid_endpoint_declarations:
            details: list[str] = []
            if undeclared:
                details.append(
                    "undeclared=" + ",".join(_render_key(key) for key in undeclared)
                )
            if duplicate_routes:
                details.append(
                    "duplicates="
                    + ",".join(_render_key(key) for key in sorted(set(duplicate_routes)))
                )
            if invalid_endpoint_declarations:
                details.append(
                    "invalid_endpoint_declarations="
                    + ",".join(sorted(invalid_endpoint_declarations))
                )
            raise RuntimeError("control-plane route permission validation failed: " + "; ".join(details))

        declarations_by_route: dict[
            int,
            tuple[APIRoute, dict[str, RoutePermission]],
        ] = {}
        for key, route in routes_by_key.items():
            declaration = effective_declarations[key]
            _, declarations = declarations_by_route.setdefault(
                id(route),
                (route, {}),
            )
            declarations[key[0]] = declaration

        for route, declarations in declarations_by_route.values():
            setattr(route, ROUTE_PERMISSIONS_ATTR, dict(declarations))
            extra = dict(route.openapi_extra or {})
            extra["x-route-permissions"] = {
                method: {
                    "permission": declaration.permission.value,
                    "enforcement": declaration.enforcement.value,
                }
                for method, declaration in sorted(declarations.items())
            }
            route.openapi_extra = extra

        app.state.route_permission_registry = self
        app.state.bound_route_permissions = tuple(
            effective_declarations[key] for key in sorted(routes_by_key)
        )


def _iter_effective_api_routes(app: FastAPI) -> Iterable[tuple[APIRoute, str]]:
    """Yield request-visible routes with their final mounted path.

    FastAPI's lazy ``_IncludedRouter`` keeps the request-visible ``APIRoute``
    under an effective route context.  We use the public-ish callable exposed
    by that wrapper without importing its private class, retaining compatibility
    with FastAPI versions which flatten included routes eagerly.
    """

    for mounted in app.routes:
        if isinstance(mounted, APIRoute):
            yield mounted, mounted.path
            continue
        effective_candidates = getattr(mounted, "effective_candidates", None)
        if not callable(effective_candidates):
            continue
        for context in effective_candidates():
            route = getattr(context, "original_route", None)
            path = str(getattr(context, "path", "") or "")
            if isinstance(route, APIRoute) and path:
                yield route, path


def _route_key(method: str, path: str) -> tuple[str, str]:
    return (
        str(method or "").upper(),
        "/" + str(path or "").strip().strip("/").lower(),
    )


def _is_control_plane_path(path: str) -> bool:
    normalized = "/" + str(path or "").strip().strip("/").lower()
    return normalized == "/v1/admin" or normalized.startswith(_CONTROL_PLANE_PREFIXES)


def _render_key(key: tuple[str, str]) -> str:
    return f"{key[0]} {key[1]}"


_AUTH_HANDLER_KEYS = frozenset(
    {
        ("GET", "/v1/admin/auth/me"),
        ("GET", "/v1/admin/auth/session"),
        ("POST", "/v1/admin/auth/session"),
    }
)
_PUBLIC_SESSION_KEYS = frozenset({("DELETE", "/v1/admin/auth/session")})


def _parse_manifest(raw: str) -> tuple[RoutePermission, ...]:
    declarations: list[RoutePermission] = []
    for line_number, raw_line in enumerate(raw.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            permission_name, method, path = line.split(maxsplit=2)
            permission = AdminPermission[permission_name]
        except (KeyError, ValueError) as exc:
            raise ValueError(f"invalid route permission manifest line {line_number}: {line!r}") from exc
        key = _route_key(method, path)
        enforcement = RoutePermissionEnforcement.ADMIN_GUARD
        if key in _AUTH_HANDLER_KEYS:
            enforcement = RoutePermissionEnforcement.AUTH_HANDLER
        elif key in _PUBLIC_SESSION_KEYS:
            enforcement = RoutePermissionEnforcement.PUBLIC_SESSION_CLEAR
        declarations.append(
            RoutePermission(
                method=key[0],
                path=key[1],
                permission=permission,
                enforcement=enforcement,
            )
        )
    return tuple(declarations)


# One reviewed declaration per currently shipped control-plane route.  Keep
# this list sorted by permission, method, and path to make security review and
# merge-conflict resolution deterministic.
_ROUTE_PERMISSION_MANIFEST = """
READ GET /plugins/amap/admin/config
READ GET /plugins/amap/files/{file_name:path}
READ GET /plugins/commands/catalog
READ GET /plugins/commands/config/{tenant_id}
READ GET /plugins/credits/balance/{tenant_id}/{session_id}/{user_id}
READ GET /plugins/credits/checkin-status/{tenant_id}/{session_id}/{user_id}
READ GET /plugins/credits/config/{tenant_id}/{session_id}
READ GET /plugins/credits/ledger/{tenant_id}/{session_id}
READ GET /plugins/credits/member/{tenant_id}/{session_id}/{user_id}
READ GET /plugins/credits/members/{tenant_id}/{session_id}
READ GET /plugins/credits/top/{tenant_id}/{session_id}
READ GET /plugins/draw/files/{file_name:path}
READ GET /plugins/draw/images
READ GET /plugins/draw/images/{image_id}
READ GET /plugins/draw/tasks
READ GET /plugins/draw/tasks/{task_id}
READ GET /plugins/group_activity/config/{tenant_id}/{session_id:path}
READ GET /plugins/group_activity/configs/{tenant_id}
READ GET /plugins/group_activity/events/{tenant_id}
READ GET /plugins/memory/events
READ GET /plugins/memory/extraction-jobs
READ GET /plugins/memory/extraction-jobs/stats
READ GET /plugins/memory/graph/entities
READ GET /plugins/memory/graph/episodes
READ GET /plugins/memory/graph/facts
READ GET /plugins/memory/graph/preview
READ GET /plugins/memory/group-graph
READ GET /plugins/memory/group-graph/evidence/{edge_id:path}
READ GET /plugins/memory/group-graph/history-dates
READ GET /plugins/memory/group-graph/window-stats
READ GET /plugins/memory/items
READ GET /plugins/memory/items/acceptance-legacy-audit
READ GET /plugins/memory/items/acceptance-stats
READ GET /plugins/memory/items/retrieve
READ GET /plugins/memory/profile-enrichment/candidates
READ GET /plugins/memory/profile-enrichment/candidates/{candidate_id}
READ GET /plugins/memory/profiles
READ GET /plugins/memory/profiles/{tenant_id}/{channel}/{source_key}/{user_id:path}
READ GET /plugins/memory/runtime-profile/{tenant_id}/{channel}/{source_key}/{session_id:path}
READ GET /plugins/memory/session-profiles
READ GET /plugins/memory/session-profiles/{tenant_id}/{channel}/{source_key}/{session_id:path}
READ GET /plugins/moderation/config/{tenant_id}/{session_id}
READ GET /plugins/moderation/events/{tenant_id}
READ GET /plugins/moderation/events/{tenant_id}/{session_id}
READ GET /plugins/moderation/keywords/{tenant_id}/{session_id}
READ GET /plugins/moderation/sessions/{tenant_id}
READ GET /plugins/persona_extract/jobs
READ GET /plugins/persona_extract/jobs/{job_id}
READ GET /plugins/persona_extract/profiles
READ GET /plugins/persona_extract/profiles/{profile_id}
READ GET /plugins/repeater/config/{tenant_id}/{session_id:path}
READ GET /plugins/repeater/events/{tenant_id}
READ GET /plugins/tibo_reset/deliveries
READ GET /plugins/tibo_reset/feed
READ GET /plugins/tibo_reset/stats
READ GET /plugins/tibo_reset/status
READ GET /plugins/wxbot/admin/agent-tools/audit
READ GET /plugins/wxbot/admin/agent-tools/catalog
READ GET /plugins/wxbot/admin/agent-tools/policy/{tenant_id}/{session_id:path}
READ GET /plugins/wxbot/admin/event-subscriptions
READ GET /plugins/wxbot/admin/group-members/settings/{session_id:path}
READ GET /plugins/wxbot/admin/images/{media_id}
READ GET /plugins/wxbot/admin/media-ready-events
READ GET /plugins/wxbot/admin/member-events
READ GET /plugins/wxbot/admin/reply-policy/aggregate
READ GET /plugins/wxbot/admin/reply-policy/global/{tenant_id}
READ GET /plugins/wxbot/admin/reply-policy/{tenant_id}/{session_id:path}
READ GET /plugins/wxbot/admin/reply-queue/messages
READ GET /plugins/wxbot/admin/reply-queue/stats
READ GET /plugins/wxbot/admin/reports/messages/{session_id:path}
READ GET /plugins/wxbot/admin/reports/preview/{session_id:path}
READ GET /plugins/wxbot/admin/reports/subscriptions
READ GET /plugins/wxbot/admin/roster/groups
READ GET /plugins/wxbot/admin/roster/groups/{session_id:path}/members
READ GET /plugins/wxbot/admin/sdk/debug/trigger-config
READ GET /plugins/wxbot/admin/sdk/queue/messages
READ GET /plugins/wxbot/admin/sdk/queue/stats
READ GET /plugins/wxbot/admin/self-review/jobs
READ GET /plugins/wxbot/admin/self-review/preview/{session_id:path}
READ GET /plugins/wxbot/admin/self-review/subscriptions
READ GET /plugins/wxbot/admin/session-state/{tenant_id}/{session_id:path}
READ GET /plugins/wxbot/admin/sessions
READ GET /plugins/wxbot/bridge/status
READ GET /v1/admin/auth/me
READ GET /v1/admin/auth/session
READ GET /v1/admin/dlq/messages
READ GET /v1/admin/dlq/messages/{entry_id}
READ GET /v1/admin/faqs
READ GET /v1/admin/kb/documents
READ GET /v1/admin/kb/documents/{doc_id}
READ GET /v1/admin/message-flows
READ GET /v1/admin/message-flows/effects
READ GET /v1/admin/message-flows/effects/summary
READ GET /v1/admin/message-flows/resolve
READ GET /v1/admin/message-flows/runtime
READ GET /v1/admin/message-flows/traces/{trace_id}
READ GET /v1/admin/message-flows/{name}/shadow-run
READ GET /v1/admin/plugins/events
READ GET /v1/admin/plugins/installed
READ GET /v1/admin/plugins/marketplace
READ GET /v1/admin/plugins/scopes
READ GET /v1/admin/plugins/summary
READ GET /v1/admin/plugins/{name}/config-schema
READ GET /v1/admin/plugins/{name}/runtime
READ GET /v1/admin/runtime/llm-config
READ GET /v1/admin/streams/messages
READ GET /v1/admin/streams/messages/{stream}/{entry_id}
READ GET /v1/admin/streams/recent-messages
READ GET /v1/admin/streams/summary
READ GET /v1/admin/tenants/{tenant_id}/capabilities
WRITE DELETE /v1/admin/auth/session
WRITE PATCH /plugins/memory/items/{item_id}
WRITE POST /plugins/commands/config/{tenant_id}
WRITE POST /plugins/credits/checkin/{tenant_id}/{session_id}/{user_id}
WRITE POST /plugins/credits/config/{tenant_id}/{session_id}
WRITE POST /plugins/group_activity/config/{tenant_id}/{session_id:path}
WRITE POST /plugins/memory/group-graph/edges/{edge_id:path}/acceptance-review
WRITE POST /plugins/memory/group-graph/extract-daily
WRITE POST /plugins/memory/group-graph/extract-window
WRITE POST /plugins/memory/group-graph/extract-window-catchup
WRITE POST /plugins/memory/items
WRITE POST /plugins/memory/items/acceptance-legacy-backfill
WRITE POST /plugins/memory/items/vector-smoke
WRITE POST /plugins/memory/items/{item_id}/acceptance-review
WRITE POST /plugins/memory/profile-enrichment/candidates
WRITE POST /plugins/memory/profile-enrichment/candidates/from-report
WRITE POST /plugins/memory/profile-enrichment/candidates/{candidate_id}/review
WRITE POST /plugins/memory/profiles
WRITE POST /plugins/memory/remember
WRITE POST /plugins/memory/search
WRITE POST /plugins/memory/session-profiles
WRITE POST /plugins/memory/update
WRITE POST /plugins/moderation/config/{tenant_id}/{session_id}
DANGER POST /plugins/moderation/keywords/{tenant_id}/{session_id}
WRITE POST /plugins/persona_extract/jobs
WRITE POST /plugins/persona_extract/jobs/{job_id}/cancel
WRITE POST /plugins/persona_extract/jobs/{job_id}/run
WRITE POST /plugins/persona_extract/profiles
WRITE POST /plugins/wxbot/admin/reply-policy/global/{tenant_id}
WRITE POST /plugins/wxbot/admin/reply-policy/{tenant_id}/{session_id:path}
WRITE POST /plugins/wxbot/admin/reports/subscriptions
WRITE POST /plugins/wxbot/admin/self-review/subscriptions
WRITE POST /plugins/wxbot/admin/session-state/{tenant_id}/{session_id:path}
WRITE POST /v1/admin/auth/session
WRITE POST /v1/admin/faqs
READ POST /v1/admin/faqs/test
WRITE POST /v1/admin/kb/documents
READ POST /v1/admin/kb/documents/search
WRITE POST /v1/admin/plugins/install/preview
WRITE POST /v1/admin/plugins/{name}/upgrade/preview
WRITE POST /v1/admin/runtime/restart-instructions
WRITE PUT /v1/admin/faqs/{faq_id}
WRITE PUT /v1/admin/kb/documents/{doc_id}
DANGER DELETE /plugins/memory/items/{item_id}
DANGER DELETE /plugins/moderation/keywords/{tenant_id}/{session_id}
DANGER DELETE /plugins/persona_extract/profiles/{profile_id}
DANGER DELETE /plugins/wxbot/admin/event-subscriptions/{subscription_id}
DANGER DELETE /plugins/wxbot/admin/reports/subscriptions/{session_id:path}
DANGER DELETE /plugins/wxbot/admin/self-review/subscriptions/{session_id:path}
DANGER DELETE /v1/admin/dlq/messages/{entry_id}
DANGER DELETE /v1/admin/faqs/{faq_id}
DANGER DELETE /v1/admin/kb/documents/{doc_id}
DANGER POST /plugins/amap/admin/config
DANGER POST /plugins/credits/adjust
DANGER POST /plugins/credits/transfer
DANGER POST /plugins/draw/tasks/recover-stale
DANGER POST /plugins/draw/tasks/{task_id}/resend-callback
DANGER POST /plugins/draw/tasks/{task_id}/retry
DANGER POST /plugins/group_activity/scheduler/run-once
DANGER POST /plugins/group_activity/trigger/{tenant_id}/{session_id:path}
DANGER POST /plugins/memory/backfill
DANGER POST /plugins/memory/extraction-jobs/maintenance
DANGER POST /plugins/memory/forget
DANGER POST /plugins/memory/governance/cleanup
DANGER POST /plugins/memory/graph/vector-rebuild
DANGER POST /plugins/memory/items/vector-rebuild
DANGER POST /plugins/persona_extract/profiles/apply-job
DANGER POST /plugins/repeater/config/{tenant_id}/{session_id:path}
DANGER POST /plugins/tibo_reset/poll/run-once
DANGER POST /plugins/wxbot/admin/agent-tools/policy/{tenant_id}/{session_id:path}
DANGER POST /plugins/wxbot/admin/event-subscriptions
DANGER POST /plugins/wxbot/admin/group-members/settings/{session_id:path}
DANGER POST /plugins/wxbot/admin/reply-policy/aggregate
DANGER POST /plugins/wxbot/admin/reply-queue/clear
DANGER POST /plugins/wxbot/admin/reports/send
DANGER POST /plugins/wxbot/admin/sdk/debug/trigger-config
DANGER POST /plugins/wxbot/admin/sdk/query/read
DANGER POST /plugins/wxbot/admin/sdk/queue/clear
DANGER POST /plugins/wxbot/admin/sdk/queue/messages/{row_id}/reconcile
DANGER POST /plugins/wxbot/admin/self-review/jobs/{job_id}/publish
DANGER POST /plugins/wxbot/admin/send
DANGER POST /plugins/wxbot/admin/send/batch
DANGER POST /plugins/wxbot/admin/send/envelope
DANGER POST /plugins/wxbot/admin/send/envelope/batch
DANGER POST /plugins/wxbot/admin/tenants/{tenant_id}/groups/{session_id:path}/simulate-inbound
DANGER POST /v1/admin/dlq/messages/{entry_id}/replay
DANGER POST /v1/admin/kb/reindex
DANGER POST /v1/admin/message-flows/effects/probe
DANGER POST /v1/admin/plugins/install
DANGER POST /v1/admin/plugins/{name}/disable
DANGER POST /v1/admin/plugins/{name}/enable
DANGER POST /v1/admin/plugins/{name}/scopes
DANGER POST /v1/admin/plugins/{name}/uninstall
DANGER POST /v1/admin/plugins/{name}/upgrade
DANGER POST /v1/admin/runtime/llm-config
"""


DEFAULT_ROUTE_PERMISSION_REGISTRY = RoutePermissionRegistry(
    _parse_manifest(_ROUTE_PERMISSION_MANIFEST)
)


def bind_default_route_permissions(app: FastAPI) -> None:
    DEFAULT_ROUTE_PERMISSION_REGISTRY.bind_and_validate(app)
