from __future__ import annotations

import asyncio

import pytest

from app.common.config import Settings
from app.common.exceptions import CapabilityError
from app.common.types import ChatResponse, ChatUsage
from app.faq.engine import FAQEngine
from app.faq.store import FAQStore, InMemoryFAQRepository, faq_collection_for
from app.kb.vector.memory_store import InMemoryVectorStore

from ._fake_llm import FakeEmbeddingsProvider, hash_embed, make_preprocessed, make_session


class _CapturingFAQProvider(FakeEmbeddingsProvider):
    def __init__(self, reply: str) -> None:
        super().__init__()
        self._reply = reply
        self.last_request = None

    async def chat(self, request):  # type: ignore[override]
        self.last_request = request
        return ChatResponse(
            content=self._reply,
            model="canned",
            finish_reason="stop",
            usage=ChatUsage(input_tokens=10, output_tokens=len(self._reply)),
            latency_ms=1,
        )


class _MissingCollectionError(Exception):
    status_code = 404
    content = b"Not found: Collection faq_default does not exist"


class _MissingCollectionVector:
    async def search(self, collection, vector, top_k=10, filter_=None):  # type: ignore[no-untyped-def]
        _ = collection, vector, top_k, filter_
        raise _MissingCollectionError("Not found: Collection faq_default does not exist")


class _UnavailableVector:
    async def search(self, collection, vector, top_k=10, filter_=None):  # type: ignore[no-untyped-def]
        _ = collection, vector, top_k, filter_
        raise RuntimeError("qdrant unavailable")


class _ToggleFailingIndexVector(InMemoryVectorStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_index = False

    async def ensure_collection(self, name: str, dim: int) -> None:
        if self.fail_index:
            raise RuntimeError("qdrant indexing unavailable")
        await super().ensure_collection(name, dim)


class _FailOnceDeleteVector(InMemoryVectorStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_next_delete = False
        self.delete_calls = 0

    async def delete(self, collection: str, ids: list[str]) -> None:
        self.delete_calls += 1
        if self.fail_next_delete:
            self.fail_next_delete = False
            raise RuntimeError("qdrant delete unavailable")
        await super().delete(collection, ids)


class _FailOnceDeleteFAQRepository(InMemoryFAQRepository):
    def __init__(self) -> None:
        super().__init__()
        self.fail_next_delete = False

    async def delete(
        self,
        tenant_id: str,
        session_id: str | None,
        faq_id: int,
    ) -> bool:
        if self.fail_next_delete:
            self.fail_next_delete = False
            raise RuntimeError("simulated crash before faq db delete")
        return await super().delete(tenant_id, session_id, faq_id)


class _BlockingUpsertVector(InMemoryVectorStore):
    def __init__(self) -> None:
        super().__init__()
        self.block_next_upsert = False
        self.upsert_entered = asyncio.Event()
        self.release_upsert = asyncio.Event()

    async def upsert(self, collection, records):  # type: ignore[no-untyped-def]
        if self.block_next_upsert:
            self.block_next_upsert = False
            self.upsert_entered.set()
            await self.release_upsert.wait()
        await super().upsert(collection, records)


@pytest.mark.asyncio
async def test_faq_engine_returns_top_hit_when_similar() -> None:
    vector = InMemoryVectorStore()
    llm = FakeEmbeddingsProvider()
    repo = InMemoryFAQRepository()
    store = FAQStore(repo, vector, llm, embed_model="fake")

    await store.create(
        tenant_id="demo",
        question="How do I reset my password?",
        answer="Click the forgot password link on the login page.",
        variants=["password reset", "forgot password"],
    )
    await store.create(
        tenant_id="demo",
        question="What are your business hours?",
        answer="We are open 9-5 Monday through Friday.",
    )
    await store.create(
        tenant_id="demo",
        question="How do I cancel my subscription?",
        answer="Go to account settings and click cancel.",
    )

    engine = FAQEngine(vector, llm, threshold=0.5, embed_model="fake")
    pre = make_preprocessed("How do I reset my password")
    session = make_session("demo")
    result = await engine.answer(pre, session)

    assert (
        "forgot password" in result.reply_text.lower() or "login page" in result.reply_text.lower()
    )
    assert result.citations
    assert result.citations[0].source == "faq"


@pytest.mark.asyncio
async def test_faq_engine_missing_qdrant_collection_is_miss() -> None:
    llm = FakeEmbeddingsProvider()
    engine = FAQEngine(_MissingCollectionVector(), llm, threshold=0.5, embed_model="fake")  # type: ignore[arg-type]

    preview = await engine.preview_match(make_preprocessed("reset password"), make_session("demo"))

    assert preview["matched"] is False
    assert preview["verdict"] == "LOW"
    with pytest.raises(CapabilityError, match="no_faq_hit"):
        await engine.answer(make_preprocessed("reset password"), make_session("demo"))


@pytest.mark.asyncio
async def test_faq_engine_qdrant_unavailable_still_reports_search_failed() -> None:
    llm = FakeEmbeddingsProvider()
    engine = FAQEngine(_UnavailableVector(), llm, threshold=0.5, embed_model="fake")  # type: ignore[arg-type]

    with pytest.raises(CapabilityError) as exc_info:
        await engine.preview_match(make_preprocessed("reset password"), make_session("demo"))

    assert str(exc_info.value).startswith("faq_search_failed:")


@pytest.mark.asyncio
async def test_faq_engine_vector_preview_includes_answer() -> None:
    vector = InMemoryVectorStore()
    llm = FakeEmbeddingsProvider()
    repo = InMemoryFAQRepository()
    store = FAQStore(repo, vector, llm, embed_model="fake")

    await store.create(
        tenant_id="demo",
        question="How do I reset my password?",
        answer="Click the forgot password link on the login page.",
    )

    engine = FAQEngine(vector, llm, threshold=0.5, embed_model="fake")
    preview = await engine.preview_match(make_preprocessed("reset password"), make_session("demo"))
    result = await engine.answer(make_preprocessed("reset password"), make_session("demo"))

    assert preview["answer"] == "Click the forgot password link on the login page."
    assert result.reply_text == "Click the forgot password link on the login page."


@pytest.mark.asyncio
async def test_faq_engine_preview_includes_verdicts() -> None:
    vector = InMemoryVectorStore()
    llm = FakeEmbeddingsProvider()
    repo = InMemoryFAQRepository()
    store = FAQStore(repo, vector, llm, embed_model="fake")

    await store.create(
        tenant_id="demo",
        question="How do I reset my password?",
        answer="Click the forgot password link on the login page.",
    )

    engine = FAQEngine(vector, llm, threshold=0.5, embed_model="fake")
    clear = await engine.preview_match(make_preprocessed("reset password"), make_session("demo"))
    low = await engine.preview_match(make_preprocessed("unrelated gibberish"), make_session("demo"))

    assert clear["verdict"] == "CLEAR"
    assert low["verdict"] in {"LOW", "INSUFFICIENT", "AMBIGUOUS"}
    assert "matched" in clear
    assert "score" in clear
    assert "threshold" in clear


@pytest.mark.asyncio
async def test_faq_engine_result_metadata_includes_preview_verdict() -> None:
    vector = InMemoryVectorStore()
    llm = FakeEmbeddingsProvider()
    repo = InMemoryFAQRepository()
    store = FAQStore(repo, vector, llm, embed_model="fake")

    await store.create(
        tenant_id="demo",
        question="How do I reset my password?",
        answer="Click the forgot password link on the login page.",
    )

    engine = FAQEngine(vector, llm, threshold=0.5, embed_model="fake")
    result = await engine.answer(make_preprocessed("reset password"), make_session("demo"))

    assert result.metadata["verdict"] == "CLEAR"


@pytest.mark.asyncio
async def test_faq_engine_reuses_preview_hint() -> None:
    class CountingFAQEngine(FAQEngine):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.preview_calls = 0

        async def preview_match(self, pre, session, hints=None):  # type: ignore[override]
            self.preview_calls += 1
            return await super().preview_match(pre, session, hints)

    vector = InMemoryVectorStore()
    llm = FakeEmbeddingsProvider()
    repo = InMemoryFAQRepository()
    store = FAQStore(repo, vector, llm, embed_model="fake")

    await store.create(
        tenant_id="demo",
        question="How do I reset my password?",
        answer="Click the forgot password link on the login page.",
    )

    engine = CountingFAQEngine(vector, llm, threshold=0.5, embed_model="fake")
    preview = await engine.preview_match(make_preprocessed("reset password"), make_session("demo"))
    result = await engine.answer(
        make_preprocessed("reset password"),
        make_session("demo"),
        {"faq_preview": preview},
    )

    assert result.reply_text == "Click the forgot password link on the login page."
    assert engine.preview_calls == 1


@pytest.mark.asyncio
async def test_faq_engine_raises_when_below_threshold() -> None:
    vector = InMemoryVectorStore()
    llm = FakeEmbeddingsProvider()
    repo = InMemoryFAQRepository()
    store = FAQStore(repo, vector, llm, embed_model="fake")

    await store.create(
        tenant_id="demo",
        question="How do I reset my password?",
        answer="Click forgot password.",
    )

    # Use an impossibly-high threshold so no hit can pass.
    engine = FAQEngine(vector, llm, threshold=0.9999, embed_model="fake")
    with pytest.raises(CapabilityError):
        await engine.answer(make_preprocessed("unrelated gibberish"), make_session("demo"))


@pytest.mark.asyncio
async def test_faq_crud_updates_vector_index() -> None:
    vector = InMemoryVectorStore()
    llm = FakeEmbeddingsProvider()
    repo = InMemoryFAQRepository()
    store = FAQStore(repo, vector, llm, embed_model="fake")

    rec = await store.create(tenant_id="demo", question="Foo bar baz", answer="ans")
    assert rec.id > 0
    rows = await store.list("demo")
    assert any(r.id == rec.id for r in rows)

    ok = await store.delete("demo", rec.id)
    assert ok
    rows = await store.list("demo")
    assert not any(r.id == rec.id for r in rows)


@pytest.mark.asyncio
async def test_faq_delete_vector_failure_preserves_db_and_retry_converges() -> None:
    vector = _FailOnceDeleteVector()
    repo = InMemoryFAQRepository()
    store = FAQStore(repo, vector, FakeEmbeddingsProvider(), embed_model="fake")
    rec = await store.create("demo", "Delete me", "answer")
    vector.fail_next_delete = True

    with pytest.raises(RuntimeError, match="qdrant delete unavailable"):
        await store.delete("demo", rec.id)

    assert await repo.get("demo", None, rec.id) is not None
    assert await vector.search(
        faq_collection_for("demo"),
        hash_embed("Delete me"),
        top_k=5,
    )

    assert await store.delete("demo", rec.id) is True
    assert await repo.get("demo", None, rec.id) is None
    assert vector.delete_calls == 2
    assert (
        await vector.search(
            faq_collection_for("demo"),
            hash_embed("Delete me"),
            top_k=5,
        )
        == []
    )


@pytest.mark.asyncio
async def test_faq_delete_crash_after_vector_cleanup_is_retryable() -> None:
    vector = _FailOnceDeleteVector()
    repo = _FailOnceDeleteFAQRepository()
    store = FAQStore(repo, vector, FakeEmbeddingsProvider(), embed_model="fake")
    rec = await store.create("demo", "Delete after cleanup", "answer")
    repo.fail_next_delete = True

    with pytest.raises(RuntimeError, match="simulated crash"):
        await store.delete("demo", rec.id)

    assert await repo.get("demo", None, rec.id) is not None
    assert (
        await vector.search(
            faq_collection_for("demo"),
            hash_embed("Delete after cleanup"),
            top_k=5,
        )
        == []
    )

    assert await store.delete("demo", rec.id) is True
    assert await repo.get("demo", None, rec.id) is None
    assert vector.delete_calls == 2


@pytest.mark.asyncio
async def test_faq_delete_waits_for_inflight_update_and_leaves_no_orphan_vectors() -> None:
    vector = _BlockingUpsertVector()
    repo = InMemoryFAQRepository()
    store = FAQStore(repo, vector, FakeEmbeddingsProvider(), embed_model="fake")
    rec = await store.create("demo", "Original question", "answer")

    vector.block_next_upsert = True
    update_task = asyncio.create_task(store.update("demo", rec.id, question="Replacement question"))
    await asyncio.wait_for(vector.upsert_entered.wait(), timeout=1)
    delete_task = asyncio.create_task(store.delete("demo", rec.id))
    await asyncio.sleep(0)
    assert delete_task.done() is False

    vector.release_upsert.set()
    updated, deleted = await asyncio.gather(update_task, delete_task)

    assert updated is not None
    assert deleted is True
    assert await repo.get("demo", None, rec.id) is None
    assert (
        await vector.search(
            faq_collection_for("demo"),
            hash_embed("Replacement question"),
            top_k=5,
        )
        == []
    )


@pytest.mark.asyncio
async def test_faq_create_returns_degraded_when_vector_indexing_fails() -> None:
    vector = _ToggleFailingIndexVector()
    vector.fail_index = True
    llm = FakeEmbeddingsProvider()
    repo = InMemoryFAQRepository()
    store = FAQStore(repo, vector, llm, embed_model="fake")

    rec = await store.create(
        tenant_id="demo",
        question="Foo bar baz",
        answer="ans",
    )

    assert rec.status == "degraded"
    rows = await store.list("demo")
    assert rows[0].id == rec.id
    assert rows[0].status == "degraded"


@pytest.mark.asyncio
async def test_faq_update_returns_degraded_when_vector_reindexing_fails() -> None:
    vector = _ToggleFailingIndexVector()
    llm = FakeEmbeddingsProvider()
    repo = InMemoryFAQRepository()
    store = FAQStore(repo, vector, llm, embed_model="fake")

    rec = await store.create(
        tenant_id="demo",
        question="Foo bar baz",
        answer="ans",
    )
    vector.fail_index = True

    updated = await store.update(
        tenant_id="demo",
        faq_id=rec.id,
        question="Foo bar updated",
    )

    assert updated is not None
    assert updated.question == "Foo bar updated"
    assert updated.status == "degraded"


@pytest.mark.asyncio
async def test_faq_engine_rewrites_answer_when_style_or_memory_present() -> None:
    vector = InMemoryVectorStore()
    llm = _CapturingFAQProvider("可以的，退款会在审核通过后 7 个工作日内原路退回。")
    repo = InMemoryFAQRepository()
    store = FAQStore(repo, vector, llm, embed_model="fake")

    await store.create(
        tenant_id="demo",
        question="退款多久到账？",
        answer="退款会在审核通过后 7 个工作日内原路退回。",
        variants=["退款多久", "退款时效"],
    )

    engine = FAQEngine(vector, llm, threshold=0.3, embed_model="fake")
    session = make_session("demo")
    session.variables["persona_skill"] = "请用稳定、温和的客服语气回复。"
    session.variables["user_memory"] = {
        "short_term": "用户刚问过退款流程",
        "long_term": "已知用户事实与偏好：\n- 偏好简洁回复",
        "manual_notes": "高优先级客户",
    }

    result = await engine.answer(make_preprocessed("退款多久到账"), session)

    assert result.reply_text.startswith("可以的")
    assert result.metadata["rewritten"] is True
    assert llm.last_request is not None
    assert "<persona_style_data>" in (llm.last_request.system or "")
    assert "<persona_style_data>" in (llm.last_request.system or "")
    assert "历史记忆" in (llm.last_request.system or "")


@pytest.mark.asyncio
async def test_faq_engine_rewrites_for_structured_relevant_memory_without_legacy_text() -> None:
    vector = InMemoryVectorStore()
    llm = _CapturingFAQProvider("会在审核通过后 7 个工作日内原路退回。")
    repo = InMemoryFAQRepository()
    store = FAQStore(repo, vector, llm, embed_model="fake")

    await store.create(
        tenant_id="demo",
        question="退款多久到账？",
        answer="退款会在审核通过后 7 个工作日内原路退回。",
        variants=["退款多久", "退款时效"],
    )

    engine = FAQEngine(vector, llm, threshold=0.3, embed_model="fake")
    session = make_session("demo")
    session.variables["user_memory"] = {
        "memory_items": {"identity": [], "session": []},
        "relevant_memory_items": [
            {
                "source_type": "explicit_user",
                "status": "active",
                "confidence": 1.0,
                "sensitivity": "normal",
                "content": "用户希望回复简洁",
            }
        ],
    }

    result = await engine.answer(make_preprocessed("退款多久到账"), session)

    assert result.metadata["rewritten"] is True
    assert llm.last_request is not None
    assert "用户希望回复简洁" in (llm.last_request.system or "")


@pytest.mark.asyncio
async def test_faq_engine_rewrites_for_active_persona_profile_without_prompt_text() -> None:
    vector = InMemoryVectorStore()
    llm = _CapturingFAQProvider("小海答：7 个工作日内原路退回。")
    repo = InMemoryFAQRepository()
    store = FAQStore(repo, vector, llm, embed_model="fake")

    await store.create(
        tenant_id="demo",
        question="退款多久到账？",
        answer="退款会在审核通过后 7 个工作日内原路退回。",
        variants=["退款多久", "退款时效"],
    )

    engine = FAQEngine(vector, llm, threshold=0.3, embed_model="fake")
    session = make_session("demo")
    session.variables["persona_skill"] = ""
    session.variables["persona_profile"] = {
        "profile_id": "persona-1",
        "name": "小海",
    }

    result = await engine.answer(make_preprocessed("退款多久到账"), session)

    assert result.metadata["rewritten"] is True
    assert llm.last_request is not None
    assert (
        "<active_persona_name>\n小海\n</active_persona_name>"
        in (llm.last_request.system or "")
    )


@pytest.mark.asyncio
async def test_faq_engine_rewrite_prompt_can_disable_customer_service_style() -> None:
    vector = InMemoryVectorStore()
    llm = _CapturingFAQProvider("可以，今晚能到。")
    repo = InMemoryFAQRepository()
    store = FAQStore(repo, vector, llm, embed_model="fake")

    await store.create(
        tenant_id="demo",
        question="今晚能到吗？",
        answer="今晚能到。",
        variants=["今晚到", "今晚送达"],
    )

    engine = FAQEngine(
        vector,
        llm,
        settings=Settings(customer_service_prompt_enabled=False),
        threshold=0.3,
        embed_model="fake",
    )
    session = make_session("demo")
    session.variables["persona_skill"] = "自然一点。"

    await engine.answer(make_preprocessed("今晚能到吗"), session)

    assert llm.last_request is not None
    assert "客户服务助手" not in (llm.last_request.system or "")


@pytest.mark.asyncio
async def test_faq_engine_group_rewrite_prompt_includes_concise_group_rules() -> None:
    vector = InMemoryVectorStore()
    llm = _CapturingFAQProvider("就他。")
    repo = InMemoryFAQRepository()
    store = FAQStore(repo, vector, llm, embed_model="fake")

    await store.create(
        tenant_id="demo",
        question="群里谁最帅？",
        answer="群里最帅的是提问的人。",
        variants=["谁最帅", "本群最帅"],
    )

    engine = FAQEngine(vector, llm, threshold=0.3, embed_model="fake")
    session = make_session("demo", session_id="group-1@chatroom")
    session.channel = session.channel.WECHAT
    session.variables["persona_skill"] = "嘴贫一点。"

    await engine.answer(make_preprocessed("群里谁最帅？"), session)

    assert llm.last_request is not None
    system = llm.last_request.system or ""
    assert "现在是微信群聊" in system
    assert "别写成小作文" in system


@pytest.mark.asyncio
async def test_faq_engine_prefers_session_scope_before_global() -> None:
    vector = InMemoryVectorStore()
    llm = FakeEmbeddingsProvider()
    repo = InMemoryFAQRepository()
    store = FAQStore(repo, vector, llm, embed_model="fake")

    await store.create(
        tenant_id="demo",
        question="退款多久到账",
        answer="全局答案：7 个工作日。",
    )
    await store.create(
        tenant_id="demo",
        question="退款多久到账",
        answer="群专属答案：本群活动订单 1 个工作日内处理。",
        session_id="group-1@chatroom",
    )

    engine = FAQEngine(vector, llm, threshold=0.3, embed_model="fake")
    result = await engine.answer(
        make_preprocessed("退款多久到账"),
        make_session("demo", session_id="group-1@chatroom"),
    )

    assert "1 个工作日" in result.reply_text
    assert result.metadata["scope"] == "session"
    assert result.metadata["scope_session_id"] == "group-1@chatroom"


@pytest.mark.asyncio
async def test_faq_engine_falls_back_to_global_scope_when_session_not_hit() -> None:
    vector = InMemoryVectorStore()
    llm = FakeEmbeddingsProvider()
    repo = InMemoryFAQRepository()
    store = FAQStore(repo, vector, llm, embed_model="fake")

    await store.create(
        tenant_id="demo",
        question="退款多久到账",
        answer="全局答案：7 个工作日。",
    )
    await store.create(
        tenant_id="demo",
        question="本群暗号是什么",
        answer="芝麻开门。",
        session_id="group-1@chatroom",
    )

    engine = FAQEngine(vector, llm, threshold=0.3, embed_model="fake")
    result = await engine.answer(
        make_preprocessed("退款多久到账"),
        make_session("demo", session_id="group-1@chatroom"),
    )

    assert "7 个工作日" in result.reply_text
    assert result.metadata["scope"] == "global"
    assert result.metadata["scope_session_id"] is None


@pytest.mark.asyncio
async def test_faq_lexical_match_prefers_more_specific_phrase() -> None:
    vector = InMemoryVectorStore()
    llm = FakeEmbeddingsProvider()
    repo = InMemoryFAQRepository()
    store = FAQStore(repo, vector, llm, embed_model="fake")
    await store.create(
        tenant_id="demo",
        question="退款多久",
        answer="泛化答案。",
    )
    await store.create(
        tenant_id="demo",
        question="退款多久到账",
        answer="具体答案：7 个工作日。",
    )
    engine = FAQEngine(
        vector,
        llm,
        threshold=0.88,
        embed_model="fake",
        faq_store=store,
    )

    result = await engine.answer(
        make_preprocessed("请问退款多久到账呀"),
        make_session("demo"),
    )

    assert "7 个工作日" in result.reply_text


@pytest.mark.asyncio
async def test_faq_lexical_match_rejects_short_or_negated_substrings() -> None:
    vector = InMemoryVectorStore()
    llm = FakeEmbeddingsProvider()
    repo = InMemoryFAQRepository()
    store = FAQStore(repo, vector, llm, embed_model="fake")
    await store.create(
        tenant_id="demo",
        question="退款多久到账",
        answer="7 个工作日。",
        variants=["到账"],
    )
    engine = FAQEngine(
        vector,
        llm,
        threshold=0.88,
        embed_model="fake",
        faq_store=store,
    )

    short_preview = await engine.preview_match(
        make_preprocessed("到账呢"),
        make_session("demo"),
    )
    negated_preview = await engine.preview_match(
        make_preprocessed("不是问退款多久到账"),
        make_session("demo"),
    )

    assert short_preview["matched"] is False
    assert negated_preview["matched"] is False


@pytest.mark.asyncio
async def test_faq_lexical_match_downgrades_distant_negation_in_zh_and_en() -> None:
    vector = InMemoryVectorStore()
    llm = FakeEmbeddingsProvider()
    repo = InMemoryFAQRepository()
    store = FAQStore(repo, vector, llm, embed_model="fake")
    await store.create(
        tenant_id="demo",
        question="退款多久到账以及具体处理流程",
        answer="7 个工作日。",
    )
    await store.create(
        tenant_id="demo",
        question="How long does a refund take and what is the process",
        answer="Seven business days.",
    )
    engine = FAQEngine(
        vector,
        llm,
        threshold=0.88,
        embed_model="fake",
        faq_store=store,
    )

    queries = [
        "我并不是现在想问退款多久到账以及具体处理流程",
        (
            "I am definitely not currently asking about "
            "how long does a refund take and what is the process"
        ),
    ]
    for query in queries:
        preview = await engine.preview_match(
            make_preprocessed(query),
            make_session("demo"),
        )

        assert preview["matched"] is False
        assert preview["verdict"] != "CLEAR"

    renewed_queries = [
        "我不是问物流，而是想问退款多久到账以及具体处理流程",
        (
            "I am not asking about shipping, but I want to know "
            "how long does a refund take and what is the process"
        ),
    ]
    for query in renewed_queries:
        preview = await engine.preview_match(
            make_preprocessed(query),
            make_session("demo"),
        )

        assert preview["matched"] is True
        assert preview["verdict"] == "CLEAR"
