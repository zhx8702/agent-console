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
  enabled?: boolean;
  job_id?: number | null;
  updated_at?: string | null;
};

export type PortraitClaim = {
  text?: string;
  count?: number;
  last_seen?: string;
  examples?: string[];
};

export type PortraitPayload = {
  summary?: string;
  likes?: PortraitClaim[];
  dislikes?: PortraitClaim[];
  topics?: PortraitClaim[];
  routines?: PortraitClaim[];
  voice?: PortraitClaim[];
  social?: PortraitClaim[];
  recent_7d?: PortraitClaim[];
  recent_30d?: PortraitClaim[];
  unknowns?: string[];
  confidence?: number;
  coverage?: {
    lines_total?: number;
    lines_read?: number;
    complete?: boolean;
  };
};

export type PortraitRecord = {
  id?: number;
  tenant_id?: string;
  channel?: string;
  source_key?: string;
  speaker_id?: string;
  display_name?: string;
  session_id?: string;
  status?: string;
  pending_messages?: number;
  last_message_at?: string;
  hot_update_enabled?: boolean;
  message_count?: number;
  created_at?: string | null;
  updated_at?: string | null;
  revision_created_at?: string | null;
  portrait?: PortraitPayload | null;
  evidence?: Record<string, unknown> | null;
};

export type PortraitJob = {
  id: number;
  tenant_id?: string;
  session_id?: string;
  session_name?: string;
  speaker_id?: string;
  speaker_name?: string;
  status?: string;
  error?: string;
  days_limit?: number;
  max_messages?: number;
  message_count?: number;
  mode?: string;
  since_timestamp?: string;
  created_at?: string | null;
  updated_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
};

export type PortraitStylePreview = {
  status?: string;
  name?: string;
  prompt?: string;
  prompt_chars?: number;
  profile_id?: number;
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

export function portraitJobStatusLabel(status?: string) {
  const labels: Record<string, string> = {
    queued: "已排队",
    running: "运行中",
    completed: "已完成",
    failed: "失败",
  };
  return labels[status || ""] || status || "未知";
}

export function portraitJobModeLabel(mode?: string) {
  const labels: Record<string, string> = {
    full: "全量画像",
    incremental: "增量热更新",
  };
  return labels[mode || ""] || mode || "-";
}

export function portraitJobDurationLabel(job: PortraitJob, now = Date.now()) {
  const started = Date.parse(String(job.started_at || job.created_at || ""));
  const terminal = ["completed", "failed"].includes(String(job.status || ""));
  const ended = Date.parse(String(job.finished_at || (terminal ? job.updated_at : "") || ""));
  if (!Number.isFinite(started)) return "-";
  const durationSeconds = Math.max(
    0,
    Math.floor(((Number.isFinite(ended) ? ended : now) - started) / 1000),
  );
  if (durationSeconds < 60) return `${durationSeconds} 秒`;
  const minutes = Math.floor(durationSeconds / 60);
  const seconds = durationSeconds % 60;
  if (minutes < 60) return seconds ? `${minutes} 分 ${seconds} 秒` : `${minutes} 分`;
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  return remainingMinutes ? `${hours} 小时 ${remainingMinutes} 分` : `${hours} 小时`;
}

export function shortPortraitJobError(job: PortraitJob) {
  const error = (job.error || "").replace(/\s+/g, " ").trim();
  if (!error) return "-";
  return error.length > 120 ? `${error.slice(0, 117)}...` : error;
}

export function portraitConfidenceLabel(portrait?: PortraitPayload | null) {
  const confidence = Number(portrait?.confidence);
  if (!Number.isFinite(confidence)) return "-";
  return `${Math.round(confidence * 100)}%`;
}

export function portraitCoverageLabel(portrait?: PortraitPayload | null) {
  const coverage = portrait?.coverage;
  if (!coverage) return "-";
  const total = Number(coverage.lines_total);
  const read = Number(coverage.lines_read);
  if (!Number.isFinite(total) || total <= 0) return coverage.complete ? "完整" : "-";
  const readText = Number.isFinite(read) ? read : "?";
  return `${readText}/${total}${coverage.complete ? "（完整）" : "（部分）"}`;
}

export const PORTRAIT_CLAIM_SECTIONS: Array<{ key: keyof PortraitPayload; label: string }> = [
  { key: "voice", label: "怎么说话" },
  { key: "social", label: "怎么接话" },
  { key: "likes", label: "喜欢" },
  { key: "dislikes", label: "反感" },
  { key: "topics", label: "常聊话题" },
  { key: "routines", label: "日常节奏" },
  { key: "recent_7d", label: "最近 7 天" },
  { key: "recent_30d", label: "最近 30 天" },
];
