import { useCallback, useMemo, useState } from "react";

import { apiRequest, formatJson } from "../../lib/api";
import { useStableIdempotencyKeys } from "../../lib/idempotency";
import type { ConsoleConfig } from "../../state/console-config";
import {
  joinMultivalue,
  splitMultivalue,
  type FAQItem,
  type FAQListResponse,
  type FAQPreviewResponse,
} from "./model";

type FaqControllerOptions = {
  config: ConsoleConfig;
  effectiveSessionId: string;
  setOutput: (value: string) => void;
};

export function useFaqController({ config, effectiveSessionId, setOutput }: FaqControllerOptions) {
  const { keyFor, clear } = useStableIdempotencyKeys();
  const [items, setItems] = useState<FAQItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState("");
  const [variantsText, setVariantsText] = useState("");
  const [tagsText, setTagsText] = useState("");
  const [status, setStatus] = useState("published");
  const [testQuery, setTestQuery] = useState("");
  const [preview, setPreview] = useState<FAQPreviewResponse | null>(null);

  const selected = useMemo(() => items.find((item) => item.id === selectedId) || null, [items, selectedId]);
  const publishedCount = useMemo(
    () => items.filter((item) => (item.status || "published") === "published").length,
    [items],
  );
  const disabledCount = items.length - publishedCount;
  const filteredItems = useMemo(() => {
    const query = search.trim().toLowerCase();
    return items.filter((item) => {
      if (statusFilter && (item.status || "published") !== statusFilter) return false;
      if (!query) return true;
      return [item.question, item.answer, ...(item.variants || []), ...(item.tags || [])]
        .join("\n").toLowerCase().includes(query);
    });
  }, [items, search, statusFilter]);

  const hydrateEditor = useCallback((item: FAQItem | null) => {
    setSelectedId(item?.id ?? null);
    setQuestion(item?.question || "");
    setAnswer(item?.answer || "");
    setVariantsText(joinMultivalue(item?.variants));
    setTagsText(joinMultivalue(item?.tags));
    setStatus(item?.status || "published");
  }, []);

  const load = useCallback(async (preferredId?: number | null) => {
    setLoading(true);
    try {
      const result = await apiRequest<FAQListResponse>(config, "/v1/admin/faqs", {
        auth: true,
        query: { tenant_id: config.tenantId, session_id: effectiveSessionId || undefined },
      });
      const nextItems = result.items || [];
      setItems(nextItems);
      hydrateEditor(
        (preferredId != null && nextItems.find((item) => item.id === preferredId)) || nextItems[0] || null,
      );
      setOutput(formatJson(result));
    } catch (error) {
      setOutput(formatJson({ error: error instanceof Error ? error.message : "查询 FAQ 失败" }));
      setItems([]);
      hydrateEditor(null);
    } finally {
      setLoading(false);
    }
  }, [config, effectiveSessionId, hydrateEditor, setOutput]);

  const save = async () => {
    if (!question.trim() || !answer.trim()) {
      setOutput(formatJson({ error: "FAQ 的问题和答案不能为空" }));
      return;
    }
    const payload = {
      question: question.trim(),
      answer: answer.trim(),
      variants: splitMultivalue(variantsText),
      tags: splitMultivalue(tagsText),
      status,
    };
    try {
      if (selectedId == null) {
        const created = await apiRequest<FAQItem>(config, "/v1/admin/faqs", {
          auth: true,
          init: {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              tenant_id: config.tenantId,
              session_id: effectiveSessionId || undefined,
              question: payload.question,
              answer: payload.answer,
              variants: payload.variants,
              tags: payload.tags,
            }),
          },
        });
        const finalRecord = status === "published" ? created : await apiRequest<FAQItem>(
          config,
          `/v1/admin/faqs/${created.id}`,
          {
            auth: true,
            query: { tenant_id: config.tenantId, session_id: effectiveSessionId || undefined },
            init: {
              method: "PUT",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ status }),
            },
          },
        );
        setOutput(formatJson(finalRecord));
        await load(finalRecord.id);
        return;
      }
      const updated = await apiRequest<FAQItem>(config, `/v1/admin/faqs/${selectedId}`, {
        auth: true,
        query: { tenant_id: config.tenantId, session_id: effectiveSessionId || undefined },
        init: {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        },
      });
      setOutput(formatJson(updated));
      await load(updated.id);
    } catch (error) {
      setOutput(formatJson({ error: error instanceof Error ? error.message : "保存 FAQ 失败" }));
    }
  };

  const remove = async () => {
    if (selectedId == null) return;
    const intent = `knowledge:faq:delete:${config.tenantId}:${effectiveSessionId}:${selectedId}`;
    try {
      const result = await apiRequest(config, `/v1/admin/faqs/${selectedId}`, {
        auth: true,
        query: { tenant_id: config.tenantId, session_id: effectiveSessionId || undefined },
        init: {
          method: "DELETE",
          headers: { "Idempotency-Key": keyFor(intent) },
        },
      });
      clear(intent);
      setOutput(formatJson(result));
      await load(null);
    } catch (error) {
      setOutput(formatJson({ error: error instanceof Error ? error.message : "删除 FAQ 失败" }));
    }
  };

  const toggleStatus = async () => {
    if (selectedId == null) return;
    const nextStatus = status === "published" ? "disabled" : "published";
    try {
      const result = await apiRequest<FAQItem>(config, `/v1/admin/faqs/${selectedId}`, {
        auth: true,
        query: { tenant_id: config.tenantId, session_id: effectiveSessionId || undefined },
        init: {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status: nextStatus }),
        },
      });
      setOutput(formatJson(result));
      setStatus(nextStatus);
      await load(result.id);
    } catch (error) {
      setOutput(formatJson({ error: error instanceof Error ? error.message : "切换 FAQ 状态失败" }));
    }
  };

  const runPreview = async () => {
    if (!testQuery.trim()) {
      setOutput(formatJson({ error: "请输入测试问题" }));
      return;
    }
    try {
      const result = await apiRequest<FAQPreviewResponse>(config, "/v1/admin/faqs/test", {
        auth: true,
        init: {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            tenant_id: config.tenantId,
            session_id: effectiveSessionId || undefined,
            query: testQuery.trim(),
          }),
        },
      });
      setPreview(result);
      setOutput(formatJson(result));
    } catch (error) {
      setPreview(null);
      setOutput(formatJson({ error: error instanceof Error ? error.message : "FAQ 命中测试失败" }));
    }
  };

  return {
    items, loading, search, setSearch, statusFilter, setStatusFilter, selectedId, selected, question,
    setQuestion, answer, setAnswer, variantsText, setVariantsText, tagsText, setTagsText, status,
    setStatus, testQuery, setTestQuery, preview, setPreview, publishedCount, disabledCount, filteredItems,
    hydrateEditor, load, save, remove, toggleStatus, runPreview,
  };
}

export type FaqController = ReturnType<typeof useFaqController>;
