import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { DangerAction } from "../../components/DangerAction";
import { OutputPanel } from "../../components/OutputPanel";
import { PageHeader } from "../../components/PageHeader";
import { SearchableSelect } from "../../components/SearchableSelect";
import { apiRequest, formatJson } from "../../lib/api";
import { useStableIdempotencyKeys } from "../../lib/idempotency";
import { requireSelectedGroup, useConsoleConfig } from "../../state/console-config";

import {
  type WxbotSession,
  type GroupRosterCandidate,
  type GroupRosterPayload,
  type GroupSessionRosterPayload,
  type PersonaProfile,
  type PortraitJob,
  type PortraitRecord,
  type PortraitStylePreview,
  PORTRAIT_CLAIM_SECTIONS,
  getMemberDisplayName,
  isGroupSession,
  portraitConfidenceLabel,
  portraitCoverageLabel,
  portraitJobDurationLabel,
  portraitJobModeLabel,
  portraitJobStatusLabel,
  shortPortraitJobError,
} from "./model";

const PORTRAIT_JOB_POLL_DELAYS = [2_000, 5_000, 10_000, 15_000] as const;

function isActivePortraitJob(job: PortraitJob | null | undefined) {
  return ["queued", "running"].includes(String(job?.status || ""));
}

function isAbortError(error: unknown) {
  return error instanceof DOMException
    ? error.name === "AbortError"
    : error instanceof Error && error.name === "AbortError";
}

export function PersonaWorkspace() {
  const { config, verifiedGroupIds } = useConsoleConfig();
  const { keyFor, clear } = useStableIdempotencyKeys();

  const [sessions, setSessions] = useState<WxbotSession[]>([]);
  const [members, setMembers] = useState<GroupRosterCandidate[]>([]);
  const [jobs, setJobs] = useState<PortraitJob[]>([]);
  const [profiles, setProfiles] = useState<PersonaProfile[]>([]);
  const [portrait, setPortrait] = useState<PortraitRecord | null>(null);
  const [stylePreview, setStylePreview] = useState<PortraitStylePreview | null>(null);

  const [sessionName, setSessionName] = useState("");
  const [selectedMemberWxid, setSelectedMemberWxid] = useState("");
  const [daysLimit, setDaysLimit] = useState(90);
  const [maxMessages, setMaxMessages] = useState(4000);
  const [jobMode, setJobMode] = useState<"full" | "incremental">("full");
  const [styleEnabled, setStyleEnabled] = useState(true);
  const [jobNotice, setJobNotice] = useState("");

  const [selectionOutput, setSelectionOutput] = useState('{\n  "status": "waiting"\n}');
  const [jobOutput, setJobOutput] = useState('{\n  "status": "waiting"\n}');
  const [portraitOutput, setPortraitOutput] = useState('{\n  "status": "waiting"\n}');
  const [profileOutput, setProfileOutput] = useState('{\n  "status": "waiting"\n}');

  const jobRequestRef = useRef<AbortController | null>(null);
  const autoLoadedSessionRef = useRef("");

  const effectiveSessionId = config.sessionId.trim();
  const selectedSessionIsVerified = Boolean(
    effectiveSessionId && verifiedGroupIds.has(effectiveSessionId),
  );
  const memberOptions = useMemo(
    () =>
      members.map((item) => ({
        value: item.wxid,
        label: `${getMemberDisplayName(item)} (${item.wxid})`,
        keywords: [item.wxid, item.name || "", item.alias || "", item.remark || "", item.nick_name || ""],
      })),
    [members],
  );
  const selectedMember = members.find((item) => item.wxid === selectedMemberWxid) || null;
  const selectedMemberName = selectedMember ? getMemberDisplayName(selectedMember) : "";
  const pollingJobId = useMemo(
    () => String(jobs.find(isActivePortraitJob)?.id || ""),
    [jobs],
  );
  const appliedProfile = useMemo(
    () =>
      profiles.find(
        (item) =>
          selectedMemberWxid &&
          (item.target_user_id === selectedMemberWxid ||
            item.skill_slug === `portrait-${selectedMemberWxid.replace(/_/g, "-")}`),
      ) || null,
    [profiles, selectedMemberWxid],
  );

  const loadSessions = useCallback(async () => {
    try {
      const result = await apiRequest<GroupSessionRosterPayload>(
        config,
        "/plugins/wxbot/admin/roster/groups",
        { auth: true },
      );
      const nextSessions = (result.sessions || []).filter(isGroupSession);
      setSessions(nextSessions);
      setSelectionOutput(formatJson({ sessions: nextSessions.slice(0, 50), count: nextSessions.length }));
    } catch (err) {
      setSessions([]);
      setSelectionOutput(formatJson({
        error: err instanceof Error ? err.message : "读取权威群列表失败",
        recovery: "请刷新页面上方的已验证群聊列表后重试",
      }));
    }
  }, [config]);

  const loadMembers = useCallback(async () => {
    if (!selectedSessionIsVerified) {
      setSelectionOutput(formatJson({ error: "请先选择群会话" }));
      setMembers([]);
      return;
    }
    try {
      const result = await apiRequest<GroupRosterPayload>(
        config,
        `/plugins/wxbot/admin/roster/groups/${encodeURIComponent(effectiveSessionId)}/members`,
        { auth: true },
      );
      const candidates = result.candidates || [];
      setMembers(candidates);
      setSelectedMemberWxid((current) => current || candidates[0]?.wxid || "");
      setSelectionOutput(formatJson(result));
    } catch (err) {
      setSelectionOutput(formatJson({ error: err instanceof Error ? err.message : "读取群成员失败" }));
    }
  }, [config, effectiveSessionId, selectedSessionIsVerified]);

  const listJobs = useCallback(async (options?: { quiet?: boolean }) => {
    if (!selectedSessionIsVerified) {
      if (!options?.quiet) setJobOutput(formatJson({ error: "请先选择群会话" }));
      setJobs([]);
      return [] as PortraitJob[];
    }
    try {
      const result = await apiRequest<{ items?: PortraitJob[] }>(
        config,
        "/plugins/speaker_portrait/jobs",
        { query: { tenant_id: config.tenantId, session_id: effectiveSessionId } },
      );
      const items = result.items || [];
      setJobs(items);
      if (!options?.quiet) setJobOutput(formatJson(result));
      return items;
    } catch (err) {
      if (!options?.quiet) {
        setJobs([]);
        setJobOutput(formatJson({ error: err instanceof Error ? err.message : "查询画像任务失败" }));
      }
      return null;
    }
  }, [config, effectiveSessionId, selectedSessionIsVerified]);

  const loadPortrait = useCallback(async (speakerId?: string, options?: { quiet?: boolean }) => {
    const target = String(speakerId || selectedMemberWxid || "").trim();
    if (!target) {
      if (!options?.quiet) setPortraitOutput(formatJson({ error: "请先选择群成员" }));
      setPortrait(null);
      return null;
    }
    try {
      const result = await apiRequest<PortraitRecord>(
        config,
        `/plugins/speaker_portrait/portraits/${encodeURIComponent(target)}`,
        { query: { tenant_id: config.tenantId } },
      );
      setPortrait(result);
      setStylePreview(null);
      if (!options?.quiet) setPortraitOutput(formatJson(result));
      return result;
    } catch (err) {
      setPortrait(null);
      setStylePreview(null);
      if (!options?.quiet) {
        const message = err instanceof Error ? err.message : "读取画像失败";
        setPortraitOutput(formatJson(
          message.includes("portrait_not_found") || message.includes("404")
            ? { status: "portrait_not_found", hint: "该成员还没有画像，请先创建画像任务。" }
            : { error: message },
        ));
      }
      return null;
    }
  }, [config, selectedMemberWxid]);

  const listProfiles = useCallback(async (options?: { quiet?: boolean }) => {
    if (!selectedSessionIsVerified) {
      if (!options?.quiet) setProfileOutput(formatJson({ error: "请先选择群会话" }));
      setProfiles([]);
      return;
    }
    try {
      const result = await apiRequest<{ items?: PersonaProfile[] }>(
        config,
        "/plugins/persona_extract/profiles",
        { query: { tenant_id: config.tenantId, session_id: effectiveSessionId } },
      );
      setProfiles(result.items || []);
      if (!options?.quiet) setProfileOutput(formatJson(result));
    } catch (err) {
      setProfiles([]);
      if (!options?.quiet) {
        setProfileOutput(formatJson({ error: err instanceof Error ? err.message : "读取风格档案失败" }));
      }
    }
  }, [config, effectiveSessionId, selectedSessionIsVerified]);

  useEffect(() => {
    void loadSessions();
  }, [loadSessions]);

  useEffect(() => {
    const matched = sessions.find((item) => item.session_id === effectiveSessionId);
    setSessionName(matched?.session_name || "");
  }, [effectiveSessionId, sessions]);

  useEffect(() => {
    autoLoadedSessionRef.current = "";
    setMembers([]);
    setJobs([]);
    setProfiles([]);
    setPortrait(null);
    setStylePreview(null);
    setSelectedMemberWxid("");
  }, [config.tenantId, effectiveSessionId]);

  useEffect(() => {
    if (!selectedSessionIsVerified) {
      autoLoadedSessionRef.current = "";
      return;
    }
    const sessionKey = `${config.tenantId}:${effectiveSessionId}`;
    if (autoLoadedSessionRef.current === sessionKey) return;
    autoLoadedSessionRef.current = sessionKey;
    void loadMembers();
    void listJobs();
    void listProfiles();
  }, [
    config.tenantId,
    effectiveSessionId,
    listJobs,
    listProfiles,
    loadMembers,
    selectedSessionIsVerified,
  ]);

  useEffect(() => {
    if (!selectedMemberWxid) {
      setPortrait(null);
      setStylePreview(null);
      return;
    }
    void loadPortrait(selectedMemberWxid, { quiet: true });
  }, [loadPortrait, selectedMemberWxid]);

  useEffect(() => {
    if (!pollingJobId || !selectedSessionIsVerified) return undefined;

    let disposed = false;
    let timer: number | undefined;
    let controller: AbortController | null = null;
    let delayIndex = 0;

    const clearTimer = () => {
      if (timer !== undefined) {
        window.clearTimeout(timer);
        timer = undefined;
      }
    };

    const scheduleNext = () => {
      clearTimer();
      if (disposed || document.visibilityState === "hidden") return;
      const delay = PORTRAIT_JOB_POLL_DELAYS[Math.min(delayIndex, PORTRAIT_JOB_POLL_DELAYS.length - 1)];
      delayIndex = Math.min(delayIndex + 1, PORTRAIT_JOB_POLL_DELAYS.length - 1);
      timer = window.setTimeout(() => void poll(), delay);
    };

    const poll = async () => {
      if (disposed || document.visibilityState === "hidden") return;
      if (jobRequestRef.current) {
        scheduleNext();
        return;
      }
      controller = new AbortController();
      jobRequestRef.current = controller;
      let shouldContinue = true;
      try {
        const result = await apiRequest<PortraitJob>(
          config,
          `/plugins/speaker_portrait/jobs/${pollingJobId}`,
          { init: { signal: controller.signal } },
        );
        if (disposed) return;
        setJobs((current) =>
          current.map((item) => (String(item.id) === String(result.id) ? result : item)),
        );
        shouldContinue = isActivePortraitJob(result);
        setJobNotice("");
        if (!shouldContinue) {
          setJobOutput(formatJson(result));
          // A finished job may have refreshed the portrait and (via the
          // backend style sync) any applied reply-style profiles.
          void loadPortrait(result.speaker_id, { quiet: true });
          void listProfiles({ quiet: true });
        }
      } catch (err) {
        if (!isAbortError(err) && !disposed) {
          setJobNotice("任务状态自动刷新暂时失败，页面会继续退避重试；无需重新创建任务。");
        }
      } finally {
        if (jobRequestRef.current === controller) jobRequestRef.current = null;
        controller = null;
        if (shouldContinue && !disposed) scheduleNext();
      }
    };

    const handleVisibilityChange = () => {
      if (document.visibilityState === "hidden") {
        clearTimer();
        controller?.abort();
        return;
      }
      delayIndex = 0;
      void poll();
    };

    document.addEventListener("visibilitychange", handleVisibilityChange);
    scheduleNext();
    return () => {
      disposed = true;
      clearTimer();
      controller?.abort();
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, [config, listProfiles, loadPortrait, pollingJobId, selectedSessionIsVerified]);

  const createJob = async () => {
    const groupId = requireSelectedGroup(config, verifiedGroupIds);
    const member = members.find((item) => item.wxid === selectedMemberWxid);
    if (!member) {
      const error = new Error("画像目标必须来自当前群的已验证成员名册");
      setJobOutput(formatJson({ error: error.message }));
      throw error;
    }
    const intent = `portrait:create:${config.tenantId}:${groupId}:${member.wxid}:${jobMode}:${daysLimit}:${maxMessages}`;
    const clientRequestId = keyFor(intent);
    setJobNotice("");
    try {
      const result = await apiRequest<{ status?: string; job?: PortraitJob }>(
        config,
        "/plugins/speaker_portrait/jobs",
        {
          auth: true,
          init: {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "Idempotency-Key": clientRequestId,
            },
            body: JSON.stringify({
              tenant_id: config.tenantId,
              session_id: groupId,
              session_name: sessionName,
              speaker_id: member.wxid,
              speaker_name: getMemberDisplayName(member),
              connection_id: "legacy-wechat-default",
              external_session_id: groupId,
              days_limit: daysLimit,
              max_messages: maxMessages,
              mode: jobMode,
            }),
          },
        },
      );
      if (result.job) {
        const job = result.job;
        setJobs((current) => {
          const found = current.some((item) => String(item.id) === String(job.id));
          return found
            ? current.map((item) => (String(item.id) === String(job.id) ? job : item))
            : [job, ...current];
        });
      } else {
        await listJobs({ quiet: true });
      }
      setJobOutput(formatJson(result));
      setJobNotice("画像任务已进入队列，页面会自动跟踪任务状态；完成后画像和已应用风格会自动刷新。");
      clear(intent);
    } catch (err) {
      const message = err instanceof Error ? err.message : "创建画像任务失败";
      setJobOutput(formatJson({ error: message }));
      throw err;
    }
  };

  const previewStyle = async () => {
    const target = String(selectedMemberWxid || "").trim();
    if (!target) {
      setPortraitOutput(formatJson({ error: "请先选择群成员" }));
      return;
    }
    try {
      const result = await apiRequest<PortraitStylePreview>(
        config,
        `/plugins/speaker_portrait/portraits/${encodeURIComponent(target)}/style`,
        { query: { tenant_id: config.tenantId } },
      );
      setStylePreview(result);
      setPortraitOutput(formatJson({ status: result.status, name: result.name, prompt_chars: result.prompt_chars }));
    } catch (err) {
      setStylePreview(null);
      setPortraitOutput(formatJson({ error: err instanceof Error ? err.message : "预览风格失败" }));
    }
  };

  const applyStyle = async () => {
    const groupId = requireSelectedGroup(config, verifiedGroupIds);
    const target = String(selectedMemberWxid || "").trim();
    if (!target) {
      setProfileOutput(formatJson({ error: "请先选择群成员" }));
      return;
    }
    const intent = `portrait:apply:${config.tenantId}:${groupId}:${target}:${styleEnabled}`;
    try {
      const result = await apiRequest<PortraitStylePreview>(
        config,
        `/plugins/speaker_portrait/portraits/${encodeURIComponent(target)}/apply-style`,
        {
          auth: true,
          init: {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "Idempotency-Key": keyFor(intent),
            },
            body: JSON.stringify({
              tenant_id: config.tenantId,
              session_id: groupId,
              session_name: sessionName,
              channel: "wechat",
              source_key: "wxbot",
              enabled: styleEnabled,
            }),
          },
        },
      );
      setStylePreview(result);
      setProfileOutput(formatJson(result));
      await listProfiles({ quiet: true });
      clear(intent);
    } catch (err) {
      setProfileOutput(formatJson({ error: err instanceof Error ? err.message : "应用回复风格失败" }));
      throw err;
    }
  };

  const activateProfile = async (profile: PersonaProfile) => {
    const groupId = requireSelectedGroup(config, verifiedGroupIds);
    if (profile.session_id !== groupId) {
      throw new Error("只能启用当前群的风格档案");
    }
    const intent = `portrait:activate:${config.tenantId}:${groupId}:${profile.id}`;
    try {
      const result = await apiRequest<PersonaProfile>(
        config,
        `/plugins/persona_extract/profiles/${profile.id}/activate`,
        {
          auth: true,
          init: {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "Idempotency-Key": keyFor(intent),
            },
            body: JSON.stringify({ tenant_id: config.tenantId, session_id: groupId }),
          },
        },
      );
      setProfileOutput(formatJson(result));
      await listProfiles({ quiet: true });
      clear(intent);
    } catch (err) {
      setProfileOutput(formatJson({ error: err instanceof Error ? err.message : "启用风格档案失败" }));
      throw err;
    }
  };

  const deleteProfile = async (profile: PersonaProfile) => {
    const groupId = requireSelectedGroup(config, verifiedGroupIds);
    if (profile.session_id !== groupId) {
      throw new Error("只能删除当前群的风格档案");
    }
    const deleteQuery = new URLSearchParams({
      tenant_id: config.tenantId,
      session_id: groupId,
    });
    try {
      const result = await apiRequest(
        config,
        `/plugins/persona_extract/profiles/${profile.id}?${deleteQuery.toString()}`,
        { auth: true, init: { method: "DELETE" } },
      );
      setProfileOutput(formatJson(result));
      await listProfiles({ quiet: true });
    } catch (err) {
      setProfileOutput(formatJson({ error: err instanceof Error ? err.message : "删除风格档案失败" }));
      throw err;
    }
  };

  const portraitPayload = portrait?.portrait || null;
  const claimSections = PORTRAIT_CLAIM_SECTIONS.map(({ key, label }) => ({
    key,
    label,
    claims: Array.isArray(portraitPayload?.[key]) ? (portraitPayload?.[key] as Array<Record<string, unknown>>) : [],
  })).filter((section) => section.claims.length > 0);

  return (
    <div className="page-grid persona-page">
      <section className="panel span-2">
        <PageHeader
          eyebrow="回复风格"
          title="人物画像 / 回复风格"
          description="以说话人画像为唯一蒸馏管线：为群成员构建画像，画像编译成回复风格并应用到本群；画像热更新后，已应用的风格会自动同步。"
        />
        <div className="form-grid">
          <div className="field span-2">
            <span>当前已验证群聊</span>
            <strong>{selectedSessionIsVerified ? (sessionName || effectiveSessionId) : "尚未选择"}</strong>
            <small>
              {selectedSessionIsVerified
                ? effectiveSessionId
                : "请从页面上方的后端群聊名册选择；本页不接受手工群 ID。"}
            </small>
          </div>
          <label className="field span-2">
            <span>群成员候选</span>
            <SearchableSelect
              value={selectedMemberWxid}
              onChange={setSelectedMemberWxid}
              options={memberOptions}
              placeholder="请选择群成员"
              searchPlaceholder="搜索成员名或 WXID"
              emptyText={selectedSessionIsVerified ? "暂无群成员" : "请先选择已验证群聊"}
              noResultsText="没有匹配的群成员"
              disabled={!selectedSessionIsVerified || !memberOptions.length}
            />
          </label>
        </div>
        <div className="action-row">
          <button className="button button-secondary" onClick={() => void loadSessions()}>
            刷新群列表
          </button>
          <button
            className="button button-secondary"
            onClick={() => void loadMembers()}
            disabled={!selectedSessionIsVerified}
          >
            加载群成员
          </button>
          <button
            className="button button-secondary"
            onClick={() => void loadPortrait()}
            disabled={!selectedMemberWxid}
          >
            读取画像
          </button>
        </div>
        <div className="table-scroll member-table-scroll">
          <table>
            <caption className="sr-only">当前群画像成员候选</caption>
            <thead>
              <tr>
                <th scope="col">成员</th>
                <th scope="col">WXID</th>
                <th scope="col">发言数</th>
                <th scope="col">最近活跃</th>
                <th scope="col">状态</th>
              </tr>
            </thead>
            <tbody>
              {members.map((item) => (
                <tr
                  key={item.wxid}
                  className={item.wxid === selectedMemberWxid ? "table-row-active" : ""}
                >
                  <th scope="row">
                    <button
                      type="button"
                      className="table-cell-action"
                      onClick={() => setSelectedMemberWxid(item.wxid || "")}
                    >
                      {getMemberDisplayName(item)}
                    </button>
                  </th>
                  <td className="mono">{item.wxid}</td>
                  <td>{item.msg_count ?? 0}</td>
                  <td>{item.last_ts || "-"}</td>
                  <td>{item.has_history ? "可提取" : "无历史"}</td>
                </tr>
              ))}
              {!members.length && (
                <tr>
                  <td colSpan={5}>当前群还没有加载成员候选</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <div className="persona-side-stack">
        <section className="panel panel-scroll">
          <div className="panel-header">
            <div>
              <p className="section-kicker">画像蒸馏</p>
              <h3>画像任务</h3>
            </div>
          </div>
          <div className="form-grid">
            <label className="field">
              <span>画像目标成员</span>
              <strong>{selectedMemberName || "尚未选择"}</strong>
              <small className="mono">{selectedMemberWxid || "仅可从当前群成员名册选择"}</small>
            </label>
            <label className="field">
              <span>任务模式</span>
              <select
                value={jobMode}
                onChange={(event) => setJobMode(event.target.value === "incremental" ? "incremental" : "full")}
              >
                <option value="full">全量画像</option>
                <option value="incremental">增量热更新</option>
              </select>
            </label>
            <label className="field">
              <span>回溯天数</span>
              <input
                type="number"
                value={daysLimit}
                onChange={(event) => setDaysLimit(Number(event.target.value))}
              />
            </label>
            <label className="field">
              <span>最多消息数</span>
              <input
                type="number"
                value={maxMessages}
                onChange={(event) => setMaxMessages(Number(event.target.value))}
              />
            </label>
          </div>
          <div className="action-row">
            <button
              className="button button-primary"
              onClick={() => void createJob()}
              disabled={!selectedSessionIsVerified || !selectedMemberWxid}
            >
              创建画像任务
            </button>
            <button className="button button-secondary" onClick={() => void listJobs()}>
              刷新任务
            </button>
          </div>
          {jobNotice ? <p className="muted-copy">{jobNotice}</p> : null}
          <div className="table-scroll">
            <table>
              <caption className="sr-only">当前群画像蒸馏任务</caption>
              <thead>
                <tr>
                  <th scope="col">#</th>
                  <th scope="col">成员</th>
                  <th scope="col">模式</th>
                  <th scope="col">状态</th>
                  <th scope="col">消息数</th>
                  <th scope="col">耗时</th>
                  <th scope="col">错误</th>
                </tr>
              </thead>
              <tbody>
                {jobs.map((item) => (
                  <tr key={item.id}>
                    <td className="mono">{item.id}</td>
                    <td>{item.speaker_name || item.speaker_id || "-"}</td>
                    <td>{portraitJobModeLabel(item.mode)}</td>
                    <td>{portraitJobStatusLabel(item.status)}</td>
                    <td>{item.message_count ?? 0}</td>
                    <td>{portraitJobDurationLabel(item)}</td>
                    <td>{shortPortraitJobError(item)}</td>
                  </tr>
                ))}
                {!jobs.length && (
                  <tr>
                    <td colSpan={7}>当前群还没有画像任务</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          <OutputPanel title="任务响应" value={jobOutput} />
        </section>

        <section className="panel panel-scroll">
          <div className="panel-header">
            <div>
              <p className="section-kicker">运行时注入</p>
              <h3>已应用回复风格</h3>
            </div>
          </div>
          <p className="muted-copy">
            画像编译出的风格档案按群启用，每个群同一渠道最多一个启用中的档案；画像热更新后，后台会自动重编译并同步这些档案。
          </p>
          <div className="table-scroll">
            <table>
              <caption className="sr-only">当前群回复风格档案</caption>
              <thead>
                <tr>
                  <th scope="col">名称</th>
                  <th scope="col">标识</th>
                  <th scope="col">启用</th>
                  <th scope="col">更新时间</th>
                  <th scope="col">操作</th>
                </tr>
              </thead>
              <tbody>
                {profiles.map((item) => (
                  <tr key={item.id}>
                    <th scope="row">{item.profile_name || item.target_name || `#${item.id}`}</th>
                    <td className="mono">{item.skill_slug || "-"}</td>
                    <td>{item.enabled ? "启用中" : "已停用"}</td>
                    <td>{item.updated_at || "-"}</td>
                    <td>
                      <div className="action-row action-row-compact">
                        <button
                          className="button button-secondary"
                          onClick={() => void activateProfile(item)}
                          disabled={Boolean(item.enabled)}
                        >
                          启用
                        </button>
                        <DangerAction
                          label="删除"
                          title={`删除风格档案 ${item.profile_name || item.id}`}
                          impact="删除后该群将不再注入此回复风格。"
                          confirmLabel="确认删除"
                          onConfirm={() => deleteProfile(item)}
                        />
                      </div>
                    </td>
                  </tr>
                ))}
                {!profiles.length && (
                  <tr>
                    <td colSpan={5}>当前群还没有风格档案；先创建画像再应用。</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          <div className="action-row">
            <button className="button button-secondary" onClick={() => void listProfiles()}>
              刷新档案
            </button>
          </div>
          <OutputPanel title="档案响应" value={profileOutput} />
        </section>
      </div>

      <section className="panel span-2">
        <div className="panel-header">
          <div>
            <p className="section-kicker">画像结果</p>
            <h3>{selectedMemberName ? `${selectedMemberName} 的画像` : "画像"}</h3>
          </div>
        </div>
        {portrait ? (
          <>
            <div className="form-grid">
              <div className="field">
                <span>画像概要</span>
                <strong>{portraitPayload?.summary || "-"}</strong>
              </div>
              <div className="field">
                <span>置信度 / 覆盖</span>
                <strong>
                  {portraitConfidenceLabel(portraitPayload)} · {portraitCoverageLabel(portraitPayload)}
                </strong>
              </div>
              <div className="field">
                <span>最近更新</span>
                <strong>{portrait.updated_at || "-"}</strong>
                <small>
                  热更新{portrait.hot_update_enabled === false ? "已关闭" : "开启"}
                  {typeof portrait.pending_messages === "number"
                    ? ` · 待处理新消息 ${portrait.pending_messages} 条`
                    : ""}
                </small>
              </div>
            </div>
            <div className="form-grid">
              {claimSections.map((section) => (
                <div className="field" key={String(section.key)}>
                  <span>{section.label}</span>
                  <ul className="plain-list">
                    {section.claims.slice(0, 8).map((claim, index) => (
                      <li key={`${String(section.key)}-${index}`}>
                        {String((claim as { text?: string }).text || "")}
                        {Number((claim as { count?: number }).count) > 1
                          ? `（${Number((claim as { count?: number }).count)} 次）`
                          : ""}
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
              {portraitPayload?.unknowns?.length ? (
                <div className="field">
                  <span>未知信息</span>
                  <ul className="plain-list">
                    {portraitPayload.unknowns.slice(0, 8).map((item, index) => (
                      <li key={`unknown-${index}`}>{item}</li>
                    ))}
                  </ul>
                </div>
              ) : null}
            </div>
          </>
        ) : (
          <p className="muted-copy">
            {selectedMemberWxid
              ? "该成员还没有画像；创建画像任务完成后这里会显示画像内容。"
              : "请先从上方名册选择群成员。"}
          </p>
        )}
        <OutputPanel title="画像数据" value={portraitOutput} />
      </section>

      <section className="panel span-2">
        <div className="panel-header">
          <div>
            <p className="section-kicker">画像 → 回复风格</p>
            <h3>编译并应用回复风格</h3>
          </div>
        </div>
        <p className="muted-copy">
          风格由画像即时编译（第一人称 COS 提示词），应用后写入本群风格档案并参与群聊回复。
          {appliedProfile
            ? ` 当前成员已应用：${appliedProfile.profile_name || appliedProfile.skill_slug}（${appliedProfile.enabled ? "启用中" : "已停用"}）。`
            : " 当前成员尚未应用回复风格。"}
        </p>
        <div className="action-row">
          <button
            className="button button-secondary"
            onClick={() => void previewStyle()}
            disabled={!selectedMemberWxid || !portrait}
          >
            预览风格提示词
          </button>
          <label className="toggle-chip">
            <input
              type="checkbox"
              checked={styleEnabled}
              onChange={(event) => setStyleEnabled(event.target.checked)}
            />
            <strong>应用后立即启用</strong>
          </label>
          <button
            className="button button-primary"
            onClick={() => void applyStyle()}
            disabled={!selectedSessionIsVerified || !selectedMemberWxid || !portrait}
          >
            应用为本群回复风格
          </button>
        </div>
        {stylePreview?.prompt ? (
          <label className="field span-2">
            <span>
              风格提示词（{stylePreview.name || selectedMemberName || "-"} · {stylePreview.prompt_chars ?? stylePreview.prompt.length} 字）
            </span>
            <textarea rows={12} value={stylePreview.prompt} readOnly />
          </label>
        ) : null}
      </section>

      <section className="panel span-2">
        <OutputPanel title="选择过程" value={selectionOutput} />
      </section>
    </div>
  );
}
