import type { ParticipationPolicyValues } from "../../lib/api";
import { formatProbabilityInput, numberValue } from "./policyModel";
import { ToggleCard } from "./presentation";

export type ParticipationValueUpdater = <Key extends keyof ParticipationPolicyValues>(
  key: Key,
  value: ParticipationPolicyValues[Key],
) => void;

export function ParticipationBudgetSection({
  policy,
  onUpdate,
}: {
  policy: ParticipationPolicyValues;
  onUpdate: ParticipationValueUpdater;
}) {
  return (
    <section className="panel span-2" aria-labelledby="participation-policy-heading">
      <div className="panel-header">
        <div>
          <p className="section-kicker">发言预算</p>
          <h2 id="participation-policy-heading">阈值、预算与安静时段</h2>
        </div>
      </div>
      <div className="form-grid">
        <label className="field">
          <span>群内 @发送者策略</span>
          <select
            aria-label="群内 @发送者策略"
            value={policy.mention_sender_strategy}
            onChange={(event) =>
              onUpdate(
                "mention_sender_strategy",
                event.target.value as ParticipationPolicyValues["mention_sender_strategy"],
              )
            }
          >
            <option value="never">从不自动 @（默认）</option>
            <option value="reply_or_ambiguous">回复机器人或对象不明确时 @</option>
          </select>
          <small>成员级“不在群内提及”始终拥有最终否决权。</small>
        </label>
        <label className="field">
          <span>提示上下文保留秒数（最多 24 小时）</span>
          <input
            aria-label="提示上下文保留秒数"
            type="number"
            min={0}
            max={86400}
            step={300}
            value={policy.prompt_context_retention_seconds}
            onChange={(event) =>
              onUpdate(
                "prompt_context_retention_seconds",
                numberValue(event.target.value),
              )
            }
          />
          <small>
            仅控制进入模型提示的群观察与摘要；0 表示关闭，不会删除底层记录。
          </small>
        </label>
      </div>
      <div className="form-grid">
        <label className="field">
          <span>柔性回复阈值（0–200）</span>
          <input
            aria-label="柔性回复阈值"
            type="number"
            min={0}
            max={200}
            value={policy.threshold}
            onChange={(event) => onUpdate("threshold", numberValue(event.target.value))}
          />
        </label>
        <label className="field">
          <span>最近 40 条最大机器人占比</span>
          <input
            type="number"
            min={0}
            max={1}
            step={0.01}
            value={formatProbabilityInput(policy.max_bot_ratio_last_40)}
            onChange={(event) =>
              onUpdate("max_bot_ratio_last_40", numberValue(event.target.value))
            }
          />
        </label>
        <label className="field">
          <span>10 分钟柔性回复上限</span>
          <input
            type="number"
            min={0}
            max={100}
            value={policy.max_soft_replies_10m}
            onChange={(event) =>
              onUpdate("max_soft_replies_10m", numberValue(event.target.value))
            }
          />
        </label>
        <label className="field">
          <span>每小时柔性回复上限</span>
          <input
            type="number"
            min={0}
            max={500}
            value={policy.max_soft_replies_hour}
            onChange={(event) =>
              onUpdate("max_soft_replies_hour", numberValue(event.target.value))
            }
          />
        </label>
        <label className="field">
          <span>连续机器人消息上限</span>
          <input
            type="number"
            min={0}
            max={20}
            value={policy.max_consecutive_bot_messages}
            onChange={(event) =>
              onUpdate("max_consecutive_bot_messages", numberValue(event.target.value))
            }
          />
        </label>
        <label className="field">
          <span>时区</span>
          <input
            value={policy.timezone}
            onChange={(event) => onUpdate("timezone", event.target.value)}
          />
        </label>
        <label className="field">
          <span>安静时段开始（小时）</span>
          <input
            type="number"
            min={0}
            max={23}
            value={policy.quiet_start_hour}
            onChange={(event) =>
              onUpdate("quiet_start_hour", numberValue(event.target.value))
            }
          />
        </label>
        <label className="field">
          <span>安静时段结束（小时）</span>
          <input
            type="number"
            min={0}
            max={23}
            value={policy.quiet_end_hour}
            onChange={(event) =>
              onUpdate("quiet_end_hour", numberValue(event.target.value))
            }
          />
        </label>
      </div>
    </section>
  );
}

export function ProactiveParticipationSection({
  policy,
  onUpdate,
}: {
  policy: ParticipationPolicyValues;
  onUpdate: ParticipationValueUpdater;
}) {
  return (
    <section className="panel" aria-labelledby="proactive-heading">
      <div className="panel-header">
        <div>
          <p className="section-kicker">主动参与</p>
          <h2 id="proactive-heading">主动开口边界</h2>
        </div>
      </div>
      <div className="form-grid">
        <ToggleCard
          checked={policy.proactive_enabled}
          label="允许主动参与"
          description="仅在预算、静默时间和发送时复核都通过时开口"
          onChange={(checked) => onUpdate("proactive_enabled", checked)}
        />
        <label className="field">
          <span>每天主动参与上限</span>
          <input
            type="number"
            min={0}
            max={100}
            value={policy.max_proactive_per_day}
            onChange={(event) =>
              onUpdate("max_proactive_per_day", numberValue(event.target.value))
            }
          />
        </label>
        <label className="field span-2">
          <span>主动参与前最短沉默秒数</span>
          <input
            type="number"
            min={0}
            max={604800}
            value={policy.proactive_min_silence_seconds}
            onChange={(event) =>
              onUpdate(
                "proactive_min_silence_seconds",
                numberValue(event.target.value),
              )
            }
          />
        </label>
      </div>
    </section>
  );
}
