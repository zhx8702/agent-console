import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { DangerAction } from "../../components/DangerAction";
import { OutputPanel } from "../../components/OutputPanel";
import { PageHeader } from "../../components/PageHeader";
import { SearchableSelect } from "../../components/SearchableSelect";
import { UnsavedChangesGuard } from "../../components/UnsavedChangesGuard";
import { apiRequest, formatJson, parseJsonInput } from "../../lib/api";
import { useStableIdempotencyKeys } from "../../lib/idempotency";
import { requireSelectedGroup, useConsoleConfig } from "../../state/console-config";

import {
  type WxbotSession,
  type GroupRosterCandidate,
  type GroupRosterPayload,
  type GroupSessionRosterPayload,
  type PersonaArtifact,
  type PersonaJob,
  type PersonaProfile,
  isGroupSession,
  getMemberDisplayName,
  buildSkillFrontmatter,
  buildDefaultMeta,
  getArtifactPrompt,
  personaArtifactModeLabel,
  personaJobDurationLabel,
  personaJobRetryLabel,
  personaJobStageLabel,
  personaJobStatusLabel,
  shortJobError,
} from "./model";

const PERSONA_JOB_POLL_DELAYS = [2_000, 5_000, 10_000, 15_000] as const;

function isActivePersonaJob(job: PersonaJob | null | undefined) {
  return ["pending", "queued", "running"].includes(String(job?.status || ""));
}

function isAbortError(error: unknown) {
  return error instanceof DOMException
    ? error.name === "AbortError"
    : error instanceof Error && error.name === "AbortError";
}

type PersonaJobMutationResponse = {
  job_id?: number;
  status?: string;
  accepted?: boolean;
  status_url?: string;
  cancel_requested?: boolean;
  job?: PersonaJob;
};

export function PersonaWorkspace() {
  const { config, verifiedGroupIds } = useConsoleConfig();
  const { keyFor, clear } = useStableIdempotencyKeys();
  const [sessions, setSessions] = useState<WxbotSession[]>([]);
  const [members, setMembers] = useState<GroupRosterCandidate[]>([]);
  const [jobs, setJobs] = useState<PersonaJob[]>([]);
  const [profiles, setProfiles] = useState<PersonaProfile[]>([]);

  const [personaSessionName, setPersonaSessionName] = useState("");
  const [selectedMemberWxid, setSelectedMemberWxid] = useState("");

  const [selectionOutput, setSelectionOutput] = useState('{\n  "status": "waiting"\n}');
  const [output, setOutput] = useState('{\n  "status": "waiting"\n}');
  const [profileOutput, setProfileOutput] = useState('{\n  "status": "waiting"\n}');

  const [targetUserId, setTargetUserId] = useState("");
  const [targetName, setTargetName] = useState("");
  const [fullExtract, setFullExtract] = useState(false);
  const [daysLimit, setDaysLimit] = useState(90);
  const [maxMessages, setMaxMessages] = useState(2000);
  const [jobId, setJobId] = useState("");
  const [messages, setMessages] = useState("");

  const [profileId, setProfileId] = useState("");
  const [profileName, setProfileName] = useState("default");
  const [profileChannel, setProfileChannel] = useState("wechat");
  const [profileSourceKey, setProfileSourceKey] = useState("wxbot");
  const [profileSourceLabel, setProfileSourceLabel] = useState("微信机器人");
  const [profileEnabled, setProfileEnabled] = useState("true");
  const [profileSkillSlug, setProfileSkillSlug] = useState("");

  const [artifactMode, setArtifactMode] = useState("manual");
  const [artifactSkillPrompt, setArtifactSkillPrompt] = useState("");
  const [artifactSkillMd, setArtifactSkillMd] = useState("");
  const [artifactPersonaMd, setArtifactPersonaMd] = useState("");
  const [artifactWorkMd, setArtifactWorkMd] = useState("");
  const [artifactMetaJson, setArtifactMetaJson] = useState("{\n  \n}");
  const [artifactKnowledgeText, setArtifactKnowledgeText] = useState("");
  const [artifactFirstTimestamp, setArtifactFirstTimestamp] = useState("");
  const [artifactLastTimestamp, setArtifactLastTimestamp] = useState("");
  const [artifactMessageCount, setArtifactMessageCount] = useState("0");
  const [profileLoaded, setProfileLoaded] = useState(false);
  const [loadedProfileFingerprint, setLoadedProfileFingerprint] = useState("");
  const [profileBaselineRequest, setProfileBaselineRequest] = useState(0);
  const [profileBaselineCaptured, setProfileBaselineCaptured] = useState(0);
  const [jobNotice, setJobNotice] = useState("");
  const profileDirtyRef = useRef(false);
  const artifactEditorDirtyRef = useRef(false);
  const jobRequestRef = useRef<AbortController | null>(null);

  const effectiveSessionId = config.sessionId.trim();
  const selectedSessionIsVerified = Boolean(effectiveSessionId && verifiedGroupIds.has(effectiveSessionId));
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
  const selectedJob = useMemo(
    () => jobs.find((item) => String(item.id) === String(jobId || "")) || null,
    [jobs, jobId],
  );
  const pollingJobId = useMemo(() => {
    if (isActivePersonaJob(selectedJob)) return String(selectedJob?.id || "");
    return String(jobs.find(isActivePersonaJob)?.id || "");
  }, [jobs, selectedJob]);
  const canApplySelectedJob = Boolean(String(jobId || "").trim()) && (!selectedJob || selectedJob.status === "completed");

  const syncArtifactEditors = useCallback(
    (artifact: PersonaArtifact | null | undefined, fallback?: { promptText?: string; mode?: string; slug?: string }) => {
      artifactEditorDirtyRef.current = false;
      const nextArtifact = artifact || null;
      const nextPrompt = getArtifactPrompt(nextArtifact, fallback?.promptText || "");
      const nextSlug =
        nextArtifact?.slug ||
        fallback?.slug ||
        (typeof nextArtifact?.meta?.slug === "string" ? nextArtifact.meta.slug : "") ||
        profileSkillSlug;
      const nextMode = nextArtifact?.mode || fallback?.mode || "manual";
      const nextKnowledge = nextArtifact?.knowledge?.messages_text || "";
      const nextMeta =
        nextArtifact?.meta && Object.keys(nextArtifact.meta).length
          ? nextArtifact.meta
          : buildDefaultMeta({
              targetName: nextArtifact?.target?.name || targetName,
              targetUserId: nextArtifact?.target?.user_id || targetUserId,
              slug: nextSlug,
              sessionName: personaSessionName,
              sessionId: effectiveSessionId,
              messageCount:
                nextArtifact?.knowledge?.message_count ||
                (nextKnowledge ? nextKnowledge.split("\n").filter(Boolean).length : 0),
              firstTimestamp: nextArtifact?.knowledge?.first_timestamp || "",
              lastTimestamp: nextArtifact?.knowledge?.last_timestamp || "",
            });

      setArtifactMode(nextMode);
      setArtifactSkillPrompt(nextPrompt);
      setArtifactSkillMd(nextArtifact?.files?.["SKILL.md"] || buildSkillFrontmatter(nextSlug, targetName, nextPrompt));
      setArtifactPersonaMd(nextArtifact?.files?.["persona.md"] || "");
      setArtifactWorkMd(nextArtifact?.files?.["work.md"] || "");
      setArtifactMetaJson(formatJson(nextMeta));
      setArtifactKnowledgeText(nextKnowledge);
      setArtifactFirstTimestamp(nextArtifact?.knowledge?.first_timestamp || "");
      setArtifactLastTimestamp(nextArtifact?.knowledge?.last_timestamp || "");
      setArtifactMessageCount(
        String(
          nextArtifact?.knowledge?.message_count ||
            (nextKnowledge ? nextKnowledge.split("\n").filter(Boolean).length : 0),
        ),
      );
      setProfileSkillSlug(nextSlug);
    },
    [effectiveSessionId, personaSessionName, profileSkillSlug, targetName, targetUserId],
  );

  const hydrateProfileForm = useCallback(
    (profile: PersonaProfile, options?: { syncJobId?: boolean }) => {
      setProfileId(String(profile.id || ""));
      setProfileName(String(profile.profile_name || profile.target_name || "default"));
      setProfileChannel(String(profile.channel || "wechat"));
      setProfileSourceKey(String(profile.source_key || "wxbot"));
      setProfileSourceLabel(String(profile.source_label || personaSessionName || ""));
      setProfileEnabled(String(Boolean(profile.enabled)));
      setProfileSkillSlug(String(profile.skill_slug || profile.artifact?.slug || ""));
      if (options?.syncJobId && profile.job_id != null) {
        setJobId(String(profile.job_id));
      }
      if (profile.target_user_id) {
        setTargetUserId(String(profile.target_user_id));
      }
      if (profile.target_name) {
        setTargetName(String(profile.target_name));
      }
      syncArtifactEditors(profile.artifact, {
        promptText: profile.prompt_text || "",
        slug: profile.skill_slug || "",
      });
      setProfileBaselineRequest((current) => current + 1);
    },
    [personaSessionName, syncArtifactEditors],
  );

  const hydrateJobSelection = useCallback(
    (job: PersonaJob, options?: { preserveDirtyArtifact?: boolean }) => {
      setJobId(String(job.id || ""));
      setTargetUserId(String(job.target_user_id || ""));
      setTargetName(String(job.target_name || ""));
      if (
        String(job.status || "") === "completed"
        && !(
          options?.preserveDirtyArtifact
          && (profileDirtyRef.current || artifactEditorDirtyRef.current)
        )
      ) {
        syncArtifactEditors(job.artifact, {
          promptText: job.result_text || "",
          mode: job.mode || "",
          slug: job.output_slug || "",
        });
      }
      setOutput(formatJson(job));
    },
    [syncArtifactEditors],
  );

  const mergeJobUpdate = useCallback(
    (job: PersonaJob, options?: { select?: boolean; preserveDirtyArtifact?: boolean }) => {
      setJobs((current) => {
        const found = current.some((item) => String(item.id) === String(job.id));
        return found
          ? current.map((item) => (String(item.id) === String(job.id) ? job : item))
          : [job, ...current];
      });
      if (options?.select || String(job.id) === String(jobId || "")) {
        hydrateJobSelection(job, {
          preserveDirtyArtifact: options?.preserveDirtyArtifact ?? true,
        });
      }
    },
    [hydrateJobSelection, jobId],
  );

  const loadSessions = useCallback(async () => {
    try {
      const result = await apiRequest<GroupSessionRosterPayload>(config, "/plugins/wxbot/admin/roster/groups", {
        auth: true,
      });
      const nextSessions = (result.sessions || []).filter(isGroupSession);
      setSessions(nextSessions);
      if (effectiveSessionId) {
        const matched = nextSessions.find((item) => item.session_id === effectiveSessionId);
        setPersonaSessionName(matched?.session_name || "");
      }
      setSelectionOutput(formatJson({ sessions: nextSessions.slice(0, 50), count: nextSessions.length }));
    } catch (err) {
      setSessions([]);
      setPersonaSessionName("");
      setSelectionOutput(formatJson({
        error: err instanceof Error ? err.message : "读取权威群列表失败",
        recovery: "请刷新页面上方的已验证群聊列表后重试",
      }));
    }
  }, [config, effectiveSessionId]);

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
      if (candidates[0] && !selectedMemberWxid) {
        setSelectedMemberWxid(candidates[0].wxid || "");
      }
      setSelectionOutput(formatJson(result));
    } catch (err) {
      setSelectionOutput(formatJson({ error: err instanceof Error ? err.message : "读取群成员失败" }));
    }
  }, [config, effectiveSessionId, selectedMemberWxid, selectedSessionIsVerified]);

  const listJobs = useCallback(async (options?: { hydrateFirst?: boolean; quiet?: boolean }) => {
    if (!selectedSessionIsVerified) {
      if (!options?.quiet) setOutput(formatJson({ error: "请先选择群会话" }));
      setJobs([]);
      return [] as PersonaJob[];
    }
    try {
      const result = await apiRequest<{ items?: PersonaJob[] }>(config, "/plugins/persona_extract/jobs", {
        query: { tenant_id: config.tenantId, session_id: effectiveSessionId },
      });
      const items = result.items || [];
      setJobs(items);
      if (items[0] && !jobId && options?.hydrateFirst !== false) {
        hydrateJobSelection(items[0], { preserveDirtyArtifact: true });
      }
      const active = items.find((item) => String(item.id) === String(jobId || "")) || items[0] || null;
      if (!options?.quiet) setOutput(formatJson(active || result));
      return items;
    } catch (err) {
      if (!options?.quiet) {
        setJobs([]);
        setOutput(formatJson({ error: err instanceof Error ? err.message : "查询任务失败" }));
      }
      return null;
    }
  }, [config, effectiveSessionId, hydrateJobSelection, jobId, selectedSessionIsVerified]);

  const getJob = useCallback(async () => {
    const scopedJob = jobs.find((item) => String(item.id) === String(jobId));
    if (!selectedSessionIsVerified || !scopedJob) {
      setOutput(formatJson({ error: "请先从当前群任务列表选择任务" }));
      return;
    }
    if (jobRequestRef.current) return;
    const controller = new AbortController();
    jobRequestRef.current = controller;
    try {
      const result = await apiRequest<PersonaJob>(config, `/plugins/persona_extract/jobs/${jobId}`, {
        init: { signal: controller.signal },
      });
      mergeJobUpdate(result, { select: true, preserveDirtyArtifact: false });
    } catch (err) {
      if (!isAbortError(err)) {
        setOutput(formatJson({ error: err instanceof Error ? err.message : "读取任务失败" }));
      }
    } finally {
      if (jobRequestRef.current === controller) jobRequestRef.current = null;
    }
  }, [config, jobId, jobs, mergeJobUpdate, selectedSessionIsVerified]);

  const reconcileSubmittedJob = useCallback(async (
    clientRequestId: string,
    fallback?: { jobId: string; previousAttemptCount: number },
  ) => {
    const items = await listJobs({ hydrateFirst: false, quiet: true });
    if (!items) return null;
    const exact = items.find((item) => item.client_request_id === clientRequestId);
    const fallbackMatch = fallback
      ? items.find((item) => (
          String(item.id) === fallback.jobId
          && (
            isActivePersonaJob(item)
            || Number(item.attempt_count || 0) > fallback.previousAttemptCount
          )
        ))
      : null;
    const recovered = exact || fallbackMatch || null;
    if (recovered) {
      mergeJobUpdate(recovered, { select: true, preserveDirtyArtifact: true });
    }
    return recovered;
  }, [listJobs, mergeJobUpdate]);

  const listProfiles = useCallback(async (options?: { hydrateFirst?: boolean }) => {
    if (!selectedSessionIsVerified) {
      setProfileOutput(formatJson({ error: "请先选择群会话" }));
      setProfiles([]);
      setProfileLoaded(false);
      return;
    }
    try {
      const result = await apiRequest<{ items?: PersonaProfile[] }>(config, "/plugins/persona_extract/profiles", {
        query: { tenant_id: config.tenantId, session_id: effectiveSessionId },
      });
      const items = result.items || [];
      setProfiles(items);
      setProfileLoaded(true);
      if (items[0] && options?.hydrateFirst !== false) {
        hydrateProfileForm(items[0], { syncJobId: false });
      } else if (!items.length) {
        setProfileId("");
        setProfileName(targetName || "default");
        setProfileSkillSlug("");
        setProfileSourceKey("wxbot");
        setProfileSourceLabel(personaSessionName || "当前群");
        setProfileBaselineRequest((current) => current + 1);
      }
      setProfileOutput(formatJson(result));
    } catch (err) {
      setProfiles([]);
      setProfileLoaded(false);
      setProfileOutput(formatJson({ error: err instanceof Error ? err.message : "读取风格技能失败" }));
    }
  }, [config, effectiveSessionId, hydrateProfileForm, personaSessionName, selectedSessionIsVerified, targetName]);

  useEffect(() => {
    void loadSessions();
  }, [loadSessions]);

  useEffect(() => {
    const matched = sessions.find((item) => item.session_id === effectiveSessionId);
    setPersonaSessionName(matched?.session_name || "");
    setProfileSourceLabel(matched?.session_name || (effectiveSessionId ? "当前群" : ""));
    setMembers([]);
    setJobs([]);
    setProfiles([]);
    setSelectedMemberWxid("");
    setTargetUserId("");
    setTargetName("");
    setProfileLoaded(false);
    setLoadedProfileFingerprint("");
  }, [effectiveSessionId, sessions]);

  useEffect(() => {
    if (selectedSessionIsVerified) {
      void loadMembers();
      void listJobs();
      void listProfiles({ hydrateFirst: false });
    }
  }, [listJobs, listProfiles, loadMembers, selectedSessionIsVerified]);

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
      const delay = PERSONA_JOB_POLL_DELAYS[Math.min(delayIndex, PERSONA_JOB_POLL_DELAYS.length - 1)];
      delayIndex = Math.min(delayIndex + 1, PERSONA_JOB_POLL_DELAYS.length - 1);
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
        const result = await apiRequest<PersonaJob>(
          config,
          `/plugins/persona_extract/jobs/${pollingJobId}`,
          { init: { signal: controller.signal } },
        );
        if (disposed) return;
        mergeJobUpdate(result, { preserveDirtyArtifact: true });
        shouldContinue = isActivePersonaJob(result);
        setJobNotice("");
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
  }, [config, mergeJobUpdate, pollingJobId, selectedSessionIsVerified]);

  const applySelectedMember = () => {
    if (!selectedMember) {
      setSelectionOutput(formatJson({ error: "请先选择群成员" }));
      return;
    }
    setTargetUserId(selectedMember.wxid || "");
    setTargetName(getMemberDisplayName(selectedMember));
    setProfileName(getMemberDisplayName(selectedMember));
    setSelectionOutput(formatJson({ applied: true, session_id: effectiveSessionId, member: selectedMember }));
  };

  const createJob = async () => {
    const groupId = requireSelectedGroup(config, verifiedGroupIds);
    const member = members.find((item) => item.wxid === targetUserId);
    if (!member) {
      const error = new Error("蒸馏目标必须来自当前群的已验证成员名册");
      setOutput(formatJson({ error: error.message }));
      throw error;
    }
    const intent = `persona:create:${config.tenantId}:${groupId}:${targetUserId}:${fullExtract}:${daysLimit}:${maxMessages}`;
    const clientRequestId = keyFor(intent);
    setJobNotice("");
    try {
      const result = await apiRequest<PersonaJobMutationResponse & {
        result?: {
          prompt_text?: string;
          skill_slug?: string;
          mode?: string;
          artifact?: PersonaArtifact;
        };
      }>(config, "/plugins/persona_extract/jobs", {
        auth: true,
        init: {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": clientRequestId,
          },
          body: JSON.stringify({
            client_request_id: clientRequestId,
            tenant_id: config.tenantId,
            session_id: groupId,
            session_name: personaSessionName,
            connection_id: "legacy-wechat-default",
            adapter_id: "wxbot",
            external_session_id: groupId,
            target_user_id: targetUserId,
            target_name: targetName,
            days_limit: fullExtract ? 0 : daysLimit,
            max_messages: fullExtract ? 0 : maxMessages,
            messages: parseJsonInput(messages, []),
          }),
        },
      });
      if (result.job_id != null) {
        setJobId(String(result.job_id));
      }
      if (result.job) {
        mergeJobUpdate(result.job, { select: true, preserveDirtyArtifact: true });
      }
      if (result.result?.artifact && !artifactEditorDirtyRef.current) {
        syncArtifactEditors(result.result.artifact, {
          promptText: result.result.prompt_text || "",
          mode: result.result.mode || "",
          slug: result.result.skill_slug || "",
        });
      }
      if (!result.job) await listJobs({ hydrateFirst: false });
      setOutput(formatJson(result));
      setJobNotice(result.accepted === false
        ? "任务已记录，但执行器暂未接收；请稍后刷新任务状态。"
        : "任务已进入异步队列，页面会自动刷新当前任务状态。",
      );
      clear(intent);
    } catch (err) {
      const recovered = await reconcileSubmittedJob(clientRequestId);
      if (recovered) {
        setJobNotice("提交响应中断，但已按请求标识核对到任务；无需重复创建。页面将继续跟踪该任务。");
        setOutput(formatJson({
          status: "submission_result_reconciled",
          client_request_id: clientRequestId,
          job: recovered,
        }));
        clear(intent);
        return;
      }
      const message = err instanceof Error ? err.message : "创建蒸馏任务失败";
      setJobNotice(`提交结果未知（请求标识 ${clientRequestId}）。请先刷新任务核对，不要盲目重复提交。`);
      setOutput(formatJson({
        status: "submission_result_unknown",
        client_request_id: clientRequestId,
        error: message,
      }));
      throw new Error("提交结果未知，系统未核对到对应任务；请先刷新任务列表后再决定是否重试");
    }
  };

  const buildArtifactDraft = () => {
    let parsedMeta: Record<string, unknown>;
    try {
      parsedMeta = artifactMetaJson.trim()
        ? (JSON.parse(artifactMetaJson) as Record<string, unknown>)
        : {};
    } catch (err) {
      throw new Error(`meta.json 不是合法 JSON：${err instanceof Error ? err.message : "未知错误"}`);
    }

    const slug = profileSkillSlug || targetUserId || "default";
    const skillPrompt = artifactSkillPrompt.trim();
    const skillMd = (artifactSkillMd.trim() || buildSkillFrontmatter(slug, targetName, skillPrompt)).trim();
    const messageCount =
      Number(artifactMessageCount) ||
      artifactKnowledgeText
        .split("\n")
        .map((item) => item.trim())
        .filter(Boolean).length;

    const meta =
      Object.keys(parsedMeta).length > 0
        ? parsedMeta
        : buildDefaultMeta({
            targetName,
            targetUserId,
            slug,
            sessionName: personaSessionName,
            sessionId: effectiveSessionId,
            messageCount,
            firstTimestamp: artifactFirstTimestamp,
            lastTimestamp: artifactLastTimestamp,
          });

    return {
      version: "persona-skill-v1",
      generated_at: new Date().toISOString(),
      slug,
      mode: artifactMode || "manual",
      target: {
        user_id: targetUserId,
        name: targetName,
      },
      source: {
        tenant_id: config.tenantId,
        session_id: effectiveSessionId,
        session_name: personaSessionName,
        channel: profileChannel,
        source_key: profileSourceKey || "wxbot",
        source_label: profileSourceLabel || personaSessionName || "当前群",
        job_id: jobId ? Number(jobId) : null,
      },
      knowledge: {
        message_count: messageCount,
        first_timestamp: artifactFirstTimestamp,
        last_timestamp: artifactLastTimestamp,
        messages_text: artifactKnowledgeText,
        knowledge_sources: Array.isArray((meta as { knowledge_sources?: unknown }).knowledge_sources)
          ? ((meta as { knowledge_sources: unknown[] }).knowledge_sources as string[])
          : [],
        source_sessions: Array.isArray((meta as { source_sessions?: unknown }).source_sessions)
          ? ((meta as { source_sessions: unknown[] }).source_sessions as string[])
          : [effectiveSessionId].filter(Boolean),
      },
      files: {
        "SKILL.md": skillMd,
        skill_prompt: skillPrompt,
        "persona.md": artifactPersonaMd,
        "work.md": artifactWorkMd,
      },
      meta,
    } satisfies PersonaArtifact;
  };

  const saveProfile = async () => {
    const groupId = requireSelectedGroup(config, verifiedGroupIds);
    if (!profileLoaded) {
      const error = new Error("当前群风格技能尚未成功读取，请先读取后再保存，避免覆盖线上配置");
      setProfileOutput(formatJson({ error: error.message }));
      throw error;
    }
    if (!members.some((item) => item.wxid === targetUserId)) {
      const error = new Error("目标人物必须来自当前群的已验证成员名册");
      setProfileOutput(formatJson({ error: error.message }));
      throw error;
    }
    const intent = `persona:save:${config.tenantId}:${groupId}:${profileId || "new"}:${profileSkillSlug}:${targetUserId}`;
    try {
      const artifact = buildArtifactDraft();
      const result = await apiRequest<PersonaProfile>(config, "/plugins/persona_extract/profiles", {
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
            session_name: personaSessionName,
            channel: profileChannel,
            source_key: profileSourceKey || "wxbot",
            source_label: profileSourceLabel || personaSessionName || "当前群",
            profile_name: profileName || targetName || "default",
            target_user_id: targetUserId,
            target_name: targetName,
            skill_slug: profileSkillSlug,
            prompt_text: artifactSkillPrompt,
            artifact,
            enabled: profileEnabled === "true",
            job_id: jobId ? Number(jobId) : null,
          }),
        },
      });
      if (result.id != null) {
        setProfileId(String(result.id));
      }
      await listProfiles();
      setProfileOutput(formatJson(result));
      clear(intent);
    } catch (err) {
      setProfileOutput(formatJson({ error: err instanceof Error ? err.message : "保存风格技能失败" }));
      throw err;
    }
  };

  const applyJobToProfile = async (selectedJobId?: number | string) => {
    const groupId = requireSelectedGroup(config, verifiedGroupIds);
    const effectiveJobId = String(selectedJobId || jobId || "").trim();
    if (!effectiveJobId) {
      setProfileOutput(formatJson({ error: "请先选择任务 ID" }));
      return;
    }
    const numericJobId = Number(effectiveJobId);
    if (!Number.isFinite(numericJobId)) {
      setProfileOutput(formatJson({ error: "任务 ID 必须是数字" }));
      return;
    }
    const jobForApply = jobs.find((item) => String(item.id) === effectiveJobId) || null;
    if (
      !jobForApply
      || jobForApply.session_id !== groupId
      || jobForApply.status !== "completed"
      || !members.some((item) => item.wxid === jobForApply.target_user_id)
    ) {
      const error = new Error("只能应用当前已验证群内已完成的蒸馏任务");
      setProfileOutput(formatJson({ error: error.message }));
      throw error;
    }
    const intent = `persona:apply:${config.tenantId}:${groupId}:${numericJobId}:${profileEnabled}`;
    try {
      const result = await apiRequest<PersonaProfile>(config, "/plugins/persona_extract/profiles/apply-job", {
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
            session_name: personaSessionName || jobForApply?.session_name || "",
            job_id: numericJobId,
            channel: profileChannel,
            source_key: profileSourceKey || "wxbot",
            source_label: profileSourceLabel || personaSessionName || "当前群",
            profile_name: jobForApply?.target_name || profileName || targetName || "default",
            enabled: profileEnabled === "true",
          }),
        },
      });
      hydrateProfileForm(result, { syncJobId: false });
      await listProfiles({ hydrateFirst: false });
      hydrateProfileForm(result, { syncJobId: false });
      setProfileOutput(formatJson(result));
      clear(intent);
    } catch (err) {
      setProfileOutput(formatJson({ error: err instanceof Error ? err.message : "应用蒸馏任务失败" }));
      throw err;
    }
  };

  const rerunJob = async (selectedJobId: number | string) => {
    const groupId = requireSelectedGroup(config, verifiedGroupIds);
    const effectiveJobId = String(selectedJobId || "").trim();
    if (!effectiveJobId) {
      setOutput(formatJson({ error: "请先选择任务 ID" }));
      return;
    }
    const scopedJob = jobs.find((item) => String(item.id) === effectiveJobId);
    if (
      !scopedJob
      || scopedJob.session_id !== groupId
      || !members.some((item) => item.wxid === scopedJob.target_user_id)
    ) {
      const error = new Error("只能重跑当前已验证群内的任务");
      setOutput(formatJson({ error: error.message }));
      throw error;
    }
    const intent = `persona:rerun:${config.tenantId}:${groupId}:${effectiveJobId}`;
    const clientRequestId = keyFor(intent);
    const previousAttemptCount = Number(scopedJob.attempt_count || 0);
    setJobNotice("");
    try {
      const result = await apiRequest<PersonaJobMutationResponse>(config, `/plugins/persona_extract/jobs/${effectiveJobId}/run`, {
        auth: true,
        init: {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": clientRequestId,
          },
          body: "[]",
        },
      });
      if (result.job_id != null) {
        setJobId(String(result.job_id));
      }
      if (result.job) {
        mergeJobUpdate(result.job, { select: true, preserveDirtyArtifact: true });
      }
      if (!result.job) await listJobs({ hydrateFirst: false });
      setOutput(formatJson(result));
      setJobNotice(result.accepted === false
        ? "重跑请求已记录，但执行器暂未接收；请稍后刷新任务状态。"
        : "任务已重新进入异步队列。",
      );
      clear(intent);
    } catch (err) {
      const recovered = await reconcileSubmittedJob(clientRequestId, {
        jobId: effectiveJobId,
        previousAttemptCount,
      });
      if (recovered) {
        setJobNotice("重跑响应中断，但已核对到任务进入执行流程；无需再次重跑。");
        setOutput(formatJson({ status: "submission_result_reconciled", job: recovered }));
        clear(intent);
        return;
      }
      const message = err instanceof Error ? err.message : "重跑任务失败";
      setJobNotice(`重跑结果未知（请求标识 ${clientRequestId}）。请先刷新任务核对，不要重复操作。`);
      setOutput(formatJson({
        status: "submission_result_unknown",
        client_request_id: clientRequestId,
        error: message,
      }));
      throw new Error("重跑结果未知，系统未核对到任务状态变化；请先刷新任务列表");
    }
  };

  const cancelJob = async (selectedJobId: number | string) => {
    const groupId = requireSelectedGroup(config, verifiedGroupIds);
    const effectiveJobId = String(selectedJobId || "").trim();
    const scopedJob = jobs.find((item) => String(item.id) === effectiveJobId);
    if (!scopedJob || scopedJob.session_id !== groupId || !isActivePersonaJob(scopedJob)) {
      const error = new Error("只能取消当前已验证群内仍在排队或运行的任务");
      setOutput(formatJson({ error: error.message }));
      throw error;
    }
    const intent = `persona:cancel:${config.tenantId}:${groupId}:${effectiveJobId}`;
    const clientRequestId = keyFor(intent);
    setJobNotice("");
    try {
      const result = await apiRequest<PersonaJob | PersonaJobMutationResponse>(
        config,
        `/plugins/persona_extract/jobs/${effectiveJobId}/cancel`,
        {
          auth: true,
          init: {
            method: "POST",
            headers: { "Idempotency-Key": clientRequestId },
          },
        },
      );
      const nestedJob = "job" in result ? result.job : undefined;
      const directJob = "id" in result ? result : undefined;
      const updatedJob = nestedJob || directJob || {
        ...scopedJob,
        status: result.status || scopedJob.status,
        cancel_requested: result.cancel_requested ?? true,
      };
      mergeJobUpdate(updatedJob, { preserveDirtyArtifact: true });
      setOutput(formatJson(result));
      setJobNotice(updatedJob.status === "cancelled"
        ? `任务 #${effectiveJobId} 已取消。`
        : `任务 #${effectiveJobId} 已申请取消，执行器会在安全检查点停止。`,
      );
      clear(intent);
    } catch (err) {
      const items = await listJobs({ hydrateFirst: false, quiet: true });
      const recovered = items?.find((item) => (
        String(item.id) === effectiveJobId
        && (item.status === "cancelled" || item.cancel_requested)
      ));
      if (recovered) {
        mergeJobUpdate(recovered, { preserveDirtyArtifact: true });
        setJobNotice("取消响应中断，但已核对到取消状态；无需重复操作。");
        clear(intent);
        return;
      }
      const message = err instanceof Error ? err.message : "取消任务失败";
      setJobNotice("取消结果未知，请刷新任务确认状态后再操作。");
      setOutput(formatJson({ status: "cancel_result_unknown", job_id: effectiveJobId, error: message }));
      throw new Error("取消结果未知，请刷新任务确认状态后再操作");
    }
  };

  const deleteProfile = async () => {
    const groupId = requireSelectedGroup(config, verifiedGroupIds);
    const scopedProfile = profiles.find((item) => String(item.id) === String(profileId));
    if (!scopedProfile || scopedProfile.session_id !== groupId) {
      const error = new Error("请先从当前群的风格技能列表选择目标");
      setProfileOutput(formatJson({ error: error.message }));
      throw error;
    }
    const intent = `persona:delete:${config.tenantId}:${groupId}:${profileId}`;
    try {
      const deleteQuery = new URLSearchParams({
        tenant_id: config.tenantId,
        session_id: groupId,
      });
      const result = await apiRequest(config, `/plugins/persona_extract/profiles/${profileId}?${deleteQuery.toString()}`, {
        auth: true,
        init: {
          method: "DELETE",
          headers: { "Idempotency-Key": keyFor(intent) },
        },
      });
      await listProfiles();
      setProfileOutput(formatJson(result));
      clear(intent);
    } catch (err) {
      setProfileOutput(formatJson({ error: err instanceof Error ? err.message : "删除风格技能失败" }));
      throw err;
    }
  };

  const profileDraftFingerprint = useMemo(
    () => JSON.stringify({
      profileId,
      profileName,
      profileChannel,
      profileSourceKey,
      profileSourceLabel,
      profileEnabled,
      profileSkillSlug,
      targetUserId,
      targetName,
      jobId,
      artifactMode,
      artifactSkillPrompt,
      artifactSkillMd,
      artifactPersonaMd,
      artifactWorkMd,
      artifactMetaJson,
      artifactKnowledgeText,
      artifactFirstTimestamp,
      artifactLastTimestamp,
      artifactMessageCount,
    }),
    [
      artifactFirstTimestamp,
      artifactKnowledgeText,
      artifactLastTimestamp,
      artifactMessageCount,
      artifactMetaJson,
      artifactMode,
      artifactPersonaMd,
      artifactSkillMd,
      artifactSkillPrompt,
      artifactWorkMd,
      jobId,
      profileChannel,
      profileEnabled,
      profileId,
      profileName,
      profileSkillSlug,
      profileSourceKey,
      profileSourceLabel,
      targetName,
      targetUserId,
    ],
  );
  const profileDirty = profileLoaded
    && Boolean(loadedProfileFingerprint)
    && profileDraftFingerprint !== loadedProfileFingerprint;

  useEffect(() => {
    profileDirtyRef.current = profileDirty;
  }, [profileDirty]);

  useEffect(() => {
    if (profileBaselineRequest <= profileBaselineCaptured) {
      return;
    }
    setLoadedProfileFingerprint(profileDraftFingerprint);
    setProfileBaselineCaptured(profileBaselineRequest);
  }, [profileBaselineCaptured, profileBaselineRequest, profileDraftFingerprint]);

  const artifactSummary = useMemo(
    () => ({
      slug: profileSkillSlug || "-",
      mode: artifactMode || "-",
      messages: artifactMessageCount || "0",
      first: artifactFirstTimestamp || "-",
      last: artifactLastTimestamp || "-",
    }),
    [artifactFirstTimestamp, artifactLastTimestamp, artifactMessageCount, artifactMode, profileSkillSlug],
  );

  return (
    <div className="page-grid persona-page">
      <UnsavedChangesGuard when={profileDirty} />
      <section className="panel span-2">
        <PageHeader
          eyebrow="回复风格"
          title="人物蒸馏 / 回复风格"
          description="沿用旧版微信机器人的人物蒸馏流程：先选择群成员提炼风格，再围绕 work.md、persona.md、SKILL.md 和 meta.json 管理、应用完整产物，而不是只保存一段模型总结。"
        />
        <div className="data-flow-note">
          <strong>当前链路</strong>
          <span>控制台会通过 SDK 的 <code>ext/persona/messages</code> 拉取目标成员消息，再在本地生成工作记录、人物描述、技能说明和元数据产物。</span>
          <span>增量蒸馏会尽量复用同一人物已有产物中的知识文本与技能标识，延续旧版 <code>distill_worker</code> 的技能累积流程。</span>
        </div>
        <div className="form-grid">
          <div className="field span-2">
            <span>当前已验证群聊</span>
            <strong>{selectedSessionIsVerified ? (personaSessionName || effectiveSessionId) : "尚未选择"}</strong>
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
          <button className="button button-secondary" onClick={() => void loadMembers()} disabled={!selectedSessionIsVerified}>
            加载群成员
          </button>
          <button className="button button-primary" onClick={applySelectedMember} disabled={!selectedMember}>
            应用到蒸馏目标
          </button>
        </div>
        <div className="persona-subhead">
          <p className="muted-copy">
            当前会话：<span className="mono">{effectiveSessionId || "-"}</span>
          </p>
          <p className="muted-copy">候选列表来自群成员名册，`可提取` 表示当前消息库中存在该成员可用于蒸馏的文本记录。</p>
        </div>
        <div className="table-scroll member-table-scroll">
          <table>
            <caption className="sr-only">当前群画像蒸馏成员候选</caption>
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
                      onClick={() => {
                      setSelectedMemberWxid(item.wxid || "");
                      setTargetUserId(item.wxid || "");
                      setTargetName(getMemberDisplayName(item));
                      setProfileName(getMemberDisplayName(item));
                      }}
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
              <p className="section-kicker">风格提炼任务</p>
              <h3>蒸馏任务</h3>
            </div>
          </div>
          <div className="form-grid">
            <label className="field">
              <span>蒸馏目标成员</span>
              <strong>{targetName || "尚未选择"}</strong>
              <small className="mono">{targetUserId || "仅可从当前群成员名册选择"}</small>
            </label>
            <label className="field">
              <span>目标来源</span>
              <strong>{members.some((item) => item.wxid === targetUserId) ? "已验证群成员" : "未验证"}</strong>
              <small>不会接受手工 user_id。</small>
            </label>
            <div className="field span-2 field-toggle">
              <span>提取范围</span>
              <label className="toggle-chip">
                <input type="checkbox" checked={fullExtract} onChange={(event) => setFullExtract(event.target.checked)} />
                <strong>全量刷新</strong>
                <em>开启后会按全量重建语义重新生成风格技能，不再限制回溯天数和消息数。</em>
              </label>
            </div>
            <label className="field">
              <span>回溯天数</span>
              <input
                type="number"
                value={daysLimit}
                disabled={fullExtract}
                onChange={(event) => setDaysLimit(Number(event.target.value))}
              />
            </label>
            <label className="field">
              <span>最多消息数</span>
              <input
                type="number"
                value={maxMessages}
                disabled={fullExtract}
                onChange={(event) => setMaxMessages(Number(event.target.value))}
              />
            </label>
            <label className="field span-2">
              <span>当前任务</span>
              <select value={jobId} onChange={(event) => setJobId(event.target.value)}>
                <option value="">从当前群任务列表选择</option>
                {jobs.map((item) => (
                  <option key={item.id} value={item.id}>{`#${item.id} · ${item.target_name || item.target_user_id || "未命名"} · ${personaJobStatusLabel(item.status)}`}</option>
                ))}
              </select>
            </label>
            <details className="technical-details span-2">
              <summary>技术详情：测试消息 JSON</summary>
              <label className="field">
                <span>仅用于授权测试数据；留空时从当前群按目标成员采样</span>
                <textarea
                  rows={6}
                  value={messages}
                  onChange={(event) => setMessages(event.target.value)}
                  placeholder='[{"sender_name":"Alice","text":"...","timestamp":"2026-04-21 10:00:00"}]'
                />
              </label>
            </details>
          </div>
          <div className="action-row">
            <button className="button button-secondary" onClick={() => void listJobs()}>
              刷新任务
            </button>
            <button className="button button-secondary" onClick={() => void getJob()} disabled={!jobId}>
              读取任务
            </button>
            <DangerAction
              label="创建并执行蒸馏"
              title="确认创建人物蒸馏任务"
              confirmLabel="确认创建任务"
              pendingLabel="正在创建…"
              disabled={
                !selectedSessionIsVerified
                || !members.some((item) => item.wxid === targetUserId)
              }
              impact={(
                <dl>
                  <div><dt>目标群</dt><dd><code>{effectiveSessionId || "未选择"}</code></dd></div>
                  <div><dt>目标成员</dt><dd>{targetName || "未选择"} <code>{targetUserId || ""}</code></dd></div>
                  <div><dt>样本范围</dt><dd>{fullExtract ? "全量重建" : `最近 ${daysLimit} 天，最多 ${maxMessages} 条`}</dd></div>
                  <div><dt>影响</dt><dd>会读取该成员在当前群内获授权的消息样本并生成可审计的风格产物。</dd></div>
                </dl>
              )}
              onConfirm={createJob}
            />
          </div>
          {jobNotice ? <p className="muted-copy" role="status">{jobNotice}</p> : null}
          <div
            className="table-scroll compact-table-scroll"
            role="region"
            aria-label="当前群人物画像蒸馏任务表格"
            tabIndex={0}
          >
            <table>
              <caption className="sr-only">当前群人物画像蒸馏任务</caption>
              <thead>
                <tr>
                  <th scope="col">ID</th>
                  <th scope="col">人物</th>
                  <th scope="col">技能标识 / 生成模式</th>
                  <th scope="col">阶段</th>
                  <th scope="col">耗时 / 尝试</th>
                  <th scope="col">状态</th>
                  <th scope="col">操作</th>
                </tr>
              </thead>
              <tbody>
                {jobs.map((item) => (
                  <tr
                    key={item.id}
                    className={String(item.id) === String(jobId || "") ? "table-row-active" : ""}
                  >
                    <th scope="row" className="mono">
                      <button type="button" className="table-cell-action mono" onClick={() => hydrateJobSelection(item)}>
                        {item.id}
                      </button>
                    </th>
                    <td>{item.target_name || item.target_user_id || "-"}</td>
                    <td>{item.output_slug || "-"} / {personaArtifactModeLabel(item.mode)}</td>
                    <td>{personaJobStageLabel(item.current_stage, item.checkpoint)}</td>
                    <td>
                      <span>{personaJobDurationLabel(item)}</span>
                      <div className="muted-copy">{personaJobRetryLabel(item)}</div>
                    </td>
                    <td>
                      <span className={item.status === "failed" ? "pill pill-danger" : item.status === "completed" ? "pill pill-ok" : "pill pill-muted"}>
                        {item.cancel_requested && isActivePersonaJob(item) ? "取消中" : personaJobStatusLabel(item.status)}
                      </span>
                      {item.status === "failed" ? (
                        <div className="persona-job-error" title={item.error || ""}>
                          {shortJobError(item)}
                        </div>
                      ) : null}
                    </td>
                    <td>
                      <DangerAction
                        label="应用"
                        title="确认应用蒸馏结果"
                        confirmLabel="确认应用"
                        className="button-compact"
                        disabled={item.status !== "completed" || item.session_id !== effectiveSessionId}
                        impact={<p>将任务 #{item.id} 的人物风格应用到当前群；现有同范围配置可能被更新。</p>}
                        onConfirm={async () => {
                          hydrateJobSelection(item);
                          await applyJobToProfile(item.id);
                        }}
                      />
                      {item.status === "failed" ? (
                        <DangerAction
                          label="重跑"
                          title="确认重跑蒸馏任务"
                          confirmLabel="确认重跑"
                          className="button-compact"
                          disabled={item.session_id !== effectiveSessionId}
                          impact={<p>任务 #{item.id} 将重新读取当前群授权样本并执行，失败记录会保留。</p>}
                          onConfirm={() => rerunJob(item.id)}
                        />
                      ) : null}
                      {isActivePersonaJob(item) ? (
                        <DangerAction
                          label="取消"
                          title="确认取消蒸馏任务"
                          confirmLabel="确认取消"
                          pendingLabel="正在取消…"
                          className="button-compact"
                          disabled={item.session_id !== effectiveSessionId || Boolean(item.cancel_requested)}
                          impact={<p>任务 #{item.id} 会在安全检查点停止；已经持久化的分段进度会保留。</p>}
                          onConfirm={() => cancelJob(item.id)}
                        />
                      ) : null}
                    </td>
                  </tr>
                ))}
                {!jobs.length && (
                  <tr>
                    <td colSpan={7}>当前群还没有蒸馏任务</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </section>

        <section className="panel panel-scroll">
          <div className="panel-header">
            <div>
              <p className="section-kicker">已应用风格技能</p>
              <h3>当前群启用的风格技能</h3>
            </div>
          </div>
          <div className="persona-scope-note">
            <strong>{personaSessionName || "未选择群"}</strong>
            <span>保存范围由会话 ID、渠道和来源键共同确定；风格技能会保留完整产物，便于持续迭代。</span>
          </div>
          <div className="form-grid">
            <label className="field">
              <span>当前风格技能</span>
              <select
                value={profileId}
                onChange={(event) => {
                  const next = profiles.find((item) => String(item.id) === event.target.value);
                  if (next) {
                    hydrateProfileForm(next, { syncJobId: true });
                  } else {
                    setProfileId("");
                  }
                }}
              >
                <option value="">新建当前群风格技能</option>
                {profiles.map((item) => (
                  <option key={item.id} value={item.id}>{`#${item.id} · ${item.profile_name || item.target_name || "未命名"}`}</option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>配置名称</span>
              <input value={profileName} onChange={(event) => setProfileName(event.target.value)} />
            </label>
            <label className="field">
              <span>消息渠道</span>
              <select value={profileChannel} onChange={(event) => setProfileChannel(event.target.value)}>
                <option value="wechat">微信</option>
                <option value="all">全部渠道</option>
                <option value="web">网页</option>
                <option value="whatsapp">WhatsApp</option>
                <option value="telegram">Telegram</option>
                <option value="email">电子邮件</option>
                <option value="sms">短信</option>
                <option value="voice">语音</option>
                <option value="custom">自定义</option>
              </select>
            </label>
            <label className="field">
              <span>来源键</span>
              <input value={profileSourceKey} onChange={(event) => setProfileSourceKey(event.target.value)} />
            </label>
            <label className="field span-2">
              <span>来源名称</span>
              <input value={profileSourceLabel} onChange={(event) => setProfileSourceLabel(event.target.value)} />
            </label>
            <label className="field">
              <span>技能标识</span>
              <input value={profileSkillSlug} onChange={(event) => setProfileSkillSlug(event.target.value)} />
            </label>
            <label className="field">
              <span>是否启用</span>
              <select value={profileEnabled} onChange={(event) => setProfileEnabled(event.target.value)}>
                <option value="true">启用</option>
                <option value="false">停用</option>
              </select>
            </label>
          </div>
          <div className="action-row">
            <button className="button button-secondary" onClick={() => void listProfiles()}>
              读取当前群 Skill
            </button>
            <DangerAction
              label="从任务应用"
              title="确认应用蒸馏任务"
              confirmLabel="确认应用"
              disabled={!canApplySelectedJob}
              impact={(
                <dl>
                  <div><dt>目标群</dt><dd><code>{effectiveSessionId || "未选择"}</code></dd></div>
                  <div><dt>任务</dt><dd><code>{jobId || "未选择"}</code></dd></div>
                  <div><dt>影响</dt><dd>将任务产物设为当前群人物风格配置，并保留任务和配置审计链。</dd></div>
                </dl>
              )}
              onConfirm={() => applyJobToProfile()}
            />
            <DangerAction
              label="保存风格技能"
              title="确认保存人物回复风格技能"
              confirmLabel="确认保存"
              pendingLabel="正在保存…"
              disabled={
                !selectedSessionIsVerified
                || !profileLoaded
                || !members.some((item) => item.wxid === targetUserId)
              }
              impact={(
                <dl>
                  <div><dt>目标群</dt><dd><code>{effectiveSessionId || "未选择"}</code></dd></div>
                  <div><dt>目标人物</dt><dd>{targetName || targetUserId || "未选择"}</dd></div>
                  <div><dt>状态</dt><dd>{profileEnabled === "true" ? "保存后启用" : "保存但不启用"}</dd></div>
                  <div><dt>影响</dt><dd>会更新当前群的回复风格；安全、事实和记忆受众规则仍优先。</dd></div>
                </dl>
              )}
              onConfirm={saveProfile}
            />
            <DangerAction
              label="删除风格技能"
              title="确认删除人物回复风格技能"
              confirmLabel="确认删除"
              pendingLabel="正在删除…"
              disabled={!selectedSessionIsVerified || !profiles.some((item) => String(item.id) === String(profileId))}
              impact={(
                <dl>
                  <div><dt>配置 ID</dt><dd><code>{profileId || "未选择"}</code></dd></div>
                  <div><dt>名称</dt><dd>{profileName || "未命名"}</dd></div>
                  <div><dt>目标人物</dt><dd>{targetName || targetUserId || "未填写"}</dd></div>
                  <div><dt>技能标识</dt><dd><code>{profileSkillSlug || "未生成"}</code></dd></div>
                  <div><dt>影响</dt><dd>删除后该配置不再参与回复风格选择；已生成的蒸馏任务记录不会自动删除。</dd></div>
                </dl>
              )}
              onConfirm={deleteProfile}
            />
          </div>
          <div
            className="table-scroll compact-table-scroll"
            role="region"
            aria-label="当前群已应用回复风格表格"
            tabIndex={0}
          >
            <table>
              <caption className="sr-only">当前群已应用回复风格</caption>
              <thead>
                <tr>
                  <th scope="col">ID</th>
                  <th scope="col">名称</th>
                  <th scope="col">目标人物</th>
                  <th scope="col">技能标识</th>
                </tr>
              </thead>
              <tbody>
                {profiles.map((item) => (
                  <tr key={item.id}>
                    <th scope="row" className="mono">
                      <button type="button" className="table-cell-action mono" onClick={() => hydrateProfileForm(item, { syncJobId: true })}>
                        {item.id}
                      </button>
                    </th>
                    <td>{item.profile_name || "-"}</td>
                    <td>{item.target_name || item.target_user_id || "-"}</td>
                    <td className="mono">{item.skill_slug || "-"}</td>
                  </tr>
                ))}
                {!profiles.length && (
                  <tr>
                    <td colSpan={4}>当前群还没有已应用的回复风格技能</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </section>
      </div>

      <section className="panel span-2 panel-scroll">
        <div className="panel-header">
          <div>
            <p className="section-kicker">产物</p>
            <h3>蒸馏产物编辑器</h3>
          </div>
        </div>
        <div className="persona-artifact-summary">
          <div>
            <span>技能标识</span>
            <strong>{artifactSummary.slug}</strong>
          </div>
          <div>
            <span>生成模式</span>
            <strong>{personaArtifactModeLabel(artifactSummary.mode)}</strong>
          </div>
          <div>
            <span>消息数</span>
            <strong>{artifactSummary.messages}</strong>
          </div>
          <div>
            <span>时间范围</span>
            <strong>{`${artifactSummary.first} ~ ${artifactSummary.last}`}</strong>
          </div>
        </div>
        <div className="form-grid">
          <label className="field span-2">
            <span>技能提示词正文（运行时注入）</span>
            <textarea
              rows={8}
              value={artifactSkillPrompt}
              onChange={(event) => {
                artifactEditorDirtyRef.current = true;
                setArtifactSkillPrompt(event.target.value);
              }}
            />
          </label>
          <label className="field span-2">
            <span>SKILL.md</span>
            <textarea
              rows={10}
              value={artifactSkillMd}
              onChange={(event) => {
                artifactEditorDirtyRef.current = true;
                setArtifactSkillMd(event.target.value);
              }}
            />
          </label>
          <label className="field span-2">
            <span>persona.md</span>
            <textarea
              rows={10}
              value={artifactPersonaMd}
              onChange={(event) => {
                artifactEditorDirtyRef.current = true;
                setArtifactPersonaMd(event.target.value);
              }}
            />
          </label>
          <label className="field span-2">
            <span>work.md</span>
            <textarea
              rows={10}
              value={artifactWorkMd}
              onChange={(event) => {
                artifactEditorDirtyRef.current = true;
                setArtifactWorkMd(event.target.value);
              }}
            />
          </label>
          <details className="technical-details span-2">
            <summary>技术详情：meta.json</summary>
            <label className="field">
              <span>结构化元数据 JSON</span>
              <textarea
                rows={10}
                value={artifactMetaJson}
                onChange={(event) => {
                  artifactEditorDirtyRef.current = true;
                  setArtifactMetaJson(event.target.value);
                }}
              />
            </label>
          </details>
          <label className="field">
            <span>知识消息最早时间</span>
            <input
              value={artifactFirstTimestamp}
              onChange={(event) => {
                artifactEditorDirtyRef.current = true;
                setArtifactFirstTimestamp(event.target.value);
              }}
            />
          </label>
          <label className="field">
            <span>知识消息最晚时间</span>
            <input
              value={artifactLastTimestamp}
              onChange={(event) => {
                artifactEditorDirtyRef.current = true;
                setArtifactLastTimestamp(event.target.value);
              }}
            />
          </label>
          <label className="field">
            <span>知识消息数量</span>
            <input
              value={artifactMessageCount}
              onChange={(event) => {
                artifactEditorDirtyRef.current = true;
                setArtifactMessageCount(event.target.value);
              }}
            />
          </label>
          <label className="field">
            <span>产物生成模式</span>
            <input
              value={artifactMode}
              onChange={(event) => {
                artifactEditorDirtyRef.current = true;
                setArtifactMode(event.target.value);
              }}
            />
          </label>
          <label className="field span-2">
            <span>knowledge/messages.txt</span>
            <textarea
              rows={8}
              value={artifactKnowledgeText}
              onChange={(event) => {
                artifactEditorDirtyRef.current = true;
                setArtifactKnowledgeText(event.target.value);
              }}
            />
          </label>
        </div>
      </section>

      <OutputPanel title="群会话 / 成员响应" value={selectionOutput} />
      <OutputPanel title="蒸馏任务响应" value={output} />
      <OutputPanel title="风格技能管理响应" value={profileOutput} />
    </div>
  );
}
