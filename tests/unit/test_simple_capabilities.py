from __future__ import annotations

from typing import ClassVar

import httpx
import pytest

from app.common.config import Settings
from app.common.context import clear_context, set_trace_id
from app.common.intent import IntentDecision, IntentDomain
from app.common.intent_runtime import persist_decision
from app.common.image_preview import FetchedImage
from app.common.types import Channel, ChatResponse, ChatUsage, Role, Session, Turn
from app.orchestrator.simple_capabilities import LLMCapabilityEngine, parse_at_wxids

from ._fake_llm import make_preprocessed


def _avatar_pre(text: str, *, name: str = ""):
    pre = make_preprocessed(text)
    persist_decision(
        IntentDecision(
            domain=IntentDomain.AVATAR,
            action="analyze",
            confidence=0.95,
            slots={"name": name} if name else {},
        ),
        pre=pre,
    )
    return pre


class _CapturingLLMService:
    def __init__(self) -> None:
        self.last_request = None

    async def chat(self, request):
        self.last_request = request
        return ChatResponse(
            content="好的，我记住了",
            model="fake-chat",
            finish_reason="stop",
            usage=ChatUsage(input_tokens=12, output_tokens=6),
            latency_ms=1,
        )


class _FakeAvatarAsyncClient:
    calls: ClassVar[list[str]] = []
    members: ClassVar[list[dict[str, object]]] = []

    def __init__(self, *args, **kwargs) -> None:
        _ = args, kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        _ = exc_type, exc, tb

    async def get(self, url: str):
        self.calls.append(url)
        return httpx.Response(
            200,
            json={"members": self.members},
            request=httpx.Request("GET", url),
        )


def _avatar_member(
    wxid: str,
    *,
    display_name: str = "",
    nickname: str = "",
    cache_url: str = "",
) -> dict[str, object]:
    return {
        "wxid": wxid,
        "display_name": display_name,
        "nickname": nickname,
        "avatar": {"cache_url": cache_url or f"/ext/roster/avatars/{wxid}"},
    }


def _install_avatar_fakes(
    monkeypatch: pytest.MonkeyPatch,
    members: list[dict[str, object]],
    *,
    expected_avatar_wxid: str = "",
) -> None:
    _FakeAvatarAsyncClient.calls = []
    _FakeAvatarAsyncClient.members = members

    async def _safe_trusted_service_request(
        client,
        method: str,
        base_url: str,
        path: str,
        **kwargs,
    ):
        _ = client, method, kwargs
        url = f"{base_url.rstrip('/')}{path}"
        _FakeAvatarAsyncClient.calls.append(url)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"members": _FakeAvatarAsyncClient.members},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(
        "plugins.draw.avatar.safe_trusted_service_request",
        _safe_trusted_service_request,
    )

    async def _fetch_image(client, url: str, *, max_bytes=None):
        _ = client, max_bytes
        assert url.startswith("http://wxbot-sdk:5080/ext/roster/avatars/")
        if expected_avatar_wxid:
            assert url.endswith(f"/{expected_avatar_wxid}")
        return FetchedImage(url=url, content=b"avatar-bytes", media_type="image/jpeg")

    monkeypatch.setattr(
        "app.orchestrator.simple_capabilities.fetch_image_once",
        _fetch_image,
    )


def test_parse_at_wxids_accepts_list_string_and_json() -> None:
    assert parse_at_wxids(["wxid_bot", "wxid_target", "wxid_target"]) == [
        "wxid_bot",
        "wxid_target",
    ]
    assert parse_at_wxids("wxid_bot, wxid_target") == ["wxid_bot", "wxid_target"]
    assert parse_at_wxids('["wxid_bot", "wxid_target"]') == ["wxid_bot", "wxid_target"]


@pytest.mark.asyncio
async def test_llm_capability_injects_style_and_user_memory_into_system_prompt() -> None:
    llm = _CapturingLLMService()
    engine = LLMCapabilityEngine(llm)
    session = Session(session_id="s1", tenant_id="demo", user_id="u1", channel=Channel.WEB)
    session.variables["persona_skill"] = "请用克制、专业的客服口吻回复。"
    session.variables["user_memory"] = {
        "short_term": "用户刚问过退款进度",
        "long_term": "已知用户事实与偏好：\n- 偏好微信联系",
        "manual_notes": "VIP 客户",
    }

    await engine.answer(make_preprocessed("退款什么时候到"), session)

    assert llm.last_request is not None
    system = llm.last_request.system or ""
    assert "<persona_style_data>" in system
    assert "请用克制、专业的客服口吻回复。" in system
    assert "你就是当前这个人" in system
    assert "短期记忆" in system
    assert "长期记忆" in system


@pytest.mark.asyncio
async def test_llm_capability_can_disable_customer_service_prompt_style() -> None:
    llm = _CapturingLLMService()
    settings = Settings(customer_service_prompt_enabled=False)
    engine = LLMCapabilityEngine(llm, settings=settings)
    session = Session(session_id="s1", tenant_id="demo", user_id="u1", channel=Channel.WEB)

    await engine.answer(make_preprocessed("今天吃什么"), session)

    assert llm.last_request is not None
    assert "客户服务助手" not in (llm.last_request.system or "")
    assert "不要使用客服专用话术" in (llm.last_request.system or "")


@pytest.mark.asyncio
async def test_llm_capability_uses_native_tool_choice_instead_of_keyword_search_rules() -> None:
    llm = _CapturingLLMService()
    settings = Settings(openai_web_search_enabled=True)
    engine = LLMCapabilityEngine(llm, settings=settings)
    session = Session(session_id="s1", tenant_id="demo", user_id="u1", channel=Channel.WEB)

    result = await engine.answer(make_preprocessed("x上搜一下怎么快速搞钱"), session)

    assert llm.last_request is not None
    assert llm.last_request.metadata["openai_web_search"] is True
    assert llm.last_request.metadata["openai_web_search_required"] is False
    assert llm.last_request.metadata["semantic_intent_mode"] == "native_tool_choice"
    assert result.metadata["semantic_intent"] == {
        "operation": "converse",
        "source": "none",
        "artifact": "text",
        "domain": "none",
        "confidence": 0.0,
        "needs_tool": False,
        "query": "x上搜一下怎么快速搞钱",
    }
    assert result.metadata["web_search_used"] is False


@pytest.mark.asyncio
async def test_llm_capability_formats_group_history_with_speaker_and_skips_duplicate_current_turn() -> None:
    llm = _CapturingLLMService()
    engine = LLMCapabilityEngine(llm)
    session = Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_current",
        channel=Channel.WECHAT,
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="三体电视剧拍了一部就不拍了",
            metadata={"sender_name": "泽北", "sender_wxid": "wxid_a"},
        ),
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="@zzz\u2005签到",
            trace_id="trace-current",
            metadata={
                "sender_name": "小石",
                "sender_wxid": "wxid_current",
                "mentioned_me": True,
                "cleaned_content": "签到",
            },
        ),
    ]

    set_trace_id("trace-current")
    try:
        await engine.answer(make_preprocessed("签到"), session)
    finally:
        clear_context()

    assert llm.last_request is not None
    contents = [item.content for item in llm.last_request.messages]
    assert "历史群消息[泽北]：三体电视剧拍了一部就不拍了" in contents
    assert contents[-1] == (
        "当前发言人[小石]（明确 @ 了你；消息里的机器人称呼指你本人）：签到"
    )
    assert "@zzz" not in "\n".join(contents)
    assert "明确 @ 了才是在叫你" in (llm.last_request.system or "")


@pytest.mark.asyncio
async def test_llm_capability_uses_full_session_window_for_group_context() -> None:
    llm = _CapturingLLMService()
    engine = LLMCapabilityEngine(llm, history_turns=8)
    session = Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_current",
        channel=Channel.WECHAT,
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content=f"群消息{index}",
            metadata={"sender_name": f"成员{index}", "sender_wxid": f"wxid_{index}"},
        )
        for index in range(12)
    ]
    session.turns.append(
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="总结一下刚才",
            trace_id="trace-current-window",
            metadata={
                "sender_name": "小石",
                "sender_wxid": "wxid_current",
                "mentioned_me": True,
                "cleaned_content": "总结一下刚才",
            },
        )
    )

    set_trace_id("trace-current-window")
    try:
        await engine.answer(make_preprocessed("总结一下刚才"), session)
    finally:
        clear_context()

    assert llm.last_request is not None
    contents = [item.content for item in llm.last_request.messages]
    assert "历史群消息[成员0]：群消息0" in contents
    assert "历史群消息[成员11]：群消息11" in contents


@pytest.mark.asyncio
async def test_llm_capability_attaches_current_wechat_image_to_llm_request() -> None:
    llm = _CapturingLLMService()
    engine = LLMCapabilityEngine(llm)
    session = Session(
        session_id="wx-private",
        tenant_id="demo",
        user_id="wxid_current",
        channel=Channel.WECHAT,
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="[图片]",
            trace_id="trace-current",
            metadata={
                "image_url": "data:image/png;base64,aW1hZ2U=",
                "image_path": r"C:\Users\Example\AppData\Local\Programs\wx-bot-client\data\images\hash\295.png",
            },
        ),
    ]

    set_trace_id("trace-current")
    try:
        await engine.answer(make_preprocessed("[图片]"), session)
    finally:
        clear_context()

    assert llm.last_request is not None
    last = llm.last_request.messages[-1]
    assert last.content == "[图片]"
    assert len(last.attachments) == 1
    assert last.attachments[0].url == "data:image/png;base64,aW1hZ2U="


@pytest.mark.asyncio
async def test_llm_capability_prefers_preview_image_for_llm_request() -> None:
    llm = _CapturingLLMService()
    engine = LLMCapabilityEngine(llm)
    session = Session(
        session_id="wx-private",
        tenant_id="demo",
        user_id="wxid_current",
        channel=Channel.WECHAT,
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="[图片]",
            trace_id="trace-current",
            metadata={
                "image_url": "data:image/png;base64,dGh1bWI=",
                "image_preview_url": "data:image/png;base64,cHJldmlldw==",
                "image_thumbnail_url": "data:image/png;base64,dGh1bWI=",
            },
        ),
    ]

    set_trace_id("trace-current")
    try:
        await engine.answer(make_preprocessed("[图片]"), session)
    finally:
        clear_context()

    assert llm.last_request is not None
    last = llm.last_request.messages[-1]
    assert len(last.attachments) == 1
    assert last.attachments[0].url == "data:image/png;base64,cHJldmlldw=="


@pytest.mark.asyncio
async def test_llm_capability_prefers_nested_preview_variant_for_llm_request() -> None:
    llm = _CapturingLLMService()
    engine = LLMCapabilityEngine(llm)
    session = Session(
        session_id="wx-private",
        tenant_id="demo",
        user_id="wxid_current",
        channel=Channel.WECHAT,
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="[图片]",
            trace_id="trace-current",
            metadata={
                "image_url": "data:image/png;base64,dGh1bWI=",
                "media": {
                    "image_variants": {
                        "preview": {"image_url": "data:image/png;base64,cHJldmlldw=="},
                        "thumbnail": {"image_url": "data:image/png;base64,dGh1bWI="},
                    }
                },
            },
        ),
    ]

    set_trace_id("trace-current")
    try:
        await engine.answer(make_preprocessed("[图片]"), session)
    finally:
        clear_context()

    assert llm.last_request is not None
    last = llm.last_request.messages[-1]
    assert len(last.attachments) == 1
    assert last.attachments[0].url == "data:image/png;base64,cHJldmlldw=="


@pytest.mark.asyncio
async def test_llm_capability_skips_unreachable_http_wechat_image_url() -> None:
    llm = _CapturingLLMService()
    engine = LLMCapabilityEngine(llm)
    session = Session(
        session_id="wx-private",
        tenant_id="demo",
        user_id="wxid_current",
        channel=Channel.WECHAT,
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="[图片]",
            trace_id="trace-current",
            metadata={
                "image_url": "http://127.0.0.1:9/images/hash-349/349_thumbnail.jpg",
                "media_status": "thumbnail",
            },
        ),
    ]

    set_trace_id("trace-current")
    try:
        await engine.answer(make_preprocessed("[图片]"), session)
    finally:
        clear_context()

    assert llm.last_request is not None
    last = llm.last_request.messages[-1]
    assert last.attachments == []
    observation = llm.last_request.metadata["image_attachment_observation"]
    assert observation["current_image_found"] is True
    assert observation["attachment_count"] == 0
    assert observation["result"] == "skipped"


@pytest.mark.asyncio
async def test_llm_capability_uses_thumbnail_when_preview_fetch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _CapturingLLMService()
    engine = LLMCapabilityEngine(llm)

    async def _fail_preview(*args, **kwargs):
        _ = args, kwargs
        raise httpx.ConnectError("preview unavailable")

    async def _fetch_thumbnail(client, url: str, *, max_bytes=None):
        _ = client, max_bytes
        assert url == "http://127.0.0.1:5080/images/hash-350/350_thumbnail.jpg"
        return FetchedImage(url=url, content=b"thumb", media_type="image/jpeg")

    monkeypatch.setattr(
        "app.orchestrator.simple_capabilities.wait_for_image",
        _fail_preview,
    )
    monkeypatch.setattr(
        "app.orchestrator.simple_capabilities.fetch_image_once",
        _fetch_thumbnail,
    )

    session = Session(
        session_id="wx-private",
        tenant_id="demo",
        user_id="wxid_current",
        channel=Channel.WECHAT,
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="[图片]",
            trace_id="trace-current",
            metadata={
                "image_thumbnail_url": "http://127.0.0.1:5080/images/hash-350/350_thumbnail.jpg",
                "image_url": "http://127.0.0.1:5080/images/hash-350/350_thumbnail.jpg",
            },
        ),
    ]

    set_trace_id("trace-current")
    try:
        await engine.answer(make_preprocessed("[图片]"), session)
    finally:
        clear_context()

    assert llm.last_request is not None
    last = llm.last_request.messages[-1]
    assert len(last.attachments) == 1
    assert last.attachments[0].url == "data:image/jpeg;base64,dGh1bWI="
    observation = llm.last_request.metadata["image_attachment_observation"]
    assert observation["attachment_count"] == 1
    assert observation["data_url_count"] == 1
    assert observation["reason"] == "fallback_converted_data_url"


@pytest.mark.asyncio
async def test_llm_capability_skips_oversized_http_wechat_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _CapturingLLMService()
    engine = LLMCapabilityEngine(llm)

    async def _too_large(client, url: str, *, max_bytes=None):
        _ = client, url, max_bytes
        raise httpx.HTTPError("image response too large")

    monkeypatch.setattr(
        "app.orchestrator.simple_capabilities.fetch_image_once",
        _too_large,
    )

    session = Session(
        session_id="wx-private",
        tenant_id="demo",
        user_id="wxid_current",
        channel=Channel.WECHAT,
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="[图片]",
            trace_id="trace-current",
            metadata={"image_url": "http://127.0.0.1:5080/images/too-large.png"},
        ),
    ]

    set_trace_id("trace-current")
    try:
        await engine.answer(make_preprocessed("[图片]"), session)
    finally:
        clear_context()

    assert llm.last_request is not None
    last = llm.last_request.messages[-1]
    assert last.attachments == []
    observation = llm.last_request.metadata["image_attachment_observation"]
    assert observation["result"] == "skipped"
    assert observation["reason"] == "too_large"


@pytest.mark.asyncio
async def test_llm_capability_attaches_quoted_wechat_image_to_llm_request() -> None:
    llm = _CapturingLLMService()
    engine = LLMCapabilityEngine(llm)
    session = Session(
        session_id="wx-private",
        tenant_id="demo",
        user_id="wxid_current",
        channel=Channel.WECHAT,
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="这张图改成梵高风格",
            trace_id="trace-current",
            metadata={
                "quote_image_url": "data:image/png;base64,cXVvdGVk",
                "quote": {"msg_svr_id": "quoted-image"},
            },
        ),
    ]

    set_trace_id("trace-current")
    try:
        await engine.answer(make_preprocessed("这张图改成梵高风格"), session)
    finally:
        clear_context()

    assert llm.last_request is not None
    last = llm.last_request.messages[-1]
    assert last.content == "这张图改成梵高风格"
    assert len(last.attachments) == 1
    assert last.attachments[0].url == "data:image/png;base64,cXVvdGVk"
    observation = llm.last_request.metadata["image_attachment_observation"]
    assert observation["source"] == "quote"
    assert observation["quote_image_found"] is True
    assert observation["attachment_count"] == 1


@pytest.mark.asyncio
async def test_llm_capability_converts_non_http_quoted_wechat_image_to_data_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")

    llm = _CapturingLLMService()
    settings = Settings(wxbot_sdk_url="http://127.0.0.1:5080")
    engine = LLMCapabilityEngine(llm, settings=settings)

    async def _fetch_image(client, url: str, *, max_bytes=None):
        _ = max_bytes
        assert isinstance(client, httpx.AsyncClient)
        assert client.trust_env is False
        assert url == "http://127.0.0.1:5080/images/hash-601/601.png"
        return FetchedImage(url=url, content=b"quoted", media_type="image/png")

    monkeypatch.setattr(
        "app.orchestrator.simple_capabilities.fetch_image_once",
        _fetch_image,
    )
    session = Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_current",
        channel=Channel.WECHAT,
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="图片有什么内容",
            trace_id="trace-current",
            metadata={
                "quote_image_path": (
                    r"C:\Users\Example\AppData\Local\Programs\wx-bot-client"
                    r"\data\images\hash-601\601.png"
                ),
                "quote": {"msg_svr_id": "quoted-image"},
            },
        ),
    ]

    set_trace_id("trace-current")
    try:
        await engine.answer(make_preprocessed("图片有什么内容"), session)
    finally:
        clear_context()

    assert llm.last_request is not None
    last = llm.last_request.messages[-1]
    assert len(last.attachments) == 1
    assert last.attachments[0].url == "data:image/png;base64,cXVvdGVk"
    observation = llm.last_request.metadata["image_attachment_observation"]
    assert observation["source"] == "quote"
    assert observation["quote_image_found"] is True
    assert observation["attachment_count"] == 1
    assert observation["data_url_count"] == 1
    assert observation["reason"] == "converted_data_url"


@pytest.mark.asyncio
async def test_llm_capability_quoted_wechat_image_falls_back_to_path_after_url_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _CapturingLLMService()
    settings = Settings(wxbot_sdk_url="http://wxbot-sdk:5080")
    engine = LLMCapabilityEngine(llm, settings=settings)
    fetched_urls: list[str] = []

    async def _fail_url(*args, **kwargs):
        _ = args, kwargs
        raise httpx.ConnectError("quoted url unavailable")

    async def _fetch_image(client, url: str, *, max_bytes=None):
        _ = client, max_bytes
        fetched_urls.append(url)
        assert url == "http://wxbot-sdk:5080/images/hash-601/601.png"
        return FetchedImage(url=url, content=b"quoted", media_type="image/png")

    monkeypatch.setattr(
        "app.orchestrator.simple_capabilities.wait_for_image",
        _fail_url,
    )
    monkeypatch.setattr(
        "app.orchestrator.simple_capabilities.fetch_image_once",
        _fetch_image,
    )
    session = Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_current",
        channel=Channel.WECHAT,
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="图片有什么内容",
            trace_id="trace-current",
            metadata={
                "quote_image_url": "http://127.0.0.1:5080/images/hash-601/unreachable.png",
                "quote_image_path": (
                    r"C:\Users\Example\AppData\Local\Programs\wx-bot-client"
                    r"\data\images\hash-601\601.png"
                ),
                "quote": {"msg_svr_id": "quoted-image"},
            },
        ),
    ]

    set_trace_id("trace-current")
    try:
        await engine.answer(make_preprocessed("图片有什么内容"), session)
    finally:
        clear_context()

    assert llm.last_request is not None
    last = llm.last_request.messages[-1]
    assert len(last.attachments) == 1
    assert last.attachments[0].url == "data:image/png;base64,cXVvdGVk"
    assert fetched_urls == ["http://wxbot-sdk:5080/images/hash-601/601.png"]
    observation = llm.last_request.metadata["image_attachment_observation"]
    assert observation["source"] == "quote"
    assert observation["quote_image_found"] is True
    assert observation["attachment_count"] == 1
    assert observation["data_url_count"] == 1
    assert observation["reason"] == "fallback_converted_data_url"


@pytest.mark.asyncio
async def test_llm_capability_rewrites_loopback_quoted_wechat_image_url_to_sdk_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _CapturingLLMService()
    settings = Settings(wxbot_sdk_url="http://wxbot-sdk:5080")
    engine = LLMCapabilityEngine(llm, settings=settings)

    async def _fetch_image(client, url: str, *, max_bytes=None):
        _ = client, max_bytes
        assert url == "http://wxbot-sdk:5080/images/hash-602/602.png"
        return FetchedImage(url=url, content=b"quoted", media_type="image/png")

    monkeypatch.setattr(
        "app.orchestrator.simple_capabilities.fetch_image_once",
        _fetch_image,
    )
    session = Session(
        session_id="wx-private",
        tenant_id="demo",
        user_id="wxid_current",
        channel=Channel.WECHAT,
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="图片有什么内容",
            trace_id="trace-current",
            metadata={
                "quote_image_url": "http://127.0.0.1:5080/images/hash-602/602.png",
                "quote": {"msg_svr_id": "quoted-image"},
            },
        ),
    ]

    set_trace_id("trace-current")
    try:
        await engine.answer(make_preprocessed("图片有什么内容"), session)
    finally:
        clear_context()

    assert llm.last_request is not None
    last = llm.last_request.messages[-1]
    assert len(last.attachments) == 1
    assert last.attachments[0].url == "data:image/png;base64,cXVvdGVk"
    observation = llm.last_request.metadata["image_attachment_observation"]
    assert observation["source"] == "quote"
    assert observation["quote_image_found"] is True
    assert observation["attachment_count"] == 1
    assert observation["reason"] == "converted_data_url"


@pytest.mark.asyncio
async def test_llm_capability_quoted_wechat_image_fetch_failure_keeps_quote_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _CapturingLLMService()
    settings = Settings(wxbot_sdk_url="http://wxbot-sdk:5080")
    engine = LLMCapabilityEngine(llm, settings=settings)

    async def _fetch_image(client, url: str, *, max_bytes=None):
        _ = client, url, max_bytes
        raise httpx.ConnectError("quoted image unavailable")

    monkeypatch.setattr(
        "app.orchestrator.simple_capabilities.fetch_image_once",
        _fetch_image,
    )
    session = Session(
        session_id="wx-private",
        tenant_id="demo",
        user_id="wxid_current",
        channel=Channel.WECHAT,
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="图片有什么内容",
            trace_id="trace-current",
            metadata={
                "quote_image_url": "http://127.0.0.1:5080/images/hash-603/603.png",
                "quote": {"msg_svr_id": "quoted-image"},
            },
        ),
    ]

    set_trace_id("trace-current")
    try:
        await engine.answer(make_preprocessed("图片有什么内容"), session)
    finally:
        clear_context()

    assert llm.last_request is not None
    last = llm.last_request.messages[-1]
    assert last.attachments == []
    observation = llm.last_request.metadata["image_attachment_observation"]
    assert observation["source"] == "quote"
    assert observation["quote_image_found"] is True
    assert observation["attachment_count"] == 0
    assert observation["result"] == "skipped"
    assert observation["reason"] == "fetch_failed"


@pytest.mark.asyncio
async def test_llm_capability_direct_quote_media_still_works() -> None:
    llm = _CapturingLLMService()
    engine = LLMCapabilityEngine(llm)
    session = Session(
        session_id="wx-private",
        tenant_id="demo",
        user_id="wxid_current",
        channel=Channel.WECHAT,
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="这张图是什么",
            trace_id="trace-current",
            metadata={
                "quote": {
                    "msg_svr_id": "quoted-image",
                    "image_url": "data:image/png;base64,ZGlyZWN0",
                },
            },
        ),
    ]

    set_trace_id("trace-current")
    try:
        await engine.answer(make_preprocessed("这张图是什么"), session)
    finally:
        clear_context()

    assert llm.last_request is not None
    last = llm.last_request.messages[-1]
    assert len(last.attachments) == 1
    assert last.attachments[0].url == "data:image/png;base64,ZGlyZWN0"
    observation = llm.last_request.metadata["image_attachment_observation"]
    assert observation["source"] == "quote"
    assert observation["quote_image_found"] is True
    assert observation["attachment_count"] == 1


@pytest.mark.asyncio
async def test_llm_capability_resolves_referenced_turn_image_same_session() -> None:
    llm = _CapturingLLMService()
    engine = LLMCapabilityEngine(llm)
    session = Session(
        session_id="wx-private",
        tenant_id="demo",
        user_id="wxid_current",
        channel=Channel.WECHAT,
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="[图片]",
            metadata={
                "msg_svr_id": "refer-image",
                "image_url": "data:image/png;base64,cmVmZXI=",
            },
        ),
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="图片有什么内容",
            trace_id="trace-current",
            metadata={
                "quote": {
                    "msg_svr_id": "quote-wrapper",
                    "refer_msg_svr_id": "refer-image",
                    "type": "text",
                },
            },
        ),
    ]

    set_trace_id("trace-current")
    try:
        await engine.answer(make_preprocessed("图片有什么内容"), session)
    finally:
        clear_context()

    assert llm.last_request is not None
    last = llm.last_request.messages[-1]
    assert len(last.attachments) == 1
    assert last.attachments[0].url == "data:image/png;base64,cmVmZXI="
    observation = llm.last_request.metadata["image_attachment_observation"]
    assert observation["source"] == "quote"
    assert observation["quote_image_found"] is True
    assert observation["attachment_count"] == 1


@pytest.mark.asyncio
async def test_llm_capability_does_not_resolve_cross_session_quote_reference() -> None:
    llm = _CapturingLLMService()
    engine = LLMCapabilityEngine(llm)
    session = Session(
        session_id="wx-private",
        tenant_id="demo",
        user_id="wxid_current",
        channel=Channel.WECHAT,
    )
    session.turns = [
        Turn(
            session_id="other-session",
            role=Role.USER,
            content="[图片]",
            metadata={
                "msg_svr_id": "refer-image",
                "image_url": "data:image/png;base64,b3RoZXI=",
            },
        ),
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="图片有什么内容",
            trace_id="trace-current",
            metadata={"quote": {"refer_msg_svr_id": "refer-image", "type": "text"}},
        ),
    ]

    set_trace_id("trace-current")
    try:
        await engine.answer(make_preprocessed("图片有什么内容"), session)
    finally:
        clear_context()

    assert llm.last_request is not None
    last = llm.last_request.messages[-1]
    assert last.attachments == []
    observation = llm.last_request.metadata["image_attachment_observation"]
    assert observation["source"] == "none"
    assert observation["quote_image_found"] is False
    assert observation["attachment_count"] == 0


@pytest.mark.asyncio
async def test_llm_capability_resolves_nested_quoted_image_from_referenced_turn() -> None:
    llm = _CapturingLLMService()
    engine = LLMCapabilityEngine(llm)
    session = Session(
        session_id="wx-private",
        tenant_id="demo",
        user_id="wxid_current",
        channel=Channel.WECHAT,
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="这张图改成梵高风格",
            metadata={
                "msg_svr_id": "refer-text",
                "quote_image_url": "data:image/png;base64,bmVzdGVk",
            },
        ),
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="图片有什么内容",
            trace_id="trace-current",
            metadata={"quote": {"refer_msg_svr_id": "refer-text", "type": "text"}},
        ),
    ]

    set_trace_id("trace-current")
    try:
        await engine.answer(make_preprocessed("图片有什么内容"), session)
    finally:
        clear_context()

    assert llm.last_request is not None
    last = llm.last_request.messages[-1]
    assert len(last.attachments) == 1
    assert last.attachments[0].url == "data:image/png;base64,bmVzdGVk"
    observation = llm.last_request.metadata["image_attachment_observation"]
    assert observation["source"] == "quote"
    assert observation["quote_image_found"] is True
    assert observation["attachment_count"] == 1


@pytest.mark.asyncio
async def test_llm_capability_attaches_mentioned_avatar_by_at_wxid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_avatar_fakes(
        monkeypatch,
        [_avatar_member("wxid_target", display_name="千雨")],
    )
    llm = _CapturingLLMService()
    settings = Settings(wxbot_sdk_url="http://wxbot-sdk:5080")
    engine = LLMCapabilityEngine(llm, settings=settings)
    session = Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_sender",
        channel=Channel.WECHAT,
        metadata={"self_wxid": "wxid_bot"},
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="@zzz @千雨 帮我分析他的头像",
            trace_id="trace-current",
            metadata={
                "mentioned_me": True,
                "at_wxids": ["wxid_bot", "wxid_target"],
                "sender_wxid": "wxid_sender",
                "wxbot_normalized_content": "@千雨 帮我分析他的头像",
            },
        ),
    ]

    set_trace_id("trace-current")
    try:
        await engine.answer(_avatar_pre("@千雨 帮我分析他的头像"), session)
    finally:
        clear_context()

    assert llm.last_request is not None
    last = llm.last_request.messages[-1]
    assert len(last.attachments) == 1
    assert last.attachments[0].url == "data:image/jpeg;base64,YXZhdGFyLWJ5dGVz"
    assert "已附图：被 @ 成员的微信头像" in last.content
    observation = llm.last_request.metadata["image_attachment_observation"]
    assert observation["source"] == "mentioned_avatar"
    assert observation["mentioned_avatar_found"] is True
    assert observation["target_resolved"] is True
    assert observation["attachment_count"] == 1
    assert "http://" not in str(observation)


@pytest.mark.asyncio
async def test_llm_capability_attaches_ordered_second_mention_without_bot_wxid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_avatar_fakes(
        monkeypatch,
        [
            _avatar_member("wxid_bot_unknown", display_name="机器人"),
            _avatar_member("wxid_target", display_name="千雨"),
        ],
        expected_avatar_wxid="wxid_target",
    )
    llm = _CapturingLLMService()
    settings = Settings(wxbot_sdk_url="http://wxbot-sdk:5080")
    engine = LLMCapabilityEngine(llm, settings=settings)
    session = Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_sender",
        channel=Channel.WECHAT,
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="@机器人 @千雨 帮我分析他的头像",
            trace_id="trace-current",
            metadata={
                "mentioned_me": True,
                "at_wxids": ["wxid_bot_unknown", "wxid_target"],
                "sender_wxid": "wxid_sender",
                "wxbot_normalized_content": "@千雨 帮我分析他的头像",
                "wxbot_original_content": "@机器人 @千雨 帮我分析他的头像",
            },
        ),
    ]

    set_trace_id("trace-current")
    try:
        await engine.answer(_avatar_pre("@千雨 帮我分析他的头像"), session)
    finally:
        clear_context()

    assert llm.last_request is not None
    last = llm.last_request.messages[-1]
    assert len(last.attachments) == 1
    assert last.attachments[0].url == "data:image/jpeg;base64,YXZhdGFyLWJ5dGVz"
    assert "已附图：被 @ 成员的微信头像" in last.content
    observation = llm.last_request.metadata["image_attachment_observation"]
    assert observation["source"] == "mentioned_avatar"
    assert observation["mentioned_avatar_found"] is True
    assert observation["target_resolved"] is True
    assert observation["attachment_count"] == 1
    assert "http://" not in str(observation)
    assert any("/members" in url for url in _FakeAvatarAsyncClient.calls)


@pytest.mark.asyncio
async def test_llm_capability_mentioned_avatar_falls_back_to_group_display_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_avatar_fakes(
        monkeypatch,
        [_avatar_member("wxid_target", display_name="千雨")],
    )
    llm = _CapturingLLMService()
    engine = LLMCapabilityEngine(
        llm,
        settings=Settings(wxbot_sdk_url="http://wxbot-sdk:5080"),
    )
    session = Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_sender",
        channel=Channel.WECHAT,
        metadata={"self_wxid": "wxid_bot"},
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="@zzz 看下千雨的头像",
            trace_id="trace-current",
            metadata={
                "mentioned_me": True,
                "at_wxids": '["wxid_bot"]',
                "wxbot_normalized_content": "看下千雨的头像",
            },
        ),
    ]

    set_trace_id("trace-current")
    try:
        await engine.answer(_avatar_pre("看下千雨的头像", name="千雨"), session)
    finally:
        clear_context()

    assert llm.last_request is not None
    last = llm.last_request.messages[-1]
    assert len(last.attachments) == 1
    observation = llm.last_request.metadata["image_attachment_observation"]
    assert observation["source"] == "mentioned_avatar"
    assert observation["target_resolved"] is True


@pytest.mark.asyncio
async def test_llm_capability_mentioned_avatar_falls_back_to_name_when_id_not_in_roster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_avatar_fakes(
        monkeypatch,
        [_avatar_member("wxid_actual", display_name="千雨")],
    )
    llm = _CapturingLLMService()
    engine = LLMCapabilityEngine(
        llm,
        settings=Settings(wxbot_sdk_url="http://wxbot-sdk:5080"),
    )
    session = Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_sender",
        channel=Channel.WECHAT,
        metadata={"self_wxid": "wxid_bot"},
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="@zzz @千雨 看下他的头像",
            trace_id="trace-current",
            metadata={
                "mentioned_me": True,
                "at_wxids": ["wxid_bot", "wxid_missing"],
                "wxbot_normalized_content": "@千雨 看下他的头像",
            },
        ),
    ]

    set_trace_id("trace-current")
    try:
        await engine.answer(_avatar_pre("@千雨 看下他的头像"), session)
    finally:
        clear_context()

    assert llm.last_request is not None
    last = llm.last_request.messages[-1]
    assert len(last.attachments) == 1
    observation = llm.last_request.metadata["image_attachment_observation"]
    assert observation["source"] == "mentioned_avatar"
    assert observation["target_resolved"] is True


@pytest.mark.asyncio
async def test_llm_capability_mentioned_avatar_falls_back_to_personal_nickname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_avatar_fakes(
        monkeypatch,
        [_avatar_member("wxid_target", nickname="阿棋")],
    )
    llm = _CapturingLLMService()
    engine = LLMCapabilityEngine(
        llm,
        settings=Settings(wxbot_sdk_url="http://wxbot-sdk:5080"),
    )
    session = Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_sender",
        channel=Channel.WECHAT,
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="@zzz 阿棋的头像是谁",
            trace_id="trace-current",
            metadata={
                "mentioned_me": True,
                "at_wxids": "wxid_bot",
                "wxbot_normalized_content": "阿棋的头像是谁",
            },
        ),
    ]

    set_trace_id("trace-current")
    try:
        await engine.answer(_avatar_pre("阿棋的头像是谁", name="阿棋"), session)
    finally:
        clear_context()

    assert llm.last_request is not None
    last = llm.last_request.messages[-1]
    assert len(last.attachments) == 1
    observation = llm.last_request.metadata["image_attachment_observation"]
    assert observation["source"] == "mentioned_avatar"
    assert observation["attachment_count"] == 1


@pytest.mark.asyncio
async def test_llm_capability_current_image_priority_wins_over_mentioned_avatar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_avatar_fakes(
        monkeypatch,
        [_avatar_member("wxid_target", display_name="千雨")],
    )
    llm = _CapturingLLMService()
    engine = LLMCapabilityEngine(
        llm,
        settings=Settings(wxbot_sdk_url="http://wxbot-sdk:5080"),
    )
    session = Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_sender",
        channel=Channel.WECHAT,
        metadata={"self_wxid": "wxid_bot"},
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="@zzz @千雨 帮我分析他的头像",
            trace_id="trace-current",
            metadata={
                "mentioned_me": True,
                "at_wxids": ["wxid_bot", "wxid_target"],
                "image_url": "data:image/png;base64,Y3VycmVudA==",
            },
        ),
    ]

    set_trace_id("trace-current")
    try:
        await engine.answer(_avatar_pre("@千雨 帮我分析他的头像"), session)
    finally:
        clear_context()

    assert llm.last_request is not None
    last = llm.last_request.messages[-1]
    assert len(last.attachments) == 1
    assert last.attachments[0].url == "data:image/png;base64,Y3VycmVudA=="
    observation = llm.last_request.metadata["image_attachment_observation"]
    assert observation["source"] == "current"
    assert observation["mentioned_avatar_found"] is False
    assert _FakeAvatarAsyncClient.calls == []


@pytest.mark.asyncio
async def test_llm_capability_quote_image_priority_wins_over_mentioned_avatar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_avatar_fakes(
        monkeypatch,
        [_avatar_member("wxid_target", display_name="千雨")],
    )
    llm = _CapturingLLMService()
    engine = LLMCapabilityEngine(
        llm,
        settings=Settings(wxbot_sdk_url="http://wxbot-sdk:5080"),
    )
    session = Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_sender",
        channel=Channel.WECHAT,
        metadata={"self_wxid": "wxid_bot"},
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="@zzz @千雨 帮我分析他的头像",
            trace_id="trace-current",
            metadata={
                "mentioned_me": True,
                "at_wxids": ["wxid_bot", "wxid_target"],
                "quote_image_url": "data:image/png;base64,cXVvdGU=",
                "quote": {"msg_svr_id": "quoted-image"},
            },
        ),
    ]

    set_trace_id("trace-current")
    try:
        await engine.answer(_avatar_pre("@千雨 帮我分析他的头像"), session)
    finally:
        clear_context()

    assert llm.last_request is not None
    last = llm.last_request.messages[-1]
    assert len(last.attachments) == 1
    assert last.attachments[0].url == "data:image/png;base64,cXVvdGU="
    observation = llm.last_request.metadata["image_attachment_observation"]
    assert observation["source"] == "quote"
    assert observation["mentioned_avatar_found"] is False
    assert _FakeAvatarAsyncClient.calls == []


@pytest.mark.asyncio
async def test_llm_capability_skips_ambiguous_multiple_non_bot_mentions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_avatar_fakes(
        monkeypatch,
        [
            _avatar_member("wxid_a", display_name="甲"),
            _avatar_member("wxid_b", display_name="乙"),
        ],
    )
    llm = _CapturingLLMService()
    engine = LLMCapabilityEngine(
        llm,
        settings=Settings(wxbot_sdk_url="http://wxbot-sdk:5080"),
    )
    session = Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_sender",
        channel=Channel.WECHAT,
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="@zzz @甲 @乙 帮我分析他的头像",
            trace_id="trace-current",
            metadata={
                "mentioned_me": True,
                "at_wxids": ["wxid_bot", "wxid_a", "wxid_b"],
            },
        ),
    ]

    set_trace_id("trace-current")
    try:
        await engine.answer(_avatar_pre("@甲 @乙 帮我分析他的头像"), session)
    finally:
        clear_context()

    assert llm.last_request is not None
    last = llm.last_request.messages[-1]
    assert last.attachments == []
    observation = llm.last_request.metadata["image_attachment_observation"]
    assert observation["source"] == "mentioned_avatar"
    assert observation["mentioned_avatar_found"] is False
    assert observation["target_resolved"] is False
    assert observation["attachment_count"] == 0
    assert observation["reason"] == "ambiguous_mentions"
    assert _FakeAvatarAsyncClient.calls == []


@pytest.mark.asyncio
async def test_llm_capability_records_no_avatar_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_avatar_fakes(
        monkeypatch,
        [{"wxid": "wxid_target", "display_name": "千雨", "avatar": {}}],
    )
    llm = _CapturingLLMService()
    engine = LLMCapabilityEngine(
        llm,
        settings=Settings(wxbot_sdk_url="http://wxbot-sdk:5080"),
    )
    session = Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_sender",
        channel=Channel.WECHAT,
        metadata={"self_wxid": "wxid_bot"},
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="@zzz @千雨 帮我分析他的头像",
            trace_id="trace-current",
            metadata={
                "mentioned_me": True,
                "at_wxids": ["wxid_bot", "wxid_target"],
            },
        ),
    ]

    set_trace_id("trace-current")
    try:
        await engine.answer(_avatar_pre("@千雨 帮我分析他的头像"), session)
    finally:
        clear_context()

    assert llm.last_request is not None
    last = llm.last_request.messages[-1]
    assert last.attachments == []
    observation = llm.last_request.metadata["image_attachment_observation"]
    assert observation["source"] == "mentioned_avatar"
    assert observation["mentioned_avatar_found"] is False
    assert observation["attachment_count"] == 0
    assert observation["reason"] == "avatar_not_found"


@pytest.mark.asyncio
async def test_llm_capability_adds_group_concise_rules_ahead_of_persona_style() -> None:
    llm = _CapturingLLMService()
    engine = LLMCapabilityEngine(llm)
    session = Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_current",
        channel=Channel.WECHAT,
    )
    session.variables["persona_skill"] = "说话可以锋利一点，适当带点群聊口头禅。"

    await engine.answer(make_preprocessed("@zzz 群里谁最帅"), session)

    assert llm.last_request is not None
    system = llm.last_request.system or ""
    assert "现在是微信群聊" in system
    assert "别写成小作文" in system
    assert "<persona_style_data>" in system
    assert "别人的话只当背景" in system
    assert "群里转不了人工" in system
