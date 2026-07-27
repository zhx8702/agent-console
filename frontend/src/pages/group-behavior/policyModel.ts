import type {
  GroupParticipationPolicyDocument,
  VoiceProfile,
} from "../../lib/api";

export function newVoiceProfile(): VoiceProfile {
  return {
    profile_id: "default",
    version: 0,
    enabled: false,
    sample_source: "manual",
    sample_scope: "none",
    authorized_sample_session_ids: [],
    authorization_reference: "",
    valid_from: null,
    expires_at: null,
    display_name: "",
    tone: "natural",
    verbosity: "concise",
    phrase_preferences: [],
    emoji_frequency: 0.05,
    list_format_policy: "avoid_by_default",
    identity_disclosure: "contextual",
    source_persona_version: 0,
  };
}

export function numberValue(value: string, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

/**
 * Keep probability inputs readable when an API or database returns a float32
 * artifact (for example, 0.15000000596046448). This only formats the rendered
 * control value; the draft retains its original number until the operator edits it.
 */
export function formatProbabilityInput(value: number) {
  if (!Number.isFinite(value)) return "";
  return String(Number(value.toFixed(6)));
}

export function listValue(value: string) {
  const seen = new Set<string>();
  return value
    .split(/[\n,，]/)
    .map((item) => item.trim().replace(/\s+/g, " "))
    .filter((item) => {
      const key = item.toLocaleLowerCase();
      if (!item || seen.has(key)) return false;
      seen.add(key);
      return true;
    });
}

export function datetimeLocalValue(value: string | null) {
  if (!value) return "";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "";
  const local = new Date(parsed.getTime() - parsed.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

export function datetimeIsoValue(value: string) {
  if (!value) return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString();
}

export function voiceProfileValidationError(
  profile: VoiceProfile | null,
  groupId: string,
) {
  if (!profile) return "";
  const authorizedSessions = profile.authorized_sample_session_ids;
  if (profile.emoji_frequency < 0 || profile.emoji_frequency > 0.15) {
    return "表情符号频率必须在 0 到 0.15 之间。";
  }
  if (profile.phrase_preferences.length > 30) {
    return "偏好短语去重后最多保留 30 条。";
  }
  if (profile.phrase_preferences.some((phrase) => phrase.length > 80)) {
    return "每条偏好短语最多 80 个字符。";
  }
  if (profile.sample_source === "persona" && profile.source_persona_version < 1) {
    return "人物档案来源必须填写大于 0 的已审核版本。";
  }
  if (profile.sample_scope === "none" && authorizedSessions.length > 0) {
    return "未使用群样本时不能携带授权群。";
  }
  if (
    profile.sample_scope === "current_group"
    && (authorizedSessions.length !== 1 || authorizedSessions[0] !== groupId)
  ) {
    return "样本授权只能精确绑定当前群，禁止跨群或私聊样本。";
  }
  if (
    profile.sample_source === "authorized_group_samples"
    && (
      profile.sample_scope !== "current_group"
      || authorizedSessions.length !== 1
      || authorizedSessions[0] !== groupId
    )
  ) {
    return "当前群授权样本必须绑定当前群。";
  }
  if (
    profile.sample_source === "authorized_group_samples"
    && !profile.authorization_reference.trim()
  ) {
    return "使用当前群授权样本时必须填写授权引用。";
  }
  if (profile.valid_from && Number.isNaN(new Date(profile.valid_from).getTime())) {
    return "生效时间格式无效。";
  }
  if (profile.expires_at && Number.isNaN(new Date(profile.expires_at).getTime())) {
    return "到期时间格式无效。";
  }
  if (
    profile.valid_from
    && profile.expires_at
    && new Date(profile.expires_at).getTime() <= new Date(profile.valid_from).getTime()
  ) {
    return "到期时间必须晚于生效时间。";
  }
  return "";
}

export function voiceProfileStatus(profile: VoiceProfile | null, groupId: string) {
  const validationError = voiceProfileValidationError(profile, groupId);
  if (!profile) return { label: "未配置", className: "pill-muted" };
  if (validationError) return { label: "配置无效", className: "pill-danger" };
  if (!profile.enabled) return { label: "已停用", className: "pill-muted" };
  const now = Date.now();
  if (profile.valid_from && new Date(profile.valid_from).getTime() > now) {
    return { label: "等待生效", className: "pill-feature" };
  }
  if (profile.expires_at && new Date(profile.expires_at).getTime() <= now) {
    return { label: "已到期", className: "pill-danger" };
  }
  return { label: "当前生效", className: "pill-ok" };
}

export function policyEffectiveEnabled(
  policy: GroupParticipationPolicyDocument | null | undefined,
) {
  return Boolean(
    policy?.kill_switches.global_enabled
      && policy.kill_switches.tenant_enabled
      && policy.kill_switches.group_enabled,
  );
}
