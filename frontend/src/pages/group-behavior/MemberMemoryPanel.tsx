import { useState } from "react";

import { Alert, DangerAction, EmptyState } from "../../components";
import {
  audienceScopeLabel,
  friendlyErrorMessage,
  formatTime,
  memoryTypeLabel,
  scopeTypeLabel,
  TechnicalDetails,
} from "./presentation";

export type MemberMemoryItem = {
  item_id: number;
  content: string;
  memory_type: string;
  scope_type: "identity" | "session";
  audience_scope: "private" | "session" | "explicit";
  status: string;
  sensitivity_category: string;
  pinned: boolean;
  expires_at: string | null;
  updated_at: string;
  etag: string;
};

type MemberMemoryPanelProps = {
  items: MemberMemoryItem[];
  loading: boolean;
  loadingMore: boolean;
  mutatingId: number | null;
  error: string;
  nextCursor: string | null;
  onRefresh: () => void;
  onLoadMore: () => void;
  onCorrect: (item: MemberMemoryItem, content: string, reason: string) => Promise<void>;
  onDelete: (item: MemberMemoryItem) => Promise<void>;
};

export function MemberMemoryPanel({
  items,
  loading,
  loadingMore,
  mutatingId,
  error,
  nextCursor,
  onRefresh,
  onLoadMore,
  onCorrect,
  onDelete,
}: MemberMemoryPanelProps) {
  const [editingId, setEditingId] = useState<number | null>(null);
  const [content, setContent] = useState("");
  const [reason, setReason] = useState("");

  return (
    <section className="panel span-3" aria-labelledby="member-memory-items-heading">
      <div className="panel-header">
        <div>
          <p className="section-kicker">隐私安全内容</p>
          <h2 id="member-memory-items-heading">当前群获授权的记忆内容</h2>
        </div>
        <button className="button button-secondary" type="button" onClick={onRefresh} disabled={loading || loadingMore || mutatingId !== null}>
          {loading ? "读取中…" : "刷新记忆"}
        </button>
      </div>
      <p className="muted-copy">只展示同时通过租户、群、成员、受众、保留期和敏感级别校验的条目；来源原文和内部定位字段不会返回。</p>
      {error ? (
        <>
          <Alert variant="danger" title="成员记忆读取失败">
            {friendlyErrorMessage(error, "成员记忆读取或操作未完成，请稍后重试。")}
          </Alert>
          <TechnicalDetails
            data={{ error }}
            summary="查看成员记忆错误详情"
            label="成员记忆错误 JSON"
          />
        </>
      ) : null}
      {!loading && !error && !items.length ? (
        <EmptyState compact title="没有可见记忆" description="成员未授权当前群召回、条目已过期，或当前群没有符合受众范围的记忆。" />
      ) : null}
      <div className="stack-list">
        {items.map((item) => {
          const editing = editingId === item.item_id;
          return (
            <article className="panel panel-subtle" key={item.item_id}>
              <div className="panel-header">
                <div>
                  <p className="section-kicker">
                    记忆条目 {item.item_id} · {memoryTypeLabel(item.memory_type)}
                  </p>
                  <h3>{item.content}</h3>
                </div>
                <span className="pill pill-muted">
                  {audienceScopeLabel(item.audience_scope)} · {scopeTypeLabel(item.scope_type)}
                </span>
              </div>
              <p className="muted-copy">更新于 {formatTime(item.updated_at)}{item.expires_at ? ` · 到期 ${formatTime(item.expires_at)}` : ""}{item.pinned ? " · 已固定" : ""}</p>
              {editing ? (
                <div className="form-grid">
                  <label className="field span-2">
                    <span>更正后的记忆</span>
                    <textarea rows={3} maxLength={500} value={content} onChange={(event) => setContent(event.target.value)} />
                  </label>
                  <label className="field">
                    <span>更正原因（不含记忆正文）</span>
                    <input maxLength={240} value={reason} onChange={(event) => setReason(event.target.value)} />
                  </label>
                  <div className="action-row">
                    <button
                      className="button button-primary"
                      type="button"
                      disabled={!content.trim() || mutatingId === item.item_id}
                      onClick={() => void onCorrect(item, content.trim(), reason.trim()).then(() => {
                        setEditingId(null);
                        setContent("");
                        setReason("");
                      })}
                    >
                      {mutatingId === item.item_id ? "保存中…" : "保存更正"}
                    </button>
                    <button className="button button-secondary" type="button" onClick={() => setEditingId(null)}>取消</button>
                  </div>
                </div>
              ) : (
                <div className="action-row">
                  <button className="button button-secondary" type="button" onClick={() => {
                    setEditingId(item.item_id);
                    setContent(item.content);
                    setReason("");
                  }}>更正</button>
                  <DangerAction
                    label="删除"
                    title="确认删除这条成员记忆"
                    impact={<p>该条目将从当前成员的持久记忆中软删除并停止召回。操作按租户、群、成员与版本令牌复核，审计记录不保存正文。</p>}
                    confirmLabel="确认删除"
                    pendingLabel="删除中…"
                    onConfirm={() => onDelete(item)}
                    disabled={mutatingId !== null}
                  />
                </div>
              )}
              <TechnicalDetails
                data={item}
                summary="查看记忆技术详情"
                label={`记忆条目 ${item.item_id} 的完整 JSON`}
              />
            </article>
          );
        })}
      </div>
      {nextCursor ? (
        <div className="action-row">
          <button className="button button-secondary" type="button" onClick={onLoadMore} disabled={loadingMore || mutatingId !== null}>
            {loadingMore ? "加载中…" : "加载更多记忆"}
          </button>
        </div>
      ) : null}
    </section>
  );
}
