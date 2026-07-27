import { useCallback, useMemo, useState } from "react";

import { apiRequest, formatJson } from "../../lib/api";
import type { ConsoleConfig } from "../../state/console-config";
import {
  extractFaqDrafts,
  parsePastedChatLines,
  splitMultivalue,
  type ChatDraftLine,
  type FAQDraftItem,
  type FAQImportMode,
  type FAQItem,
  type ReportMessagesPayload,
  type WxbotSession,
} from "./model";

type ImportControllerOptions = {
  config: ConsoleConfig;
  effectiveSessionId: string;
  sessions: WxbotSession[];
  selectedFaqId: number | null;
  loadFaqs: (preferredId?: number | null) => Promise<void>;
  setOutput: (value: string) => void;
};

export function useImportController({
  config,
  effectiveSessionId,
  sessions,
  selectedFaqId,
  loadFaqs,
  setOutput,
}: ImportControllerOptions) {
  const [mode, setMode] = useState<FAQImportMode>("paste");
  const [text, setText] = useState("");
  const [reportType, setReportType] = useState<"daily" | "monthly">("daily");
  const [date, setDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [yearMonth, setYearMonth] = useState(() => new Date().toISOString().slice(0, 7));
  const [messages, setMessages] = useState<ReportMessagesPayload | null>(null);
  const [drafts, setDrafts] = useState<FAQDraftItem[]>([]);
  const [loading, setLoading] = useState(false);

  const sourceSessionId = (effectiveSessionId || config.sessionId || "").trim();
  const sourceSession = useMemo(
    () => sessions.find((item) => item.session_id === sourceSessionId) || null,
    [sessions, sourceSessionId],
  );

  const buildDrafts = useCallback((lines: ChatDraftLine[], source: Record<string, unknown>) => {
    const nextDrafts = extractFaqDrafts(lines);
    setDrafts(nextDrafts);
    setOutput(formatJson({
      source,
      extracted_count: nextDrafts.length,
      draft_preview: nextDrafts.slice(0, 3).map((item) => ({
        question: item.question,
        answer: item.answer,
        tags: splitMultivalue(item.tagsText),
      })),
    }));
  }, [setOutput]);

  const generateFromPaste = async () => {
    const lines = parsePastedChatLines(text);
    if (!lines.length) {
      setOutput(formatJson({ error: "请先粘贴聊天记录" }));
      setDrafts([]);
      return;
    }
    buildDrafts(lines, { mode: "paste", line_count: lines.length });
  };

  const generateFromSession = async () => {
    if (!sourceSessionId) {
      setOutput(formatJson({ error: "请先选择一个群或会话，或者在顶部全局目标群中指定 session_id" }));
      return;
    }
    setLoading(true);
    try {
      const result = await apiRequest<ReportMessagesPayload>(
        config,
        `/plugins/wxbot/admin/reports/messages/${encodeURIComponent(sourceSessionId)}`,
        {
          auth: true,
          query: {
            report_type: reportType,
            session_name: sourceSession?.session_name || sourceSessionId,
            date: reportType === "daily" ? date : "",
            year_month: reportType === "monthly" ? yearMonth : "",
          },
        },
      );
      setMessages(result);
      buildDrafts((result.messages || []).map((item) => ({
        senderName: item.sender_name || "",
        text: item.text || "",
        timestamp: item.timestamp || "",
        msgType: item.msg_type || "text",
        isSelfSent: Boolean(item.is_self_sent),
      })), {
        mode: "session",
        session_id: result.session_id,
        session_name: result.session_name,
        report_type: result.report_type,
        period: result.period,
        message_count: result.count,
      });
    } catch (error) {
      setMessages(null);
      setDrafts([]);
      setOutput(formatJson({ error: error instanceof Error ? error.message : "读取聊天记录失败" }));
    } finally {
      setLoading(false);
    }
  };

  const updateDraft = useCallback((draftId: string, patch: Partial<FAQDraftItem>) => {
    setDrafts((current) => current.map((item) => item.draftId === draftId ? { ...item, ...patch } : item));
  }, []);

  const setAllSelection = useCallback((selected: boolean) => {
    setDrafts((current) => current.map((item) => ({ ...item, selected })));
  }, []);

  const importSelected = async () => {
    const selectedDrafts = drafts.filter((item) => item.selected && item.question.trim() && item.answer.trim());
    if (!selectedDrafts.length) {
      setOutput(formatJson({ error: "请至少勾选一条有效草稿" }));
      return;
    }
    setLoading(true);
    const createdIds: number[] = [];
    const failures: Array<{ draft_id: string; question: string; error: string }> = [];
    try {
      for (const draft of selectedDrafts) {
        try {
          const created = await apiRequest<FAQItem>(config, "/v1/admin/faqs", {
            auth: true,
            init: {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                tenant_id: config.tenantId,
                session_id: effectiveSessionId || undefined,
                question: draft.question.trim(),
                answer: draft.answer.trim(),
                variants: splitMultivalue(draft.variantsText),
                tags: splitMultivalue(draft.tagsText),
              }),
            },
          });
          createdIds.push(created.id);
        } catch (error) {
          failures.push({
            draft_id: draft.draftId,
            question: draft.question,
            error: error instanceof Error ? error.message : "导入失败",
          });
        }
      }
      const failedIds = new Set(failures.map((item) => item.draft_id));
      const importedIds = new Set(
        selectedDrafts.filter((item) => !failedIds.has(item.draftId)).map((item) => item.draftId),
      );
      setDrafts((current) => current.filter((item) => !importedIds.has(item.draftId)));
      setOutput(formatJson({
        scope: effectiveSessionId ? "session" : "global",
        session_id: effectiveSessionId || null,
        imported_count: createdIds.length,
        failed_count: failures.length,
        created_ids: createdIds,
        failures,
      }));
      await loadFaqs(createdIds[0] ?? selectedFaqId);
    } finally {
      setLoading(false);
    }
  };

  const clear = () => {
    setText("");
    setMessages(null);
    setDrafts([]);
  };

  return {
    mode, setMode, text, setText, reportType, setReportType, date, setDate, yearMonth, setYearMonth,
    messages, drafts, loading, sourceSessionId, sourceSession, generateFromPaste, generateFromSession,
    updateDraft, setAllSelection, importSelected, clear,
  };
}

export type ImportController = ReturnType<typeof useImportController>;
