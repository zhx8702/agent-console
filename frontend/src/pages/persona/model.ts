export type WxbotSession = {
  session_id: string;
  session_name: string;
  kind?: string;
};

export type GroupRosterCandidate = {
  wxid: string;
  name?: string;
  alias?: string;
  remark?: string;
  nick_name?: string;
  msg_count?: number;
  first_ts?: string;
  last_ts?: string;
  has_history?: boolean;
};

export type GroupRosterPayload = {
  session_id?: string;
  source_kind?: string;
  source_note?: string;
  message_scope?: string;
  candidates?: GroupRosterCandidate[];
};

export type GroupSessionRosterPayload = {
  sessions?: WxbotSession[];
  count?: number;
};

export type PersonaArtifact = {
  version?: string;
  generated_at?: string;
  slug?: string;
  mode?: string;
  target?: {
    user_id?: string;
    name?: string;
  };
  source?: {
    tenant_id?: string;
    session_id?: string;
    session_name?: string;
    channel?: string;
    source_key?: string;
    source_label?: string;
    job_id?: number | null;
  };
  knowledge?: {
    message_count?: number;
    first_timestamp?: string;
    last_timestamp?: string;
    messages_text?: string;
    knowledge_sources?: string[];
    source_sessions?: string[];
  };
  files?: {
    "SKILL.md"?: string;
    skill_prompt?: string;
    "persona.md"?: string;
    "work.md"?: string;
  };
  meta?: Record<string, unknown>;
};

export type PersonaJob = {
  id: number;
  tenant_id?: string;
  session_id: string;
  session_name?: string;
  target_user_id?: string;
  target_name?: string;
  status?: string;
  msg_count?: number;
  days_limit?: number;
  max_messages?: number;
  output_slug?: string;
  mode?: string;
  current_stage?: string;
  checkpoint?: {
    progress?: {
      total_chunks?: number;
      completed_chunks?: number;
    };
    [key: string]: unknown;
  } | null;
  client_request_id?: string;
  attempt_count?: number;
  max_attempts?: number;
  cancel_requested?: boolean;
  result_text?: string;
  artifact?: PersonaArtifact | null;
  error?: string;
  created_at?: string | null;
  started_at?: string | null;
  updated_at?: string | null;
  completed_at?: string | null;
  lease_expires_at?: string | null;
};

export type PersonaProfile = {
  id: number;
  session_id: string;
  channel?: string;
  source_key?: string;
  source_label?: string;
  profile_name?: string;
  target_user_id?: string;
  target_name?: string;
  skill_slug?: string;
  prompt_text?: string;
  artifact?: PersonaArtifact | null;
  enabled?: boolean;
  job_id?: number | null;
  updated_at?: string | null;
};

export function isGroupSession(session: Pick<WxbotSession, "session_id" | "kind">) {
  return (
    session.session_id.endsWith("@chatroom") ||
    session.kind === "group" ||
    session.kind === "chatroom"
  );
}

export function getMemberDisplayName(member: GroupRosterCandidate) {
  return member.name || member.remark || member.alias || member.nick_name || member.wxid;
}

export function personaJobStatusLabel(status?: string) {
  const labels: Record<string, string> = {
    pending: "等待中",
    queued: "已排队",
    running: "运行中",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消",
  };
  return labels[status || ""] || status || "未知";
}

export function personaJobStageLabel(stage?: string, checkpoint?: PersonaJob["checkpoint"]) {
  const labels: Record<string, string> = {
    queued: "已排队",
    pending: "等待执行",
    claiming: "领取任务",
    collect_messages: "收集消息",
    collecting_messages: "收集消息",
    prepare: "准备样本",
    chunking: "拆分消息",
    map_chunks: "提取分段特征",
    map: "提取分段特征",
    reduce: "合并特征",
    synthesize: "合成人物风格",
    synthesis: "合成人物风格",
    synthesis_complete: "人物风格已合成",
    work: "提炼工作特征",
    work_complete: "工作特征已完成",
    persona: "提炼表达风格",
    persona_complete: "表达风格已完成",
    skill: "生成风格技能",
    persist: "保存蒸馏产物",
    finalizing: "整理蒸馏产物",
    retry_wait: "等待重试",
    cancelled: "已取消",
    disabled: "插件已停用",
    done: "已完成",
    completed: "已完成",
  };
  const normalized = stage || "";
  const chunkMatch = normalized.match(/^(?:summarize|map|extract)_chunk_(\d+)(?:_of_(\d+))?$/);
  if (chunkMatch) {
    return `提取分段 ${chunkMatch[1]}${chunkMatch[2] ? `/${chunkMatch[2]}` : ""}`;
  }
  const progress = checkpoint?.progress;
  const completedChunks = Number(progress?.completed_chunks);
  const totalChunks = Number(progress?.total_chunks);
  const progressText = Number.isFinite(completedChunks) && Number.isFinite(totalChunks) && totalChunks > 0
    ? `（${Math.max(0, completedChunks)}/${totalChunks}）`
    : "";
  return `${labels[normalized] || normalized || "-"}${progressText}`;
}

export function personaJobDurationLabel(job: PersonaJob, now = Date.now()) {
  const started = Date.parse(String(job.started_at || job.created_at || ""));
  const terminal = ["completed", "failed", "cancelled"].includes(String(job.status || ""));
  const ended = Date.parse(String(job.completed_at || (terminal ? job.updated_at : "") || ""));
  if (!Number.isFinite(started)) return "-";
  const durationSeconds = Math.max(0, Math.floor(((Number.isFinite(ended) ? ended : now) - started) / 1000));
  if (durationSeconds < 60) return `${durationSeconds} 秒`;
  const minutes = Math.floor(durationSeconds / 60);
  const seconds = durationSeconds % 60;
  if (minutes < 60) return seconds ? `${minutes} 分 ${seconds} 秒` : `${minutes} 分`;
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  return remainingMinutes ? `${hours} 小时 ${remainingMinutes} 分` : `${hours} 小时`;
}

export function personaJobRetryLabel(job: PersonaJob) {
  const attempts = Math.max(0, Number(job.attempt_count) || 0);
  const maxAttempts = Math.max(0, Number(job.max_attempts) || 0);
  if (!attempts) return "尚未尝试";
  const retries = Math.max(0, attempts - 1);
  const attemptText = maxAttempts ? `第 ${attempts}/${maxAttempts} 次` : `第 ${attempts} 次`;
  return retries ? `${attemptText}（已重试 ${retries} 次）` : `${attemptText}（未重试）`;
}

export function personaArtifactModeLabel(mode?: string) {
  const labels: Record<string, string> = {
    manual: "手工编辑",
    incremental: "增量更新",
    rebuild: "全量重建",
    full: "全量生成",
  };
  return labels[mode || ""] || mode || "-";
}

export function buildSkillFrontmatter(slug: string, targetName: string, body: string) {
  const cleanBody = body.trim();
  const safeSlug = slug || "default";
  const safeName = targetName || safeSlug;
  return [
    "---",
    `name: colleague-${safeSlug}`,
    `description: "${safeName} — 基于聊天记录蒸馏"`,
    "user-invocable: true",
    "---",
    "",
    cleanBody,
  ].join("\n");
}

export function stripFrontmatter(text: string) {
  const lines = text.replace(/^\uFEFF/, "").split("\n");
  if (!lines.length || lines[0].trim() !== "---") {
    return text.trim();
  }
  for (let index = 1; index < lines.length; index += 1) {
    if (lines[index].trim() === "---") {
      return lines.slice(index + 1).join("\n").trim();
    }
  }
  return text.trim();
}

export function buildDefaultMeta({
  targetName,
  targetUserId,
  slug,
  sessionName,
  sessionId,
  messageCount,
  firstTimestamp,
  lastTimestamp,
}: {
  targetName: string;
  targetUserId: string;
  slug: string;
  sessionName: string;
  sessionId: string;
  messageCount: number;
  firstTimestamp: string;
  lastTimestamp: string;
}) {
  const dateMin = firstTimestamp ? firstTimestamp.slice(0, 10) : "?";
  const dateMax = lastTimestamp ? lastTimestamp.slice(0, 10) : "?";
  return {
    name: targetName,
    slug,
    wxid: targetUserId,
    version: "v1",
    profile: {},
    tags: { personality: [], culture: [] },
    impression: "",
    knowledge_sources: [`${sessionName || sessionId} — ${messageCount} 条 (${dateMin} ~ ${dateMax})`],
    message_count: messageCount,
    source_sessions: sessionId ? [sessionId] : [],
    corrections_count: 0,
  };
}

export function getArtifactPrompt(artifact: PersonaArtifact | null | undefined, fallbackPrompt = "") {
  return (
    artifact?.files?.skill_prompt ||
    stripFrontmatter(artifact?.files?.["SKILL.md"] || "") ||
    fallbackPrompt ||
    ""
  );
}

export function shortJobError(job: PersonaJob) {
  const stage = (job.current_stage || "").trim();
  const error = (job.error || "").replace(/\s+/g, " ").trim();
  const shortError = error.length > 120 ? `${error.slice(0, 117)}...` : error;
  if (stage && shortError) return `${stage}: ${shortError}`;
  return shortError || stage || "-";
}
