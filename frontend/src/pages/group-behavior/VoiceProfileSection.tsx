import { useEffect, useState } from "react";

import { Alert } from "../../components";
import type {
  GroupParticipationPolicyDocument,
  VoiceProfile,
  VoiceProfilePreviewDocument,
} from "../../lib/api";
import {
  datetimeIsoValue,
  datetimeLocalValue,
  formatProbabilityInput,
  listValue,
  newVoiceProfile,
  numberValue,
  voiceProfileStatus,
  voiceProfileValidationError,
} from "./policyModel";

type PolicyDraftUpdater = (
  updater: (
    draft: GroupParticipationPolicyDocument,
  ) => GroupParticipationPolicyDocument,
) => void;

type VoiceProfileSectionProps = {
  profile: VoiceProfile | null;
  groupId: string;
  reason: string;
  onReasonChange: (reason: string) => void;
  onUpdateDraft: PolicyDraftUpdater;
  onPreview: (
    profile: VoiceProfile,
    replyText: string,
    explicitlyDetailed: boolean,
  ) => Promise<VoiceProfilePreviewDocument>;
};

const PREVIEW_RUNTIME_LABELS: Record<string, string> = {
  voice_profile_active: "已按当前风格计算",
  voice_profile_disabled: "风格已停用，保持原文",
  voice_profile_not_yet_valid: "尚未到生效时间，保持原文",
  voice_profile_expired: "风格已到期，保持原文",
  voice_profile_sample_scope_invalid: "样本授权范围无效，保持原文",
};

export function VoiceProfileSection({
  profile,
  groupId,
  reason,
  onReasonChange,
  onUpdateDraft,
  onPreview,
}: VoiceProfileSectionProps) {
  const validationError = voiceProfileValidationError(profile, groupId);
  const status = voiceProfileStatus(profile, groupId);
  const [previewText, setPreviewText] = useState(
    "这个思路可以继续看🙂。如果需要，我再展开。",
  );
  const [previewDetailed, setPreviewDetailed] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const [previewError, setPreviewError] = useState("");
  const [previewResult, setPreviewResult] = useState<VoiceProfilePreviewDocument | null>(null);

  useEffect(() => {
    setPreviewError("");
    setPreviewResult(null);
  }, [profile]);

  const runPreview = async () => {
    if (!profile || !previewText.trim() || validationError) return;
    setPreviewing(true);
    setPreviewError("");
    try {
      setPreviewResult(await onPreview(profile, previewText, previewDetailed));
    } catch (caught) {
      setPreviewResult(null);
      setPreviewError(caught instanceof Error ? caught.message : "表达风格预览失败");
    } finally {
      setPreviewing(false);
    }
  };

  const updateVoiceValue = <Key extends keyof VoiceProfile>(
    key: Key,
    value: VoiceProfile[Key],
  ) => {
    onUpdateDraft((draft) => ({
      ...draft,
      voice_profile: {
        ...(draft.voice_profile || newVoiceProfile()),
        [key]: value,
      },
    }));
  };

  const updateVoiceSource = (sampleSource: VoiceProfile["sample_source"]) => {
    onUpdateDraft((draft) => {
      const current = draft.voice_profile || newVoiceProfile();
      const usesGroupSamples = sampleSource === "authorized_group_samples";
      return {
        ...draft,
        voice_profile: {
          ...current,
          sample_source: sampleSource,
          sample_scope: usesGroupSamples ? "current_group" : "none",
          authorized_sample_session_ids: usesGroupSamples ? [groupId] : [],
          authorization_reference: "",
          source_persona_version: sampleSource === "persona"
            ? Math.max(1, current.source_persona_version)
            : 0,
        },
      };
    });
  };

  return (
    <section className="panel span-3" aria-labelledby="voice-profile-heading">
      <div className="panel-header">
        <div>
          <p className="section-kicker">表达风格</p>
          <h2 id="voice-profile-heading">群聊表达风格</h2>
        </div>
        <div className="action-row">
          <span className={`pill ${status.className}`}>{status.label}</span>
          <label className="toggle-chip">
            <strong>
              <input
                type="checkbox"
                checked={Boolean(profile?.enabled)}
                onChange={(event) =>
                  onUpdateDraft((draft) => ({
                    ...draft,
                    voice_profile: {
                      ...(draft.voice_profile || newVoiceProfile()),
                      enabled: event.target.checked,
                    },
                  }))
                }
              />
              启用表达风格
            </strong>
          </label>
        </div>
      </div>
      {profile ? (
        <div className="form-grid">
          <div className="data-flow-note span-2">
            <strong>样本与受众边界</strong>
            <span>
              本页只保存来源、授权群和授权记录引用，不采集或回显聊天正文。当前群样本不能来自私聊，也不能跨群复用。
            </span>
          </div>
          <label className="field">
            <span>样本来源</span>
            <select
              value={profile.sample_source}
              onChange={(event) =>
                updateVoiceSource(event.target.value as VoiceProfile["sample_source"])
              }
            >
              <option value="manual">人工配置（不使用聊天样本）</option>
              <option value="persona">已审核人物档案版本</option>
              <option value="authorized_group_samples">当前群授权样本</option>
            </select>
          </label>
          <label className="field">
            <span>样本范围</span>
            <select value={profile.sample_scope} disabled>
              <option value="none">不使用群聊样本</option>
              <option value="current_group">仅当前群</option>
            </select>
          </label>
          <label className="field">
            <span>授权群</span>
            <input
              readOnly
              value={profile.authorized_sample_session_ids[0] || "无"}
              aria-label="授权群"
              aria-describedby="voice-profile-authorized-group-help"
            />
            <small id="voice-profile-authorized-group-help" className="muted-copy">
              授权样本来源只允许锁定当前群，不能手填其他会话。
            </small>
          </label>
          <label className="field">
            <span>授权引用</span>
            <input
              maxLength={240}
              value={profile.authorization_reference}
              onChange={(event) =>
                updateVoiceValue("authorization_reference", event.target.value)
              }
              placeholder="审批单号、授权记录或样本集编号；不要粘贴聊天正文"
            />
          </label>
          <label className="field">
            <span>生效时间</span>
            <input
              type="datetime-local"
              value={datetimeLocalValue(profile.valid_from)}
              onChange={(event) =>
                updateVoiceValue("valid_from", datetimeIsoValue(event.target.value))
              }
            />
          </label>
          <label className="field">
            <span>到期时间</span>
            <input
              type="datetime-local"
              value={datetimeLocalValue(profile.expires_at)}
              onChange={(event) =>
                updateVoiceValue("expires_at", datetimeIsoValue(event.target.value))
              }
            />
          </label>
          <div className="route-list span-2" aria-live="polite">
            <div>
              生效状态：<strong>{status.label}</strong>
              {profile.valid_from
                ? ` · 起始 ${new Date(profile.valid_from).toLocaleString()}`
                : " · 立即生效"}
              {profile.expires_at
                ? ` · 到期 ${new Date(profile.expires_at).toLocaleString()}`
                : " · 无固定到期时间"}
            </div>
            <div>
              运行时会再次核对启停、时间和当前群授权；不满足任一条件时不会应用风格。
            </div>
          </div>
          {validationError ? (
            <div className="span-2">
              <Alert variant="danger" title="表达风格配置不可保存">
                {validationError}
              </Alert>
            </div>
          ) : null}
          <label className="field">
            <span>档案标识</span>
            <input
              value={profile.profile_id}
              onChange={(event) => updateVoiceValue("profile_id", event.target.value)}
            />
          </label>
          <label className="field">
            <span>显示名称</span>
            <input
              value={profile.display_name}
              onChange={(event) => updateVoiceValue("display_name", event.target.value)}
            />
          </label>
          <label className="field">
            <span>语气</span>
            <input
              value={profile.tone}
              onChange={(event) => updateVoiceValue("tone", event.target.value)}
              placeholder="例如：自然、温暖、直接"
            />
          </label>
          <label className="field">
            <span>回复长度</span>
            <select
              value={profile.verbosity}
              onChange={(event) =>
                updateVoiceValue("verbosity", event.target.value as VoiceProfile["verbosity"])
              }
            >
              <option value="terse">极短</option>
              <option value="concise">简洁</option>
              <option value="balanced">均衡</option>
            </select>
          </label>
          <label className="field span-2">
            <span>偏好短语（逗号或换行分隔，去重后最多 30 条）</span>
            <textarea
              rows={3}
              value={profile.phrase_preferences.join("\n")}
              onChange={(event) =>
                updateVoiceValue("phrase_preferences", listValue(event.target.value))
              }
            />
          </label>
          <label className="field">
            <span>表情符号频率（0–0.15）</span>
            <input
              type="number"
              min={0}
              max={0.15}
              step={0.01}
              value={formatProbabilityInput(profile.emoji_frequency)}
              onChange={(event) =>
                updateVoiceValue("emoji_frequency", numberValue(event.target.value))
              }
            />
          </label>
          <label className="field">
            <span>列表格式</span>
            <select
              value={profile.list_format_policy}
              onChange={(event) =>
                updateVoiceValue(
                  "list_format_policy",
                  event.target.value as VoiceProfile["list_format_policy"],
                )
              }
            >
              <option value="avoid_by_default">默认不用列表</option>
              <option value="allow">允许列表</option>
            </select>
          </label>
          <label className="field">
            <span>身份说明</span>
            <select
              value={profile.identity_disclosure}
              onChange={(event) =>
                updateVoiceValue(
                  "identity_disclosure",
                  event.target.value as VoiceProfile["identity_disclosure"],
                )
              }
            >
              <option value="contextual">有需要时说明</option>
              <option value="always">每次都说明</option>
            </select>
          </label>
          <label className="field">
            <span>来源人物档案版本</span>
            <input
              type="number"
              min={0}
              value={profile.source_persona_version}
              disabled={profile.sample_source !== "persona"}
              onChange={(event) =>
                updateVoiceValue("source_persona_version", numberValue(event.target.value))
              }
            />
          </label>
          <section className="span-2" aria-labelledby="voice-profile-preview-heading">
            <div className="panel-header">
              <div>
                <p className="section-kicker">不发送预览</p>
                <h3 id="voice-profile-preview-heading">用真实风格保护器试算</h3>
              </div>
              <span className="pill pill-muted">不会发到群里</span>
            </div>
            <div className="data-flow-note">
              <strong>瞬时处理，不进入策略历史或参与事件</strong>
              <span>
                候选回复只发送给预览接口完成本次计算；服务端不保存候选回复或聊天正文。
              </span>
            </div>
            <div className="form-grid">
              <label className="field span-2">
                <span>候选回复</span>
                <textarea
                  rows={3}
                  maxLength={4000}
                  value={previewText}
                  onChange={(event) => setPreviewText(event.target.value)}
                />
              </label>
              <label className="toggle-chip">
                <span>
                  <input
                    type="checkbox"
                    checked={previewDetailed}
                    onChange={(event) => setPreviewDetailed(event.target.checked)}
                  />
                  模拟用户明确要求详细回答
                </span>
                <em>只影响本次句长与列表试算。</em>
              </label>
            </div>
            <div className="action-row">
              <button
                className="button button-secondary"
                type="button"
                onClick={() => void runPreview()}
                disabled={previewing || !previewText.trim() || Boolean(validationError)}
              >
                {previewing ? "正在试算…" : "预览表达效果"}
              </button>
            </div>
            {previewError ? (
              <Alert variant="danger" title="预览失败">{previewError}</Alert>
            ) : null}
            {previewResult ? (
              <div className="route-list" aria-live="polite">
                <div>
                  <strong>
                    {PREVIEW_RUNTIME_LABELS[previewResult.runtime_reason]
                      || previewResult.runtime_reason}
                  </strong>
                  <span>
                    {previewResult.transformed ? "已调整表达" : "无需调整"}
                    {` · 模式 ${previewResult.mode}`}
                  </span>
                </div>
                <div className="admin-notice">{previewResult.output_text}</div>
              </div>
            ) : null}
          </section>
        </div>
      ) : (
        <p className="muted-copy">
          尚未建立群级风格档案。打开“启用表达风格”后会创建一份默认关闭样本采集的人工配置。
        </p>
      )}
      <label className="field u-mt-4">
        <span>本次变更原因（进入审计记录）</span>
        <input
          maxLength={240}
          value={reason}
          onChange={(event) => onReasonChange(event.target.value)}
          placeholder="例如：降低晚间群聊打扰"
        />
      </label>
    </section>
  );
}
