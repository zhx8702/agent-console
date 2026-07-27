import { useState } from "react";

import { Alert, EmptyState } from "../../components";
import type {
  ParticipationDecisionDocument,
  ParticipationPreviewRequest,
} from "../../lib/api";
import {
  createDefaultParticipationPreview,
  extractParticipationSignals,
  type ParticipationHistoryExtraction,
} from "./participationSimulatorModel";
import { formatProbabilityInput } from "./policyModel";
import {
  TechnicalDetails,
  ToggleCard,
  decisionPill,
  friendlyErrorMessage,
  formatTime,
  reasonLabel,
} from "./presentation";

const PREVIEW_BOOLEAN_FIELDS: Array<{
  key: keyof ParticipationPreviewRequest;
  label: string;
  description: string;
}> = [
  { key: "base_eligible", label: "基础策略允许", description: "上游基础规则已允许参与" },
  { key: "mentioned_me", label: "明确 @ 机器人", description: "消息直接提及机器人" },
  { key: "replied_to_bot", label: "回复机器人", description: "消息是对机器人上一条回复的回应" },
  { key: "explicit_command", label: "明确命令", description: "用户发出了可执行命令" },
  { key: "safety_response_required", label: "需要安全响应", description: "安全策略要求必须回应" },
  { key: "explicit_question_to_bot", label: "向机器人提问", description: "问题对象明确是机器人" },
  { key: "keyword_triggered", label: "命中关注话题", description: "命中群配置的关键词" },
  { key: "topic_continuation", label: "延续话题", description: "与机器人刚参与的话题连续" },
  { key: "unfinished_task_continuation", label: "延续未完成任务", description: "继续上一次未完成的任务" },
  { key: "directed_to_other_member", label: "指向其他成员", description: "主要交流对象不是机器人" },
  { key: "rapid_multi_party_chat", label: "多人快速对话", description: "当前是高频的人类对话" },
  { key: "bot_replied_within_60s", label: "机器人 60 秒内回复过", description: "抑制连续抢话" },
  { key: "valid_member_answer_exists", label: "已有成员回答", description: "群友已经给出有效答案" },
  { key: "requested_proactive", label: "请求主动参与", description: "模拟一次主动暖场或跟进" },
  { key: "is_self_sent", label: "机器人自发消息", description: "用于验证自消息抑制" },
  { key: "topic_changed", label: "发送前话题已变化", description: "验证发送时重新检查" },
  { key: "superseded_by_newer_message", label: "已被新消息取代", description: "旧回复不应继续发送" },
  { key: "reply_target_ambiguous", label: "回复对象不明确", description: "无法确认应该回复谁" },
];

function numberValue(value: string, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

type ParticipationSimulatorPanelProps = {
  onPreview: (preview: ParticipationPreviewRequest) => Promise<ParticipationDecisionDocument>;
};

export function ParticipationSimulatorPanel({ onPreview }: ParticipationSimulatorPanelProps) {
  const [preview, setPreview] = useState<ParticipationPreviewRequest>(() =>
    createDefaultParticipationPreview(),
  );
  const [naturalHistory, setNaturalHistory] = useState("");
  const [extraction, setExtraction] = useState<ParticipationHistoryExtraction | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [error, setError] = useState("");
  const [decision, setDecision] = useState<ParticipationDecisionDocument | null>(null);

  const run = async () => {
    setError("");
    setDecision(null);
    if (preview.bot_messages_last_40 > preview.total_messages_last_40) {
      setError("最近 40 条中的机器人消息数不能大于总消息数");
      return;
    }
    if (Number.isNaN(new Date(preview.now).getTime())) {
      setError("模拟时间必须是有效的 ISO 8601 时间");
      return;
    }
    setPreviewing(true);
    try {
      setDecision(await onPreview(preview));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "模拟失败");
    } finally {
      setPreviewing(false);
    }
  };

  return (
    <div className="page-grid">
      <section className="panel span-3" aria-labelledby="history-assist-heading">
        <div className="panel-header">
          <div>
            <p className="section-kicker">隐私保护的辅助提取</p>
            <h2 id="history-assist-heading">从自然语言历史生成可编辑信号</h2>
          </div>
          <span className="pill pill-ok">仅在浏览器本地解析</span>
        </div>
        <p className="muted-copy">
          可粘贴少量脱敏群聊片段；规则只在本页识别时间、@、问答与安全线索，原文不会随模拟请求发送或写入参与事件。
        </p>
        <label className="field">
          <span>自然语言历史（仅本地解析）</span>
          <textarea
            aria-label="自然语言历史（仅本地解析）"
            rows={7}
            value={naturalHistory}
            placeholder={"[23:40:01] 张三：@机器人 你是谁？\n[23:40:07] 李四：答案是周五发布\n[23:40:12] 王五：换个话题"}
            onChange={(event) => {
              setNaturalHistory(event.target.value);
              setExtraction(null);
            }}
          />
        </label>
        <div className="action-row">
          <button
            className="button button-secondary"
            type="button"
            disabled={!naturalHistory.trim()}
            onClick={() => {
              const next = extractParticipationSignals(naturalHistory, preview);
              setPreview(next.draft);
              setExtraction(next);
              setDecision(null);
              setError("");
            }}
          >
            辅助提取结构化信号
          </button>
        </div>
        {extraction ? (
          <div aria-live="polite">
            <p className="muted-copy">
              已更新下方草稿；所有开关、计数和时间仍可手动复核与修改。
            </p>
            <ul className="route-list" aria-label="本地提取线索">
              {extraction.matchedSignals.length ? (
                extraction.matchedSignals.map((signal) => <li key={signal}>{signal}</li>)
              ) : (
                <li>没有命中明确规则，请手动设置结构化信号。</li>
              )}
            </ul>
            {extraction.caveats.map((caveat) => (
              <p className="muted-copy" key={caveat}>{caveat}</p>
            ))}
          </div>
        ) : (
          <p className="muted-copy">
            辅助规则不是语义模型，可能漏判或误判；运行前请检查下方草稿。
          </p>
        )}
      </section>
      <section className="panel span-3" aria-labelledby="preview-signals-heading">
        <div className="panel-header">
          <div>
            <p className="section-kicker">原文不离开浏览器</p>
            <h2 id="preview-signals-heading">结构化参与模拟器</h2>
          </div>
          <span className="pill pill-feature">后端仅接收结构化信号</span>
        </div>
        <p className="muted-copy">只提交决策所需的布尔信号、计数和置信度，用来验证策略边界。</p>
        <div className="form-grid">
          {PREVIEW_BOOLEAN_FIELDS.map((field) => (
            <ToggleCard
              key={field.key}
              checked={Boolean(preview[field.key])}
              label={field.label}
              description={field.description}
              onChange={(checked) => setPreview((current) => ({ ...current, [field.key]: checked }))}
            />
          ))}
        </div>
      </section>
      <section className="panel span-2" aria-labelledby="preview-counters-heading">
        <div className="panel-header">
          <div>
            <p className="section-kicker">当前场景</p>
            <h2 id="preview-counters-heading">消息窗口与预算</h2>
          </div>
        </div>
        <div className="form-grid">
          <label className="field span-2">
            <span>模拟时间（ISO 8601）</span>
            <input
              aria-label="模拟时间（ISO 8601）"
              value={preview.now}
              onChange={(event) =>
                setPreview((current) => ({ ...current, now: event.target.value }))
              }
            />
          </label>
          {(
            [
              ["intent_confidence", "意图置信度", 0, 1, 0.05],
              ["bot_messages_last_40", "最近 40 条机器人消息", 0, 40, 1],
              ["total_messages_last_40", "最近窗口总消息", 0, 40, 1],
              ["soft_replies_last_10m", "10 分钟柔性回复数", 0, 1000, 1],
              ["soft_replies_last_hour", "每小时柔性回复数", 0, 1000, 1],
              ["consecutive_bot_messages", "连续机器人消息数", 0, 1000, 1],
              ["proactive_messages_today", "今日主动参与数", 0, 1000, 1],
              ["group_silence_seconds", "群聊沉默秒数", 0, 604800, 1],
            ] as const
          ).map(([key, label, min, max, step]) => (
            <label className="field" key={key}>
              <span>{label}</span>
              <input
                type="number"
                min={min}
                max={max}
                step={step}
                value={key === "intent_confidence"
                  ? formatProbabilityInput(preview.intent_confidence)
                  : preview[key] as number}
                onChange={(event) =>
                  setPreview((current) => ({ ...current, [key]: numberValue(event.target.value) }))
                }
              />
            </label>
          ))}
          <label className="field">
            <span>基础抑制原因</span>
            <select
              value={preview.base_reason}
              onChange={(event) =>
                setPreview((current) => ({
                  ...current,
                  base_reason: event.target.value as ParticipationPreviewRequest["base_reason"],
                }))
              }
            >
              <option value="">无</option>
              <option value="base_policy_not_eligible">基础策略不允许</option>
              <option value="not_addressed">未指向机器人</option>
              <option value="channel_suppressed">渠道已抑制</option>
              <option value="member_opt_out">成员已退出</option>
              <option value="group_disabled">群参与已关闭</option>
            </select>
          </label>
          <label className="field">
            <span>预期回复类型</span>
            <select
              value={preview.response_kind}
              onChange={(event) =>
                setPreview((current) => ({
                  ...current,
                  response_kind: event.target.value as ParticipationPreviewRequest["response_kind"],
                }))
              }
            >
              <option value="short">短回复</option>
              <option value="tool_progress">工具进度</option>
              <option value="tool_result">工具结果</option>
            </select>
          </label>
        </div>
        <div className="action-row">
          <button className="button button-primary" type="button" onClick={() => void run()} disabled={previewing}>
            {previewing ? "正在计算…" : "运行结构化模拟"}
          </button>
          <button
            className="button button-secondary"
            type="button"
            onClick={() => {
              setPreview(createDefaultParticipationPreview());
              setNaturalHistory("");
              setExtraction(null);
              setDecision(null);
              setError("");
            }}
          >
            重置场景
          </button>
        </div>
      </section>
      <section className="panel" aria-labelledby="preview-result-heading">
        <div className="panel-header">
          <div>
            <p className="section-kicker">决策结果</p>
            <h2 id="preview-result-heading">模拟结果</h2>
          </div>
          {decision ? decisionPill(decision.status) : null}
        </div>
        {error ? (
          <>
            <Alert variant="danger" title="模拟失败">
              {friendlyErrorMessage(error, "结构化模拟未完成，请检查输入后重试。")}
            </Alert>
            <TechnicalDetails
              data={{ error }}
              summary="查看模拟错误详情"
              label="结构化模拟错误 JSON"
            />
          </>
        ) : null}
        {decision ? (
          <>
            <div className="status-grid">
              <article className="status-tile"><span>得分</span><strong>{decision.score}</strong></article>
              <article className="status-tile"><span>策略版本</span><strong>v{decision.policy_version}</strong></article>
              <article className="status-tile"><span>提及发送者</span><strong>{decision.mention_sender ? "是" : "否"}</strong></article>
            </div>
            <ul className="route-list u-mt-4" aria-label="决策依据">
              {decision.reason_codes.map((reason) => (
                <li key={reason}>{reasonLabel(reason)}</li>
              ))}
              {!decision.reason_codes.length ? <li>没有额外抑制或加分原因</li> : null}
            </ul>
            <p className="muted-copy">
              计划窗口：{formatTime(decision.not_before)} 至 {formatTime(decision.expires_at)}
            </p>
            <TechnicalDetails
              data={decision}
              summary="查看完整模拟结果"
              label="完整模拟结果 JSON"
            />
          </>
        ) : (
          <EmptyState compact title="等待模拟" description="可直接编辑结构化场景，或先在本地辅助提取后再运行。" />
        )}
      </section>
    </div>
  );
}
