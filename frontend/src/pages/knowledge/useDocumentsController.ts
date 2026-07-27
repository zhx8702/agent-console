import { useCallback, useMemo, useState } from "react";

import { apiRequest, formatJson, parseJsonInput } from "../../lib/api";
import { useStableIdempotencyKeys } from "../../lib/idempotency";
import type { ConsoleConfig } from "../../state/console-config";
import type {
  KnowledgeDoc,
  KnowledgeDocListResponse,
  KnowledgeDocSearchResponse,
} from "./model";

type DocumentsControllerOptions = {
  config: ConsoleConfig;
  effectiveSessionId: string;
  setOutput: (value: string) => void;
};

export function useDocumentsController({ config, effectiveSessionId, setOutput }: DocumentsControllerOptions) {
  const { keyFor, clear } = useStableIdempotencyKeys();
  const [items, setItems] = useState<KnowledgeDoc[]>([]);
  const [loading, setLoading] = useState(false);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [title, setTitle] = useState("");
  const [source, setSource] = useState("manual");
  const [url, setUrl] = useState("");
  const [meta, setMeta] = useState('{"category":"policy"}');
  const [content, setContent] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResult, setSearchResult] = useState<KnowledgeDocSearchResponse | null>(null);
  const [searchLoading, setSearchLoading] = useState(false);

  const selected = useMemo(() => items.find((item) => item.id === selectedId) || null, [items, selectedId]);

  const hydrateEditor = useCallback((item: KnowledgeDoc | null) => {
    if (!item) {
      setSelectedId(null);
      setTitle("");
      setSource("manual");
      setUrl("");
      setMeta('{"category":"policy"}');
      setContent("");
      return;
    }
    setSelectedId(item.id);
    setTitle(item.title || "");
    setSource(item.source || "manual");
    setUrl(item.url || "");
    setMeta(formatJson(item.metadata || {}));
    setContent(item.content || "");
  }, []);

  const load = useCallback(async (preferredId?: number | null) => {
    setLoading(true);
    try {
      const result = await apiRequest<KnowledgeDocListResponse>(config, "/v1/admin/kb/documents", {
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
      setOutput(formatJson({ error: error instanceof Error ? error.message : "查询文档失败" }));
      setItems([]);
      hydrateEditor(null);
    } finally {
      setLoading(false);
    }
  }, [config, effectiveSessionId, hydrateEditor, setOutput]);

  const open = useCallback(async (item: KnowledgeDoc) => {
    hydrateEditor(item);
    try {
      const result = await apiRequest<KnowledgeDoc>(config, `/v1/admin/kb/documents/${item.id}`, {
        auth: true,
        query: { tenant_id: config.tenantId, session_id: effectiveSessionId || undefined },
      });
      hydrateEditor(result);
      setOutput(formatJson(result));
    } catch (error) {
      setOutput(formatJson({ error: error instanceof Error ? error.message : "读取文档正文失败" }));
    }
  }, [config, effectiveSessionId, hydrateEditor, setOutput]);

  const save = async () => {
    if (!title.trim() || !content.trim()) {
      setOutput(formatJson({ error: "文档标题和内容不能为空" }));
      return;
    }
    const payload = {
      tenant_id: config.tenantId,
      session_id: effectiveSessionId || undefined,
      title: title.trim(),
      content: content.trim(),
      source: source.trim() || "manual",
      url: url.trim() || null,
      metadata: parseJsonInput<Record<string, unknown>>(meta, {}),
    };
    try {
      const result = await apiRequest<{ doc_id: number }>(
        config,
        selectedId == null ? "/v1/admin/kb/documents" : `/v1/admin/kb/documents/${selectedId}`,
        {
          auth: true,
          query: selectedId == null
            ? undefined
            : { tenant_id: config.tenantId, session_id: effectiveSessionId || undefined },
          init: {
            method: selectedId == null ? "POST" : "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          },
        },
      );
      setOutput(formatJson(result));
      await load(result.doc_id);
      const refreshed = await apiRequest<KnowledgeDoc>(config, `/v1/admin/kb/documents/${result.doc_id}`, {
        auth: true,
        query: { tenant_id: config.tenantId, session_id: effectiveSessionId || undefined },
      });
      hydrateEditor(refreshed);
    } catch (error) {
      setOutput(formatJson({ error: error instanceof Error ? error.message : "保存文档失败" }));
    }
  };

  const runSearch = async () => {
    if (!searchQuery.trim()) {
      setOutput(formatJson({ error: "请输入文档召回测试问题" }));
      return;
    }
    setSearchLoading(true);
    try {
      const result = await apiRequest<KnowledgeDocSearchResponse>(config, "/v1/admin/kb/documents/search", {
        auth: true,
        init: {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            tenant_id: config.tenantId,
            session_id: effectiveSessionId || undefined,
            query: searchQuery.trim(),
            top_k: 5,
          }),
        },
      });
      setSearchResult(result);
      setOutput(formatJson(result));
    } catch (error) {
      setSearchResult(null);
      setOutput(formatJson({ error: error instanceof Error ? error.message : "文档召回测试失败" }));
    } finally {
      setSearchLoading(false);
    }
  };

  const remove = async () => {
    if (selectedId == null) return;
    const intent = `knowledge:document:delete:${config.tenantId}:${effectiveSessionId}:${selectedId}`;
    try {
      const result = await apiRequest(config, `/v1/admin/kb/documents/${selectedId}`, {
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
      setOutput(formatJson({ error: error instanceof Error ? error.message : "删除文档失败" }));
    }
  };

  return {
    items, loading, selectedId, selected, title, setTitle, source, setSource, url, setUrl, meta, setMeta,
    content, setContent, searchQuery, setSearchQuery, searchResult, setSearchResult, searchLoading,
    hydrateEditor, load, open, save, runSearch, remove,
  };
}

export type DocumentsController = ReturnType<typeof useDocumentsController>;
