import { useMemo } from "react";

import { Alert, DataTable, type DataTableColumn } from "../../components";
import type {
  ParticipationDecisionStatus,
  ParticipationEventDocument,
} from "../../lib/api";
import {
  DECISION_LABELS,
  TechnicalDetails,
  decisionPill,
  deliveryStageLabel,
  friendlyErrorMessage,
  formatTime,
  reasonLabel,
  runtimeStageLabel,
} from "./presentation";

export type ParticipationEventFilters = {
  source: "" | ParticipationEventDocument["event_kind"];
  status: "" | ParticipationDecisionStatus;
  version: string;
  reason: string;
  runtimeStage: string;
  deliveryStage: string;
};

type RuntimeParticipationEvent = ParticipationEventDocument & {
  runtime_stage?: string;
  delivery_stage?: string;
};

type ParticipationEventsPanelProps = {
  events: RuntimeParticipationEvent[];
  loading: boolean;
  loadingMore: boolean;
  error: string;
  nextCursor: string | null;
  filters: ParticipationEventFilters;
  onFiltersChange: (filters: ParticipationEventFilters) => void;
  onRefresh: () => void;
  onLoadMore: () => void;
};

export function ParticipationEventsPanel({
  events,
  loading,
  loadingMore,
  error,
  nextCursor,
  filters,
  onFiltersChange,
  onRefresh,
  onLoadMore,
}: ParticipationEventsPanelProps) {
  const eventColumns = useMemo<DataTableColumn<RuntimeParticipationEvent>[]>(
    () => [
      {
        id: "stage",
        header: "运行 / 投递阶段",
        width: "150px",
        cell: (row) =>
          `${runtimeStageLabel(row.runtime_stage)} / ${deliveryStageLabel(row.delivery_stage)}`,
      },
      {
        id: "time",
        header: "时间",
        width: "132px",
        cell: (row) => formatTime(row.created_at),
      },
      {
        id: "kind",
        header: "来源",
        width: "88px",
        cell: (row) => (row.event_kind === "preview" ? "控制台模拟" : "实际运行"),
      },
      {
        id: "decision",
        header: "决策",
        width: "105px",
        cell: (row) => decisionPill(row.status),
      },
      {
        id: "score",
        header: "得分",
        width: "72px",
        align: "right",
        cell: (row) => row.score,
      },
      {
        id: "version",
        header: "策略版本",
        width: "92px",
        cell: (row) => `v${row.policy_version}`,
      },
      {
        id: "reasons",
        header: "决策依据",
        cell: (row) =>
          row.reason_codes.length
            ? row.reason_codes.map(reasonLabel).join("；")
            : "无额外原因",
      },
      {
        id: "signals",
        header: "结构化信号",
        cell: (row) => {
          const activeSignals = Object.entries(row.signal_summary)
            .filter(([, value]) => value === true || (typeof value === "number" && value > 0))
            .slice(0, 4)
            .map(([key]) => reasonLabel(key));
          return activeSignals.length ? activeSignals.join("、") : "无活跃信号";
        },
      },
      {
        id: "technical-details",
        header: "技术详情",
        width: "130px",
        cell: (row) => (
          <TechnicalDetails
            data={row}
            summary="查看完整记录"
            label={`事件 ${row.event_id} 的完整 JSON`}
          />
        ),
      },
    ],
    [],
  );

  const filtersActive = Boolean(
    filters.source ||
      filters.status ||
      filters.version.trim() ||
      filters.reason ||
      filters.runtimeStage ||
      filters.deliveryStage,
  );

  return (
    <section className="panel" aria-labelledby="participation-events-heading">
      <div className="panel-header">
        <div>
          <p className="section-kicker">可审计事件</p>
          <h2 id="participation-events-heading">参与决策事件</h2>
        </div>
        <button
          className="button button-secondary"
          type="button"
          onClick={onRefresh}
          disabled={loading || loadingMore}
        >
          {loading ? "刷新中…" : "刷新事件"}
        </button>
      </div>
      <p className="muted-copy">
        事件只保存结构化信号、决策、版本和追踪标识，不展示聊天原文。筛选由服务器执行并保持游标范围一致。
      </p>
      <div className="form-grid" aria-label="事件筛选">
        <label className="field">
          <span>事件来源</span>
          <select value={filters.source} onChange={(event) => onFiltersChange({ ...filters, source: event.target.value as ParticipationEventFilters["source"] })}>
            <option value="">全部来源</option>
            <option value="runtime">实际运行</option>
            <option value="preview">控制台模拟</option>
          </select>
        </label>
        <label className="field">
          <span>决策依据</span>
          <select value={filters.reason} onChange={(event) => onFiltersChange({ ...filters, reason: event.target.value })}>
            <option value="">全部原因</option>
            <option value="direct_mention">明确 @ 机器人</option>
            <option value="explicit_command">明确命令</option>
            <option value="quiet_hours">安静时段</option>
            <option value="answered_before_send">发送前已有成员回答</option>
            <option value="rollout_shadow_only">影子评估</option>
          </select>
        </label>
        <label className="field">
          <span>运行阶段</span>
          <select value={filters.runtimeStage} onChange={(event) => onFiltersChange({ ...filters, runtimeStage: event.target.value })}>
            <option value="">全部阶段</option>
            <option value="decision">首次决策</option>
            <option value="revalidation">发送前复核</option>
            <option value="delivery">投递</option>
          </select>
        </label>
        <label className="field">
          <span>投递阶段</span>
          <select value={filters.deliveryStage} onChange={(event) => onFiltersChange({ ...filters, deliveryStage: event.target.value })}>
            <option value="">全部阶段</option>
            <option value="not_applicable">不适用</option>
            <option value="queued">已排队</option>
            <option value="sent">已发送</option>
            <option value="cancelled">已取消</option>
            <option value="failed">失败</option>
          </select>
        </label>
        <label className="field">
          <span>决策结果</span>
          <select
            value={filters.status}
            onChange={(event) => onFiltersChange({ ...filters, status: event.target.value as ParticipationEventFilters["status"] })}
          >
            <option value="">全部结果</option>
            {Object.entries(DECISION_LABELS).map(([value, label]) => (
              <option key={value} value={value}>{label}</option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>策略版本</span>
          <input
            type="number"
            min={0}
            value={filters.version}
            onChange={(event) => onFiltersChange({ ...filters, version: event.target.value })}
            placeholder="全部版本"
          />
        </label>
        <div className="action-row">
          <button
            className="button button-secondary"
            type="button"
            disabled={!filtersActive}
            onClick={() => {
              onFiltersChange({
                source: "",
                status: "",
                version: "",
                reason: "",
                runtimeStage: "",
                deliveryStage: "",
              });
            }}
          >
            清除筛选
          </button>
          <span className="pill pill-muted">当前已加载 {events.length} 条</span>
        </div>
      </div>
      {error ? (
        <>
          <Alert variant="danger" title="事件读取失败">
            {friendlyErrorMessage(error, "参与事件读取未完成，请稍后重试。")}
          </Alert>
          <TechnicalDetails
            data={{ error }}
            summary="查看事件读取错误详情"
            label="参与事件错误 JSON"
          />
        </>
      ) : null}
      <DataTable
        caption="当前群最近 50 条参与决策事件"
        columns={eventColumns}
        rows={events}
        rowKey={(row) => row.event_id}
        emptyMessage={
          loading
            ? "正在读取事件…"
            : filtersActive
              ? "已加载记录中没有符合筛选条件的事件"
              : "当前群还没有参与决策事件"
        }
      />
      <div className="action-row">
        {nextCursor ? (
          <button
            className="button button-secondary"
            type="button"
            onClick={onLoadMore}
            disabled={loading || loadingMore}
          >
            {loadingMore ? "加载中…" : "加载更早事件"}
          </button>
        ) : (
          <span className="pill pill-muted">没有更多已返回事件</span>
        )}
      </div>
    </section>
  );
}
