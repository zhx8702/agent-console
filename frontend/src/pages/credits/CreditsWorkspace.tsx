import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { DangerAction } from "../../components/DangerAction";
import { Alert } from "../../components/Alert";
import { GroupScopeEmpty } from "../../components/GroupScopeEmpty";
import { OutputPanel } from "../../components/OutputPanel";
import { PageHeader } from "../../components/PageHeader";
import { SearchableSelect } from "../../components/SearchableSelect";
import { UnsavedChangesGuard } from "../../components/UnsavedChangesGuard";
import {
  VersionConflictError,
  apiRequest,
  apiVersionedResource,
  formatJson,
} from "../../lib/api";
import { useStableIdempotencyKeys } from "../../lib/idempotency";
import {
  requireSelectedGroup,
  useConsoleConfig,
} from "../../state/console-config";

import {
  type GroupRosterCandidate,
  type CreditsConfig,
  type CreditsMember,
  type CreditsLedgerRow,
  type CreditsMembersPayload,
  type CreditsMemberDetail,
  type CreditsLedgerPayload,
  type CreditsMemberRow,
  CHECKIN_MODE_OPTIONS,
  getMemberDisplayName,
  formatTimestamp,
  formatDay,
  formatDelta,
  getReasonLabel,
  getCheckinModeText,
  type CreditsConfigDraft,
  configFingerprint,
} from "./model";
export function CreditsWorkspace() {
  const { config, verifiedGroupIds } = useConsoleConfig();
  const { keyFor, clear } = useStableIdempotencyKeys();
  const basePath = "/plugins/credits";

  const [rosterMembers, setRosterMembers] = useState<GroupRosterCandidate[]>([]);
  const [creditsMembers, setCreditsMembers] = useState<CreditsMember[]>([]);
  const [membersSummary, setMembersSummary] = useState<CreditsMembersPayload["summary"] | null>(null);
  const [leaderboard, setLeaderboard] = useState<CreditsMember[]>([]);
  const [ledger, setLedger] = useState<CreditsLedgerRow[]>([]);
  const [selectedUserId, setSelectedUserId] = useState("");
  const [memberSearch, setMemberSearch] = useState("");
  const [memberDetail, setMemberDetail] = useState<CreditsMemberDetail | null>(null);

  const [enabled, setEnabled] = useState(false);
  const [creditName, setCreditName] = useState("积分");
  const [costPerChat, setCostPerChat] = useState(0);
  const [commandCostsText, setCommandCostsText] = useState("");
  const [drawQualityCostsText, setDrawQualityCostsText] = useState("low=5\nmedium=10\nhigh=20");
  const [amapSearchCreditCost, setAmapSearchCreditCost] = useState(2);
  const [amapMapCreditCost, setAmapMapCreditCost] = useState(8);
  const [amapRouteMapCreditCost, setAmapRouteMapCreditCost] = useState(12);
  const [initialCredits, setInitialCredits] = useState(100);
  const [dailyCheckin, setDailyCheckin] = useState(10);
  const [streakBonus, setStreakBonus] = useState(5);
  const [streakCap, setStreakCap] = useState(50);
  const [checkinMode, setCheckinMode] = useState(1);

  const [adjustMode, setAdjustMode] = useState<"delta" | "set">("delta");
  const [delta, setDelta] = useState(10);
  const [setAmount, setSetAmount] = useState(100);
  const [reason, setReason] = useState("admin_adjust");
  const [transferToUserId, setTransferToUserId] = useState("");
  const [transferAmount, setTransferAmount] = useState(1);

  const [configOutput, setConfigOutput] = useState('{\n  "status": "waiting"\n}');
  const [opsOutput, setOpsOutput] = useState('{\n  "status": "waiting"\n}');
  const [configLoaded, setConfigLoaded] = useState(false);
  const [configEtag, setConfigEtag] = useState<string | null>(null);
  const [loadedConfigFingerprint, setLoadedConfigFingerprint] = useState("");
  const [configConflict, setConfigConflict] = useState("");

  const effectiveSessionId = config.sessionId.trim();
  const selectedSessionIsGroup = Boolean(effectiveSessionId && verifiedGroupIds.has(effectiveSessionId));
  const configDraft = useMemo<CreditsConfigDraft>(() => ({
    enabled,
    credit_name: creditName,
    cost_per_chat: costPerChat,
    command_costs_text: commandCostsText,
    draw_quality_costs_text: drawQualityCostsText,
    amap_search_credit_cost: amapSearchCreditCost,
    amap_map_credit_cost: amapMapCreditCost,
    amap_route_map_credit_cost: amapRouteMapCreditCost,
    initial_credits: initialCredits,
    daily_checkin: dailyCheckin,
    streak_bonus: streakBonus,
    streak_cap: streakCap,
    checkin_mode: checkinMode,
  }), [
    amapMapCreditCost,
    amapRouteMapCreditCost,
    amapSearchCreditCost,
    checkinMode,
    commandCostsText,
    costPerChat,
    creditName,
    dailyCheckin,
    drawQualityCostsText,
    enabled,
    initialCredits,
    streakBonus,
    streakCap,
  ]);
  const configDirty = configLoaded && configFingerprint(configDraft) !== loadedConfigFingerprint;
  const selectedRosterMember = rosterMembers.find((item) => item.wxid === selectedUserId) || null;
  const selectedCreditsMember = creditsMembers.find((item) => item.user_id === selectedUserId) || null;

  const combinedMemberRows = useMemo(() => {
    const merged = new Map<string, CreditsMemberRow>();
    for (const member of rosterMembers) {
      if (!member.wxid) {
        continue;
      }
      merged.set(member.wxid, {
        user_id: member.wxid,
        display_name: getMemberDisplayName(member),
        source: "roster",
        msg_count: member.msg_count,
      });
    }
    for (const item of creditsMembers) {
      if (!item.user_id) {
        continue;
      }
      const current = merged.get(item.user_id);
      merged.set(item.user_id, {
        user_id: item.user_id,
        display_name: current?.display_name || item.display_name || item.user_id,
        source: current?.source || "credits",
        msg_count: current?.msg_count,
        credits: item.credits,
        rank: item.rank,
        checked_in_today: item.checked_in_today,
        today_reward: item.today_reward,
        last_checkin_date: item.last_checkin_date,
      });
    }
    return Array.from(merged.values()).sort((left, right) => {
      const leftRank = left.rank ?? Number.MAX_SAFE_INTEGER;
      const rightRank = right.rank ?? Number.MAX_SAFE_INTEGER;
      if (leftRank !== rightRank) {
        return leftRank - rightRank;
      }
      return left.display_name.localeCompare(right.display_name, "zh-CN");
    });
  }, [creditsMembers, rosterMembers]);

  const filteredMemberRows = useMemo(() => {
    const query = memberSearch.trim().toLowerCase();
    if (!query) {
      return combinedMemberRows;
    }
    return combinedMemberRows.filter((item) => {
      const haystack = [item.user_id, item.display_name].join("\n").toLowerCase();
      return haystack.includes(query);
    });
  }, [combinedMemberRows, memberSearch]);

  const memberOptions = useMemo(
    () =>
      rosterMembers
        .filter((item) => Boolean(item.wxid))
        .map((item) => ({
          value: item.wxid,
          label: `${getMemberDisplayName(item)} (${item.wxid})`,
          keywords: [item.wxid, getMemberDisplayName(item)],
        })),
    [rosterMembers],
  );

  const selectedDisplayName =
    selectedRosterMember
      ? getMemberDisplayName(selectedRosterMember)
      : selectedCreditsMember?.display_name || memberDetail?.display_name || selectedUserId || "-";

  const applyConfig = useCallback((result: CreditsConfig) => {
    setEnabled(Boolean(result.enabled));
    setCreditName(String(result.credit_name || "积分"));
    setCostPerChat(Number(result.cost_per_chat ?? 0));
    setCommandCostsText(String(result.command_costs_text || ""));
    setDrawQualityCostsText(String(result.draw_quality_costs_text || "low=5\nmedium=10\nhigh=20"));
    setAmapSearchCreditCost(Number(result.amap_search_credit_cost ?? 2));
    setAmapMapCreditCost(Number(result.amap_map_credit_cost ?? 8));
    setAmapRouteMapCreditCost(Number(result.amap_route_map_credit_cost ?? 12));
    setInitialCredits(Number(result.initial_credits ?? 100));
    setDailyCheckin(Number(result.daily_checkin ?? 10));
    setStreakBonus(Number(result.streak_bonus ?? 5));
    setStreakCap(Number(result.streak_cap ?? 50));
    setCheckinMode(Number(result.checkin_mode ?? 1));
  }, []);

  const loadConfig = useCallback(
    async (pushOutput = true) => {
      if (!selectedSessionIsGroup) {
        setConfigLoaded(false);
        setConfigEtag(null);
        setLoadedConfigFingerprint("");
        setConfigConflict("");
        if (pushOutput) {
          setConfigOutput(formatJson({ message: "请先从已验证群聊列表选择目标群" }));
        }
        return;
      }
      try {
        const response = await apiVersionedResource<CreditsConfig>(
          config,
          `${basePath}/config/${encodeURIComponent(config.tenantId)}/${encodeURIComponent(effectiveSessionId)}`,
          { auth: true },
        );
        const result = response.value;
        applyConfig(result);
        const fingerprint = configFingerprint({
          enabled: Boolean(result.enabled),
          credit_name: String(result.credit_name || "积分"),
          cost_per_chat: Number(result.cost_per_chat ?? 0),
          command_costs_text: String(result.command_costs_text || ""),
          draw_quality_costs_text: String(result.draw_quality_costs_text || "low=5\nmedium=10\nhigh=20"),
          amap_search_credit_cost: Number(result.amap_search_credit_cost ?? 2),
          amap_map_credit_cost: Number(result.amap_map_credit_cost ?? 8),
          amap_route_map_credit_cost: Number(result.amap_route_map_credit_cost ?? 12),
          initial_credits: Number(result.initial_credits ?? 100),
          daily_checkin: Number(result.daily_checkin ?? 10),
          streak_bonus: Number(result.streak_bonus ?? 5),
          streak_cap: Number(result.streak_cap ?? 50),
          checkin_mode: Number(result.checkin_mode ?? 1),
        });
        setLoadedConfigFingerprint(fingerprint);
        setConfigEtag(response.etag);
        setConfigLoaded(true);
        setConfigConflict("");
        if (pushOutput) {
          setConfigOutput(formatJson(result));
        }
      } catch (err) {
        setConfigLoaded(false);
        setConfigEtag(null);
        if (pushOutput) {
          setConfigOutput(formatJson({ error: err instanceof Error ? err.message : "读取失败" }));
        }
      }
    },
    [applyConfig, config, effectiveSessionId, selectedSessionIsGroup],
  );

  const loadRosterMembers = useCallback(async () => {
    if (!selectedSessionIsGroup) {
      setRosterMembers([]);
      return;
    }
    try {
      const result = await apiRequest<{ candidates?: GroupRosterCandidate[] }>(
        config,
        `/plugins/wxbot/admin/roster/groups/${encodeURIComponent(effectiveSessionId)}/members`,
        { auth: true },
      );
      setRosterMembers(result.candidates || []);
    } catch {
      setRosterMembers([]);
    }
  }, [config, effectiveSessionId, selectedSessionIsGroup]);

  const loadCreditsCollections = useCallback(async () => {
    if (!selectedSessionIsGroup) {
      setCreditsMembers([]);
      setMembersSummary(null);
      setLeaderboard([]);
      setLedger([]);
      return;
    }
    try {
      const [membersResult, topResult, ledgerResult] = await Promise.all([
        apiRequest<CreditsMembersPayload>(
          config,
          `${basePath}/members/${encodeURIComponent(config.tenantId)}/${encodeURIComponent(effectiveSessionId)}`,
          { query: { limit: 200 } },
        ),
        apiRequest<{ items?: CreditsMember[] }>(
          config,
          `${basePath}/top/${encodeURIComponent(config.tenantId)}/${encodeURIComponent(effectiveSessionId)}`,
          { query: { limit: 10 } },
        ),
        apiRequest<CreditsLedgerPayload>(
          config,
          `${basePath}/ledger/${encodeURIComponent(config.tenantId)}/${encodeURIComponent(effectiveSessionId)}`,
          { query: { limit: 30 } },
        ),
      ]);
      setCreditsMembers(membersResult.items || []);
      setMembersSummary(membersResult.summary || null);
      setLeaderboard(topResult.items || []);
      setLedger(ledgerResult.items || []);
    } catch {
      setCreditsMembers([]);
      setMembersSummary(null);
      setLeaderboard([]);
      setLedger([]);
    }
  }, [config, effectiveSessionId, selectedSessionIsGroup]);

  const loadMemberDetail = useCallback(
    async (userId: string, pushOutput = false) => {
      const value = userId.trim();
      if (!value || !selectedSessionIsGroup || !rosterMembers.some((item) => item.wxid === value)) {
        setMemberDetail(null);
        return;
      }
      try {
        const result = await apiRequest<CreditsMemberDetail>(
          config,
          `${basePath}/member/${encodeURIComponent(config.tenantId)}/${encodeURIComponent(effectiveSessionId)}/${encodeURIComponent(value)}`,
          { query: { ledger_limit: 12 } },
        );
        setMemberDetail(result);
        if (pushOutput) {
          setOpsOutput(formatJson(result));
        }
      } catch (err) {
        setMemberDetail(null);
        if (pushOutput) {
          setOpsOutput(formatJson({ error: err instanceof Error ? err.message : "成员详情加载失败" }));
        }
      }
    },
    [config, effectiveSessionId, rosterMembers, selectedSessionIsGroup],
  );

  const refreshAll = useCallback(async () => {
    await Promise.all([loadConfig(false), loadRosterMembers(), loadCreditsCollections()]);
    if (selectedUserId.trim()) {
      await loadMemberDetail(selectedUserId, false);
    }
  }, [loadConfig, loadCreditsCollections, loadMemberDetail, loadRosterMembers, selectedUserId]);

  const saveConfig = async () => {
    const groupId = requireSelectedGroup(config, verifiedGroupIds);
    if (!configLoaded) {
      throw new Error("配置尚未成功读取。请先读取配置，避免用默认值覆盖线上设置。");
    }
    try {
      const intent = `credits:config:${config.tenantId}:${groupId}:${loadedConfigFingerprint}:${configFingerprint(configDraft)}`;
      const response = await apiVersionedResource<CreditsConfig, CreditsConfigDraft>(
        config,
        `${basePath}/config/${encodeURIComponent(config.tenantId)}/${encodeURIComponent(groupId)}`,
        {
          method: "POST",
          auth: true,
          ifMatch: configEtag || undefined,
          idempotencyKey: keyFor(intent),
          body: configDraft,
        },
      );
      const result = response.value;
      applyConfig(result);
      const nextFingerprint = configFingerprint({
        ...configDraft,
        enabled: Boolean(result.enabled ?? configDraft.enabled),
        credit_name: String(result.credit_name ?? configDraft.credit_name),
      });
      setLoadedConfigFingerprint(nextFingerprint);
      setConfigEtag(response.etag || configEtag);
      setConfigLoaded(true);
      setConfigConflict("");
      setConfigOutput(formatJson(result));
      clear(intent);
    } catch (err) {
      if (err instanceof VersionConflictError) {
        setConfigConflict("线上配置已被其他管理员更新。当前草稿已保留，请先重新读取并核对差异。");
      }
      setConfigOutput(formatJson({ error: err instanceof Error ? err.message : "保存失败" }));
      throw err;
    }
  };

  const runOps = async (kind: "balance" | "checkin" | "adjust" | "transfer" | "detail" | "refresh") => {
    let groupId: string;
    try {
      groupId = requireSelectedGroup(config, verifiedGroupIds);
    } catch (error) {
      setOpsOutput(formatJson({ error: error instanceof Error ? error.message : "请先选择目标群" }));
      if (kind === "adjust" || kind === "transfer") {
        throw error;
      }
      return;
    }
    const userId = selectedUserId.trim();
    const selectedUserIsVerified = rosterMembers.some((item) => item.wxid === userId);
    const transferTargetIsVerified = rosterMembers.some((item) => item.wxid === transferToUserId.trim());
    const transferSourceUserId = selectedUserIsVerified ? userId : "";
    const mutationIntent = kind === "adjust"
      ? `credits:adjust:${config.tenantId}:${groupId}:${userId}:${adjustMode}:${adjustMode === "delta" ? delta : setAmount}:${reason.trim()}`
      : kind === "transfer"
        ? `credits:transfer:${config.tenantId}:${groupId}:${transferSourceUserId}:${transferToUserId.trim()}:${transferAmount}:${reason.trim()}`
        : kind === "checkin"
          ? `credits:checkin:${config.tenantId}:${groupId}:${userId}:${new Date().toISOString().slice(0, 10)}`
          : "";
    try {
      if (kind === "refresh") {
        await refreshAll();
        setOpsOutput(
          formatJson({
            refreshed: true,
            session_id: effectiveSessionId,
            selected_user_id: userId || null,
          }),
        );
        return;
      }
      if (kind === "detail") {
        await loadMemberDetail(userId, true);
        return;
      }
      if ((kind === "balance" || kind === "checkin" || kind === "adjust") && !selectedUserIsVerified) {
        const error = new Error("请先从当前群的已验证成员列表中选择成员");
        setOpsOutput(formatJson({ error: error.message }));
        if (kind === "adjust") {
          throw error;
        }
        return;
      }

      let result: unknown;
      if (kind === "balance") {
        result = await apiRequest(
          config,
          `${basePath}/balance/${encodeURIComponent(config.tenantId)}/${encodeURIComponent(groupId)}/${encodeURIComponent(userId)}`,
        );
      } else if (kind === "checkin") {
        result = await apiRequest(
          config,
          `${basePath}/checkin/${encodeURIComponent(config.tenantId)}/${encodeURIComponent(groupId)}/${encodeURIComponent(userId)}`,
          {
            auth: true,
            init: {
              method: "POST",
              headers: { "Idempotency-Key": keyFor(mutationIntent) },
            },
          },
        );
      } else if (kind === "adjust") {
        result = await apiRequest(
          config,
          `${basePath}/adjust`,
          {
            auth: true,
            init: {
              method: "POST",
              headers: {
                "Content-Type": "application/json",
                "Idempotency-Key": keyFor(mutationIntent),
              },
              body: JSON.stringify({
                tenant_id: config.tenantId,
                session_id: groupId,
                user_id: userId,
                mode: adjustMode,
                delta: adjustMode === "delta" ? delta : undefined,
                amount: adjustMode === "set" ? setAmount : undefined,
                reason: reason.trim() || (adjustMode === "set" ? "admin_set_balance" : "admin_adjust"),
                display_name: selectedDisplayName !== "-" ? selectedDisplayName : "",
              }),
            },
          },
        );
      } else {
        if (!transferSourceUserId || !transferTargetIsVerified) {
          const error = new Error("转出和转入成员都必须来自当前群的已验证成员列表");
          setOpsOutput(formatJson({ error: error.message }));
          throw error;
        }
        result = await apiRequest(
          config,
          `${basePath}/transfer`,
          {
            auth: true,
            init: {
              method: "POST",
              headers: {
                "Content-Type": "application/json",
                "Idempotency-Key": keyFor(mutationIntent),
              },
              body: JSON.stringify({
                tenant_id: config.tenantId,
                session_id: groupId,
                from_user_id: transferSourceUserId,
                to_user_id: transferToUserId.trim(),
                amount: transferAmount,
                reason: reason.trim() || "credit_transfer",
              }),
            },
          },
        );
      }

      setOpsOutput(formatJson(result));
      await loadCreditsCollections();
      if (userId) {
        await loadMemberDetail(userId, false);
      }
      if (kind === "transfer" && transferToUserId.trim() === userId) {
        await loadMemberDetail(transferToUserId.trim(), false);
      }
      if (mutationIntent) {
        clear(mutationIntent);
      }
    } catch (err) {
      setOpsOutput(formatJson({ error: err instanceof Error ? err.message : "操作失败" }));
      if (kind === "adjust" || kind === "transfer") {
        throw err;
      }
    }
  };

  useEffect(() => {
    setSelectedUserId("");
    setTransferToUserId("");
    setMemberDetail(null);
    setMemberSearch("");
    setConfigLoaded(false);
    setConfigEtag(null);
    setLoadedConfigFingerprint("");
    setConfigConflict("");
  }, [effectiveSessionId]);

  useEffect(() => {
    void Promise.all([loadConfig(), loadRosterMembers(), loadCreditsCollections()]);
  }, [loadConfig, loadRosterMembers, loadCreditsCollections]);

  useEffect(() => {
    if (!selectedUserId.trim()) {
      const firstMember = rosterMembers.find((item) => Boolean(item.wxid))?.wxid || "";
      if (firstMember) {
        setSelectedUserId(firstMember);
      }
    }
  }, [rosterMembers, selectedUserId]);

  useEffect(() => {
    if (!selectedUserId.trim()) {
      setMemberDetail(null);
      return;
    }
    void loadMemberDetail(selectedUserId, false);
  }, [loadMemberDetail, selectedUserId]);

  useEffect(() => {
    if (transferToUserId && !rosterMembers.some((item) => item.wxid === transferToUserId)) {
      setTransferToUserId("");
    }
  }, [rosterMembers, transferToUserId]);

  if (!selectedSessionIsGroup) {
    return (
      <GroupScopeEmpty
        eyebrow="积分运营"
        title="按群积分配置与签到运营"
        description="对齐旧 wx-bot 的核心体验：按群单独启用和配置积分、按成员查看积分与签到状态、查看排行榜与流水，并管理三种签到模式。"
      />
    );
  }

  return (
    <div className="page-grid credits-page">
      <UnsavedChangesGuard when={configDirty} />
      <section className="panel span-3">
        <PageHeader
          eyebrow="积分运营"
          title="按群积分配置与签到运营"
          description="积分、签到和排行榜只作用于当前已验证群聊。"
        />

        <div className="summary-grid page-hero-metrics">
            <div className="summary-card" data-status={enabled ? "ok" : "warning"}>
              <span>插件状态</span>
              <strong>{enabled ? "已启用" : "未启用"}</strong>
            </div>
            <div className="summary-card">
              <span>积分成员</span>
              <strong>{membersSummary?.member_count ?? creditsMembers.length}</strong>
            </div>
            <div className="summary-card" data-status={(membersSummary?.checked_in_today_count || 0) > 0 ? "ok" : "warning"}>
              <span>今日已签到</span>
              <strong>{membersSummary?.checked_in_today_count ?? 0}</strong>
            </div>
            <div className="summary-card">
              <span>签到模式</span>
              <strong>{getCheckinModeText(checkinMode)}</strong>
            </div>
          </div>
        <div className="credits-config-layout">
          <div className="form-grid">
            <label className="field">
              <span>启用</span>
              <select value={enabled ? "true" : "false"} onChange={(event) => setEnabled(event.target.value === "true")}>
                <option value="true">开启</option>
                <option value="false">关闭</option>
              </select>
            </label>
            <label className="field">
              <span>积分名称</span>
              <input value={creditName} onChange={(event) => setCreditName(event.target.value)} />
            </label>
            <label className="field">
              <span>每次对话扣费</span>
              <input
                type="number"
                value={costPerChat}
                onChange={(event) => setCostPerChat(Number(event.target.value))}
              />
            </label>
            <label className="field">
              <span>初始积分</span>
              <input
                type="number"
                value={initialCredits}
                onChange={(event) => setInitialCredits(Number(event.target.value))}
              />
            </label>
            <div className="field span-2">
              <span>签到模式</span>
              <div className="credits-mode-switch" role="radiogroup" aria-label="签到模式">
                {CHECKIN_MODE_OPTIONS.map((item) => (
                  <button
                    key={item.value}
                    type="button"
                    role="radio"
                    aria-checked={checkinMode === item.value}
                    className={checkinMode === item.value ? "is-active" : undefined}
                    onClick={() => setCheckinMode(item.value)}
                  >
                    <strong>{item.label}</strong>
                    <small>{item.description}</small>
                  </button>
                ))}
              </div>
            </div>
            <label className="field">
              <span>签到基础奖励</span>
              <input
                type="number"
                value={dailyCheckin}
                onChange={(event) => setDailyCheckin(Number(event.target.value))}
              />
            </label>
            <label className="field">
              <span>每 7 天额外奖励</span>
              <input
                type="number"
                value={streakBonus}
                onChange={(event) => setStreakBonus(Number(event.target.value))}
              />
            </label>
            <label className="field">
              <span>额外奖励上限</span>
              <input
                type="number"
                value={streakCap}
                onChange={(event) => setStreakCap(Number(event.target.value))}
              />
            </label>
            <label className="field span-2">
              <span>命令积分规则</span>
              <textarea
                rows={5}
                value={commandCostsText}
                onChange={(event) => setCommandCostsText(event.target.value)}
                placeholder={"/签到=0\n/balance=0"}
              />
            </label>
            <label className="field span-2">
              <span>画图质量积分规则</span>
              <textarea
                rows={3}
                value={drawQualityCostsText}
                onChange={(event) => setDrawQualityCostsText(event.target.value)}
                placeholder={"low=5\nmedium=10\nhigh=20"}
              />
            </label>
            <label className="field">
              <span>高德普通查询</span>
              <input
                type="number"
                min={0}
                value={amapSearchCreditCost}
                onChange={(event) => setAmapSearchCreditCost(Number(event.target.value))}
              />
            </label>
            <label className="field">
              <span>高德地图二维码</span>
              <input
                type="number"
                min={0}
                value={amapMapCreditCost}
                onChange={(event) => setAmapMapCreditCost(Number(event.target.value))}
              />
            </label>
            <label className="field">
              <span>高德复杂路线地图</span>
              <input
                type="number"
                min={0}
                value={amapRouteMapCreditCost}
                onChange={(event) => setAmapRouteMapCreditCost(Number(event.target.value))}
              />
            </label>
          </div>
          <div className="action-row">
            <button className="button button-secondary" onClick={() => void loadConfig()} disabled={!selectedSessionIsGroup}>
              读取配置
            </button>
            <DangerAction
              label="保存群积分配置"
              title="确认更新群积分配置"
              confirmLabel="确认保存"
              pendingLabel="正在保存…"
              disabled={!selectedSessionIsGroup || !configLoaded || !configDirty}
              impact={(
                <dl>
                  <div><dt>目标群</dt><dd><code>{effectiveSessionId || "未选择"}</code></dd></div>
                  <div><dt>配置状态</dt><dd>{enabled ? "启用" : "停用"}</dd></div>
                  <div><dt>影响</dt><dd>将更新该群的积分扣费、签到和工具计费规则；其他群不受影响。</dd></div>
                  <div><dt>并发保护</dt><dd>{configEtag ? `ETag ${configEtag}` : "服务端未返回 ETag；仍使用稳定幂等键防止重复提交"}</dd></div>
                </dl>
              )}
              onConfirm={saveConfig}
            />
          </div>
          <p className="muted-copy" role="status" aria-live="polite">
            {!selectedSessionIsGroup
              ? "未选择已验证群聊，配置写入已禁用。"
              : !configLoaded
                ? "配置尚未成功读取，保存已禁用。"
                : configDirty
                  ? "有尚未保存的配置修改。"
                  : "配置已加载且没有未保存修改。"}
          </p>
          {configConflict && (
            <Alert variant="warning" title="检测到版本冲突">
              {configConflict}
              <button type="button" className="button button-secondary button-compact" onClick={() => void loadConfig()}>
                重新读取线上配置
              </button>
            </Alert>
          )}
          <details className="credits-help-disclosure">
            <summary>命令权限与计费说明</summary>
            <p className="muted-copy">
              命令权限已经迁到 <Link to="/commands">全局命令中心</Link>。这里仅管理积分与签到模式本身；
              <code>/sign-in mode 1|2|3</code> 是否可用由命令中心里的管理员和命令清单决定。
              普通 AI 回复继续走“每次对话扣费”，命令类交互可在“命令积分规则”里按 <code>/command=分值</code> 单独定价；
              自然语言触发的高德 Agent 按实际工具结果走这三档计费，不需要映射成命令。
            </p>
          </details>
        </div>
      </section>

      <section className="panel span-3">
        <div className="panel-header">
          <div>
            <p className="section-kicker">成员</p>
            <h3>群成员积分与签到状态</h3>
          </div>
        </div>
        <div className="credits-member-layout">
          <div>
            <div className="member-filter-row">
              <label className="field">
                <span>成员筛选</span>
                <input
                  value={memberSearch}
                  onChange={(event) => setMemberSearch(event.target.value)}
                  placeholder="按名称或 user_id 过滤下表"
                />
              </label>
              <div className="action-row">
                <button className="button button-secondary button-compact" onClick={() => void runOps("refresh")} disabled={!selectedSessionIsGroup}>
                  刷新数据
                </button>
                <button className="button button-secondary button-compact" onClick={() => void loadRosterMembers()} disabled={!selectedSessionIsGroup}>
                  刷新群成员
                </button>
                <button className="button button-secondary button-compact" onClick={() => void loadCreditsCollections()} disabled={!selectedSessionIsGroup}>
                  刷新积分数据
                </button>
              </div>
            </div>

            <div className="table-scroll credits-table-scroll">
              <table>
                <caption className="sr-only">当前群积分成员列表</caption>
                <thead>
                  <tr>
                    <th scope="col">成员</th>
                    <th scope="col">WXID</th>
                    <th scope="col">积分</th>
                    <th scope="col">今日签到</th>
                    <th scope="col">排名</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredMemberRows.map((item) => (
                    <tr
                      key={item.user_id}
                      className={item.user_id === selectedUserId ? "table-row-active" : ""}
                      role={rosterMembers.some((member) => member.wxid === item.user_id) ? "button" : undefined}
                      tabIndex={rosterMembers.some((member) => member.wxid === item.user_id) ? 0 : undefined}
                      onClick={() => {
                        if (rosterMembers.some((member) => member.wxid === item.user_id)) {
                          setSelectedUserId(item.user_id);
                        }
                      }}
                      onKeyDown={(event) => {
                        if (
                          rosterMembers.some((member) => member.wxid === item.user_id)
                          && (event.key === "Enter" || event.key === " ")
                        ) {
                          event.preventDefault();
                          setSelectedUserId(item.user_id);
                        }
                      }}
                    >
                      <th scope="row">
                        <div className="credits-member-name">
                          <strong>{item.display_name}</strong>
                          <span className={`pill ${item.source === "roster" ? "pill-feature" : "pill-muted"}`}>
                            {item.source}
                          </span>
                        </div>
                      </th>
                      <td className="mono">{item.user_id}</td>
                      <td>{item.credits ?? "-"}</td>
                      <td>
                        {item.checked_in_today ? (
                          <span className="pill pill-ok">已签到 +{item.today_reward ?? 0}</span>
                        ) : (
                          <span className="pill pill-muted">未签到</span>
                        )}
                      </td>
                      <td>{item.rank ?? "-"}</td>
                    </tr>
                  ))}
                  {!filteredMemberRows.length && (
                    <tr>
                      <td colSpan={5} className="empty-cell">
                        当前会话还没有可展示的积分成员或群成员。
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>

          <div className="credits-detail-stack">
            <section className="credits-detail-card">
              <div className="credits-detail-head">
                <div>
                  <p className="section-kicker">成员详情</p>
                  <h4>{selectedDisplayName}</h4>
                </div>
                <span className="mono">{selectedUserId || "-"}</span>
              </div>
              <div className="credits-detail-grid">
                <div>
                  <span>当前余额</span>
                  <strong>{memberDetail?.credits ?? selectedCreditsMember?.credits ?? 0}</strong>
                </div>
                <div>
                  <span>当前排名</span>
                  <strong>{memberDetail?.rank ?? selectedCreditsMember?.rank ?? "-"}</strong>
                </div>
                <div>
                  <span>连签天数</span>
                  <strong>{memberDetail?.checkin_status?.current_streak ?? 0}</strong>
                </div>
                <div>
                  <span>今日状态</span>
                  <strong>{memberDetail?.checkin_status?.checked_in_today ? "已签到" : "未签到"}</strong>
                </div>
              </div>
              <div className="credits-meta-list">
                <div>
                  <span>签到模式</span>
                  <strong>{memberDetail?.checkin_status?.checkin_mode_label || getCheckinModeText(checkinMode)}</strong>
                </div>
                <div>
                  <span>下次可得</span>
                  <strong>{memberDetail?.checkin_status?.next_reward ?? dailyCheckin} {creditName}</strong>
                </div>
                <div>
                  <span>最后签到</span>
                  <strong>{formatDay(memberDetail?.checkin_status?.last_checkin_date)}</strong>
                </div>
                <div>
                  <span>余额更新时间</span>
                  <strong>{formatTimestamp(memberDetail?.updated_at)}</strong>
                </div>
              </div>
              <div className="action-row">
                <button className="button button-secondary" onClick={() => void runOps("detail")} disabled={!selectedUserId}>
                  读取详情
                </button>
                <button className="button button-secondary" onClick={() => void runOps("balance")} disabled={!selectedUserId}>
                  查余额
                </button>
                <button
                  className="button button-primary"
                  onClick={() => void runOps("checkin")}
                  disabled={!selectedSessionIsGroup || !rosterMembers.some((item) => item.wxid === selectedUserId)}
                >
                  手动签到
                </button>
              </div>
            </section>

            <section className="credits-detail-card">
              <div className="panel-header">
                <div>
                  <p className="section-kicker">积分调整</p>
                  <h4>调整或设定余额</h4>
                </div>
              </div>
              <div className="credits-adjust-mode">
                <button
                  type="button"
                  className={`credits-adjust-tab${adjustMode === "delta" ? " active" : ""}`}
                  onClick={() => {
                    setAdjustMode("delta");
                    setReason("admin_adjust");
                  }}
                >
                  增减
                </button>
                <button
                  type="button"
                  className={`credits-adjust-tab${adjustMode === "set" ? " active" : ""}`}
                  onClick={() => {
                    setAdjustMode("set");
                    setReason("admin_set_balance");
                  }}
                >
                  设定余额
                </button>
              </div>
              <p className="credits-adjust-help">
                {adjustMode === "delta" ? "按差值调整当前余额。" : "直接把当前余额改成指定数值。"}
              </p>
              <div className="form-grid">
                {adjustMode === "delta" ? (
                  <label className="field">
                    <span>调整值</span>
                    <input type="number" value={delta} onChange={(event) => setDelta(Number(event.target.value))} />
                  </label>
                ) : (
                  <label className="field">
                    <span>目标余额</span>
                    <input
                      type="number"
                      value={setAmount}
                      onChange={(event) => setSetAmount(Number(event.target.value))}
                    />
                  </label>
                )}
                <label className="field">
                  <span>原因</span>
                  <input value={reason} onChange={(event) => setReason(event.target.value)} />
                </label>
              </div>
              <div className="action-row">
                <DangerAction
                  label="执行余额调整"
                  title={adjustMode === "set" ? "确认设定成员余额" : "确认调整成员余额"}
                  confirmLabel={adjustMode === "set" ? "确认设定余额" : "确认调整"}
                  pendingLabel="正在更新余额…"
                  disabled={!selectedSessionIsGroup || !rosterMembers.some((item) => item.wxid === selectedUserId)}
                  impact={(
                    <dl>
                      <div><dt>成员</dt><dd>{selectedDisplayName}（<code>{selectedUserId.trim() || "未选择"}</code>）</dd></div>
                      <div><dt>群会话</dt><dd><code>{effectiveSessionId || "未选择"}</code></dd></div>
                      <div><dt>当前余额</dt><dd>{memberDetail?.credits ?? "尚未读取"} {creditName}</dd></div>
                      <div>
                        <dt>{adjustMode === "set" ? "目标余额" : "变动值"}</dt>
                        <dd>{adjustMode === "set" ? setAmount : formatDelta(delta)} {creditName}</dd>
                      </div>
                      <div><dt>原因</dt><dd>{reason.trim() || (adjustMode === "set" ? "admin_set_balance" : "admin_adjust")}</dd></div>
                    </dl>
                  )}
                  onConfirm={() => runOps("adjust")}
                />
              </div>
            </section>

            <section className="credits-detail-card">
              <div className="panel-header">
                <div>
                  <p className="section-kicker">积分转账</p>
                  <h4>成员转账</h4>
                </div>
              </div>
              <div className="form-grid">
                <label className="field">
                  <span>转出成员</span>
                  <SearchableSelect
                    value={selectedUserId}
                    options={memberOptions}
                    onChange={setSelectedUserId}
                    placeholder="从当前群成员中选择"
                    emptyText="当前群没有已验证成员"
                    disabled={!selectedSessionIsGroup || !memberOptions.length}
                  />
                </label>
                <label className="field">
                  <span>转入成员</span>
                  <SearchableSelect
                    value={transferToUserId}
                    options={memberOptions.filter((item) => item.value !== selectedUserId)}
                    onChange={setTransferToUserId}
                    placeholder="从当前群成员中选择"
                    emptyText="没有可选的转入成员"
                    disabled={!selectedSessionIsGroup || !memberOptions.length}
                  />
                </label>
                <label className="field">
                  <span>转账数量</span>
                  <input
                    type="number"
                    value={transferAmount}
                    onChange={(event) => setTransferAmount(Number(event.target.value))}
                  />
                </label>
                <label className="field">
                  <span>原因</span>
                  <input value={reason} onChange={(event) => setReason(event.target.value)} />
                </label>
              </div>
              <div className="action-row">
                <DangerAction
                  label="执行成员转账"
                  title="确认成员积分转账"
                  confirmLabel="确认转账"
                  pendingLabel="正在转账…"
                  disabled={
                    !selectedSessionIsGroup
                    || !rosterMembers.some((item) => item.wxid === selectedUserId)
                    || !rosterMembers.some((item) => item.wxid === transferToUserId)
                    || selectedUserId === transferToUserId
                    || transferAmount <= 0
                  }
                  impact={(
                    <dl>
                      <div><dt>群会话</dt><dd><code>{effectiveSessionId || "未选择"}</code></dd></div>
                      <div><dt>转出成员</dt><dd><code>{selectedUserId.trim() || "未选择"}</code></dd></div>
                      <div><dt>转入成员</dt><dd><code>{transferToUserId.trim() || "未填写"}</code></dd></div>
                      <div><dt>转账数量</dt><dd>{transferAmount} {creditName}</dd></div>
                      <div><dt>影响</dt><dd>提交后会同时写入双方积分流水；请核对成员标识和数量。</dd></div>
                    </dl>
                  )}
                  onConfirm={() => runOps("transfer")}
                />
              </div>
            </section>
          </div>
        </div>
      </section>

      <section className="panel">
        <div className="panel-header">
          <div>
            <p className="section-kicker">排行榜</p>
            <h3>{creditName} 排行榜</h3>
          </div>
        </div>
        <div className="table-scroll compact-table-scroll">
          <table>
            <caption className="sr-only">当前群积分排行榜</caption>
            <thead>
              <tr>
                <th scope="col">排名</th>
                <th scope="col">成员</th>
                <th scope="col">积分</th>
                <th scope="col">今日签到</th>
              </tr>
            </thead>
            <tbody>
              {leaderboard.map((item) => (
                <tr key={item.user_id}>
                  <td>{item.rank ?? "-"}</td>
                  <th scope="row">{item.display_name || item.user_id}</th>
                  <td>{item.credits ?? 0}</td>
                  <td>{item.checked_in_today ? `+${item.today_reward ?? 0}` : "-"}</td>
                </tr>
              ))}
              {!leaderboard.length && (
                <tr>
                  <td colSpan={4} className="empty-cell">
                    当前会话还没有积分排行榜数据。
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section className="panel">
        <div className="panel-header">
          <div>
            <p className="section-kicker">积分流水</p>
            <h3>最近流水</h3>
          </div>
        </div>
        <div className="table-scroll compact-table-scroll">
          <table>
            <caption className="sr-only">当前群最近积分流水</caption>
            <thead>
              <tr>
                <th scope="col">成员</th>
                <th scope="col">变动</th>
                <th scope="col">原因</th>
                <th scope="col">时间</th>
              </tr>
            </thead>
            <tbody>
              {ledger.map((item) => (
                <tr key={item.id}>
                  <th scope="row">{item.display_name || item.user_id}</th>
                  <td className={Number(item.delta) >= 0 ? "credits-positive" : "credits-negative"}>
                    {formatDelta(item.delta)}
                  </td>
                  <td>{getReasonLabel(item.reason)}</td>
                  <td>{formatTimestamp(item.created_at)}</td>
                </tr>
              ))}
              {!ledger.length && (
                <tr>
                  <td colSpan={4} className="empty-cell">
                    当前会话还没有积分流水。
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section className="panel span-3">
        <div className="panel-header">
          <div>
            <p className="section-kicker">选中流水</p>
            <h3>当前成员最近流水</h3>
          </div>
        </div>
        <div className="table-scroll compact-table-scroll">
          <table>
            <caption className="sr-only">当前成员最近积分流水</caption>
            <thead>
              <tr>
                <th scope="col">时间</th>
                <th scope="col">变动</th>
                <th scope="col">原因</th>
                <th scope="col">actor</th>
              </tr>
            </thead>
            <tbody>
              {memberDetail?.recent_ledger?.map((item) => (
                <tr key={item.id}>
                  <th scope="row">{formatTimestamp(item.created_at)}</th>
                  <td className={Number(item.delta) >= 0 ? "credits-positive" : "credits-negative"}>
                    {formatDelta(item.delta)}
                  </td>
                  <td>{getReasonLabel(item.reason)}</td>
                  <td>{item.actor || "-"}</td>
                </tr>
              ))}
              {!memberDetail?.recent_ledger?.length && (
                <tr>
                  <td colSpan={4} className="empty-cell">
                    当前成员还没有积分流水。
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section className="panel span-3">
        <OutputPanel flush title="积分配置响应" value={configOutput} />
        <OutputPanel flush title="积分操作响应" value={opsOutput} />
      </section>
    </div>
  );
}
