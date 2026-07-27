from __future__ import annotations

from dataclasses import dataclass

from app.rag.retriever import HybridRetriever


@dataclass(frozen=True)
class RAGEvaluationCase:
    query: str
    expected_doc_ids: frozenset[int]
    tenant_id: str = "default"
    session_id: str | None = None


@dataclass(frozen=True)
class RAGEvaluationReport:
    cases: int
    recall_at_k: float
    mean_reciprocal_rank: float
    no_result_rate: float
    false_positive_rate: float


async def evaluate_retriever(
    retriever: HybridRetriever,
    cases: list[RAGEvaluationCase],
    *,
    top_k: int = 5,
) -> RAGEvaluationReport:
    if not cases:
        return RAGEvaluationReport(0, 0.0, 0.0, 0.0, 0.0)
    recalled = reciprocal_rank = no_result = false_positive = 0.0
    for case in cases:
        hits = await retriever.retrieve(
            case.tenant_id,
            case.query,
            top_k=top_k,
            session_id=case.session_id,
        )
        returned = [hit.doc_id for hit in hits]
        expected = set(case.expected_doc_ids)
        if not returned:
            no_result += 1
        if expected.intersection(returned):
            recalled += 1
            reciprocal_rank += 1.0 / min(
                index + 1 for index, doc_id in enumerate(returned) if doc_id in expected
            )
        if returned and not expected.intersection(returned):
            false_positive += 1
    count = float(len(cases))
    return RAGEvaluationReport(
        cases=len(cases),
        recall_at_k=recalled / count,
        mean_reciprocal_rank=reciprocal_rank / count,
        no_result_rate=no_result / count,
        false_positive_rate=false_positive / count,
    )
