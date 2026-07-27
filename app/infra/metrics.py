from __future__ import annotations

from prometheus_client import Counter, Histogram

# Ingress
INBOUND_RECEIVED = Counter(
    "cs_inbound_received_total", "Inbound webhook requests", ["tenant", "channel", "result"]
)
INBOUND_LATENCY = Histogram(
    "cs_inbound_latency_seconds", "Inbound handler latency", ["tenant"]
)

# Orchestrator
E2E_LATENCY = Histogram(
    "cs_e2e_latency_seconds",
    "End-to-end latency per message",
    ["tenant", "route"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
)
ROUTE_DECISIONS = Counter(
    "cs_route_decisions_total", "Route decisions", ["tenant", "route"]
)
PIPELINE_ERRORS = Counter(
    "cs_pipeline_errors_total", "Pipeline errors", ["stage", "code"]
)

# LLM
LLM_REQUESTS = Counter("cs_llm_requests_total", "LLM requests", ["provider", "model", "result"])
LLM_LATENCY = Histogram("cs_llm_latency_seconds", "LLM request latency", ["provider", "model"])
LLM_TOKENS = Counter(
    "cs_llm_tokens_total", "LLM tokens consumed", ["provider", "model", "kind"]
)
LLM_COST_USD = Counter("cs_llm_cost_usd_total", "LLM estimated cost USD", ["tenant", "model"])
LLM_API_ATTEMPTS = Counter(
    "cs_llm_api_attempts_total",
    "LLM provider API attempts",
    ["provider", "api_mode", "fallback", "result", "error_class"],
)
LLM_IMAGE_ATTACHMENT_EVENTS = Counter(
    "cs_llm_image_attachment_events_total",
    "LLM image attachment observations",
    ["source", "image_kind", "result", "reason"],
)

# RAG / FAQ
FAQ_HITS = Counter("cs_faq_hits_total", "FAQ hits", ["tenant", "hit"])
RAG_RETRIEVAL_LATENCY = Histogram("cs_rag_retrieval_seconds", "RAG retrieval latency", ["tenant"])
RAG_RETRIEVAL_RESULTS = Counter(
    "cs_rag_retrieval_results_total", "RAG retrieval outcomes", ["tenant", "result", "scope"]
)
RAG_CITATION_VALIDATION = Counter(
    "cs_rag_citation_validation_total", "RAG citation validation outcomes", ["tenant", "result"]
)
KB_INDEX_OPERATIONS = Counter(
    "cs_kb_index_operations_total", "Knowledge index operations", ["operation", "result"]
)
SESSION_LOCK_EVENTS = Counter(
    "cs_session_lock_events_total", "Distributed session lock events", ["event"]
)
MEMORY_GOVERNANCE_EVENTS = Counter(
    "cs_memory_governance_events_total", "Memory governance actions", ["action", "result"]
)
MEMORY_ACCEPTANCE_DECISIONS = Counter(
    "cs_memory_acceptance_decisions_total", "Memory acceptance decisions", ["status", "source"]
)
WXBOT_REPLY_SUPPRESSED = Counter(
    "cs_wxbot_reply_suppressed_total", "WeChat reply suppressions", ["reason"]
)
WXBOT_REPLY_COALESCE_SECONDS = Histogram(
    "cs_wxbot_reply_coalesce_seconds", "WeChat group reply coalescing delay"
)

# Egress
OUTBOUND_SENT = Counter(
    "cs_outbound_sent_total", "Outbound deliveries", ["tenant", "result"]
)
OUTBOUND_RETRIES = Counter("cs_outbound_retries_total", "Outbound delivery retries", ["tenant"])
DLQ_SIZE = Counter("cs_dlq_total", "Dead-letter events", ["reason"])
