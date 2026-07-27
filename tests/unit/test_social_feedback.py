from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.social.contracts import (
    MemberMemoryCorrectionResolution,
    MemberPrivacyPolicyDocument,
    MemberPrivacyValues,
)
from app.social.feedback import (
    NaturalFeedbackAction,
    NaturalFeedbackService,
    detect_natural_feedback,
)


class _PolicyStore:
    def __init__(self) -> None:
        self.document = MemberPrivacyPolicyDocument(
            tenant_id="tenant-1",
            session_id="group-1",
            user_id="user-1",
            version=2,
            policy=MemberPrivacyValues(
                memory_enabled=True,
                allow_group_recall=True,
                proactive_participation_enabled=True,
                audience_scope="session",
            ),
        )
        self.mutations: list[dict[str, object]] = []

    async def get_member_policy(self, *args: object) -> MemberPrivacyPolicyDocument:
        _ = args
        return self.document

    async def put_member_policy(self, **kwargs: object) -> object:
        self.mutations.append(kwargs)
        update = kwargs["update"]
        self.document = self.document.model_copy(
            update={
                "version": 3,
                "policy": update.policy,  # type: ignore[union-attr]
            }
        )
        return SimpleNamespace(document=self.document)


class _Memory:
    def __init__(self) -> None:
        self.forgotten = 0
        self.corrected = 0
        self.calls: list[dict[str, object]] = []

    async def forget_member(self, **kwargs: object) -> int:
        self.calls.append(dict(kwargs))
        self.forgotten += 4
        return 4

    async def resolve_member_fact_correction(
        self, **kwargs: object
    ) -> MemberMemoryCorrectionResolution:
        self.calls.append(dict(kwargs))
        self.corrected += 1
        return MemberMemoryCorrectionResolution(
            status="applied", changed=1, candidate_count=1
        )


def test_detect_natural_feedback_maps_direct_short_controls() -> None:
    actions = {
        signal.action
        for signal in detect_natural_feedback("少说点，别主动；群里别提，也别记我")
    }
    assert actions == {
        NaturalFeedbackAction.REDUCE_REPLIES,
        NaturalFeedbackAction.DISABLE_PROACTIVE,
        NaturalFeedbackAction.FORGET_MEMBER,
        NaturalFeedbackAction.KEEP_OUT_OF_GROUP,
    }
    assert detect_natural_feedback("你记错了")[-1].action == (
        NaturalFeedbackAction.CORRECT_MEMORY
    )
    assert detect_natural_feedback("转发一段长文：有人说少说点" + "。" * 80) == ()
    assert detect_natural_feedback("他说‘别记我’是什么意思") == ()
    assert detect_natural_feedback("有人说别主动，这是什么意思") == ()


@pytest.mark.asyncio
async def test_forget_member_commits_fail_closed_policy_then_deletes_memory() -> None:
    store = _PolicyStore()
    memory = _Memory()
    service = NaturalFeedbackService(store, memory=memory)
    signal = detect_natural_feedback("别记我")[0]

    result = await service.apply(
        signal,
        tenant_id="tenant-1",
        session_id="group-1",
        user_id="user-1",
        message_id="msg-1",
        trace_id="trace-1",
    )

    assert result.applied is True
    assert result.policy_version == 3
    assert result.memory_items_changed == 4
    assert store.document.policy.memory_enabled is False
    assert store.document.policy.allow_group_recall is False
    assert store.document.policy.allow_private_recall is False
    operation_key = str(store.mutations[0]["idempotency_key"])
    assert operation_key.startswith("natural-feedback:forget_member:")
    assert len(operation_key) < 128
    assert memory.calls[0]["idempotency_key"] == operation_key

    replay = await service.apply(
        signal,
        tenant_id="tenant-1",
        session_id="group-1",
        user_id="user-1",
        message_id="msg-1",
        trace_id="trace-1",
    )
    assert replay.applied is True
    assert replay.policy_version == 3
    assert len(store.mutations) == 1


@pytest.mark.asyncio
async def test_group_and_reply_controls_update_member_policy() -> None:
    store = _PolicyStore()
    service = NaturalFeedbackService(store)
    for index, text in enumerate(("群里别提", "少说点", "别主动"), start=1):
        await service.apply(
            detect_natural_feedback(text)[0],
            tenant_id="tenant-1",
            session_id="group-1",
            user_id="user-1",
            message_id=f"msg-{index}",
        )

    assert store.document.policy.no_group_mentions is True
    assert store.document.policy.allow_group_recall is False
    assert store.document.policy.soft_reply_opt_out is True
    assert store.document.policy.proactive_participation_enabled is False


@pytest.mark.asyncio
async def test_memory_correction_uses_invalidation_port() -> None:
    memory = _Memory()
    result = await NaturalFeedbackService(_PolicyStore(), memory=memory).apply(
        detect_natural_feedback("你记错了")[0],
        tenant_id="tenant-1",
        session_id="group-1",
        user_id="user-1",
        message_id="msg-correction",
        correction_text="我不住在上海",
    )
    assert result.applied is True
    assert result.memory_items_changed == 1
    assert str(memory.calls[0]["idempotency_key"]).startswith(
        "natural-feedback:correct_memory:"
    )


@pytest.mark.asyncio
async def test_ambiguous_memory_correction_requires_confirmation() -> None:
    class _AmbiguousMemory(_Memory):
        async def resolve_member_fact_correction(
            self, **kwargs: object
        ) -> MemberMemoryCorrectionResolution:
            self.calls.append(dict(kwargs))
            return MemberMemoryCorrectionResolution(
                status="confirmation_required",
                candidate_count=3,
            )

    result = await NaturalFeedbackService(
        _PolicyStore(), memory=_AmbiguousMemory()
    ).apply(
        detect_natural_feedback("你记错了")[0],
        tenant_id="tenant-1",
        session_id="group-1",
        user_id="user-1",
        message_id="msg-ambiguous",
        correction_text="你记错了",
    )
    assert result.applied is False
    assert result.memory_confirmation_required is True
    assert result.memory_candidate_count == 3
