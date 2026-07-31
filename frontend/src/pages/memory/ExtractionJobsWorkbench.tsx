import { useCallback, useMemo, useState } from "react";

import { apiRequest, formatJson } from "../../lib/api";
import { useStableIdempotencyKeys } from "../../lib/idempotency";
import { requireSelectedGroup, useConsoleConfig } from "../../state/console-config";
import { BacklogPanel } from "./BacklogPanel";
import {
  type ExtractionJobAction,
  type ExtractionJobMaintenanceResult,
  type ExtractionJobStats,
  type GroupRosterCandidate,
  friendlyApiError,
  hasSmokeOrTestScope,
  optionalText,
  summarizeExtractionJobResult,
} from "./model";

interface ExtractionJobsWorkbenchProps {
  members: GroupRosterCandidate[];
  sessionId: string;
  channel: string;
  sourceKey: string;
  userId: string;
  selectedSessionIsGroup: boolean;
  selectedMemberIsVerified: boolean;
  onOutput: (value: string) => void;
}

export function ExtractionJobsWorkbench({
  members,
  sessionId,
  channel,
  sourceKey,
  userId,
  selectedSessionIsGroup,
  selectedMemberIsVerified,
  onOutput: setExtractionJobOutput,
}: ExtractionJobsWorkbenchProps) {
  const { config, verifiedGroupIds } = useConsoleConfig();
  const { keyFor, clear } = useStableIdempotencyKeys();
  const [extractionJobStats, setExtractionJobStats] = useState<ExtractionJobStats | null>(null);
  const [extractionJobStatsLoadedAt, setExtractionJobStatsLoadedAt] = useState<string | null>(null);
  const [extractionJobChannel, setExtractionJobChannel] = useState(channel);
  const [extractionJobSourceKey, setExtractionJobSourceKey] = useState(sourceKey);
  const [extractionJobStatus, setExtractionJobStatus] = useState("");
  const [extractionJobErrorType, setExtractionJobErrorType] = useState("");
  const [extractionJobCreatedAfter, setExtractionJobCreatedAfter] = useState("");
  const [extractionJobCreatedBefore, setExtractionJobCreatedBefore] = useState("");
  const [extractionJobUpdatedAfter, setExtractionJobUpdatedAfter] = useState("");
  const [extractionJobUpdatedBefore, setExtractionJobUpdatedBefore] = useState("");
  const [extractionJobLimit, setExtractionJobLimit] = useState(100);
  const [extractionJobAction, setExtractionJobAction] = useState<ExtractionJobAction>("retry");
  const [extractionJobDryRun, setExtractionJobDryRun] = useState(true);
  const [extractionJobMaintenanceLimit, setExtractionJobMaintenanceLimit] = useState(100);
  const [extractionJobMaintenanceResult, setExtractionJobMaintenanceResult] = useState<ExtractionJobMaintenanceResult | null>(null);
  const requireMemoryMemberScope = useCallback(() => {
    const groupId = requireSelectedGroup(config, verifiedGroupIds);
    const memberId = userId.trim();
    if (!memberId || !members.some((item) => item.wxid === memberId)) {
      throw new Error("请先从当前群的已验证成员名册选择记忆对象");
    }
    return { groupId, memberId };
  }, [config, members, userId, verifiedGroupIds]);

  const extractionJobFilters = useMemo(
    () => ({
      tenant_id: config.tenantId,
      channel: optionalText(extractionJobChannel),
      source_key: optionalText(extractionJobSourceKey),
      user_id: selectedMemberIsVerified ? optionalText(userId) : undefined,
      session_id: selectedSessionIsGroup ? optionalText(sessionId) : undefined,
      status: optionalText(extractionJobStatus),
      error_type: optionalText(extractionJobErrorType),
      created_after: optionalText(extractionJobCreatedAfter),
      created_before: optionalText(extractionJobCreatedBefore),
      updated_after: optionalText(extractionJobUpdatedAfter),
      updated_before: optionalText(extractionJobUpdatedBefore),
    }),
    [
      extractionJobChannel,
      extractionJobCreatedAfter,
      extractionJobCreatedBefore,
      extractionJobErrorType,
      extractionJobSourceKey,
      extractionJobStatus,
      extractionJobUpdatedAfter,
      extractionJobUpdatedBefore,
      config.tenantId,
      selectedMemberIsVerified,
      selectedSessionIsGroup,
      sessionId,
      userId,
    ],
  );
  const extractionJobFilterEntries = useMemo(
    () =>
      Object.entries(extractionJobFilters).filter(
        (entry): entry is [string, string] => typeof entry[1] === "string" && Boolean(entry[1]),
      ),
    [extractionJobFilters],
  );
  const hasExtractionJobFilters = extractionJobFilterEntries.length > 0;
  const extractionJobStatusCounts = extractionJobStats?.status_counts || extractionJobStats?.counts || {};
  const extractionJobErrorTypeCounts = extractionJobStats?.error_type_counts || {};
  const extractionJobScopeCounts = extractionJobStats?.scope_counts || [];
  const extractionJobApiLimit = Math.min(100, Math.max(1, extractionJobLimit || 100));
  const extractionJobMaintenanceApiLimit = Math.min(100, Math.max(1, extractionJobMaintenanceLimit || 100));

  const loadExtractionJobStats = useCallback(async () => {
    if (!selectedSessionIsGroup || !selectedMemberIsVerified) {
      setExtractionJobStats(null);
      setExtractionJobOutput(formatJson({ error: "请先选择已验证群聊和群成员" }));
      return;
    }
    try {
      const result = await apiRequest<ExtractionJobStats>(
        config,
        "/plugins/memory/extraction-jobs/stats",
        {
          auth: true,
          query: {
            ...extractionJobFilters,
            limit: extractionJobApiLimit,
          },
        },
      );
      setExtractionJobStats(result);
      const loadedAt = new Date().toISOString();
      setExtractionJobStatsLoadedAt(loadedAt);
      setExtractionJobOutput(formatJson({
        status: "stats_loaded",
        loaded_at: loadedAt,
        filters: extractionJobFilters,
        status_counts: result.status_counts || result.counts || {},
        error_type_count: Object.keys(result.error_type_counts || {}).length,
        scope_count: (result.scope_counts || []).length,
        message: (result.scope_counts || []).length
          ? undefined
          : "当前群成员范围没有记忆抽取任务统计。请检查状态和时间筛选后重试。",
      }));
    } catch (err) {
      setExtractionJobStats(null);
      setExtractionJobOutput(formatJson({
        error: friendlyApiError(err, "记忆抽取任务统计读取失败"),
        filters: extractionJobFilters,
      }));
    }
  }, [config, extractionJobApiLimit, extractionJobFilters, selectedMemberIsVerified, selectedSessionIsGroup]);

  const runExtractionJobMaintenance = async (surfaceErrors = false) => {
    const fail = (message: string) => {
      setExtractionJobOutput(formatJson({ error: message }));
      if (surfaceErrors) {
        throw new Error(message);
      }
      return false;
    };
    try {
      requireMemoryMemberScope();
    } catch (error) {
      fail(error instanceof Error ? error.message : "请先选择已验证群聊和群成员");
      return;
    }
    if (!hasExtractionJobFilters && extractionJobAction !== "reset_stale") {
      fail(`${extractionJobAction} 需要至少一个过滤条件`);
      return;
    }
    if (!hasExtractionJobFilters && !extractionJobDryRun) {
      fail("写入操作需要至少一个过滤条件");
      return;
    }
    if (
      extractionJobAction === "cleanup_smoke" &&
      !extractionJobDryRun &&
      !hasSmokeOrTestScope(extractionJobFilters)
    ) {
      fail("清理冒烟或测试数据的写入操作必须包含 smoke/test 范围过滤条件");
      return;
    }

    const intent = `memory:extraction-maintenance:${extractionJobAction}:${JSON.stringify(extractionJobFilters)}:${extractionJobMaintenanceApiLimit}`;

    try {
      const result = await apiRequest<ExtractionJobMaintenanceResult>(
        config,
        "/plugins/memory/extraction-jobs/maintenance",
        {
          auth: true,
          init: {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "Idempotency-Key": keyFor(intent),
            },
            body: JSON.stringify({
              action: extractionJobAction,
              ...extractionJobFilters,
              limit: extractionJobMaintenanceApiLimit,
              dry_run: extractionJobDryRun,
            }),
          },
        },
      );
      setExtractionJobMaintenanceResult(result);
      setExtractionJobOutput(formatJson({
        filters: extractionJobFilters,
        ...summarizeExtractionJobResult(result),
      }));
      try {
        const statsResult = await apiRequest<ExtractionJobStats>(
          config,
          "/plugins/memory/extraction-jobs/stats",
          {
            auth: true,
            query: {
              ...extractionJobFilters,
              limit: extractionJobApiLimit,
            },
          },
        );
        setExtractionJobStats(statsResult);
        setExtractionJobStatsLoadedAt(new Date().toISOString());
      } catch {
        // The maintenance result is more important than a best-effort stats refresh.
      }
      clear(intent);
    } catch (err) {
      setExtractionJobMaintenanceResult(null);
      setExtractionJobOutput(formatJson({
        error: friendlyApiError(err, "记忆抽取任务维护执行失败"),
        filters: extractionJobFilters,
      }));
      if (surfaceErrors) {
        throw err;
      }
    }
  };

  return (
      <BacklogPanel
        currentTenantId={config.tenantId}
        currentUserId={userId}
        currentSessionId={sessionId}
        channel={extractionJobChannel}
        sourceKey={extractionJobSourceKey}
        status={extractionJobStatus}
        errorType={extractionJobErrorType}
        createdAfter={extractionJobCreatedAfter}
        createdBefore={extractionJobCreatedBefore}
        updatedAfter={extractionJobUpdatedAfter}
        updatedBefore={extractionJobUpdatedBefore}
        statsLimit={extractionJobLimit}
        action={extractionJobAction}
        dryRun={extractionJobDryRun}
        maintenanceLimit={extractionJobMaintenanceLimit}
        maintenanceApiLimit={extractionJobMaintenanceApiLimit}
        filters={extractionJobFilters}
        filterEntries={extractionJobFilterEntries}
        statusCounts={extractionJobStatusCounts}
        errorTypeCounts={extractionJobErrorTypeCounts}
        scopeCounts={extractionJobScopeCounts}
        statsLoadedAt={extractionJobStatsLoadedAt}
        maintenanceResult={extractionJobMaintenanceResult}
        onChannelChange={setExtractionJobChannel}
        onSourceKeyChange={setExtractionJobSourceKey}
        onStatusChange={setExtractionJobStatus}
        onErrorTypeChange={setExtractionJobErrorType}
        onCreatedAfterChange={setExtractionJobCreatedAfter}
        onCreatedBeforeChange={setExtractionJobCreatedBefore}
        onUpdatedAfterChange={setExtractionJobUpdatedAfter}
        onUpdatedBeforeChange={setExtractionJobUpdatedBefore}
        onStatsLimitChange={setExtractionJobLimit}
        onActionChange={setExtractionJobAction}
        onDryRunChange={setExtractionJobDryRun}
        onMaintenanceLimitChange={setExtractionJobMaintenanceLimit}
        onLoadStats={loadExtractionJobStats}
        onRunMaintenance={runExtractionJobMaintenance}
      />
  );
}
