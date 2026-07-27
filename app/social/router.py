from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status

from app.admin.authorization import (
    AdminPermission,
    Principal,
    RoutePermission,
    build_admin_authorization_dependency,
)
from app.admin.mutation_ledger import MutationIdempotencyConflictError
from app.admin.route_permissions import declare_route_permission
from app.common.config import Settings
from app.social.contracts import (
    GroupParticipationPolicyDocument,
    GroupParticipationPolicyUpdate,
    MemberMemoryCorrection,
    MemberMemoryDeletionResult,
    MemberMemoryItemDocument,
    MemberMemoryPage,
    MemberPrivacyPolicyDocument,
    MemberPrivacyPolicyUpdate,
    ParticipationDecisionDocument,
    ParticipationEventPage,
    ParticipationPreviewRequest,
    PolicyVersionPage,
    ScopedParticipationControlDocument,
    ScopedParticipationControlUpdate,
    TenantMemberControlDocument,
    TenantMemberControlUpdate,
    VoiceProfilePreviewDocument,
    VoiceProfilePreviewRequest,
)
from app.social.participation import SocialParticipationService
from app.social.reply_style import NaturalReplyStyleGuard, text_fingerprint
from app.social.store import (
    HistoryVersionNotFoundError,
    IdempotencyConflictError,
    SocialPolicyStore,
    VersionConflictError,
    VoiceProfileScopeError,
)
from plugins.memory.store import (
    MemoryItemConflictError,
    MemoryItemProtectedError,
    MemoryMutationError,
    MemoryStore,
)


def build_social_admin_router(
    store: SocialPolicyStore,
    settings: Settings | None = None,
    *,
    participation_service: SocialParticipationService | None = None,
    style_guard: NaturalReplyStyleGuard | None = None,
    memory_store: MemoryStore | None = None,
    authorization_dependency: Callable[..., Any] | None = None,
) -> APIRouter:
    """Build the versioned group-behavior/privacy API.

    The caller owns lifecycle wiring for ``store``.  Production should pass a
    store backed by the application's migrated async session factory.
    """

    router = APIRouter(prefix="/v1/admin", tags=["social-participation"])
    service = participation_service or SocialParticipationService()
    preview_style_guard = style_guard or NaturalReplyStyleGuard()
    authorize = authorization_dependency or build_admin_authorization_dependency(settings)
    scoped_memory = memory_store or (MemoryStore(settings) if settings is not None else None)

    @router.get(
        "/social/release-control",
        response_model=ScopedParticipationControlDocument,
    )
    @declare_route_permission(
        RoutePermission(
            method="GET",
            path="/v1/admin/social/release-control",
            permission=AdminPermission.READ,
        )
    )
    async def get_release_control(
        response: Response,
        principal: Annotated[Principal, Depends(authorize)],
    ) -> ScopedParticipationControlDocument:
        _require_platform_scope(principal)
        document = await store.get_release_control()
        _set_version_headers(response, document.version)
        return document

    @router.put(
        "/social/release-control",
        response_model=ScopedParticipationControlDocument,
    )
    @declare_route_permission(
        RoutePermission(
            method="PUT",
            path="/v1/admin/social/release-control",
            permission=AdminPermission.DANGER,
        )
    )
    async def put_release_control(
        update: ScopedParticipationControlUpdate,
        request: Request,
        response: Response,
        principal: Annotated[Principal, Depends(authorize)],
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key", max_length=128)
        ] = None,
    ) -> ScopedParticipationControlDocument:
        _require_platform_scope(principal)
        try:
            result = await store.put_release_control(
                expected_version=_required_if_match(if_match),
                update=update,
                principal=principal,
                idempotency_key=_required_idempotency_key(idempotency_key),
                trace_id=_trace_id(request),
            )
        except (VersionConflictError, IdempotencyConflictError) as exc:
            raise _mutation_error(exc) from exc
        document = result.document
        assert isinstance(document, ScopedParticipationControlDocument)
        _set_version_headers(response, document.version)
        if result.replayed:
            response.headers["Idempotent-Replayed"] = "true"
        return document

    @router.get(
        "/tenants/{tenant}/participation-control",
        response_model=ScopedParticipationControlDocument,
    )
    @declare_route_permission(
        RoutePermission(
            method="GET",
            path="/v1/admin/tenants/{tenant}/participation-control",
            permission=AdminPermission.READ,
        )
    )
    async def get_tenant_control(
        tenant: str,
        response: Response,
        principal: Annotated[Principal, Depends(authorize)],
    ) -> ScopedParticipationControlDocument:
        _require_tenant(principal, tenant)
        document = await store.get_tenant_control(tenant)
        _set_version_headers(response, document.version)
        return document

    @router.put(
        "/tenants/{tenant}/participation-control",
        response_model=ScopedParticipationControlDocument,
    )
    @declare_route_permission(
        RoutePermission(
            method="PUT",
            path="/v1/admin/tenants/{tenant}/participation-control",
            permission=AdminPermission.DANGER,
        )
    )
    async def put_tenant_control(
        tenant: str,
        update: ScopedParticipationControlUpdate,
        request: Request,
        response: Response,
        principal: Annotated[Principal, Depends(authorize)],
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key", max_length=128)
        ] = None,
    ) -> ScopedParticipationControlDocument:
        _require_tenant(principal, tenant)
        try:
            result = await store.put_tenant_control(
                tenant_id=tenant,
                expected_version=_required_if_match(if_match),
                update=update,
                principal=principal,
                idempotency_key=_required_idempotency_key(idempotency_key),
                trace_id=_trace_id(request),
            )
        except (VersionConflictError, IdempotencyConflictError) as exc:
            raise _mutation_error(exc) from exc
        document = result.document
        assert isinstance(document, ScopedParticipationControlDocument)
        _set_version_headers(response, document.version)
        if result.replayed:
            response.headers["Idempotent-Replayed"] = "true"
        return document

    @router.get(
        "/tenants/{tenant}/members/{user}/control",
        response_model=TenantMemberControlDocument,
    )
    @declare_route_permission(
        RoutePermission(
            method="GET",
            path="/v1/admin/tenants/{tenant}/members/{user}/control",
            permission=AdminPermission.READ,
        )
    )
    async def get_tenant_member_control(
        tenant: str,
        user: str,
        response: Response,
        principal: Annotated[Principal, Depends(authorize)],
    ) -> TenantMemberControlDocument:
        _require_tenant(principal, tenant)
        document = await store.get_tenant_member_control(tenant, user)
        _set_version_headers(response, document.version)
        return document

    @router.put(
        "/tenants/{tenant}/members/{user}/control",
        response_model=TenantMemberControlDocument,
    )
    @declare_route_permission(
        RoutePermission(
            method="PUT",
            path="/v1/admin/tenants/{tenant}/members/{user}/control",
            permission=AdminPermission.DANGER,
        )
    )
    async def put_tenant_member_control(
        tenant: str,
        user: str,
        update: TenantMemberControlUpdate,
        request: Request,
        response: Response,
        principal: Annotated[Principal, Depends(authorize)],
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key", max_length=128)
        ] = None,
    ) -> TenantMemberControlDocument:
        _require_tenant(principal, tenant)
        try:
            result = await store.put_tenant_member_control(
                tenant_id=tenant,
                user_id=user,
                expected_version=_required_if_match(if_match),
                update=update,
                principal=principal,
                idempotency_key=_required_idempotency_key(idempotency_key),
                trace_id=_trace_id(request),
            )
        except (VersionConflictError, IdempotencyConflictError) as exc:
            raise _mutation_error(exc) from exc
        document = result.document
        assert isinstance(document, TenantMemberControlDocument)
        _set_version_headers(response, document.version)
        if result.replayed:
            response.headers["Idempotent-Replayed"] = "true"
        return document

    @router.get(
        "/tenants/{tenant}/groups/{session}/participation-policy",
        response_model=GroupParticipationPolicyDocument,
    )
    @declare_route_permission(
        RoutePermission(
            method="GET",
            path="/v1/admin/tenants/{tenant}/groups/{session}/participation-policy",
            permission=AdminPermission.READ,
        )
    )
    async def get_participation_policy(
        tenant: str,
        session: str,
        response: Response,
        principal: Annotated[Principal, Depends(authorize)],
    ) -> GroupParticipationPolicyDocument:
        _require_tenant(principal, tenant)
        document = await store.get_group_policy(tenant, session)
        _set_version_headers(response, document.version)
        return document

    @router.get(
        "/tenants/{tenant}/groups/{session}/participation-policy/history",
        response_model=PolicyVersionPage,
    )
    @declare_route_permission(
        RoutePermission(
            method="GET",
            path="/v1/admin/tenants/{tenant}/groups/{session}/participation-policy/history",
            permission=AdminPermission.READ,
        )
    )
    async def get_participation_policy_history(
        tenant: str,
        session: str,
        principal: Annotated[Principal, Depends(authorize)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: str | None = None,
    ) -> PolicyVersionPage:
        _require_tenant(principal, tenant)
        try:
            return await store.list_group_policy_history(
                tenant_id=tenant,
                session_id=session,
                limit=limit,
                cursor=cursor,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid_cursor") from exc

    @router.get(
        "/tenants/{tenant}/groups/{session}/voice-profile/history",
        response_model=PolicyVersionPage,
    )
    @declare_route_permission(
        RoutePermission(
            method="GET",
            path="/v1/admin/tenants/{tenant}/groups/{session}/voice-profile/history",
            permission=AdminPermission.READ,
        )
    )
    async def get_voice_profile_history(
        tenant: str,
        session: str,
        principal: Annotated[Principal, Depends(authorize)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: str | None = None,
    ) -> PolicyVersionPage:
        _require_tenant(principal, tenant)
        try:
            return await store.list_voice_profile_history(
                tenant_id=tenant,
                session_id=session,
                limit=limit,
                cursor=cursor,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid_cursor") from exc

    @router.post(
        "/tenants/{tenant}/groups/{session}/voice-profile/preview",
        response_model=VoiceProfilePreviewDocument,
    )
    @declare_route_permission(
        RoutePermission(
            method="POST",
            path="/v1/admin/tenants/{tenant}/groups/{session}/voice-profile/preview",
            permission=AdminPermission.READ,
        )
    )
    async def preview_voice_profile(
        tenant: str,
        session: str,
        preview: VoiceProfilePreviewRequest,
        response: Response,
        principal: Annotated[Principal, Depends(authorize)],
    ) -> VoiceProfilePreviewDocument:
        """Apply the production style guard without sending or persisting text."""

        _require_tenant(principal, tenant)
        response.headers["Cache-Control"] = "no-store"
        profile = preview.voice_profile
        runtime_reason = profile.runtime_reason(
            session_id=session,
            now=datetime.now(UTC),
        )
        applied = runtime_reason == "voice_profile_active"
        style_result = preview_style_guard.apply(
            preview.reply_text,
            deterministic_key=text_fingerprint(
                "\0".join(
                    (
                        tenant,
                        session,
                        profile.profile_id,
                        str(profile.version),
                        preview.reply_text,
                        preview.source_text,
                    )
                )
            ),
            eligible=applied,
            source_text=preview.source_text,
            explicitly_detailed=preview.explicitly_detailed,
            voice_profile=profile.runtime_style_payload(),
        )
        return VoiceProfilePreviewDocument(
            profile_id=profile.profile_id,
            version=profile.version,
            runtime_reason=runtime_reason,
            applied=applied,
            output_text=style_result.text,
            mode=style_result.mode,
            transformed=style_result.transformed,
            emoji=style_result.emoji,
            catchphrase=style_result.catchphrase,
            identity_disclosed=style_result.identity_disclosed,
            reason_codes=list(style_result.reason_codes),
        )

    @router.put(
        "/tenants/{tenant}/groups/{session}/participation-policy",
        response_model=GroupParticipationPolicyDocument,
    )
    @declare_route_permission(
        RoutePermission(
            method="PUT",
            path="/v1/admin/tenants/{tenant}/groups/{session}/participation-policy",
            permission=AdminPermission.WRITE,
        )
    )
    async def put_participation_policy(
        tenant: str,
        session: str,
        update: GroupParticipationPolicyUpdate,
        request: Request,
        response: Response,
        principal: Annotated[Principal, Depends(authorize)],
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", max_length=128),
        ] = None,
    ) -> GroupParticipationPolicyDocument:
        _require_tenant(principal, tenant)
        expected_version = _required_if_match(if_match)
        try:
            result = await store.put_group_policy(
                tenant_id=tenant,
                session_id=session,
                expected_version=expected_version,
                update=update,
                principal=principal,
                idempotency_key=_normalize_idempotency_key(idempotency_key),
                trace_id=_trace_id(request),
            )
        except (
            VersionConflictError,
            IdempotencyConflictError,
            HistoryVersionNotFoundError,
            VoiceProfileScopeError,
        ) as exc:
            raise _mutation_error(exc) from exc
        document = result.document
        assert isinstance(document, GroupParticipationPolicyDocument)
        _set_version_headers(response, document.version)
        if result.replayed:
            response.headers["Idempotent-Replayed"] = "true"
        return document

    @router.post(
        "/tenants/{tenant}/groups/{session}/participation-preview",
        response_model=ParticipationDecisionDocument,
    )
    @declare_route_permission(
        RoutePermission(
            method="POST",
            path="/v1/admin/tenants/{tenant}/groups/{session}/participation-preview",
            permission=AdminPermission.WRITE,
        )
    )
    async def preview_participation(
        tenant: str,
        session: str,
        preview: ParticipationPreviewRequest,
        request: Request,
        response: Response,
        principal: Annotated[Principal, Depends(authorize)],
    ) -> ParticipationDecisionDocument:
        _require_tenant(principal, tenant)
        # The request is already a strict structured contract, but its result
        # still reflects a transient operator-authored scenario.
        response.headers["Cache-Control"] = "no-store"
        document = await store.get_group_policy(tenant, session)
        decision = service.decide(
            preview.to_context(tenant_id=tenant, session_id=session),
            document.policy.to_domain(enabled=document.effective_enabled),
        )
        event = await store.record_participation_event(
            tenant_id=tenant,
            session_id=session,
            policy_version=document.version,
            event_kind="preview",
            decision=decision,
            signal_summary=preview.event_summary(),
            trace_id=_trace_id(request),
        )
        return ParticipationDecisionDocument(
            event_id=event.event_id,
            tenant_id=tenant,
            session_id=session,
            policy_version=document.version,
            status=decision.status.value,
            score=decision.score,
            reason_codes=list(decision.reason_codes),
            not_before=decision.not_before,
            expires_at=decision.expires_at,
            mention_sender=decision.mention_sender,
        )

    @router.get(
        "/tenants/{tenant}/groups/{session}/participation-events",
        response_model=ParticipationEventPage,
    )
    @declare_route_permission(
        RoutePermission(
            method="GET",
            path="/v1/admin/tenants/{tenant}/groups/{session}/participation-events",
            permission=AdminPermission.READ,
        )
    )
    async def get_participation_events(
        tenant: str,
        session: str,
        principal: Annotated[Principal, Depends(authorize)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        before: datetime | None = None,
        cursor: str | None = None,
        status_filter: Annotated[
            Literal["must_reply", "may_reply", "observe_only", "defer", "cancel"] | None,
            Query(alias="status"),
        ] = None,
        source: Literal["preview", "runtime"] | None = None,
        version: Annotated[int | None, Query(ge=0)] = None,
        reason: Annotated[
            str | None,
            Query(max_length=96, pattern=r"^[a-z0-9_:+.\-]+$"),
        ] = None,
        runtime_stage: Annotated[
            str | None,
            Query(max_length=32, pattern=r"^[a-z0-9_.\-]+$"),
        ] = None,
        delivery_stage: Annotated[
            str | None,
            Query(max_length=32, pattern=r"^[a-z0-9_.\-]+$"),
        ] = None,
    ) -> ParticipationEventPage:
        _require_tenant(principal, tenant)
        try:
            return await store.list_participation_events(
                tenant_id=tenant,
                session_id=session,
                limit=limit,
                before=before,
                cursor=cursor,
                status=status_filter,
                source=source,
                version=version,
                reason=reason,
                runtime_stage=runtime_stage,
                delivery_stage=delivery_stage,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid_cursor") from exc

    @router.get(
        "/tenants/{tenant}/groups/{session}/members/{user}/privacy-policy",
        response_model=MemberPrivacyPolicyDocument,
    )
    @declare_route_permission(
        RoutePermission(
            method="GET",
            path=("/v1/admin/tenants/{tenant}/groups/{session}/members/{user}/privacy-policy"),
            permission=AdminPermission.READ,
        )
    )
    async def get_member_privacy_policy(
        tenant: str,
        session: str,
        user: str,
        response: Response,
        principal: Annotated[Principal, Depends(authorize)],
    ) -> MemberPrivacyPolicyDocument:
        _require_tenant(principal, tenant)
        document = await store.get_member_policy(tenant, session, user)
        _set_version_headers(response, document.version)
        return document

    @router.get(
        "/tenants/{tenant}/groups/{session}/members/{user}/privacy-policy/history",
        response_model=PolicyVersionPage,
    )
    @declare_route_permission(
        RoutePermission(
            method="GET",
            path=(
                "/v1/admin/tenants/{tenant}/groups/{session}/members/{user}/privacy-policy/history"
            ),
            permission=AdminPermission.READ,
        )
    )
    async def get_member_privacy_policy_history(
        tenant: str,
        session: str,
        user: str,
        principal: Annotated[Principal, Depends(authorize)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: str | None = None,
    ) -> PolicyVersionPage:
        _require_tenant(principal, tenant)
        try:
            return await store.list_member_policy_history(
                tenant_id=tenant,
                session_id=session,
                user_id=user,
                limit=limit,
                cursor=cursor,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid_cursor") from exc

    @router.put(
        "/tenants/{tenant}/groups/{session}/members/{user}/privacy-policy",
        response_model=MemberPrivacyPolicyDocument,
    )
    @declare_route_permission(
        RoutePermission(
            method="PUT",
            path=("/v1/admin/tenants/{tenant}/groups/{session}/members/{user}/privacy-policy"),
            permission=AdminPermission.WRITE,
        )
    )
    async def put_member_privacy_policy(
        tenant: str,
        session: str,
        user: str,
        update: MemberPrivacyPolicyUpdate,
        request: Request,
        response: Response,
        principal: Annotated[Principal, Depends(authorize)],
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", max_length=128),
        ] = None,
    ) -> MemberPrivacyPolicyDocument:
        _require_tenant(principal, tenant)
        expected_version = _required_if_match(if_match)
        try:
            result = await store.put_member_policy(
                tenant_id=tenant,
                session_id=session,
                user_id=user,
                expected_version=expected_version,
                update=update,
                principal=principal,
                idempotency_key=_normalize_idempotency_key(idempotency_key),
                trace_id=_trace_id(request),
            )
        except (VersionConflictError, IdempotencyConflictError, HistoryVersionNotFoundError) as exc:
            raise _mutation_error(exc) from exc
        document = result.document
        assert isinstance(document, MemberPrivacyPolicyDocument)
        _set_version_headers(response, document.version)
        if result.replayed:
            response.headers["Idempotent-Replayed"] = "true"
        return document

    @router.get(
        "/tenants/{tenant}/groups/{session}/members/{user}/memory-items",
        response_model=MemberMemoryPage,
    )
    @declare_route_permission(
        RoutePermission(
            method="GET",
            path=("/v1/admin/tenants/{tenant}/groups/{session}/members/{user}/memory-items"),
            permission=AdminPermission.READ,
        )
    )
    async def list_member_memory_items(
        tenant: str,
        session: str,
        user: str,
        principal: Annotated[Principal, Depends(authorize)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: str | None = None,
    ) -> MemberMemoryPage:
        _require_tenant(principal, tenant)
        memory = _required_memory_store(scoped_memory)
        policy = (await store.get_member_policy(tenant, session, user)).policy
        try:
            page = await memory.list_group_member_memory_items(
                tenant_id=tenant,
                session_id=session,
                user_id=user,
                policy=policy,
                limit=limit,
                cursor=cursor,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid_cursor") from exc
        return MemberMemoryPage(
            items=[_member_memory_document(item) for item in page.get("items", [])],
            next_cursor=page.get("next_cursor"),
        )

    @router.patch(
        "/tenants/{tenant}/groups/{session}/members/{user}/memory-items/{item_id}",
        response_model=MemberMemoryItemDocument,
    )
    @declare_route_permission(
        RoutePermission(
            method="PATCH",
            path=(
                "/v1/admin/tenants/{tenant}/groups/{session}/members/{user}/memory-items/{item_id}"
            ),
            permission=AdminPermission.WRITE,
        )
    )
    async def correct_member_memory_item(
        tenant: str,
        session: str,
        user: str,
        item_id: int,
        correction: MemberMemoryCorrection,
        request: Request,
        response: Response,
        principal: Annotated[Principal, Depends(authorize)],
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key", max_length=128)
        ] = None,
    ) -> MemberMemoryItemDocument:
        _require_tenant(principal, tenant)
        memory = _required_memory_store(scoped_memory)
        item_etag = _required_item_etag(if_match)
        operation_key = _required_idempotency_key(idempotency_key)
        policy = (await store.get_member_policy(tenant, session, user)).policy
        try:
            outcome = await memory.correct_group_member_memory_item_idempotent(
                item_id,
                tenant_id=tenant,
                session_id=session,
                user_id=user,
                policy=policy,
                expected_etag=item_etag,
                content=correction.content,
                reason=correction.reason,
                idempotency_key=operation_key,
                actor=principal.subject,
                actor_kind=principal.auth_kind,
                roles=principal.roles,
                trace_id=_trace_id(request),
            )
        except MutationIdempotencyConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "idempotency_key_conflict"},
            ) from exc
        except MemoryItemConflictError as exc:
            raise HTTPException(status_code=409, detail="memory_version_conflict") from exc
        except MemoryMutationError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        response.status_code = outcome.status_code
        if outcome.replayed:
            response.headers["Idempotent-Replayed"] = "true"
        document = _member_memory_document(outcome.response)
        response.headers["ETag"] = document.etag
        response.headers["Cache-Control"] = "no-store"
        return document

    @router.delete(
        "/tenants/{tenant}/groups/{session}/members/{user}/memory-items/{item_id}",
        response_model=MemberMemoryDeletionResult,
    )
    @declare_route_permission(
        RoutePermission(
            method="DELETE",
            path=(
                "/v1/admin/tenants/{tenant}/groups/{session}/members/{user}/memory-items/{item_id}"
            ),
            permission=AdminPermission.DANGER,
        )
    )
    async def delete_member_memory_item(
        tenant: str,
        session: str,
        user: str,
        item_id: int,
        request: Request,
        response: Response,
        principal: Annotated[Principal, Depends(authorize)],
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key", max_length=128)
        ] = None,
        allow_pinned: bool = False,
    ) -> MemberMemoryDeletionResult:
        _require_tenant(principal, tenant)
        memory = _required_memory_store(scoped_memory)
        item_etag = _required_item_etag(if_match)
        operation_key = _required_idempotency_key(idempotency_key)
        policy = (await store.get_member_policy(tenant, session, user)).policy
        try:
            outcome = await memory.delete_group_member_memory_item_idempotent(
                item_id,
                tenant_id=tenant,
                session_id=session,
                user_id=user,
                policy=policy,
                expected_etag=item_etag,
                allow_pinned=allow_pinned,
                idempotency_key=operation_key,
                actor=principal.subject,
                actor_kind=principal.auth_kind,
                roles=principal.roles,
                trace_id=_trace_id(request),
            )
        except MutationIdempotencyConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "idempotency_key_conflict"},
            ) from exc
        except MemoryItemConflictError as exc:
            raise HTTPException(status_code=409, detail="memory_version_conflict") from exc
        except MemoryItemProtectedError as exc:
            raise HTTPException(
                status_code=409, detail="pinned_memory_confirmation_required"
            ) from exc
        except MemoryMutationError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        response.status_code = outcome.status_code
        if outcome.replayed:
            response.headers["Idempotent-Replayed"] = "true"
        return MemberMemoryDeletionResult(**outcome.response)

    return router


def etag_for_version(version: int) -> str:
    return f'"{version}"'


def _set_version_headers(response: Response, version: int) -> None:
    response.headers["ETag"] = etag_for_version(version)
    response.headers["Cache-Control"] = "no-store"


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
    if not normalized.isdigit():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid_if_match",
        )
    return int(normalized)


def _normalize_idempotency_key(value: str | None) -> str:
    normalized = str(value or "").strip()
    if value is not None and not normalized:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid_idempotency_key",
        )
    return normalized


def _required_idempotency_key(value: str | None) -> str:
    normalized = _normalize_idempotency_key(value)
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="idempotency_key_required",
        )
    return normalized


def _required_item_etag(value: str | None) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="if_match_required",
        )
    if not normalized.startswith('"memory-') or not normalized.endswith('"'):
        raise HTTPException(status_code=400, detail="invalid_if_match")
    return normalized


def _require_tenant(principal: Principal, tenant_id: str) -> None:
    if not principal.allows_tenant(tenant_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="tenant_scope_forbidden",
        )


def _require_platform_scope(principal: Principal) -> None:
    if not principal.has_global_tenant_scope:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="platform_scope_forbidden",
        )


def _required_memory_store(memory_store: MemoryStore | None) -> MemoryStore:
    if memory_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="member_memory_service_unavailable",
        )
    return memory_store


def _member_memory_document(item: dict[str, Any]) -> MemberMemoryItemDocument:
    return MemberMemoryItemDocument(
        item_id=int(item["id"]),
        content=str(item.get("content") or ""),
        memory_type=str(item.get("memory_type") or "note"),
        scope_type=str(item.get("scope_type") or "identity"),
        audience_scope=str(item.get("audience_scope") or "private"),
        status=str(item.get("status") or "active"),
        sensitivity_category=str(item.get("sensitivity_category") or "normal"),
        pinned=bool(item.get("pinned")),
        expires_at=item.get("expires_at"),
        updated_at=item["updated_at"],
        etag=str(item["etag"]),
    )


def _memory_audit_metadata(item: dict[str, Any]) -> dict[str, Any]:
    # Never persist content, original text, normalized keys, or provenance.
    return {
        "item_id": int(item["id"]),
        "status": str(item.get("status") or ""),
        "scope_type": str(item.get("scope_type") or ""),
        "audience_scope": str(item.get("audience_scope") or ""),
        "sensitivity_category": str(item.get("sensitivity_category") or ""),
    }


def _trace_id(request: Request) -> str:
    return str(
        request.headers.get("X-Trace-ID") or request.headers.get("X-Request-ID") or ""
    ).strip()[:128]


def _mutation_error(exc: Exception) -> HTTPException:
    if isinstance(exc, VersionConflictError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "version_conflict",
                "expected_version": exc.expected,
                "current_version": exc.current,
            },
        )
    if isinstance(exc, IdempotencyConflictError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "idempotency_conflict"},
        )
    if isinstance(exc, HistoryVersionNotFoundError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "rollback_version_not_found",
                "version": exc.version,
            },
        )
    if isinstance(exc, VoiceProfileScopeError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": VoiceProfileScopeError.code},
        )
    return HTTPException(status_code=500, detail="social_policy_mutation_failed")
