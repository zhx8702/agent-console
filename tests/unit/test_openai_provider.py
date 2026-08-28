from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from tenacity import wait_none

from app.common.config import get_settings
from app.common.exceptions import UpstreamUnavailable
from app.common.types import (
    Attachment,
    ChatMessage,
    ChatRequest,
    MessageType,
    Role,
    ToolCall,
    ToolSchema,
)
from app.infra.metrics import LLM_API_ATTEMPTS
from app.llm.providers.openai_provider import (
    OpenAIProvider,
    _convert_messages,
    _convert_messages_for_responses,
    _extract_responses_payload,
)


def _make_request() -> ChatRequest:
    return ChatRequest(
        tenant_id="demo",
        trace_id="trace-openai",
        model="gpt-5.4",
        messages=[ChatMessage(role=Role.USER, content="你好")],
    )


def _metric_total(metric, **labels) -> float:
    m = metric.labels(**labels)
    return float(m._value.get())  # type: ignore[attr-defined]


def test_convert_messages_keeps_call_id_for_tool_outputs() -> None:
    converted = _convert_messages(
        [
            ChatMessage(role=Role.USER, content="查一下群人数"),
            ChatMessage(
                role=Role.TOOL,
                tool_call_id="call_123",
                content='{"ok": true, "result": {"member_count": 3}}',
            ),
        ]
    )

    assert converted[1]["role"] == "tool"
    assert converted[1]["tool_call_id"] == "call_123"
    assert converted[1]["call_id"] == "call_123"


def test_convert_messages_omits_compat_call_id_for_xai_tool_outputs() -> None:
    converted = _convert_messages(
        [
            ChatMessage(
                role=Role.TOOL,
                tool_call_id="call_xai",
                content='{"ok": true}',
            )
        ],
        include_compat_call_id=False,
    )

    assert converted == [
        {
            "role": "tool",
            "tool_call_id": "call_xai",
            "content": '{"ok": true}',
        }
    ]


def test_convert_messages_includes_image_attachments_for_chat_completions() -> None:
    converted = _convert_messages(
        [
            ChatMessage(
                role=Role.USER,
                content="看一下这张图",
                attachments=[
                    Attachment(type=MessageType.IMAGE, url="data:image/png;base64,aW1hZ2U=")
                ],
            )
        ]
    )

    assert converted == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "看一下这张图"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,aW1hZ2U="},
                },
            ],
        }
    ]


def test_convert_messages_for_responses_uses_function_call_output() -> None:
    converted = _convert_messages_for_responses(
        [
            ChatMessage(role=Role.USER, content="查一下群人数"),
            ChatMessage(
                role=Role.ASSISTANT,
                content="",
                tool_calls=[ToolCall(id="call_abc", name="get_group_info", arguments={})],
            ),
            ChatMessage(
                role=Role.TOOL,
                tool_call_id="call_abc",
                content='{"ok": true, "result": {"member_count": 3}}',
            ),
        ]
    )

    assert converted[0] == {"role": "user", "content": "查一下群人数"}
    assert converted[1]["type"] == "function_call"
    assert converted[1]["call_id"] == "call_abc"
    assert converted[1]["name"] == "get_group_info"
    assert converted[2]["type"] == "function_call_output"
    assert converted[2]["call_id"] == "call_abc"
    assert '"member_count": 3' in converted[2]["output"]


def test_convert_messages_for_responses_includes_image_attachments() -> None:
    converted = _convert_messages_for_responses(
        [
            ChatMessage(
                role=Role.USER,
                content="看一下这张图",
                attachments=[
                    Attachment(type=MessageType.IMAGE, url="data:image/png;base64,aW1hZ2U=")
                ],
            )
        ]
    )

    assert converted == [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "看一下这张图"},
                {"type": "input_image", "image_url": "data:image/png;base64,aW1hZ2U="},
            ],
        }
    ]


def test_extract_responses_payload_prefers_call_id_for_tool_calls() -> None:
    raw = SimpleNamespace(
        model="gpt-5.4",
        status="completed",
        output=[
            SimpleNamespace(
                type="function_call",
                id="fc_internal_1",
                call_id="call_real_1",
                name="get_group_info",
                arguments='{"limit": 5}',
            )
        ],
        output_text="",
        usage=SimpleNamespace(input_tokens=11, output_tokens=7),
    )

    content, tool_calls, citations, model, finish_reason, usage = _extract_responses_payload(raw)

    assert content == ""
    assert citations == []
    assert model == "gpt-5.4"
    assert finish_reason == "completed"
    assert usage.input_tokens == 11
    assert usage.output_tokens == 7
    assert len(tool_calls) == 1
    assert tool_calls[0].id == "call_real_1"
    assert tool_calls[0].name == "get_group_info"
    assert tool_calls[0].arguments == {"limit": 5}


def test_extract_responses_payload_collects_url_citations() -> None:
    raw = SimpleNamespace(
        model="gpt-5.4",
        status="completed",
        output=[
            SimpleNamespace(type="web_search_call", status="completed"),
            SimpleNamespace(
                type="message",
                content=[
                    SimpleNamespace(
                        text="这是联网搜索结果。",
                        annotations=[
                            SimpleNamespace(
                                type="url_citation",
                                url="https://example.com/news",
                                title="Example News",
                                snippet="short snippet",
                            )
                        ],
                    )
                ],
            ),
        ],
        output_text="",
        usage=SimpleNamespace(input_tokens=11, output_tokens=7),
    )

    content, tool_calls, citations, model, finish_reason, usage = _extract_responses_payload(raw)

    assert content == "这是联网搜索结果。"
    assert tool_calls == []
    assert model == "gpt-5.4"
    assert finish_reason == "completed"
    assert usage.output_tokens == 7
    assert len(citations) == 1
    assert citations[0].source == "openai_web_search"
    assert citations[0].title == "Example News"
    assert citations[0].url == "https://example.com/news"


def test_extract_responses_payload_labels_grok_web_search_citations() -> None:
    raw = SimpleNamespace(
        model="grok-4.6",
        status="completed",
        output=[
            SimpleNamespace(type="web_search_call", status="completed"),
            SimpleNamespace(
                type="message",
                content=[
                    SimpleNamespace(
                        text="Grok web search result.",
                        annotations=[
                            SimpleNamespace(
                                type="url_citation",
                                url="https://example.com/grok-search",
                            )
                        ],
                    )
                ],
            ),
        ],
        output_text="",
        usage=SimpleNamespace(input_tokens=11, output_tokens=7),
    )

    _content, _tool_calls, citations, _model, _finish_reason, _usage = (
        _extract_responses_payload(
            raw,
            citation_source="grok_web_search",
            citation_id_prefix="grok_web",
        )
    )

    assert citations[0].source == "grok_web_search"
    assert citations[0].id == "grok_web:1"


def test_extract_responses_payload_collects_completed_search_action_sources() -> None:
    raw = SimpleNamespace(
        model="gpt-5.4",
        status="completed",
        output=[
            SimpleNamespace(
                type="web_search_call",
                status="completed",
                action=SimpleNamespace(
                    type="search",
                    sources=[
                        SimpleNamespace(
                            type="url",
                            url="https://example.com/search-source",
                        )
                    ],
                ),
            ),
            SimpleNamespace(
                type="message",
                content=[
                    SimpleNamespace(
                        text="这是没有内联引用标注的联网搜索结果。",
                        annotations=[],
                    )
                ],
            ),
        ],
        output_text="",
        usage=SimpleNamespace(input_tokens=11, output_tokens=7),
    )

    content, tool_calls, citations, _model, finish_reason, _usage = _extract_responses_payload(raw)

    assert content == "这是没有内联引用标注的联网搜索结果。"
    assert tool_calls == []
    assert finish_reason == "completed"
    assert len(citations) == 1
    assert citations[0].source == "openai_web_search"
    assert citations[0].url == "https://example.com/search-source"


def test_extract_responses_payload_accepts_x_search_call_sources() -> None:
    raw = SimpleNamespace(
        model="grok-4.6",
        status="completed",
        output=[
            SimpleNamespace(
                type="x_search_call",
                status="completed",
                action=SimpleNamespace(
                    sources=[SimpleNamespace(type="url", url="https://x.com/example")]
                ),
            ),
            SimpleNamespace(
                type="message",
                content=[SimpleNamespace(text="X search result.", annotations=[])],
            ),
        ],
        output_text="",
        usage=SimpleNamespace(input_tokens=11, output_tokens=7),
    )

    _content, _tool_calls, citations, _model, _finish_reason, _usage = (
        _extract_responses_payload(raw)
    )

    assert [citation.url for citation in citations] == ["https://x.com/example"]


def test_openai_provider_exposes_web_and_x_search_to_grok_native_tool_choice() -> None:
    settings = get_settings().model_copy(
        update={
            "openai_base_url": "https://sub2api.example.test/v1",
            "grok_models_base_url": "https://sub2api.example.test/v1",
            "xai_api_key": "xai-test",
            "openai_api_mode": "responses",
            "openai_web_search_enabled": True,
            "openai_web_search_tool": "web_search",
        }
    )
    provider = OpenAIProvider(api_key="xai-test", settings=settings)
    request = _make_request().model_copy(
        update={
            "metadata": {
                "openai_web_search": True,
                "semantic_intent_mode": "native_tool_choice",
            }
        }
    )

    kwargs = provider._build_responses_kwargs(request)

    assert kwargs["tools"] == [{"type": "web_search"}, {"type": "x_search"}]
    assert "tool_choice" not in kwargs


@pytest.mark.asyncio
async def test_openai_provider_projects_native_x_search_into_structured_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openai

    created_clients: list[_FakeClient] = []
    raw = SimpleNamespace(
        model="grok-4.6",
        status="completed",
        output=[
            SimpleNamespace(type="x_search_call", status="completed"),
            SimpleNamespace(
                type="message",
                content=[SimpleNamespace(text="我综合了 X 上的结果。", annotations=[])],
            ),
        ],
        output_text="我综合了 X 上的结果。",
        usage=SimpleNamespace(input_tokens=11, output_tokens=7),
    )

    def _factory(*, api_key: str, base_url: str, max_retries: int = 0):
        _ = api_key
        assert max_retries == 0
        client = _FakeClient(base_url=base_url, responses_result=raw)
        created_clients.append(client)
        return client

    monkeypatch.setattr(openai, "AsyncOpenAI", _factory)

    settings = get_settings().model_copy(
        update={
            "openai_base_url": "https://api.x.ai/v1",
            "openai_api_mode": "responses",
            "openai_web_search_enabled": True,
            "openai_web_search_tool": "web_search",
        }
    )
    provider = OpenAIProvider(api_key="xai-test", settings=settings)
    request = _make_request().model_copy(
        update={
            "metadata": {
                "openai_web_search": True,
                "semantic_intent_mode": "native_tool_choice",
            },
            "messages": [ChatMessage(role=Role.USER, content="x上搜一下怎么快速搞钱")],
        }
    )

    response = await provider.chat(request)

    assert len(created_clients[0].responses_calls) == 1
    assert response.metadata["semantic_intent"] == {
        "operation": "retrieve",
        "source": "x",
        "artifact": "text",
        "domain": "web_search",
        "confidence": 1.0,
        "needs_tool": True,
        "query": "x上搜一下怎么快速搞钱",
        "tool_name": "x_search",
    }
    assert response.metadata["semantic_intent_method"] == "native_tool_call"


def test_extract_responses_payload_prefers_annotation_metadata_over_action_source() -> None:
    duplicated_url = "https://example.com/richer-annotation"
    raw = SimpleNamespace(
        model="gpt-5.4",
        status="completed",
        output=[
            SimpleNamespace(
                type="web_search_call",
                status="completed",
                action=SimpleNamespace(
                    type="search",
                    sources=[SimpleNamespace(type="url", url=duplicated_url)],
                ),
            ),
            SimpleNamespace(
                type="message",
                content=[
                    SimpleNamespace(
                        text="这是带有内联引用标注的联网搜索结果。",
                        annotations=[
                            SimpleNamespace(
                                type="url_citation",
                                url=duplicated_url,
                                title="Richer annotation title",
                            )
                        ],
                    )
                ],
            ),
        ],
        output_text="",
        usage=SimpleNamespace(input_tokens=11, output_tokens=7),
    )

    _content, _tool_calls, citations, _model, _finish_reason, _usage = _extract_responses_payload(
        raw
    )

    assert len(citations) == 1
    assert citations[0].url == duplicated_url
    assert citations[0].title == "Richer annotation title"


def test_extract_responses_payload_caps_sources_across_multiple_search_calls() -> None:
    raw = SimpleNamespace(
        model="gpt-5.4",
        status="completed",
        output=[
            SimpleNamespace(
                type="web_search_call",
                status="completed",
                action=SimpleNamespace(
                    type="search",
                    sources=[
                        SimpleNamespace(type="url", url=f"https://example.com/first/{index}")
                        for index in range(20)
                    ],
                ),
            ),
            SimpleNamespace(
                type="web_search_call",
                status="completed",
                action=SimpleNamespace(
                    type="search",
                    sources=[
                        SimpleNamespace(type="url", url=f"https://example.com/second/{index}")
                        for index in range(20)
                    ],
                ),
            ),
            SimpleNamespace(
                type="message",
                content=[SimpleNamespace(text="联网结果。", annotations=[])],
            ),
        ],
        output_text="",
        usage=SimpleNamespace(input_tokens=2, output_tokens=2),
    )

    _content, _tool_calls, citations, _model, _finish_reason, _usage = _extract_responses_payload(
        raw
    )

    assert len(citations) == 20
    assert citations[-1].url == "https://example.com/first/19"


def test_extract_responses_payload_does_not_count_cross_call_duplicates_toward_cap() -> None:
    duplicate_url = "https://example.com/first/0"
    new_url = "https://example.com/second/new"
    raw = SimpleNamespace(
        model="gpt-5.4",
        status="completed",
        output=[
            SimpleNamespace(
                type="web_search_call",
                status="completed",
                action=SimpleNamespace(
                    sources=[
                        SimpleNamespace(type="url", url=f"https://example.com/first/{index}")
                        for index in range(19)
                    ],
                ),
            ),
            SimpleNamespace(
                type="web_search_call",
                status="completed",
                action=SimpleNamespace(
                    sources=[
                        SimpleNamespace(type="url", url=duplicate_url),
                        SimpleNamespace(type="url", url=new_url),
                    ],
                ),
            ),
            SimpleNamespace(
                type="message",
                content=[SimpleNamespace(text="联网结果。", annotations=[])],
            ),
        ],
        output_text="",
        usage=SimpleNamespace(input_tokens=2, output_tokens=2),
    )

    _content, _tool_calls, citations, _model, _finish_reason, _usage = _extract_responses_payload(
        raw
    )

    assert len(citations) == 20
    assert citations[-1].url == new_url


@pytest.mark.parametrize("status", ["failed", "in_progress"])
def test_extract_responses_payload_rejects_sources_from_incomplete_search_call(
    status: str,
) -> None:
    raw = SimpleNamespace(
        model="gpt-5.4",
        status="completed",
        output=[
            SimpleNamespace(
                type="web_search_call",
                status=status,
                action=SimpleNamespace(
                    type="search",
                    sources=[
                        SimpleNamespace(
                            type="url",
                            url="https://example.com/unverified-source",
                        )
                    ],
                ),
            ),
            SimpleNamespace(
                type="message",
                content=[SimpleNamespace(text="未完成搜索。", annotations=[])],
            ),
        ],
        output_text="",
        usage=SimpleNamespace(input_tokens=2, output_tokens=2),
    )

    _content, _tool_calls, citations, _model, _finish_reason, _usage = _extract_responses_payload(
        raw
    )

    assert citations == []


def test_extract_responses_payload_rejects_citation_without_search_call() -> None:
    raw = SimpleNamespace(
        model="gpt-5.4",
        status="completed",
        output=[
            SimpleNamespace(
                type="message",
                content=[
                    SimpleNamespace(
                        text="未验证来源。",
                        annotations=[
                            SimpleNamespace(
                                type="url_citation",
                                url="https://example.com/unverified",
                                title="Unverified",
                            )
                        ],
                    )
                ],
            )
        ],
        output_text="",
        usage=SimpleNamespace(input_tokens=2, output_tokens=2),
    )

    _content, _tool_calls, citations, _model, _finish_reason, _usage = _extract_responses_payload(
        raw
    )

    assert citations == []


def _chat_response(model: str = "gpt-5.4", content: str = "chat ok") -> SimpleNamespace:
    return SimpleNamespace(
        model=model,
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(
                    content=content,
                    tool_calls=[],
                ),
            )
        ],
        usage=SimpleNamespace(prompt_tokens=12, completion_tokens=8),
    )


def _responses_response(model: str = "gpt-5.4", content: str = "responses ok") -> SimpleNamespace:
    return SimpleNamespace(
        model=model,
        status="completed",
        output=[SimpleNamespace(type="message", content=[SimpleNamespace(text=content)])],
        output_text=content,
        usage=SimpleNamespace(input_tokens=11, output_tokens=7),
    )


class _FakeResponsesAPI:
    def __init__(self, owner: _FakeClient) -> None:
        self._owner = owner

    async def create(self, **kwargs):
        self._owner.responses_calls.append(kwargs)
        if self._owner.responses_error is not None:
            raise self._owner.responses_error
        if kwargs.get("stream"):
            return self._owner.responses_stream
        return self._owner.responses_result


class _FakeChatCompletionsAPI:
    def __init__(self, owner: _FakeClient) -> None:
        self._owner = owner

    async def create(self, **kwargs):
        self._owner.chat_calls.append(kwargs)
        if self._owner.chat_error is not None:
            raise self._owner.chat_error
        if kwargs.get("stream"):
            return self._owner.chat_stream
        return self._owner.chat_result


class _FakeEmbeddingsAPI:
    def __init__(self, owner: _FakeClient) -> None:
        self._owner = owner

    async def create(self, **kwargs):
        self._owner.embedding_calls.append(kwargs)
        return SimpleNamespace(
            model=kwargs["model"],
            data=[SimpleNamespace(embedding=[0.1, 0.2]) for _ in kwargs["input"]],
            usage=SimpleNamespace(prompt_tokens=3),
        )


class _FakeClient:
    def __init__(
        self,
        *,
        base_url: str,
        responses_result=None,
        chat_result=None,
        responses_stream=None,
        chat_stream=None,
        responses_error: Exception | None = None,
        chat_error: Exception | None = None,
    ) -> None:
        self.base_url = base_url
        self.responses_calls: list[dict] = []
        self.chat_calls: list[dict] = []
        self.embedding_calls: list[dict] = []
        self.responses_error = responses_error
        self.chat_error = chat_error
        self.responses_result = responses_result or _responses_response()
        self.chat_result = chat_result or _chat_response()
        self.responses_stream = responses_stream
        self.chat_stream = chat_stream
        self.responses = _FakeResponsesAPI(self)
        self.chat = SimpleNamespace(completions=_FakeChatCompletionsAPI(self))
        self.embeddings = _FakeEmbeddingsAPI(self)

    async def close(self) -> None:
        return None


class _AsyncItems:
    def __init__(self, items: list[object]) -> None:
        self._items = list(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


def _internal_server_error() -> Exception:
    import openai

    response = httpx.Response(
        status_code=502,
        request=httpx.Request("POST", "https://sub2api.example/responses"),
    )
    return openai.InternalServerError("bad gateway", response=response, body=None)


@pytest.mark.asyncio
async def test_openai_provider_uses_root_url_for_responses_and_v1_for_chat_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openai

    created_clients: list[_FakeClient] = []

    def _factory(*, api_key: str, base_url: str, max_retries: int = 0):
        _ = api_key
        assert max_retries == 0
        client = _FakeClient(
            base_url=base_url,
            responses_error=RuntimeError("responses down"),
            chat_result=_chat_response(content="fallback chat"),
        )
        created_clients.append(client)
        return client

    monkeypatch.setattr(openai, "AsyncOpenAI", _factory)

    settings = get_settings().model_copy(
        update={
            "openai_base_url": "https://openai-gateway.example.test",
            "openai_api_mode": "responses",
            "openai_disable_fallback": False,
        }
    )
    provider = OpenAIProvider(api_key="sk-test", settings=settings)

    resp = await provider.chat(_make_request())

    assert resp.content == "fallback chat"
    assert len(created_clients) == 2
    assert created_clients[0].base_url == "https://openai-gateway.example.test"
    assert created_clients[1].base_url == "https://openai-gateway.example.test/v1"
    assert len(created_clients[0].responses_calls) == 1
    assert len(created_clients[1].chat_calls) == 1


@pytest.mark.asyncio
async def test_openai_provider_records_502_responses_fallback_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openai

    monkeypatch.setattr(
        "app.llm.providers.openai_provider.wait_exponential",
        lambda **_: wait_none(),
    )
    created_clients: list[_FakeClient] = []

    def _factory(*, api_key: str, base_url: str, max_retries: int = 0):
        _ = api_key
        assert max_retries == 0
        client = _FakeClient(
            base_url=base_url,
            responses_error=_internal_server_error(),
            chat_result=_chat_response(content="fallback recovered"),
        )
        created_clients.append(client)
        return client

    monkeypatch.setattr(openai, "AsyncOpenAI", _factory)

    settings = get_settings().model_copy(
        update={
            "openai_base_url": "https://openai-gateway.example.test",
            "openai_api_mode": "responses",
            "openai_disable_fallback": False,
        }
    )
    provider = OpenAIProvider(api_key="sk-test", settings=settings)

    responses_error_before = _metric_total(
        LLM_API_ATTEMPTS,
        provider="openai",
        api_mode="responses",
        fallback="none",
        result="error",
        error_class="InternalServerError",
    )
    fallback_success_before = _metric_total(
        LLM_API_ATTEMPTS,
        provider="openai",
        api_mode="chat",
        fallback="from_responses",
        result="success",
        error_class="none",
    )

    resp = await provider.chat(_make_request())

    responses_error_after = _metric_total(
        LLM_API_ATTEMPTS,
        provider="openai",
        api_mode="responses",
        fallback="none",
        result="error",
        error_class="InternalServerError",
    )
    fallback_success_after = _metric_total(
        LLM_API_ATTEMPTS,
        provider="openai",
        api_mode="chat",
        fallback="from_responses",
        result="success",
        error_class="none",
    )

    assert resp.content == "fallback recovered"
    assert len(created_clients[0].responses_calls) == 4
    assert len(created_clients[1].chat_calls) == 1
    assert responses_error_after - responses_error_before == pytest.approx(1.0)
    assert fallback_success_after - fallback_success_before == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_openai_provider_caps_required_search_upstream_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openai

    monkeypatch.setattr(
        "app.llm.providers.openai_provider.wait_exponential",
        lambda **_: wait_none(),
    )
    created_clients: list[_FakeClient] = []

    def _factory(*, api_key: str, base_url: str, max_retries: int = 0):
        _ = api_key
        assert max_retries == 0
        client = _FakeClient(
            base_url=base_url,
            responses_error=_internal_server_error(),
        )
        created_clients.append(client)
        return client

    monkeypatch.setattr(openai, "AsyncOpenAI", _factory)

    settings = get_settings().model_copy(
        update={
            "openai_base_url": "https://openai-gateway.example.test",
            "openai_api_mode": "responses",
        }
    )
    provider = OpenAIProvider(api_key="sk-test", settings=settings)
    request = _make_request().model_copy(
        update={
            "metadata": {
                "openai_web_search_required": True,
                "disable_openai_fallback": True,
            }
        }
    )

    with pytest.raises(UpstreamUnavailable):
        await provider.chat(request)

    assert len(created_clients[0].responses_calls) == 2


@pytest.mark.asyncio
async def test_openai_provider_keeps_responses_success_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openai

    created_clients: list[_FakeClient] = []

    def _factory(*, api_key: str, base_url: str, max_retries: int = 0):
        _ = api_key
        assert max_retries == 0
        client = _FakeClient(
            base_url=base_url,
            responses_result=_responses_response(content="responses primary"),
        )
        created_clients.append(client)
        return client

    monkeypatch.setattr(openai, "AsyncOpenAI", _factory)

    settings = get_settings().model_copy(
        update={
            "openai_base_url": "https://openai-gateway.example.test",
            "openai_api_mode": "responses",
            "openai_disable_fallback": False,
        }
    )
    provider = OpenAIProvider(api_key="sk-test", settings=settings)

    resp = await provider.chat(_make_request())

    assert resp.content == "responses primary"
    assert len(created_clients[0].responses_calls) == 1
    assert len(created_clients[1].chat_calls) == 0


@pytest.mark.asyncio
async def test_openai_provider_stream_falls_back_after_generic_responses_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openai

    created_clients: list[_FakeClient] = []

    def _factory(*, api_key: str, base_url: str, max_retries: int = 0):
        _ = api_key
        assert max_retries == 0
        client = _FakeClient(
            base_url=base_url,
            responses_error=RuntimeError("responses stream down"),
            chat_stream=_AsyncItems(
                [
                    SimpleNamespace(
                        choices=[SimpleNamespace(delta=SimpleNamespace(content="chat stream ok"))]
                    )
                ]
            ),
        )
        created_clients.append(client)
        return client

    monkeypatch.setattr(openai, "AsyncOpenAI", _factory)

    settings = get_settings().model_copy(
        update={
            "openai_base_url": "https://openai-gateway.example.test",
            "openai_api_mode": "responses",
            "openai_disable_fallback": False,
        }
    )
    provider = OpenAIProvider(api_key="sk-test", settings=settings)

    chunks = [chunk async for chunk in provider.stream_chat(_make_request())]

    assert chunks == ["chat stream ok"]
    assert len(created_clients[0].responses_calls) == 1
    assert len(created_clients[1].chat_calls) == 1


@pytest.mark.asyncio
async def test_openai_provider_adds_web_search_tool_for_responses_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openai

    created_clients: list[_FakeClient] = []

    def _factory(*, api_key: str, base_url: str, max_retries: int = 0):
        _ = api_key
        assert max_retries == 0
        client = _FakeClient(base_url=base_url)
        created_clients.append(client)
        return client

    monkeypatch.setattr(openai, "AsyncOpenAI", _factory)

    settings = get_settings().model_copy(
        update={
            "openai_api_mode": "responses",
            "openai_web_search_enabled": False,
            "openai_web_search_tool": "web_search",
            "openai_web_search_live_enabled": True,
        }
    )
    provider = OpenAIProvider(api_key="sk-test", settings=settings)
    req = _make_request().model_copy(
        update={
            "metadata": {
                "openai_web_search": True,
                "openai_web_search_allowed_domains": ["example.com"],
            }
        }
    )

    await provider.chat(req)

    call = created_clients[0].responses_calls[0]
    assert call["tools"] == [
        {
            "type": "web_search",
            "filters": {"allowed_domains": ["example.com"]},
            "external_web_access": True,
        }
    ]
    assert "tool_choice" not in call


@pytest.mark.asyncio
async def test_openai_provider_requires_explicit_web_search_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openai

    created_clients: list[_FakeClient] = []

    def _factory(*, api_key: str, base_url: str, max_retries: int = 0):
        _ = api_key
        assert max_retries == 0
        client = _FakeClient(base_url=base_url)
        created_clients.append(client)
        return client

    monkeypatch.setattr(openai, "AsyncOpenAI", _factory)

    settings = get_settings().model_copy(
        update={
            "openai_api_mode": "responses",
            "openai_web_search_enabled": False,
            "openai_web_search_tool": "web_search",
            "openai_web_search_live_enabled": True,
        }
    )
    provider = OpenAIProvider(api_key="sk-test", settings=settings)
    req = _make_request().model_copy(
        update={
            "metadata": {"openai_web_search_required": True},
            "tools": [
                ToolSchema(
                    name="must_not_replace_search",
                    description="local side effect",
                    parameters={"type": "object", "properties": {}},
                )
            ],
        }
    )

    await provider.chat(req)

    call = created_clients[0].responses_calls[0]
    assert call["tools"] == [{"type": "web_search", "external_web_access": True}]
    assert call["tool_choice"] == "required"
    assert call["include"] == ["web_search_call.action.sources"]


def test_openai_provider_skips_sources_include_for_custom_gateway_by_default() -> None:
    settings = get_settings().model_copy(
        update={
            "openai_base_url": "https://gateway.example.test/v1",
            "openai_api_mode": "responses",
            "openai_web_search_tool": "web_search",
            "openai_web_search_live_enabled": True,
        }
    )
    provider = OpenAIProvider(api_key="sk-test", settings=settings)
    request = _make_request().model_copy(update={"metadata": {"openai_web_search_required": True}})

    kwargs = provider._build_responses_kwargs(request)

    assert kwargs["tool_choice"] == "required"
    assert "include" not in kwargs


def test_openai_provider_uses_xai_responses_tool_shapes() -> None:
    settings = get_settings().model_copy(
        update={
            "openai_base_url": "https://api.x.ai/v1",
            "openai_api_mode": "responses",
            "openai_web_search_enabled": True,
            "openai_web_search_tool": "web_search_preview",
            "openai_web_search_live_enabled": True,
        }
    )
    provider = OpenAIProvider(api_key="xai-test", settings=settings)
    request = _make_request().model_copy(
        update={
            "tools": [
                ToolSchema(
                    name="lookup_order",
                    description="Lookup order",
                    parameters={"type": "object", "properties": {}},
                )
            ]
        }
    )

    kwargs = provider._build_responses_kwargs(request)

    assert kwargs["tools"] == [
        {
            "type": "function",
            "name": "lookup_order",
            "description": "Lookup order",
            "parameters": {"type": "object", "properties": {}},
        },
        {"type": "web_search"},
    ]


def test_openai_provider_uses_grok_web_search_shape_for_grok_gateway() -> None:
    settings = get_settings().model_copy(
        update={
            "openai_base_url": "https://sub2api.example.test/v1",
            "grok_models_base_url": "https://sub2api.example.test/v1",
            "xai_api_key": "xai-test",
            "openai_api_mode": "responses",
            "openai_web_search_enabled": True,
            "openai_web_search_tool": "web_search",
            "openai_web_search_live_enabled": True,
        }
    )
    provider = OpenAIProvider(api_key="xai-test", settings=settings)
    request = _make_request().model_copy(
        update={"metadata": {"openai_web_search_required": True}}
    )

    kwargs = provider._build_responses_kwargs(request)

    assert kwargs["tools"] == [{"type": "web_search"}]
    assert kwargs["include"] == ["web_search_call.action.sources"]


def test_openai_provider_uses_standard_xai_chat_tool_messages() -> None:
    settings = get_settings().model_copy(
        update={
            "openai_base_url": "https://api.x.ai/v1",
            "openai_api_mode": "chat",
        }
    )
    provider = OpenAIProvider(api_key="xai-test", settings=settings)
    request = _make_request().model_copy(
        update={
            "messages": [
                ChatMessage(role=Role.USER, content="查一下"),
                ChatMessage(
                    role=Role.TOOL,
                    tool_call_id="call_xai",
                    content='{"ok": true}',
                ),
            ]
        }
    )

    kwargs = provider._build_chat_kwargs(request)

    assert kwargs["messages"][1] == {
        "role": "tool",
        "tool_call_id": "call_xai",
        "content": '{"ok": true}',
    }


@pytest.mark.asyncio
async def test_openai_provider_combines_function_tools_with_web_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openai

    created_clients: list[_FakeClient] = []

    def _factory(*, api_key: str, base_url: str, max_retries: int = 0):
        _ = api_key
        assert max_retries == 0
        client = _FakeClient(base_url=base_url)
        created_clients.append(client)
        return client

    monkeypatch.setattr(openai, "AsyncOpenAI", _factory)

    settings = get_settings().model_copy(
        update={
            "openai_api_mode": "responses",
            "openai_web_search_enabled": True,
            "openai_web_search_tool": "web_search_preview",
        }
    )
    provider = OpenAIProvider(api_key="sk-test", settings=settings)
    req = _make_request().model_copy(
        update={
            "tools": [
                ToolSchema(
                    name="lookup_order",
                    description="Lookup order",
                    parameters={"type": "object", "properties": {}},
                )
            ]
        }
    )

    await provider.chat(req)

    tools = created_clients[0].responses_calls[0]["tools"]
    assert tools[0] == {
        "type": "function",
        "name": "lookup_order",
        "description": "Lookup order",
        "parameters": {"type": "object", "properties": {}},
        "strict": False,
    }
    assert tools[1] == {"type": "web_search_preview"}


@pytest.mark.asyncio
async def test_openai_provider_can_disable_responses_chat_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openai

    created_clients: list[_FakeClient] = []

    def _factory(*, api_key: str, base_url: str, max_retries: int = 0):
        _ = api_key
        assert max_retries == 0
        client = _FakeClient(
            base_url=base_url,
            responses_error=RuntimeError("responses down"),
            chat_result=_chat_response(content="fallback chat"),
        )
        created_clients.append(client)
        return client

    monkeypatch.setattr(openai, "AsyncOpenAI", _factory)

    settings = get_settings().model_copy(
        update={
            "openai_base_url": "https://openai-gateway.example.test",
            "openai_api_mode": "responses",
            "openai_disable_fallback": False,
        }
    )
    provider = OpenAIProvider(api_key="sk-test", settings=settings)

    req = _make_request().model_copy(update={"metadata": {"disable_openai_fallback": True}})

    with pytest.raises(UpstreamUnavailable):
        await provider.chat(req)

    assert len(created_clients) == 2
    assert len(created_clients[0].responses_calls) == 1
    assert len(created_clients[1].chat_calls) == 0


@pytest.mark.asyncio
async def test_openai_provider_can_disable_fallback_globally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openai

    created_clients: list[_FakeClient] = []

    def _factory(*, api_key: str, base_url: str, max_retries: int = 0):
        _ = api_key
        assert max_retries == 0
        client = _FakeClient(
            base_url=base_url,
            responses_error=RuntimeError("responses down"),
            chat_result=_chat_response(content="fallback chat"),
        )
        created_clients.append(client)
        return client

    monkeypatch.setattr(openai, "AsyncOpenAI", _factory)

    settings = get_settings().model_copy(
        update={
            "openai_base_url": "https://openai-gateway.example.test",
            "openai_api_mode": "responses",
            "openai_disable_fallback": True,
        }
    )
    provider = OpenAIProvider(api_key="sk-test", settings=settings)

    with pytest.raises(UpstreamUnavailable):
        await provider.chat(_make_request())

    assert len(created_clients) == 2
    assert len(created_clients[0].responses_calls) == 1
    assert len(created_clients[1].chat_calls) == 0
