"""Report and self-review admin route section for the wxbot facade."""

from __future__ import annotations

import hashlib
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from app.admin.audit import set_admin_audit_context
from plugins.wxbot.reports import (
    normalize_report_send_text,
    resolve_preview_period,
    should_defer_report_job_retry,
)
from plugins.wxbot.router import (
    WxbotReportSendRequest,
    WxbotReportSubscriptionRequest,
    WxbotSelfReviewSubscriptionRequest,
    _AdminEffectOutcome,
    _execute_admin_mutation,
    _mutation_audit_summary,
    _observe_admin_resource,
    _report_subscription_config,
    _request_trace_id,
    _require_admin,
    _require_default_tenant_admin,
    _require_verified_group,
    _required_idempotency_key,
    _required_version_if_match,
    _self_review_subscription_config,
    _set_no_store_etag,
    _version_etag,
    logger,
)
from plugins.wxbot.self_review import (
    SelfReviewJobNotFound,
    SelfReviewJobNotReady,
    SelfReviewPublishFailed,
    resolve_self_review_preview_period,
)


def register_report_routes(
    router: APIRouter,
    *,
    store: Any,
    bridge: Any,
    scheduler: Any,
    report_service: Any,
    self_review_service: Any,
) -> None:
    @router.get("/admin/reports/subscriptions")
    async def list_report_subscriptions(request: Request, response: Response):
        tenant_id = _require_default_tenant_admin(store, request)
        subscriptions = await store.list_report_subscriptions(tenant_id)
        version = await _observe_admin_resource(
            store,
            tenant_id,
            "report-subscriptions",
            resource_kind="report_subscriptions",
            state_payload=_report_subscription_config(subscriptions),
        )
        _set_no_store_etag(response, _version_etag(version))
        return {
            "ok": True,
            "subscriptions": subscriptions,
            "version": version,
        }

    @router.post("/admin/reports/subscriptions")
    async def upsert_report_subscription(
        body: WxbotReportSubscriptionRequest,
        request: Request,
        response: Response,
    ):
        tenant_id = _require_default_tenant_admin(store, request)
        expected_version = _required_version_if_match(request)
        if body.daily_hour < 0 or body.daily_hour > 23:
            raise HTTPException(400, "daily_hour must be between 0 and 23")
        if body.weekly_day < 1 or body.weekly_day > 7:
            raise HTTPException(400, "weekly_day must be between 1 and 7")
        if body.weekly_hour < 0 or body.weekly_hour > 23:
            raise HTTPException(400, "weekly_hour must be between 0 and 23")
        if body.monthly_day < 1 or body.monthly_day > 31:
            raise HTTPException(400, "monthly_day must be between 1 and 31")
        verified_group = await _require_verified_group(
            store,
            bridge,
            tenant_id=tenant_id,
            session_id=body.session_id,
        )
        canonical_session_name = str(
            verified_group.get("session_name")
            or body.session_name
            or body.session_id
        ).strip()
        before_items = await store.list_report_subscriptions(tenant_id)
        before_config = _report_subscription_config(before_items)
        await _observe_admin_resource(
            store,
            tenant_id,
            "report-subscriptions",
            resource_kind="report_subscriptions",
            state_payload=before_config,
        )
        intent = body.model_dump()

        async def effect() -> _AdminEffectOutcome:
            subscription = await store.upsert_report_subscription(
                tenant_id,
                session_id=body.session_id,
                session_name=canonical_session_name,
                daily_enabled=body.daily_enabled,
                weekly_enabled=body.weekly_enabled,
                monthly_enabled=body.monthly_enabled,
                daily_hour=body.daily_hour,
                weekly_day=body.weekly_day,
                weekly_hour=body.weekly_hour,
                monthly_day=body.monthly_day,
                tz=body.tz,
            )
            if scheduler is not None and hasattr(scheduler, "notify_report_scheduler"):
                scheduler.notify_report_scheduler()
            after_items = await store.list_report_subscriptions(tenant_id)
            return _AdminEffectOutcome(
                {"ok": True, "subscription": subscription},
                _report_subscription_config(after_items),
            )

        result, version, _replayed = await _execute_admin_mutation(
            store,
            request,
            tenant_id=tenant_id,
            operation="report_subscription_upsert",
            resource_key="report-subscriptions",
            request_payload=intent,
            expected_version=expected_version,
            effect=effect,
        )
        if version is not None:
            _set_no_store_etag(response, _version_etag(version))
        set_admin_audit_context(
            request,
            target_type="wxbot_report_subscription",
            tenant_id=tenant_id,
            session_id=body.session_id,
            before_state=_mutation_audit_summary(
                operation="upsert",
                affected_count=len(before_items),
            ),
            after_state=_mutation_audit_summary(
                operation="upsert",
                affected_count=max(1, len(before_items)),
                enabled=bool(
                    body.daily_enabled or body.weekly_enabled or body.monthly_enabled
                ),
            ),
            policy_version=int(version or 0),
            trace_id=_request_trace_id(request),
            reason="conditional_report_subscription_update",
        )
        return result

    @router.delete("/admin/reports/subscriptions/{session_id:path}")
    async def delete_report_subscription(
        session_id: str,
        request: Request,
        response: Response,
    ):
        tenant_id = _require_default_tenant_admin(store, request)
        expected_version = _required_version_if_match(request)
        await _require_verified_group(
            store,
            bridge,
            tenant_id=tenant_id,
            session_id=session_id,
        )
        before_items = await store.list_report_subscriptions(tenant_id)
        before_config = _report_subscription_config(before_items)
        await _observe_admin_resource(
            store,
            tenant_id,
            "report-subscriptions",
            resource_kind="report_subscriptions",
            state_payload=before_config,
        )

        async def effect() -> _AdminEffectOutcome:
            deleted = await store.delete_report_subscription(tenant_id, session_id)
            if scheduler is not None and hasattr(scheduler, "notify_report_scheduler"):
                scheduler.notify_report_scheduler()
            after_items = await store.list_report_subscriptions(tenant_id)
            return _AdminEffectOutcome(
                {"ok": True, "deleted": session_id, "existed": bool(deleted)},
                _report_subscription_config(after_items),
            )

        result, version, _replayed = await _execute_admin_mutation(
            store,
            request,
            tenant_id=tenant_id,
            operation="report_subscription_delete",
            resource_key="report-subscriptions",
            request_payload={"session_id": session_id},
            expected_version=expected_version,
            recovery_response={"ok": True, "deleted": session_id},
            effect=effect,
        )
        if version is not None:
            _set_no_store_etag(response, _version_etag(version))
        set_admin_audit_context(
            request,
            target_type="wxbot_report_subscription",
            tenant_id=tenant_id,
            session_id=session_id,
            before_state=_mutation_audit_summary(
                operation="delete",
                affected_count=len(before_items),
            ),
            after_state=_mutation_audit_summary(
                operation="delete",
                affected_count=max(0, len(before_items) - 1),
            ),
            policy_version=int(version or 0),
            trace_id=_request_trace_id(request),
            reason="conditional_report_subscription_delete",
        )
        return result

    @router.get("/admin/reports/preview/{session_id:path}")
    async def preview_report(
        session_id: str,
        request: Request,
        report_type: str = "daily",
        session_name: str = "",
        date: str = "",
        year_month: str = "",
    ):
        tenant_id = _require_default_tenant_admin(store, request)
        period_key, period_label = resolve_preview_period(
            report_type,
            date=date,
            year_month=year_month,
        )
        job = await store.get_or_create_report_job(
            tenant_id=tenant_id,
            session_id=session_id,
            session_name=session_name or session_id,
            report_type=report_type,
            period_key=period_key,
            period_label=period_label,
        )
        if str(job.get("status") or "") == "completed":
            payload = dict(job.get("report_payload") or {})
            payload.update(
                {
                    "job_id": job["id"],
                    "status": "completed",
                    "current_stage": "completed",
                    "session_id": session_id,
                    "session_name": session_name or job.get("session_name") or session_id,
                    "report_type": report_type,
                    "period": payload.get("period") or period_label,
                    "report": str(job.get("result_text") or ""),
                    "cached": True,
                }
            )
            return payload

        if str(job.get("status") or "") == "running":
            if scheduler is not None and hasattr(scheduler, "schedule_background"):
                await scheduler.schedule_background(
                    f"report-job-{job['id']}",
                    lambda: report_service.run_report_job(int(job["id"])),
                )
            return {
                "job_id": int(job["id"]),
                "status": "running",
                "current_stage": str(job.get("current_stage") or "queued"),
                "session_id": session_id,
                "session_name": session_name or job.get("session_name") or session_id,
                "report_type": report_type,
                "period": period_label,
                "cached": False,
            }

        if should_defer_report_job_retry(job):
            payload = dict(job.get("report_payload") or {})
            return {
                "job_id": int(job["id"]),
                "status": "failed",
                "current_stage": str(
                    job.get("current_stage") or payload.get("last_failed_stage") or "queued"
                ),
                "session_id": session_id,
                "session_name": session_name or job.get("session_name") or session_id,
                "report_type": report_type,
                "period": period_label,
                "error": str(job.get("error") or payload.get("last_error") or ""),
                "retry_after": str(payload.get("retry_after") or ""),
                "cached": False,
            }

        reset = await store.update_report_job(
            int(job["id"]),
            status="pending",
            current_stage="queued",
            msg_count=0,
            result_text="",
            report_payload={},
            error="",
            expected_run_attempt=int(job.get("run_attempt") or 0),
            expected_status=str(job.get("status") or "pending"),
        )
        if not reset:
            raise HTTPException(409, "report_job_state_changed")
        if scheduler is not None and hasattr(scheduler, "schedule_background"):
            await scheduler.schedule_background(
                f"report-job-{job['id']}",
                lambda: report_service.run_report_job(int(job["id"])),
            )
        refreshed = await store.get_report_job(int(job["id"]))
        if str((refreshed or {}).get("status") or "") == "completed":
            payload = dict((refreshed or {}).get("report_payload") or {})
            payload.update(
                {
                    "job_id": int(job["id"]),
                    "status": "completed",
                    "current_stage": "completed",
                    "session_id": session_id,
                    "session_name": session_name or job.get("session_name") or session_id,
                    "report_type": report_type,
                    "period": payload.get("period") or period_label,
                    "report": str((refreshed or {}).get("result_text") or ""),
                    "cached": False,
                }
            )
            return payload
        return {
            "job_id": int(job["id"]),
            "status": str((refreshed or {}).get("status") or "pending"),
            "current_stage": str((refreshed or {}).get("current_stage") or "queued"),
            "session_id": session_id,
            "session_name": session_name or job.get("session_name") or session_id,
            "report_type": report_type,
            "period": period_label,
            "cached": False,
        }

    @router.get("/admin/reports/messages/{session_id:path}")
    async def report_messages(
        session_id: str,
        request: Request,
        report_type: str = "daily",
        session_name: str = "",
        date: str = "",
        year_month: str = "",
    ):
        _require_admin(store, request)
        period_key, _ = resolve_preview_period(
            report_type,
            date=date,
            year_month=year_month,
        )
        if str(report_type or "daily").strip().lower() != "daily":
            raise HTTPException(400, "raw report messages are only available for daily reports")
        return await report_service.fetch_report_messages_payload(
            session_id,
            session_name=session_name,
            report_type=report_type,
            date=period_key if report_type == "daily" else "",
            year_month=period_key if report_type == "monthly" else "",
        )

    @router.post("/admin/reports/send")
    async def send_report(body: WxbotReportSendRequest, request: Request):
        tenant_id = _require_default_tenant_admin(store, request)
        session_id = body.session_id.strip()
        verified_group = await _require_verified_group(
            store,
            bridge,
            tenant_id=tenant_id,
            session_id=session_id,
        )
        session_name = str(
            verified_group.get("session_name")
            or body.session_name
            or session_id
        ).strip()
        report_type = str(body.report_type or "daily").strip().lower()
        period_key, period_label = resolve_preview_period(
            report_type,
            date=body.date,
            year_month=body.year_month,
        )
        job = await store.get_report_job_by_scope(
            tenant_id=tenant_id,
            session_id=session_id,
            report_type=report_type,
            period_key=period_key,
        )
        if not job or str(job.get("status") or "") != "completed":
            raise HTTPException(409, f"{report_type} report for {period_label} is not ready")
        text = normalize_report_send_text(
            report_type,
            str(job.get("result_text") or ""),
            footer=str(
                getattr(
                    store.settings,
                    "wxbot_daily_report_footer",
                    "",
                )
                or ""
            ),
        )
        response_payload = {
            "session_id": session_id,
            "session_name": session_name,
            "report_type": report_type,
            "period": period_label,
            "job_id": job["id"],
        }

        async def effect() -> _AdminEffectOutcome:
            if not await report_service.scope_execution_allowed(
                tenant_id,
                session_id,
            ):
                raise HTTPException(409, "scope_execution_denied")
            delivery_attempt = await store.try_start_report_delivery(int(job["id"]))
            if delivery_attempt is None:
                raise HTTPException(409, "report_delivery_already_started")
            delivery_attempt = int(delivery_attempt)
            if not await report_service.scope_execution_allowed(
                tenant_id,
                session_id,
            ):
                await store.release_report_delivery(
                    int(job["id"]),
                    delivery_attempt=delivery_attempt,
                    reason="scope_execution_denied",
                )
                raise HTTPException(409, "scope_execution_denied")
            try:
                sdk_result = await report_service.sdk_request(
                    "POST",
                    "/send",
                    json_body={
                        "session_id": session_id,
                        "session_name": session_name,
                        "sender_name": "",
                        "text": text,
                        "msg_type": "text",
                    },
                    request_headers={
                        "Idempotency-Key": f"wxbot-report:{int(job['id'])}"
                    },
                )
                sdk_outbound_id = report_service.sdk_outbound_id(sdk_result)
            except Exception as exc:
                mark_indeterminate = getattr(
                    store,
                    "mark_report_delivery_indeterminate",
                    store.mark_report_delivery_failed,
                )
                await mark_indeterminate(
                    int(job["id"]),
                    type(exc).__name__,
                    delivery_attempt=delivery_attempt,
                )
                raise
            if not await store.mark_report_delivery_queued(
                int(job["id"]),
                delivery_attempt=delivery_attempt,
                sdk_outbound_id=sdk_outbound_id,
            ):
                raise HTTPException(409, "report_delivery_attempt_lost")
            return _AdminEffectOutcome(
                {
                    **response_payload,
                    "queued_count": 1 if sdk_result else 0,
                    "delivery_status": "queued",
                    "sdk_outbound_id": sdk_outbound_id,
                    "send_result": sdk_result,
                }
            )

        result, _version, _replayed = await _execute_admin_mutation(
            store,
            request,
            tenant_id=tenant_id,
            operation="report_send",
            resource_key=f"report-job:{int(job['id'])}:delivery",
            request_payload={
                "job_id": int(job["id"]),
                "session_id": session_id,
                "report_type": report_type,
                "period_key": period_key,
                "text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            },
            recovery_response={**response_payload, "queued_count": 1},
            effect=effect,
        )
        set_admin_audit_context(
            request,
            target_type="wxbot_report_delivery",
            tenant_id=tenant_id,
            session_id=session_id,
            after_state=_mutation_audit_summary(
                operation="send",
                affected_count=1,
                message_count=1,
                message_chars=len(text),
            ),
            trace_id=_request_trace_id(request),
            reason="durable_report_delivery",
        )
        return result

    @router.get("/admin/self-review/subscriptions")
    async def list_self_review_subscriptions(request: Request, response: Response):
        tenant_id = _require_default_tenant_admin(store, request)
        subscriptions = await store.list_self_review_subscriptions(tenant_id)
        safe_subscriptions = [{**item, "auto_create_kb_doc": False} for item in subscriptions]
        version = await _observe_admin_resource(
            store,
            tenant_id,
            "self-review-subscriptions",
            resource_kind="self_review_subscriptions",
            state_payload=_self_review_subscription_config(safe_subscriptions),
        )
        _set_no_store_etag(response, _version_etag(version))
        return {
            "ok": True,
            "subscriptions": safe_subscriptions,
            "version": version,
        }

    @router.post("/admin/self-review/subscriptions")
    async def upsert_self_review_subscription(
        body: WxbotSelfReviewSubscriptionRequest,
        request: Request,
        response: Response,
    ):
        tenant_id = _require_default_tenant_admin(store, request)
        expected_version = _required_version_if_match(request)
        if body.daily_hour < 0 or body.daily_hour > 23:
            raise HTTPException(400, "daily_hour must be between 0 and 23")
        await _require_verified_group(
            store,
            bridge,
            tenant_id=tenant_id,
            session_id=body.session_id,
        )
        before_items = [
            {**item, "auto_create_kb_doc": False}
            for item in await store.list_self_review_subscriptions(tenant_id)
        ]
        before_config = _self_review_subscription_config(before_items)
        await _observe_admin_resource(
            store,
            tenant_id,
            "self-review-subscriptions",
            resource_kind="self_review_subscriptions",
            state_payload=before_config,
        )
        intent = {**body.model_dump(), "auto_create_kb_doc": False}

        async def effect() -> _AdminEffectOutcome:
            subscription = await store.upsert_self_review_subscription(
                tenant_id,
                session_id=body.session_id,
                session_name=body.session_name or body.session_id,
                enabled=body.enabled,
                daily_hour=body.daily_hour,
                tz=body.tz,
                focus_mode=body.focus_mode,
                auto_create_kb_doc=False,
            )
            if scheduler is not None and hasattr(scheduler, "notify_self_review_scheduler"):
                scheduler.notify_self_review_scheduler()
            after_items = [
                {**item, "auto_create_kb_doc": False}
                for item in await store.list_self_review_subscriptions(tenant_id)
            ]
            return _AdminEffectOutcome(
                {
                    "ok": True,
                    "subscription": {**subscription, "auto_create_kb_doc": False},
                },
                _self_review_subscription_config(after_items),
            )

        result, version, _replayed = await _execute_admin_mutation(
            store,
            request,
            tenant_id=tenant_id,
            operation="self_review_subscription_upsert",
            resource_key="self-review-subscriptions",
            request_payload=intent,
            expected_version=expected_version,
            effect=effect,
        )
        if version is not None:
            _set_no_store_etag(response, _version_etag(version))
        set_admin_audit_context(
            request,
            target_type="wxbot_self_review_subscription",
            tenant_id=tenant_id,
            session_id=body.session_id,
            before_state=_mutation_audit_summary(
                operation="upsert",
                affected_count=len(before_items),
            ),
            after_state=_mutation_audit_summary(
                operation="upsert",
                affected_count=max(1, len(before_items)),
                enabled=body.enabled,
            ),
            policy_version=int(version or 0),
            trace_id=_request_trace_id(request),
            reason="conditional_self_review_subscription_update",
        )
        return result

    @router.delete("/admin/self-review/subscriptions/{session_id:path}")
    async def delete_self_review_subscription(
        session_id: str,
        request: Request,
        response: Response,
    ):
        tenant_id = _require_default_tenant_admin(store, request)
        expected_version = _required_version_if_match(request)
        await _require_verified_group(
            store,
            bridge,
            tenant_id=tenant_id,
            session_id=session_id,
        )
        before_items = [
            {**item, "auto_create_kb_doc": False}
            for item in await store.list_self_review_subscriptions(tenant_id)
        ]
        before_config = _self_review_subscription_config(before_items)
        await _observe_admin_resource(
            store,
            tenant_id,
            "self-review-subscriptions",
            resource_kind="self_review_subscriptions",
            state_payload=before_config,
        )

        async def effect() -> _AdminEffectOutcome:
            deleted = await store.delete_self_review_subscription(tenant_id, session_id)
            if scheduler is not None and hasattr(scheduler, "notify_self_review_scheduler"):
                scheduler.notify_self_review_scheduler()
            after_items = [
                {**item, "auto_create_kb_doc": False}
                for item in await store.list_self_review_subscriptions(tenant_id)
            ]
            return _AdminEffectOutcome(
                {"ok": True, "deleted": session_id, "existed": bool(deleted)},
                _self_review_subscription_config(after_items),
            )

        result, version, _replayed = await _execute_admin_mutation(
            store,
            request,
            tenant_id=tenant_id,
            operation="self_review_subscription_delete",
            resource_key="self-review-subscriptions",
            request_payload={"session_id": session_id},
            expected_version=expected_version,
            recovery_response={"ok": True, "deleted": session_id},
            effect=effect,
        )
        if version is not None:
            _set_no_store_etag(response, _version_etag(version))
        set_admin_audit_context(
            request,
            target_type="wxbot_self_review_subscription",
            tenant_id=tenant_id,
            session_id=session_id,
            before_state=_mutation_audit_summary(
                operation="delete",
                affected_count=len(before_items),
            ),
            after_state=_mutation_audit_summary(
                operation="delete",
                affected_count=max(0, len(before_items) - 1),
            ),
            policy_version=int(version or 0),
            trace_id=_request_trace_id(request),
            reason="conditional_self_review_subscription_delete",
        )
        return result

    @router.get("/admin/self-review/jobs")
    async def list_self_review_jobs(request: Request, session_id: str = "", limit: int = 20):
        tenant_id = _require_default_tenant_admin(store, request)
        jobs = await store.list_self_review_jobs(
            tenant_id,
            session_id=session_id,
            limit=limit,
        )
        items: list[dict[str, Any]] = []
        for job in jobs:
            payload = dict(job.get("review_payload") or {})
            has_kb_doc = bool(job.get("kb_doc_id") or payload.get("kb_doc_id"))
            payload.update(
                {
                    "auto_create_kb_doc": False,
                    "kb_publish_status": ("published" if has_kb_doc else "pending_review"),
                }
            )
            items.append({**job, "review_payload": payload})
        return {"ok": True, "items": items, "count": len(items)}

    @router.post("/admin/self-review/jobs/{job_id}/publish")
    async def publish_self_review_job(job_id: int, request: Request):
        principal = _require_admin(store, request)
        tenant_id = str(getattr(store.settings, "wxbot_default_tenant_id", "default") or "default")
        tenant_scopes = {str(item) for item in getattr(principal, "tenant_ids", ())}
        if "*" not in tenant_scopes and tenant_id not in tenant_scopes:
            raise HTTPException(403, "admin tenant scope does not allow publication")

        job = await store.get_self_review_job(job_id)
        if not job or str(job.get("tenant_id") or "") != tenant_id:
            raise HTTPException(404, "self_review_job_not_found")
        session_id = str(job.get("session_id") or "").strip()
        await _require_verified_group(
            store,
            bridge,
            tenant_id=tenant_id,
            session_id=session_id,
        )
        actor = str(getattr(principal, "subject", "") or "admin").strip() or "admin"
        idempotency_key = _required_idempotency_key(request)
        request_id = f"wxbot-self-review-{hashlib.sha256(idempotency_key.encode()).hexdigest()[:32]}"

        async def effect() -> _AdminEffectOutcome:
            try:
                publish_result = await self_review_service.publish_self_review_job(
                    job_id,
                    tenant_id=tenant_id,
                    actor=actor,
                    request_id=request_id,
                )
            except SelfReviewJobNotFound as exc:
                raise HTTPException(404, "self_review_job_not_found") from exc
            except SelfReviewJobNotReady as exc:
                raise HTTPException(409, "self_review_job_not_ready") from exc
            except SelfReviewPublishFailed as exc:
                raise HTTPException(502, "self_review_publish_failed") from exc
            except Exception as exc:
                logger.exception(
                    "wxbot.self_review_publish_unhandled",
                    job_id=job_id,
                    tenant_id=tenant_id,
                    request_id=request_id,
                )
                raise HTTPException(502, "self_review_publish_failed") from exc
            return _AdminEffectOutcome(
                {"ok": True, **publish_result, "request_id": request_id}
            )

        result, _version, _replayed = await _execute_admin_mutation(
            store,
            request,
            tenant_id=tenant_id,
            operation="self_review_publish",
            resource_key=f"self-review-job:{job_id}:publish",
            request_payload={"job_id": int(job_id), "session_id": session_id},
            effect=effect,
        )
        set_admin_audit_context(
            request,
            target_type="wxbot_self_review_publication",
            tenant_id=tenant_id,
            session_id=session_id,
            after_state=_mutation_audit_summary(
                operation="publish",
                affected_count=1,
            ),
            trace_id=_request_trace_id(request),
            reason="durable_self_review_publication",
        )
        return result

    @router.get("/admin/self-review/preview/{session_id:path}")
    async def preview_self_review(
        session_id: str,
        request: Request,
        session_name: str = "",
        date: str = "",
        auto_create_kb_doc: bool | None = None,
    ):
        tenant_id = _require_default_tenant_admin(store, request)
        _ = auto_create_kb_doc  # Deprecated: previews always remain drafts.
        subscription = await store.get_self_review_subscription(tenant_id, session_id) or {}
        period_key, period_label = resolve_self_review_preview_period(
            date=date,
            tz=str(subscription.get("tz") or "Asia/Shanghai"),
        )
        job = await store.get_or_create_self_review_job(
            tenant_id=tenant_id,
            session_id=session_id,
            session_name=session_name or str(subscription.get("session_name") or session_id),
            period_key=period_key,
            period_label=period_label,
        )
        if str(job.get("status") or "") == "completed":
            payload = dict(job.get("review_payload") or {})
            payload.update(
                {
                    "job_id": int(job["id"]),
                    "status": "completed",
                    "current_stage": "completed",
                    "session_id": session_id,
                    "session_name": session_name or job.get("session_name") or session_id,
                    "period": payload.get("period") or period_label,
                    "report": str(job.get("result_text") or ""),
                    "cached": True,
                    "auto_create_kb_doc": False,
                    "kb_doc_id": payload.get("kb_doc_id") or job.get("kb_doc_id"),
                    "kb_doc_title": payload.get("kb_doc_title") or job.get("kb_doc_title") or "",
                    "kb_publish_status": payload.get("kb_publish_status")
                    or ("published" if job.get("kb_doc_id") else "pending_review"),
                }
            )
            return payload

        if str(job.get("status") or "") == "running":
            if scheduler is not None and hasattr(scheduler, "schedule_background"):
                await scheduler.schedule_background(
                    f"self-review-job-{job['id']}",
                    lambda: self_review_service.run_self_review_job(int(job["id"])),
                )
            return {
                "job_id": int(job["id"]),
                "status": "running",
                "current_stage": str(job.get("current_stage") or "queued"),
                "session_id": session_id,
                "session_name": session_name or job.get("session_name") or session_id,
                "period": period_label,
                "cached": False,
                "auto_create_kb_doc": False,
                "kb_doc_id": job.get("kb_doc_id"),
                "kb_doc_title": job.get("kb_doc_title") or "",
                "kb_publish_status": "pending_review",
            }

        reset = await store.update_self_review_job(
            int(job["id"]),
            status="pending",
            current_stage="queued",
            msg_count=0,
            result_text="",
            review_payload={
                "requested_auto_create_kb_doc": False,
                "auto_create_kb_doc": False,
                "kb_publish_status": "pending_review",
            },
            kb_doc_title=f"[{session_name or job.get('session_name') or session_id}] 自我迭代复盘 · {period_label}",
            error="",
            expected_run_attempt=int(job.get("run_attempt") or 0),
            expected_status=str(job.get("status") or "pending"),
        )
        if not reset:
            raise HTTPException(409, "self_review_job_state_changed")
        if scheduler is not None and hasattr(scheduler, "schedule_background"):
            await scheduler.schedule_background(
                f"self-review-job-{job['id']}",
                lambda: self_review_service.run_self_review_job(int(job["id"])),
            )
        else:
            await self_review_service.run_self_review_job(int(job["id"]))
        refreshed = await store.get_self_review_job(int(job["id"]))
        if str((refreshed or {}).get("status") or "") == "completed":
            payload = dict((refreshed or {}).get("review_payload") or {})
            payload.update(
                {
                    "job_id": int(job["id"]),
                    "status": "completed",
                    "current_stage": "completed",
                    "session_id": session_id,
                    "session_name": session_name or job.get("session_name") or session_id,
                    "period": payload.get("period") or period_label,
                    "report": str((refreshed or {}).get("result_text") or ""),
                    "cached": False,
                    "auto_create_kb_doc": False,
                    "kb_doc_id": payload.get("kb_doc_id") or (refreshed or {}).get("kb_doc_id"),
                    "kb_doc_title": payload.get("kb_doc_title")
                    or (refreshed or {}).get("kb_doc_title")
                    or "",
                    "kb_publish_status": payload.get("kb_publish_status")
                    or ("published" if (refreshed or {}).get("kb_doc_id") else "pending_review"),
                }
            )
            return payload
        return {
            "job_id": int(job["id"]),
            "status": str((refreshed or {}).get("status") or "pending"),
            "current_stage": str((refreshed or {}).get("current_stage") or "queued"),
            "session_id": session_id,
            "session_name": session_name or job.get("session_name") or session_id,
            "period": period_label,
            "cached": False,
            "auto_create_kb_doc": False,
            "kb_doc_id": (refreshed or {}).get("kb_doc_id"),
            "kb_doc_title": (refreshed or {}).get("kb_doc_title") or "",
            "kb_publish_status": "pending_review",
        }
