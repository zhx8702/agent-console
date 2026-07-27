export type GroupRosterCandidate = {
  wxid: string;
  name?: string;
  alias?: string;
  remark?: string;
  nick_name?: string;
  msg_count?: number;
};

export type CreditsConfig = {
  tenant_id?: string;
  session_id?: string;
  enabled?: boolean;
  credit_name?: string;
  cost_per_chat?: number;
  command_costs_text?: string;
  draw_quality_costs_text?: string;
  amap_search_credit_cost?: number;
  amap_map_credit_cost?: number;
  amap_route_map_credit_cost?: number;
  initial_credits?: number;
  daily_checkin?: number;
  streak_bonus?: number;
  streak_cap?: number;
  checkin_mode?: number;
  checkin_mode_label?: string;
};

export type CreditsMember = {
  user_id: string;
  display_name?: string;
  credits?: number;
  updated_at?: string | null;
  rank?: number;
  checked_in_today?: boolean;
  today_reward?: number;
  today_streak?: number;
  last_checkin_date?: string | null;
  last_reward?: number;
  last_streak?: number;
};

export type CheckinStatus = {
  today?: string;
  checked_in_today?: boolean;
  today_reward?: number;
  today_streak?: number;
  current_streak?: number;
  total_checkins?: number;
  last_checkin_date?: string | null;
  last_reward?: number;
  next_reward?: number;
  checkin_mode?: number;
  checkin_mode_label?: string;
};

export type CreditsLedgerRow = {
  id: number;
  user_id: string;
  display_name?: string;
  delta: number;
  reason: string;
  actor?: string;
  reference?: string;
  created_at?: string | null;
};

export type CreditsMembersPayload = {
  items?: CreditsMember[];
  count?: number;
  summary?: {
    member_count?: number;
    checked_in_today_count?: number;
    total_credits?: number;
    today?: string;
  };
};

export type CreditsMemberDetail = {
  tenant_id: string;
  session_id: string;
  user_id: string;
  display_name?: string;
  credits?: number;
  updated_at?: string | null;
  has_balance_record?: boolean;
  rank?: number | null;
  config?: {
    credit_name?: string;
    initial_credits?: number;
    checkin_mode?: number;
    checkin_mode_label?: string;
  };
  checkin_status?: CheckinStatus;
  recent_ledger?: CreditsLedgerRow[];
};

export type CreditsLedgerPayload = {
  items?: CreditsLedgerRow[];
  count?: number;
};

export type CreditsMemberRow = {
  user_id: string;
  display_name: string;
  source: "roster" | "credits";
  msg_count?: number;
  credits?: number;
  rank?: number;
  checked_in_today?: boolean;
  today_reward?: number;
  last_checkin_date?: string | null;
};

export const CHECKIN_MODE_OPTIONS = [
  {
    value: 1,
    label: "命令签到",
    description: "成员通过命令或后台操作主动签到。",
  },
  {
    value: 2,
    label: "当前发言签到",
    description: "群内任意有效发言触发自动签到，不额外回提示。",
  },
  {
    value: 3,
    label: "静默 @ 签到",
    description: "仅当当前发言 @ 机器人时自动签到，不额外回提示。",
  },
];

export const REASON_LABELS: Record<string, string> = {
  checkin: "签到",
  streak_bonus: "连签加成",
  chat_cost: "对话扣费",
  command_cost: "命令扣费",
  amap_search_cost: "高德查询",
  amap_map_cost: "高德地图二维码",
  amap_route_map_cost: "高德路线地图",
  transfer_in: "转入",
  transfer_out: "转出",
  admin_adjust: "人工调整",
  admin_set_balance: "设定余额",
  admin_grant: "管理员赠送",
};

export function getMemberDisplayName(member: GroupRosterCandidate) {
  return member.name || member.remark || member.alias || member.nick_name || member.wxid;
}

export function formatTimestamp(value?: string | null) {
  if (!value) {
    return "-";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString("zh-CN", { hour12: false });
}

export function formatDay(value?: string | null) {
  if (!value) {
    return "-";
  }
  return value;
}

export function formatDelta(delta?: number) {
  const amount = Number(delta || 0);
  return `${amount > 0 ? "+" : ""}${amount}`;
}

export function getReasonLabel(reason: string) {
  return REASON_LABELS[reason] || reason;
}

export function getCheckinModeText(mode: number) {
  return CHECKIN_MODE_OPTIONS.find((item) => item.value === mode)?.label || `模式 ${mode}`;
}

export function getCheckinModeDescription(mode: number) {
  return CHECKIN_MODE_OPTIONS.find((item) => item.value === mode)?.description || "";
}

export type CreditsConfigDraft = Required<Pick<
  CreditsConfig,
  | "enabled"
  | "credit_name"
  | "cost_per_chat"
  | "command_costs_text"
  | "draw_quality_costs_text"
  | "amap_search_credit_cost"
  | "amap_map_credit_cost"
  | "amap_route_map_credit_cost"
  | "initial_credits"
  | "daily_checkin"
  | "streak_bonus"
  | "streak_cap"
  | "checkin_mode"
>>;

export function configFingerprint(value: CreditsConfigDraft) {
  return JSON.stringify(value);
}
