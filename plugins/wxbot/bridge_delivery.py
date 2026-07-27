from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.common.logging import get_logger
from app.social import (
    ParticipationContext,
    ParticipationDecision,
    ParticipationPolicy,
    ParticipationStatus,
)
from app.social.speech_ledger import GroupSpeechBudgetExceeded
from app.social.telemetry import (
    observe_runtime_event_persistence,
    observe_send_revalidation,
)
from plugins.wxbot.bridge_contract import (
    _SDK_JSON_CONTENT_TYPES,
    REPLY_CLAIM_LEASE_SECONDS,
    REPLY_DRAIN_LIMIT,
    REPLY_MAX_ATTEMPTS,
    _as_utc_datetime,
    _send_time_participation_policy,
)
from plugins.wxbot.bridge_state import WxbotBridgeState

log = get_logger(__name__)

_GROUP_ACTIVITY_EXECUTION_OWNERS = frozenset({"group_activity", "wxbot"})
_EXECUTION_OWNER_GATE_TIMEOUT_SECONDS = 1.0


class WxbotBridgeDeliveryMixin(WxbotBridgeState):
    @staticmethod
    def _reply_delivery_delay_seconds(reply: dict[str, Any]) -> float:
        created_at = _as_utc_datetime(reply.get("created_at"))
        if created_at is None:
            return 0.0
        return max(0.0, (datetime.now(UTC) - created_at).total_seconds())

    async def _record_proactive_revalidation_event(
        self,
        reply: dict[str, Any],
        *,
        decision: ParticipationDecision,
        outcome: str,
        transition_applied: bool,
        state: dict[str, Any] | None = None,
    ) -> None:
        delivery_value = reply.get("delivery")
        delivery = delivery_value if isinstance(delivery_value, dict) else {}
        if not bool(delivery.get("requested_proactive")):
            return
        recorder = getattr(self._social_policy_store, "record_participation_event", None)
        if not callable(recorder):
            observe_runtime_event_persistence(succeeded=False, obligation=True)
            raise RuntimeError("proactive_revalidation_audit_unavailable")
        try:
            policy_version = max(
                0,
                int(delivery.get("participation_policy_version") or 0),
            )
        except (TypeError, ValueError):
            policy_version = 0
        observation = state if isinstance(state, dict) else {}
        duplicate_guard = delivery.get("near_duplicate_guard")
        if not isinstance(duplicate_guard, dict):
            duplicate_guard = {}
        try:
            await recorder(
                tenant_id=self._tenant_id,
                session_id=str(reply.get("session_id") or ""),
                policy_version=policy_version,
                event_kind="runtime",
                decision=decision,
                signal_summary={
                    "requested_proactive": True,
                    "delivery_outcome": str(outcome or "unknown")[:32],
                    "transition_applied": bool(transition_applied),
                    "source_message_bound": bool(
                        str(reply.get("source_message_id") or "").strip()
                    ),
                    "reply_queue_id": int(reply.get("id") or 0),
                    "actual_delay_seconds": round(
                        self._reply_delivery_delay_seconds(reply),
                        3,
                    ),
                    "humanization_stage": str(
                        delivery.get("humanization_stage") or "legacy"
                    )[:32],
                    "humanization_cohort": str(
                        delivery.get("humanization_cohort") or "legacy"
                    )[:32],
                    "speech_class": str(
                        delivery.get("speech_class") or "scheduled"
                    )[:32],
                    "speech_budget_enabled": bool(
                        delivery.get("speech_budget_enabled", True)
                    ),
                    "duplicate_guard_enabled": bool(
                        delivery.get("duplicate_guard_enabled", True)
                    ),
                    "duplicate_guard_outcome": str(
                        duplicate_guard.get("action")
                        or delivery.get("duplicate_guard_outcome")
                        or "not_triggered"
                    )[:32],
                    "valid_member_answer_exists": bool(
                        observation.get("valid_member_answer_exists")
                    ),
                    "topic_changed": bool(observation.get("topic_changed")),
                    "superseded_by_newer_message": bool(
                        observation.get("superseded_by_newer_message")
                    ),
                },
                trace_id=str(
                    reply.get("trace_id") or delivery.get("trace_id") or ""
                ),
                runtime_stage="revalidation",
                delivery_stage=str(outcome or "unknown")[:32],
            )
        except Exception as exc:
            observe_runtime_event_persistence(succeeded=False, obligation=True)
            log.warning(
                "wxbot.bridge.proactive_revalidation_event_failed",
                reply_id=reply.get("id"),
                outcome=outcome,
                error_class=exc.__class__.__name__,
            )
            raise RuntimeError("proactive_revalidation_audit_failed") from exc
        observe_runtime_event_persistence(succeeded=True, obligation=True)

    async def _persist_send_revalidation_metadata(
        self,
        reply: dict[str, Any],
        *,
        claim_token: str,
        decision: ParticipationDecision,
        outcome: str,
    ) -> bool:
        delivery_value = reply.get("delivery")
        delivery = dict(delivery_value) if isinstance(delivery_value, dict) else {}
        command_id = str(
            reply.get("command_id")
            or delivery.get("command_id")
            or delivery.get("idempotency_key")
            or f"wxbot-reply:{int(reply.get('id') or 0)}"
        ).strip()
        delivery.setdefault("command_id", command_id)
        delivery.setdefault("idempotency_key", command_id)
        delivery.setdefault("reply_queue_id", int(reply.get("id") or 0))
        delivery.setdefault("trace_id", str(reply.get("trace_id") or ""))
        delivery["send_revalidation"] = {
            "checked": True,
            "reason_codes": list(decision.reason_codes),
            "final_status": decision.status.value,
            "outcome": str(outcome or "unknown"),
            "actual_delay_seconds": self._reply_delivery_delay_seconds(reply),
        }
        updated = await self._store.update_reply_command(
            int(reply["id"]),
            tenant_id=self._tenant_id,
            connection_id=self._connection_id,
            claim_token=claim_token,
            command_id=command_id,
            delivery=delivery,
        )
        if updated:
            reply["delivery"] = delivery
            reply["command_id"] = command_id
        return bool(updated)

    async def _send_loop(self) -> None:
        await self._wait_for_bus()

        while not self._stop.is_set():
            try:
                drained = 0
                while drained < REPLY_DRAIN_LIMIT and not self._stop.is_set():
                    reply = await self._store.claim_pending_reply(
                        self._tenant_id,
                        connection_id=self._connection_id,
                        claim_owner=self._reply_claim_owner,
                        lease_seconds=REPLY_CLAIM_LEASE_SECONDS,
                        max_attempts=REPLY_MAX_ATTEMPTS,
                    )
                    if reply is None:
                        break
                    await self._send_one_reply(reply)
                    drained += 1

                if drained == 0:
                    await asyncio.sleep(self._send_interval)
                    continue

            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("wxbot.bridge.send_error")

            await asyncio.sleep(self._send_interval)

    async def _revalidate_reply_for_send(
        self,
        reply: dict[str, Any],
        *,
        claim_token: str,
    ) -> dict[str, Any]:
        status_value = str(reply.get("participation_status") or "").strip()
        if status_value not in {
            ParticipationStatus.MUST_REPLY.value,
            ParticipationStatus.MAY_REPLY.value,
            ParticipationStatus.DEFER.value,
        } or not str(reply.get("session_id") or "").endswith("@chatroom"):
            return {
                "allowed": True,
                "checked": False,
                "reason_codes": ["revalidation_not_required"],
            }

        delivery_value = reply.get("delivery")
        delivery = delivery_value if isinstance(delivery_value, dict) else {}
        policy_session_id = str(
            delivery.get("external_conversation_id")
            or reply.get("external_conversation_id")
            or reply.get("session_id")
            or ""
        ).strip()
        source_message_id = str(reply.get("source_message_id") or "").strip()
        original_reasons = delivery.get("participation_reason_codes") or []
        if not isinstance(original_reasons, list | tuple):
            original_reasons = []
        before = ParticipationDecision(
            status=ParticipationStatus(status_value),
            score=int(delivery.get("participation_score") or 0),
            reason_codes=tuple(str(item) for item in original_reasons)
            or ("queued_participation_decision",),
            not_before=_as_utc_datetime(reply.get("not_before")),
            expires_at=_as_utc_datetime(reply.get("expires_at")),
            mention_sender=bool(reply.get("mention_sender")),
        )

        async def cancel(
            after: ParticipationDecision,
            *,
            state: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            reason = str(
                after.reason_codes[-1] if after.reason_codes else "send_time_revalidation_cancelled"
            )
            metadata_persisted = await self._persist_send_revalidation_metadata(
                reply,
                claim_token=claim_token,
                decision=after,
                outcome="cancelled_before_sdk",
            )
            cancelled = False
            if metadata_persisted:
                cancelled = await self._store.cancel_claimed_reply(
                    int(reply["id"]),
                    tenant_id=self._tenant_id,
                    connection_id=self._connection_id,
                    claim_token=claim_token,
                    reason=reason,
                )
            observe_send_revalidation(before, after)
            await self._record_proactive_revalidation_event(
                reply,
                decision=after,
                outcome="cancelled_before_sdk",
                transition_applied=bool(cancelled and metadata_persisted),
                state=state,
            )
            return {
                "allowed": False,
                "checked": True,
                "cancelled": cancelled,
                "reason_codes": list(after.reason_codes),
                "final_status": after.status.value,
                "outcome": "cancelled_before_sdk",
                "actual_delay_seconds": self._reply_delivery_delay_seconds(reply),
            }

        if bool(delivery.get("requested_proactive")) and not bool(
            delivery.get("send_revalidation_enabled")
        ):
            return await cancel(
                replace(
                    before,
                    status=ParticipationStatus.CANCEL,
                    reason_codes=(
                        *before.reason_codes,
                        "proactive_send_revalidation_required",
                    ),
                )
            )

        send_policy: ParticipationPolicy | None = None
        if self._social_policy_store is not None:
            try:
                document = await self._social_policy_store.get_group_policy(
                    self._tenant_id,
                    policy_session_id,
                )
            except Exception:
                if not bool(
                    getattr(
                        self._settings,
                        "social_policy_legacy_wxbot_fallback_enabled",
                        False,
                    )
                ):
                    raise
                log.warning(
                    "wxbot.bridge.legacy_policy_fallback",
                    reply_id=reply.get("id"),
                    reason_code="social_policy_store_unavailable",
                )
                document = None
            if document is None:
                send_policy = None
            else:
                send_policy = document.policy.to_domain(enabled=document.effective_enabled)
            if document is None:
                pass
            elif not document.effective_enabled:
                after = self._participation_service.revalidate(
                    before,
                    ParticipationContext(
                        tenant_id=self._tenant_id,
                        session_id=str(reply.get("session_id") or ""),
                        message_id=source_message_id,
                        now=datetime.now(UTC),
                    ),
                    send_policy,
                )
                return await cancel(after)
            else:
                queued_version_raw = delivery.get("participation_policy_version")
                if queued_version_raw is None or queued_version_raw == "":
                    return await cancel(
                        replace(
                            before,
                            status=ParticipationStatus.CANCEL,
                            reason_codes=(
                                *before.reason_codes,
                                "participation_policy_version_missing",
                            ),
                        )
                    )
                try:
                    queued_version = int(queued_version_raw)
                except (TypeError, ValueError):
                    return await cancel(
                        replace(
                            before,
                            status=ParticipationStatus.CANCEL,
                            reason_codes=(
                                *before.reason_codes,
                                "participation_policy_version_invalid",
                            ),
                        )
                    )
                if queued_version != int(document.version):
                    return await cancel(
                        replace(
                            before,
                            status=ParticipationStatus.CANCEL,
                            reason_codes=(
                                *before.reason_codes,
                                "participation_policy_version_changed",
                            ),
                        )
                    )
                if not bool(delivery.get("send_revalidation_enabled")):
                    observe_send_revalidation(before, before)
                    return {
                        "allowed": True,
                        "checked": True,
                        "reason_codes": [
                            *before.reason_codes,
                            "semantic_revalidation_disabled_by_rollout",
                        ],
                    }

        analyzer = getattr(self._store, "get_group_reply_revalidation", None)
        snapshot_loader = getattr(self._store, "get_participation_snapshot", None)
        policy_loader = getattr(self._store, "get_session_policy", None)
        required = [analyzer, snapshot_loader]
        if send_policy is None:
            required.append(policy_loader)
        if not all(callable(item) for item in required):
            # Production WxbotStore always provides these methods.  Keeping old
            # test adapters operable does not weaken the real bridge path.
            if self._social_policy_store is not None or bool(
                delivery.get("requested_proactive")
            ):
                raise RuntimeError("send_revalidation_dependencies_unavailable")
            log.warning(
                "wxbot.bridge.send_revalidation_unavailable",
                reply_id=reply.get("id"),
                participation_status=status_value,
            )
            return {
                "allowed": True,
                "checked": False,
                "reason_codes": ["legacy_store_adapter"],
            }

        state = await analyzer(
            tenant_id=self._tenant_id,
            session_id=str(reply.get("session_id") or ""),
            source_message_id=source_message_id,
            participation_status=status_value,
        )
        if not isinstance(state, dict) or not bool(state.get("context_available")):
            reasons = list(state.get("reason_codes") or []) if isinstance(state, dict) else []
            reason = str(reasons[-1] if reasons else "send_revalidation_context_unavailable")
            return await cancel(
                replace(
                    before,
                    status=ParticipationStatus.CANCEL,
                    reason_codes=(*before.reason_codes, *(reasons or [reason])),
                ),
                state=state,
            )

        now = datetime.now(UTC)
        snapshot = await snapshot_loader(
            self._tenant_id,
            str(reply.get("session_id") or ""),
            now=now,
        )
        if not isinstance(snapshot, dict):
            raise TypeError("wxbot send-time participation snapshot must be a mapping")
        if send_policy is None:
            session_policy = await policy_loader(
                self._tenant_id,
                str(reply.get("session_id") or ""),
            )
            if not isinstance(session_policy, dict):
                raise TypeError("wxbot session policy must be a mapping")
            send_policy = _send_time_participation_policy(
                session_policy,
                force_send=bool(delivery.get("force_send")),
            )

        soft_offset = 1 if status_value == ParticipationStatus.MAY_REPLY.value else 0
        context = ParticipationContext(
            tenant_id=self._tenant_id,
            session_id=str(reply.get("session_id") or ""),
            message_id=source_message_id,
            now=now,
            valid_member_answer_exists=bool(state.get("valid_member_answer_exists")),
            topic_changed=bool(state.get("topic_changed")),
            superseded_by_newer_message=bool(state.get("superseded_by_newer_message")),
            is_self_sent=bool(state.get("source_is_self_sent")),
            bot_messages_last_40=int(snapshot.get("bot_messages_last_40") or 0),
            total_messages_last_40=int(snapshot.get("total_messages_last_40") or 0),
            soft_replies_last_10m=max(
                0,
                int(snapshot.get("soft_replies_last_10m") or 0) - soft_offset,
            ),
            soft_replies_last_hour=max(
                0,
                int(snapshot.get("soft_replies_last_hour") or 0) - soft_offset,
            ),
            consecutive_bot_messages=int(snapshot.get("consecutive_bot_messages") or 0),
        )
        revalidated = self._participation_service.revalidate(
            before,
            context,
            send_policy,
        )
        combined_reasons = [
            *list(state.get("reason_codes") or []),
            *list(revalidated.reason_codes),
        ]
        if revalidated.status == ParticipationStatus.CANCEL:
            return await cancel(
                replace(
                    revalidated,
                    reason_codes=tuple(combined_reasons),
                ),
                state=state,
            )
        if revalidated.status == ParticipationStatus.DEFER:
            observe_send_revalidation(before, revalidated)
            not_before = revalidated.not_before or (now + timedelta(seconds=45))
            expires_at = revalidated.expires_at or (not_before + timedelta(minutes=10))
            reason = str(
                combined_reasons[-1] if combined_reasons else "send_time_revalidation_deferred"
            )
            final_decision = replace(
                revalidated,
                reason_codes=tuple(combined_reasons),
                not_before=not_before,
                expires_at=expires_at,
            )
            metadata_persisted = await self._persist_send_revalidation_metadata(
                reply,
                claim_token=claim_token,
                decision=final_decision,
                outcome="deferred_before_sdk",
            )
            rescheduled = False
            if metadata_persisted:
                rescheduled = await self._store.reschedule_claimed_reply(
                    int(reply["id"]),
                    tenant_id=self._tenant_id,
                    connection_id=self._connection_id,
                    claim_token=claim_token,
                    not_before=not_before,
                    expires_at=expires_at,
                    reason=reason,
                )
            await self._record_proactive_revalidation_event(
                reply,
                decision=final_decision,
                outcome="deferred_before_sdk",
                transition_applied=bool(rescheduled),
                state=state,
            )
            return {
                "allowed": False,
                "checked": True,
                "deferred": True,
                "rescheduled": rescheduled,
                "reason_codes": combined_reasons,
                "final_status": final_decision.status.value,
                "outcome": "deferred_before_sdk",
                "actual_delay_seconds": self._reply_delivery_delay_seconds(reply),
            }
        observe_send_revalidation(before, revalidated)
        final_decision = replace(
            revalidated,
            reason_codes=tuple(combined_reasons),
        )
        return {
            "allowed": True,
            "checked": True,
            "reason_codes": combined_reasons,
            "final_status": final_decision.status.value,
            "outcome": "approved_before_sdk",
            "actual_delay_seconds": self._reply_delivery_delay_seconds(reply),
            "_decision": final_decision,
            "_state": state,
        }

    def _reply_execution_owner_contract(
        self,
        reply: dict[str, Any],
    ) -> tuple[dict[str, str] | None, str]:
        delivery_value = reply.get("delivery")
        delivery = delivery_value if isinstance(delivery_value, dict) else {}
        source = str(delivery.get("source") or "").strip()
        owners_value = delivery.get("execution_owners")
        versions_value = delivery.get("execution_owner_versions")
        contract_required = bool(
            source == "group_activity"
            or owners_value is not None
            or versions_value is not None
        )
        if not contract_required:
            return None, ""
        if not isinstance(owners_value, list | tuple) or not owners_value:
            return None, "execution_owner_contract_invalid"
        owners = tuple(str(owner or "").strip() for owner in owners_value)
        if (
            len(owners) > 16
            or any(not owner or len(owner) > 64 for owner in owners)
            or len(set(owners)) != len(owners)
        ):
            return None, "execution_owner_contract_invalid"
        owner_set = frozenset(owners)
        if (
            source == "group_activity"
            and owner_set != _GROUP_ACTIVITY_EXECUTION_OWNERS
        ) or ("group_activity" in owner_set and source != "group_activity"):
            return None, "execution_owner_contract_invalid"
        if not isinstance(versions_value, dict) or set(versions_value) != set(owners):
            return None, "execution_owner_versions_invalid"
        owner_versions = {
            owner: str(versions_value.get(owner) or "").strip()
            for owner in owners
        }
        if any(not version or len(version) > 64 for version in owner_versions.values()):
            return None, "execution_owner_versions_invalid"
        tenant_id = str(delivery.get("execution_tenant_id") or "").strip()
        session_id = str(delivery.get("execution_session_id") or "").strip()
        if tenant_id != self._tenant_id or session_id != str(
            reply.get("session_id") or ""
        ).strip():
            return None, "execution_owner_scope_mismatch"
        return owner_versions, ""

    async def _check_reply_execution_owners(
        self,
        reply: dict[str, Any],
        *,
        phase: str,
    ) -> dict[str, Any]:
        owner_versions, contract_error = self._reply_execution_owner_contract(reply)
        if owner_versions is None and not contract_error:
            return {
                "allowed": True,
                "checked": False,
                "phase": phase,
                "reason": "execution_owner_contract_not_required",
            }
        if contract_error:
            return {
                "allowed": False,
                "checked": True,
                "phase": phase,
                "reason": contract_error,
            }
        gate = self._owners_scope_execution_allowed
        if not callable(gate):
            return {
                "allowed": False,
                "checked": True,
                "phase": phase,
                "reason": "execution_owner_gate_missing",
            }
        try:
            allowed = (
                await asyncio.wait_for(
                    gate(
                        owner_versions,
                        self._tenant_id,
                        str(reply.get("session_id") or "").strip(),
                    ),
                    timeout=_EXECUTION_OWNER_GATE_TIMEOUT_SECONDS,
                )
                is True
            )
        except TimeoutError:
            reason = "execution_owner_gate_timeout"
            allowed = False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "wxbot.bridge.execution_owner_gate_error",
                reply_id=reply.get("id"),
                phase=phase,
                error_class=exc.__class__.__name__,
            )
            reason = "execution_owner_gate_error"
            allowed = False
        else:
            reason = "execution_owners_allowed" if allowed else "execution_owner_disabled"
        return {
            "allowed": allowed,
            "checked": True,
            "phase": phase,
            "reason": reason,
            "owners": list(owner_versions),
        }

    @staticmethod
    def _record_execution_owner_gate(
        delivery: dict[str, Any],
        decision: dict[str, Any],
    ) -> None:
        if not bool(decision.get("checked")):
            return
        gate_value = delivery.get("execution_owner_gate")
        gate_state = dict(gate_value) if isinstance(gate_value, dict) else {}
        phase = str(decision.get("phase") or "unknown")[:32]
        gate_state[phase] = {
            "allowed": bool(decision.get("allowed")),
            "reason": str(decision.get("reason") or "")[:64],
        }
        delivery["execution_owner_gate"] = gate_state

    async def _cancel_reply_for_execution_owner_gate(
        self,
        reply: dict[str, Any],
        *,
        claim_token: str,
        command_id: str,
        delivery: dict[str, Any],
        decision: dict[str, Any],
    ) -> bool:
        reason = str(
            decision.get("reason") or "execution_owner_disabled"
        )[:64]
        claim_active = await self._store.update_reply_command(
            int(reply["id"]),
            tenant_id=self._tenant_id,
            connection_id=self._connection_id,
            claim_token=claim_token,
            command_id=command_id,
            delivery=delivery,
        )
        cancelled = False
        if claim_active:
            cancelled = await self._store.cancel_claimed_reply(
                int(reply["id"]),
                tenant_id=self._tenant_id,
                connection_id=self._connection_id,
                claim_token=claim_token,
                reason=reason,
            )
        log.warning(
            "wxbot.bridge.reply_execution_owner_cancelled",
            reply_id=reply.get("id"),
            phase=decision.get("phase"),
            reason=reason,
            cancelled=cancelled,
        )
        return bool(cancelled)

    async def _send_one_reply(self, reply: dict[str, Any]) -> None:
        reply_id = reply["id"]
        claim_token = str(reply.get("claim_token") or "").strip()
        if not claim_token:
            log.warning("wxbot.bridge.reply_missing_claim", reply_id=reply_id)
            return
        try:
            revalidation = await self._revalidate_reply_for_send(
                reply,
                claim_token=claim_token,
            )
            if not bool(revalidation.get("allowed")):
                log.info(
                    (
                        "wxbot.bridge.reply_deferred_before_send"
                        if bool(revalidation.get("deferred"))
                        else "wxbot.bridge.reply_cancelled_before_send"
                    ),
                    reply_id=reply_id,
                    reason_codes=list(revalidation.get("reason_codes") or []),
                    cancelled=bool(revalidation.get("cancelled")),
                    rescheduled=bool(revalidation.get("rescheduled")),
                )
                return
            if self._client is None:
                self._client = httpx.AsyncClient(
                    timeout=10,
                    trust_env=False,
                )
            msg_type = str(reply.get("msg_type") or "text")
            command_id = str(reply.get("command_id") or "").strip() or f"wxbot-reply:{reply_id}"
            delivery = dict(reply.get("delivery") or {})
            delivery.setdefault("command_id", command_id)
            delivery.setdefault("idempotency_key", command_id)
            delivery.setdefault("reply_queue_id", reply_id)
            delivery["tenant_id"] = self._tenant_id
            delivery.setdefault("trace_id", reply.get("trace_id", ""))
            if bool(revalidation.get("checked", True)):
                delivery["send_revalidation"] = {
                    "checked": True,
                    "reason_codes": list(revalidation.get("reason_codes") or []),
                    "final_status": str(revalidation.get("final_status") or ""),
                    "outcome": str(revalidation.get("outcome") or ""),
                    "actual_delay_seconds": float(
                        revalidation.get("actual_delay_seconds") or 0.0
                    ),
                }
            claim_active = await self._store.update_reply_command(
                reply_id,
                tenant_id=self._tenant_id,
                connection_id=self._connection_id,
                claim_token=claim_token,
                command_id=command_id,
                delivery=delivery,
            )
            if not claim_active:
                log.warning(
                    "wxbot.bridge.reply_claim_lost",
                    reply_id=reply_id,
                    phase="prepare",
                )
                return
            reply["delivery"] = delivery
            audit_decision = revalidation.get("_decision")
            if isinstance(audit_decision, ParticipationDecision):
                await self._record_proactive_revalidation_event(
                    reply,
                    decision=audit_decision,
                    outcome="approved_before_sdk",
                    transition_applied=True,
                    state=(
                        revalidation.get("_state")
                        if isinstance(revalidation.get("_state"), dict)
                        else None
                    ),
                )
            if bool(delivery.get("speech_budget_enabled", False)):
                prepare_speech = getattr(
                    self._store,
                    "prepare_claimed_reply_speech",
                    None,
                )
                if not callable(prepare_speech):
                    raise RuntimeError("speech_budget_prepare_unavailable")
                try:
                    speech_prepared = await prepare_speech(
                        reply,
                        tenant_id=self._tenant_id,
                        connection_id=self._connection_id,
                        claim_token=claim_token,
                    )
                except GroupSpeechBudgetExceeded as exc:
                    cancelled = await self._store.cancel_claimed_reply(
                        int(reply_id),
                        tenant_id=self._tenant_id,
                        connection_id=self._connection_id,
                        claim_token=claim_token,
                        reason=str(exc.reason or "speech_budget_denied"),
                    )
                    log.info(
                        "wxbot.bridge.reply_speech_budget_cancelled",
                        reply_id=reply_id,
                        speech_class=str(delivery.get("speech_class") or "soft"),
                        reason=exc.reason,
                        cancelled=cancelled,
                    )
                    return
                if not speech_prepared:
                    log.warning(
                        "wxbot.bridge.reply_claim_lost",
                        reply_id=reply_id,
                        phase="speech_budget_prepare",
                    )
                    return
                delivery = dict(reply.get("delivery") or delivery)
                command_id = str(reply.get("command_id") or command_id)
            owner_gate = await self._check_reply_execution_owners(
                reply,
                phase="before_sdk",
            )
            self._record_execution_owner_gate(delivery, owner_gate)
            reply["delivery"] = delivery
            if not bool(owner_gate.get("allowed")):
                await self._cancel_reply_for_execution_owner_gate(
                    reply,
                    claim_token=claim_token,
                    command_id=command_id,
                    delivery=delivery,
                    decision=owner_gate,
                )
                return
            payload = {
                "target": {
                    "session_id": str(
                        delivery.get("external_conversation_id")
                        or reply.get("external_conversation_id")
                        or reply["session_id"]
                    ),
                    "session_name": reply.get("session_name", ""),
                    "session_kind": reply.get("session_kind", ""),
                },
                "sender": {
                    "wxid": reply.get("sender_wxid", ""),
                    "name": reply.get("sender_name", ""),
                },
                "content": {
                    "msg_type": msg_type,
                    "text": reply["reply_text"],
                    "image_path": reply.get("image_path", ""),
                    "image_url": reply.get("image_url", ""),
                },
                "reply": {
                    "mention_sender": bool(reply.get("mention_sender")),
                    "reply_to_msg_svr_id": reply.get("reply_to_msg_svr_id", ""),
                },
                "source_message": reply.get("source_message", {}),
                "delivery": delivery,
                "command_id": command_id,
                "metadata": {
                    "tenant_id": self._tenant_id,
                    "trace_id": reply.get("trace_id", ""),
                    "command_id": command_id,
                    "protocol": "envelope",
                },
            }
            resp = await self._request_sdk(
                self._client,
                "POST",
                self._sdk_url,
                "/send/envelope",
                json=payload,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    **self._sdk_headers,
                },
                timeout_seconds=10.0,
                max_response_bytes=2 * 1024 * 1024,
                allowed_response_content_types=_SDK_JSON_CONTENT_TYPES,
            )
            if resp.status_code in {404, 405, 501}:
                fallback_gate = await self._check_reply_execution_owners(
                    reply,
                    phase="before_legacy_sdk",
                )
                self._record_execution_owner_gate(delivery, fallback_gate)
                if not bool(fallback_gate.get("allowed")):
                    await self._cancel_reply_for_execution_owner_gate(
                        reply,
                        claim_token=claim_token,
                        command_id=command_id,
                        delivery=delivery,
                        decision=fallback_gate,
                    )
                    return
                resp = await self._request_sdk(
                    self._client,
                    "POST",
                    self._sdk_url,
                    "/send",
                    json={
                        "session_id": str(
                            delivery.get("external_conversation_id")
                            or reply.get("external_conversation_id")
                            or reply["session_id"]
                        ),
                        "session_name": reply.get("session_name", ""),
                        "sender_name": reply.get("sender_name", ""),
                        "sender_wxid": reply.get("sender_wxid", ""),
                        "mention_sender": bool(reply.get("mention_sender")),
                        "reply_to_msg_svr_id": reply.get("reply_to_msg_svr_id", ""),
                        "session_kind": reply.get("session_kind", ""),
                        "text": reply["reply_text"],
                        "msg_type": msg_type,
                        "image_path": reply.get("image_path", ""),
                        "image_url": reply.get("image_url", ""),
                        "source_message": reply.get("source_message", {}),
                        "delivery": delivery,
                        "command_id": command_id,
                    },
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        **self._sdk_headers,
                    },
                    timeout_seconds=10.0,
                    max_response_bytes=2 * 1024 * 1024,
                    allowed_response_content_types=_SDK_JSON_CONTENT_TYPES,
                )
            post_sdk_gate = await self._check_reply_execution_owners(
                reply,
                phase="after_sdk",
            )
            self._record_execution_owner_gate(delivery, post_sdk_gate)
            if bool(post_sdk_gate.get("checked")):
                claim_active = await self._store.update_reply_command(
                    reply_id,
                    tenant_id=self._tenant_id,
                    connection_id=self._connection_id,
                    claim_token=claim_token,
                    command_id=command_id,
                    delivery=delivery,
                )
                if not claim_active:
                    log.warning(
                        "wxbot.bridge.reply_claim_lost",
                        reply_id=reply_id,
                        phase="execution_owner_gate_after_sdk",
                    )
                    return
            if not bool(post_sdk_gate.get("allowed")) and resp.status_code != 200:
                await self._cancel_reply_for_execution_owner_gate(
                    reply,
                    claim_token=claim_token,
                    command_id=command_id,
                    delivery=delivery,
                    decision=post_sdk_gate,
                )
                return
            if not bool(post_sdk_gate.get("allowed")):
                # A successful remote acknowledgement is never rewritten as a
                # local cancellation: that would release speech accounting and
                # make a retry capable of duplicating an already accepted send.
                log.error(
                    "wxbot.bridge.execution_owner_disabled_after_sdk",
                    reply_id=reply_id,
                    reason=post_sdk_gate.get("reason"),
                )
            if resp.status_code == 200:
                sdk_outbound_id = None
                try:
                    body = resp.json()
                    if isinstance(body, dict):
                        sdk_outbound_id = self._to_int(body.get("id"))
                except Exception:
                    sdk_outbound_id = None
                queued = await self._store.mark_reply_queued(
                    reply_id,
                    tenant_id=self._tenant_id,
                    connection_id=self._connection_id,
                    claim_token=claim_token,
                    sdk_outbound_id=sdk_outbound_id,
                )
                if queued:
                    log.info(
                        "wxbot.bridge.reply_queued",
                        reply_id=reply_id,
                        command_id=command_id,
                        sdk_outbound_id=sdk_outbound_id,
                        msg_type=msg_type,
                    )
                else:
                    log.warning(
                        "wxbot.bridge.reply_claim_lost",
                        reply_id=reply_id,
                        phase="complete",
                    )
            else:
                result = await self._store.mark_reply_failed(
                    reply_id,
                    f"SDK returned {resp.status_code}",
                    tenant_id=self._tenant_id,
                    connection_id=self._connection_id,
                    claim_token=claim_token,
                    max_attempts=REPLY_MAX_ATTEMPTS,
                )
                log.warning(
                    "wxbot.bridge.reply_failed",
                    reply_id=reply_id,
                    status=resp.status_code,
                    result=result,
                    msg_type=msg_type,
                )
        except Exception as exc:
            safe_error = f"sdk request failed: {exc.__class__.__name__}"
            result = await self._store.mark_reply_failed(
                reply_id,
                safe_error,
                tenant_id=self._tenant_id,
                connection_id=self._connection_id,
                claim_token=claim_token,
                max_attempts=REPLY_MAX_ATTEMPTS,
            )
            log.warning(
                "wxbot.bridge.reply_error",
                reply_id=reply_id,
                error_class=exc.__class__.__name__,
                result=result,
            )
