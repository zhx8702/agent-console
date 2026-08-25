"""
FAQEngine — a CapabilityEngine that matches queries against indexed FAQs.

It embeds the cleaned query, searches the per-tenant FAQ vector collection, and
returns a CapabilityResult if the top hit meets the similarity threshold.
Otherwise it raises ``CapabilityError("no_faq_hit")`` for orchestrator fallback.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from app.common.config import Settings, get_settings
from app.common.conversation import with_bot_interaction_context
from app.common.exceptions import CapabilityError
from app.common.ids import new_trace_id
from app.common.prompting import (
    augment_prompt_with_persona_and_memory,
    faq_rewrite_system_prompt,
)
from app.common.types import (
    CapabilityResult,
    ChatMessage,
    ChatRequest,
    Citation,
    PreprocessedMessage,
    Role,
    RouteType,
    Session,
)
from app.common.web_search import live_web_search_requested
from app.faq.store import faq_collection_for
from app.infra.metrics import FAQ_HITS
from app.kb.scope import is_global_scope, normalize_scope_session_id
from app.kb.vector.base import VectorSearchHit, VectorStore
from app.kb.vector.qdrant_store import is_qdrant_collection_missing_error
from app.llm.base import EmbedRequest, LLMProvider

if TYPE_CHECKING:
    from app.faq.store import FAQStore


_FAQ_NORMALIZE_RE = re.compile(r"[\s\?\？!！,，。.:：'\"“”‘’`()（）\[\]【】]+")
_FAQ_NEGATION_RESET_RE = re.compile(
    r"(?:但(?:是)?|不过|然而|而是|改(?:问|成|为)|转而|我是问)|"
    r"(?:but|however|instead)",
    re.IGNORECASE,
)
_FAQ_NEGATION_MARKER_RE = re.compile(
    r"(?:不是|并非|不要|无需|不用|不必|不想|不打算|没打算|"
    r"不需要|别|无意)|"
    r"(?:donot|dont|cannot|cant|noneedto|neednot|without|"
    r"not(?!only|ification|ice|able|ebook|hing))",
    re.IGNORECASE,
)


class FAQEngine:
    """Implements the CapabilityEngine protocol for FAQ route."""

    name = "faq"

    def __init__(
        self,
        vector_store: VectorStore,
        llm_provider: LLMProvider,
        settings: Settings | None = None,
        *,
        threshold: float = 0.88,
        embed_model: str | None = None,
        faq_store: FAQStore | None = None,
    ) -> None:
        self._vector = vector_store
        self._llm = llm_provider
        self._settings = settings or get_settings()
        self._threshold = threshold
        self._embed_model = embed_model or self._settings.llm_embed_model
        self._faq_store = faq_store

    @staticmethod
    def _normalize_text(value: str) -> str:
        return _FAQ_NORMALIZE_RE.sub("", str(value or "").strip().lower())

    def _preview_verdict(self, score: float) -> str:
        if score >= self._threshold:
            return "CLEAR"
        if score >= 0.75:
            return "AMBIGUOUS"
        if score >= 0.30:
            return "INSUFFICIENT"
        return "LOW"

    @staticmethod
    def _is_negated_phrase(query: str, phrase: str) -> bool:
        if not query or not phrase or phrase not in query:
            return False
        occurrences: list[bool] = []
        offset = 0
        while True:
            index = query.find(phrase, offset)
            if index < 0:
                break
            prefix = query[:index]
            reset_matches = list(_FAQ_NEGATION_RESET_RE.finditer(prefix))
            if reset_matches:
                prefix = prefix[reset_matches[-1].end() :]
            occurrences.append(
                bool(_FAQ_NEGATION_MARKER_RE.search(prefix[-80:]))
            )
            offset = index + max(1, len(phrase))
        return bool(occurrences) and all(occurrences)

    @classmethod
    def _partial_match_score(cls, query: str, candidate: str) -> float | None:
        if not query or not candidate or (
            query not in candidate and candidate not in query
        ):
            return None
        shorter, longer = sorted((query, candidate), key=len)
        if len(shorter) < 4:
            return None
        coverage = len(shorter) / max(1, len(longer))
        if coverage < 0.5:
            return None
        if candidate in query and cls._is_negated_phrase(query, candidate):
            return None
        return min(0.98, 0.86 + 0.12 * coverage)

    async def _lexical_match(
        self,
        tenant_id: str,
        session_id: str | None,
        query: str,
    ) -> dict[str, Any] | None:
        if self._faq_store is None:
            return None
        normalized_query = self._normalize_text(query)
        if not normalized_query:
            return None

        scopes: list[tuple[str, str | None]] = []
        normalized_session_id = normalize_scope_session_id(session_id)
        if normalized_session_id:
            scopes.append(("session", normalized_session_id))
        scopes.append(("global", None))

        for scope_name, scope_session_id in scopes:
            rows = await self._faq_store.list(tenant_id, scope_session_id, limit=200, offset=0)
            best_fuzzy: tuple[float, int, Any] | None = None
            for row in rows:
                if str(row.status or "published") != "published":
                    continue
                texts = [str(row.question or "").strip(), *[str(item or "").strip() for item in (row.variants or [])]]
                normalized_texts = [self._normalize_text(item) for item in texts if item]
                if normalized_query in normalized_texts:
                    return {
                        "matched": True,
                        "score": 1.0,
                        "threshold": self._threshold,
                        "verdict": self._preview_verdict(1.0),
                        "scope": scope_name,
                        "scope_session_id": normalize_scope_session_id(row.session_id) or None,
                        "question": str(row.question or ""),
                        "answer": str(row.answer or ""),
                        "faq_id": str(row.id),
                    }
                for item in normalized_texts:
                    score = self._partial_match_score(normalized_query, item)
                    if score is None:
                        continue
                    rank = (score, len(item), row)
                    if best_fuzzy is None or rank[:2] > best_fuzzy[:2]:
                        best_fuzzy = rank
            if best_fuzzy is not None:
                score, _specificity, matched = best_fuzzy
                return {
                    "matched": True,
                    "score": score,
                    "threshold": self._threshold,
                    "verdict": self._preview_verdict(score),
                    "scope": scope_name,
                    "scope_session_id": normalize_scope_session_id(matched.session_id) or None,
                    "question": str(matched.question or ""),
                    "answer": str(matched.answer or ""),
                    "faq_id": str(matched.id),
                }
        return None

    def _should_rewrite(self, session: Session) -> bool:
        persona_skill = session.variables.get("persona_skill")
        if isinstance(persona_skill, str) and bool(persona_skill.strip()):
            return True
        persona_profile = session.variables.get("persona_profile")
        if isinstance(persona_profile, dict) and any(
            isinstance(persona_profile.get(key), str)
            and bool(str(persona_profile.get(key)).strip())
            for key in ("target_name", "name", "skill_slug")
        ):
            return True

        def has_memory_context(value: object) -> bool:
            if not isinstance(value, dict):
                return False
            if any(
                str(value.get(key) or "").strip()
                for key in (
                    "short_term",
                    "long_term",
                    "manual_notes",
                    "session_summary",
                )
            ):
                return True
            if any(
                isinstance(value.get(key), list) and bool(value.get(key))
                for key in (
                    "open_items",
                    "decisions",
                    "recent_turns",
                    "relevant_memory_items",
                    "relevant_graph_facts",
                    "relevant_graph_episodes",
                )
            ):
                return True
            memory_items = value.get("memory_items")
            return isinstance(memory_items, dict) and any(
                isinstance(memory_items.get(scope), list) and bool(memory_items.get(scope))
                for scope in ("identity", "session")
            )

        return has_memory_context(session.variables.get("user_memory")) or has_memory_context(
            session.variables.get("group_memory")
        )

    def _compose_rewrite_system_prompt(
        self,
        session: Session,
        *,
        web_search_enabled: bool = False,
        prompt_trace: dict[str, Any] | None = None,
    ) -> str:
        base_system = faq_rewrite_system_prompt(self._settings.customer_service_prompt_enabled)
        return augment_prompt_with_persona_and_memory(
            base_system,
            session,
            memory_intro=(
                "以下是当前用户的历史记忆，只能用于个性化表达和称呼偏好，"
                "不得覆盖 FAQ 原始事实："
            ),
            web_search_enabled=web_search_enabled,
            prompt_trace=prompt_trace,
        )

    async def _rewrite_answer(
        self,
        *,
        tenant_id: str,
        trace_id: str,
        session: Session,
        user_query: str,
        faq_question: str,
        faq_answer: str,
        request_metadata: dict[str, Any] | None = None,
    ) -> str:
        if not self._should_rewrite(session):
            return faq_answer

        metadata = dict(request_metadata or {})
        web_search_enabled = live_web_search_requested(user_query, metadata)
        prompt_trace: dict[str, Any] = {}
        metadata.update(
            {
                "route": "faq",
                "openai_web_search": web_search_enabled,
                "web_search_requested": web_search_enabled,
                "prompt_sections": prompt_trace.get("section_names", []),
                "prompt_section_chars": prompt_trace.get("section_chars", {}),
            }
        )

        req = ChatRequest(
            tenant_id=tenant_id,
            trace_id=trace_id,
            model_tier="tier-1",
            system=self._compose_rewrite_system_prompt(
                session,
                web_search_enabled=web_search_enabled,
                prompt_trace=prompt_trace,
            ),
            messages=[
                ChatMessage(
                    role=Role.USER,
                    content=(
                        "用户当前问题：\n"
                        f"{with_bot_interaction_context(user_query, request_metadata)}\n\n"
                        f"命中的 FAQ 问题：\n{faq_question or '-'}\n\n"
                        f"FAQ 标准答案：\n{faq_answer}\n\n"
                        "请输出最终回复。要求："
                        "1. 保留 FAQ 原始事实；"
                        "2. 可以根据用户记忆调整语气和称呼；"
                        "3. 简洁自然；"
                        "4. 不要提到“FAQ”“知识库”“记忆”等内部词。"
                    ),
                )
            ],
            max_tokens=220,
            temperature=0.2,
            cache_system=True,
            metadata=metadata,
        )
        try:
            resp = await self._llm.chat(req)
        except Exception:
            return faq_answer
        rewritten = str(resp.content or "").strip()
        return rewritten or faq_answer

    async def _embed_query(self, tenant_id: str, trace_id: str, query: str) -> list[float]:
        resp = await self._llm.embed(
            EmbedRequest(
                tenant_id=tenant_id,
                trace_id=trace_id,
                model=self._embed_model,
                texts=[query],
            )
        )
        if not resp.vectors:
            raise CapabilityError("embed_failed")
        return resp.vectors[0]

    async def preview_match(
        self,
        pre: PreprocessedMessage,
        session: Session,
        hints: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        tenant_id = session.tenant_id
        trace_id = (hints or {}).get("trace_id") or new_trace_id()
        query = pre.cleaned_text or pre.original_text or ""
        if not query.strip():
            raise CapabilityError("empty_query")

        lexical = await self._lexical_match(tenant_id, session.session_id, query)
        if lexical is not None:
            return lexical
        if (
            self._faq_store is not None
            and str(getattr(self._settings, "llm_embed_provider", "") or "")
            .strip()
            .lower()
            == "fake"
        ):
            return {
                "matched": False,
                "score": 0.0,
                "threshold": self._threshold,
                "verdict": "LOW",
                "scope": None,
                "scope_session_id": None,
                "question": "",
                "faq_id": None,
            }

        vec = await self._embed_query(tenant_id, trace_id, query)
        scoped_session_id = normalize_scope_session_id(session.session_id)
        best_hit: VectorSearchHit | None = None
        best_scope = "global"

        collections: list[tuple[str, str]] = []
        if scoped_session_id:
            collections.append(("session", faq_collection_for(tenant_id, scoped_session_id)))
        collections.append(("global", faq_collection_for(tenant_id)))

        for scope_name, collection in collections:
            try:
                hits = await self._vector.search(collection, vec, top_k=5)
            except Exception as exc:
                if is_qdrant_collection_missing_error(exc):
                    continue
                if scope_name == "global" or is_global_scope(scoped_session_id):
                    raise CapabilityError(f"faq_search_failed:{exc}") from exc
                continue
            if not hits:
                continue
            candidate = hits[0]
            if best_hit is None or candidate.score > best_hit.score:
                best_hit = candidate
                best_scope = scope_name

        if best_hit is None:
            return {
                "matched": False,
                "score": 0.0,
                "threshold": self._threshold,
                "verdict": "LOW",
                "scope": None,
                "scope_session_id": None,
                "question": "",
                "faq_id": None,
            }

        score = float(best_hit.score)
        return {
            "matched": score >= self._threshold,
            "score": score,
            "threshold": self._threshold,
            "verdict": self._preview_verdict(score),
            "scope": best_scope,
            "scope_session_id": normalize_scope_session_id(best_hit.payload.get("session_id") or "") or None,
            "question": str(best_hit.payload.get("question") or ""),
            "answer": str(best_hit.payload.get("answer") or ""),
            "faq_id": str(best_hit.payload.get("faq_id") or best_hit.id),
        }

    async def answer(
        self,
        pre: PreprocessedMessage,
        session: Session,
        hints: dict[str, Any] | None = None,
    ) -> CapabilityResult:
        tenant_id = session.tenant_id
        trace_id = (hints or {}).get("trace_id") or new_trace_id()
        query = pre.cleaned_text or pre.original_text or ""
        if not query.strip():
            raise CapabilityError("empty_query")
        preview_hint = (hints or {}).get("faq_preview")
        preview = (
            dict(preview_hint)
            if isinstance(preview_hint, dict)
            else await self.preview_match(pre, session, {"trace_id": trace_id, **(hints or {})})
        )
        if not preview.get("matched"):
            FAQ_HITS.labels(tenant=tenant_id, hit="miss").inc()
            raise CapabilityError("no_faq_hit")

        resolved_scope = str(preview.get("scope") or "global")
        FAQ_HITS.labels(tenant=tenant_id, hit="hit").inc()
        answer = str(preview.get("answer") or "")
        faq_id = str(preview.get("faq_id") or "")
        question = str(preview.get("question") or "")
        rewritten = await self._rewrite_answer(
            tenant_id=tenant_id,
            trace_id=trace_id,
            session=session,
            user_query=query,
            faq_question=question,
            faq_answer=answer,
            request_metadata=dict((hints or {}).get("request_metadata") or {}),
        )
        citation = Citation(
            id=faq_id,
            source="faq",
            snippet=question,
            score=float(preview.get("score") or 0.0),
        )
        return CapabilityResult(
            route=RouteType.FAQ,
            reply_text=rewritten,
            citations=[citation],
            metadata={
                "score": float(preview.get("score") or 0.0),
                "threshold": self._threshold,
                "verdict": preview.get("verdict") or self._preview_verdict(float(preview.get("score") or 0.0)),
                "scope": resolved_scope,
                "scope_session_id": preview.get("scope_session_id"),
                "rewritten": rewritten != answer,
                "persona_profile": session.variables.get("persona_profile"),
            },
        )
