import type {
  ParticipationDecisionStatus,
  VersionedResourceState,
} from "../../lib/api";

export const DECISION_LABELS: Record<ParticipationDecisionStatus, string> = {
  must_reply: "必须回复",
  may_reply: "可以回复",
  observe_only: "仅观察",
  defer: "延后判断",
  cancel: "取消发送",
};

export const REASON_LABELS: Record<string, string> = {
  direct_mention: "明确 @ 机器人",
  keyword_trigger: "命中关注关键词",
  recent_topic_continuation: "延续最近话题",
  mentioned_me: "明确提及机器人",
  replied_to_bot: "回复了机器人",
  explicit_command: "明确命令",
  explicit_question_to_bot: "明确向机器人提问",
  safety_response_required: "需要安全响应",
  keyword_triggered: "命中关注话题",
  topic_continuation: "延续当前话题",
  unfinished_task_continuation: "延续未完成任务",
  directed_to_other_member: "消息指向其他成员",
  rapid_multi_party_chat: "多人快速对话",
  bot_replied_within_60s: "机器人刚刚回复过",
  valid_member_answer_exists: "已有成员给出有效回答",
  quiet_hours: "处于安静时段",
  group_disabled: "群参与已关闭",
  reply_to_bot: "回复了机器人",
  bot_replied_recently: "机器人刚刚回复过",
  bot_ratio_above_limit: "机器人近期发言占比偏高",
  low_intent_confidence: "意图置信度不足",
  answered_by_member: "已有成员回答",
  score_below_threshold: "得分未达到参与阈值",
  score_threshold_met: "得分达到参与阈值",
  soft_budget_10m_exhausted: "10 分钟柔性回复预算已用尽",
  soft_budget_hour_exhausted: "每小时柔性回复预算已用尽",
  consecutive_bot_message_limit: "连续机器人消息达到上限",
  projected_bot_ratio_limit: "发送后机器人占比将超过上限",
  participation_disabled: "参与策略已关闭",
  participation_disabled_at_send: "发送前发现参与策略已关闭",
  self_message: "机器人自己的消息",
  self_message_at_send: "发送前发现是机器人自己的消息",
  answered_before_send: "发送前已有成员回答",
  topic_changed_before_send: "发送前话题已变化",
  superseded_before_send: "发送前已被新消息取代",
  reply_expired: "回复已过有效期",
  not_before_pending: "尚未到允许发送时间",
  quiet_hours_at_send: "发送前进入安静时段",
  soft_budget_10m_exhausted_at_send: "发送前 10 分钟预算已用尽",
  soft_budget_hour_exhausted_at_send: "发送前每小时预算已用尽",
  consecutive_bot_message_limit_at_send: "发送前连续消息达到上限",
  projected_bot_ratio_limit_at_send: "发送前占比复核未通过",
  defer_revalidated: "延后事件复核通过",
  proactive_disabled: "主动参与未开启",
  proactive_quiet_hours: "主动参与处于安静时段",
  proactive_daily_budget_exhausted: "当日主动参与预算已用尽",
  proactive_group_not_silent_long_enough: "群聊沉默时间不足",
  proactive_opted_in: "群已加入主动参与灰度",
  proactive_silence_met: "群聊沉默时间满足条件",
  superseded_by_newer_message: "已被新消息取代",
  topic_changed: "话题已经变化",
  is_self_sent: "机器人自己发送的消息",
  reply_target_ambiguous: "回复对象不明确",
  rollout_shadow_only: "当前处于影子评估，不实际发送",
  unspecified: "未提供额外原因",
};

const RUNTIME_STAGE_LABELS: Record<string, string> = {
  decision: "首次决策",
  revalidation: "发送前复核",
  delivery: "消息投递",
};

const DELIVERY_STAGE_LABELS: Record<string, string> = {
  not_applicable: "不适用",
  queued: "已排队",
  sent: "已发送",
  cancelled: "已取消",
  failed: "发送失败",
};

const MEMORY_TYPE_LABELS: Record<string, string> = {
  preference: "偏好",
  profile: "成员资料",
  fact: "事实",
  relationship: "关系",
  task: "任务",
  correction: "纠正记录",
};

const SCOPE_TYPE_LABELS: Record<string, string> = {
  identity: "成员范围",
  session: "当前会话",
};

const AUDIENCE_SCOPE_LABELS: Record<string, string> = {
  private: "仅私聊",
  session: "仅当前会话",
  explicit: "指定会话",
};

export function reasonLabel(code: string) {
  const normalized = String(code || "").trim();
  const match = /^(.*?)(?::(plus|minus)(\d+))?$/.exec(normalized);
  const base = match?.[1] || normalized;
  const label = REASON_LABELS[base] || "其他结构化原因";
  if (!match?.[2] || !match[3]) {
    return label;
  }
  return `${label}（${match[2] === "plus" ? "+" : "−"}${match[3]}）`;
}

export function runtimeStageLabel(value: string | null | undefined) {
  return RUNTIME_STAGE_LABELS[value || "decision"] || "其他运行阶段";
}

export function deliveryStageLabel(value: string | null | undefined) {
  return DELIVERY_STAGE_LABELS[value || "not_applicable"] || "其他投递阶段";
}

export function memoryTypeLabel(value: string) {
  return MEMORY_TYPE_LABELS[value] || "其他记忆";
}

export function scopeTypeLabel(value: string) {
  return SCOPE_TYPE_LABELS[value] || "其他范围";
}

export function audienceScopeLabel(value: string) {
  return AUDIENCE_SCOPE_LABELS[value] || "其他受众";
}

export function formatTime(value: string | null | undefined) {
  if (!value) {
    return "—";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

export function decisionPill(status: ParticipationDecisionStatus) {
  const className =
    status === "must_reply" || status === "may_reply"
      ? "pill pill-ok"
      : status === "cancel"
        ? "pill pill-danger"
        : "pill pill-muted";
  return <span className={className}>{DECISION_LABELS[status]}</span>;
}

export function resourceStatus<T>(state: VersionedResourceState<T>) {
  if (state.status === "loading") return "加载中";
  if (state.status === "saving") return "保存中";
  if (state.status === "conflict") return "存在版本冲突";
  if (state.status === "error") return "读取失败";
  if (state.dirty) return "有未保存修改";
  if (state.status === "loaded") return "已同步";
  return "等待加载";
}

export function friendlyErrorMessage(error: string, fallback: string) {
  const value = String(error || "").trim();
  if (!value) return fallback;
  if (/[\u3400-\u9fff]/.test(value)) return value;
  const normalized = value.toLowerCase();
  if (
    normalized.includes("network")
    || normalized.includes("failed to fetch")
    || normalized.includes("temporary")
    || normalized.includes("connection")
  ) {
    return "网络请求未完成，请检查连接后重试；未保存的草稿仍会保留。";
  }
  if (normalized.includes("timeout") || normalized.includes("timed out")) {
    return "请求等待超时，请稍后重试；未保存的草稿仍会保留。";
  }
  if (normalized.includes("401") || normalized.includes("unauthorized")) {
    return "登录状态已失效，请重新登录后继续。";
  }
  if (normalized.includes("403") || normalized.includes("forbidden")) {
    return "当前账号没有执行此操作的权限。";
  }
  if (normalized.includes("409") || normalized.includes("conflict")) {
    return "服务器已有更新，当前草稿已保留，请重新读取后核对。";
  }
  return fallback;
}

export function TechnicalDetails({
  data,
  summary = "查看技术详情",
  label = "技术详情 JSON",
}: {
  data: unknown;
  summary?: string;
  label?: string;
}) {
  return (
    <details className="route-list">
      <summary>{summary}</summary>
      <pre aria-label={label}>
        <code>{JSON.stringify(data, null, 2)}</code>
      </pre>
    </details>
  );
}

export function ToggleCard({
  checked,
  label,
  description,
  onChange,
  disabled = false,
}: {
  checked: boolean;
  label: string;
  description: string;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <label className="toggle-chip">
      <strong>
        <input
          type="checkbox"
          checked={checked}
          onChange={(event) => onChange(event.target.checked)}
          disabled={disabled}
        />
        {label}
      </strong>
      <em>{description}</em>
    </label>
  );
}
