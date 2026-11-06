import type { MemoryDecisionTrace } from "./models";

const STATUS: Record<string, string> = {
  loaded: "已加载", partial: "部分加载", blocked: "已阻断", skipped: "已跳过",
  error: "读取失败", not_run: "未执行", saved: "已保存", queued: "已排队",
};

const REASON: Record<string, string> = {
  member_privacy_blocked: "成员记忆未获授权",
  no_session: "没有可用会话",
  timeout: "读取超时",
  load_failed: "读取失败",
  scope_load_failed: "部分记忆范围读取失败",
  memory_control_handled: "本轮已由记忆指令处理",
  private_memory_disabled: "个人记忆未开启",
  not_run: "本轮未执行此步骤",
  ok: "处理完成",
};

export function MemoryTraceSummary({ memory }: { memory?: MemoryDecisionTrace }) {
  if (!memory) return null;
  return (
    <section className="panel" aria-label="本次记忆诊断">
      <h4>本次记忆诊断</h4>
      <p>
        加载：{STATUS[memory.load_status || ""] || memory.load_status || "未记录"}
        {memory.load_reason ? ` · ${REASON[memory.load_reason] || memory.load_reason}` : ""}
      </p>
      <p>
        保存：{STATUS[memory.save_status || ""] || memory.save_status || "未记录"}
        {memory.save_reason ? ` · ${REASON[memory.save_reason] || memory.save_reason}` : ""}
      </p>
      {memory.member_recall_blocked ? <p>成员个人记忆未进入本次回复；群公共记忆遵循独立群设置。</p> : null}
      <p>
        记忆候选 {memory.candidate_count ?? "—"} 条 · 选中记忆 {memory.selected_count ?? "—"} 条
        {" · "}关系事实 {memory.selected_fact_count ?? "—"} 条
        {" · "}事件片段 {memory.selected_episode_count ?? "—"} 条
      </p>
      <small>这里记录加载与筛选结果；选中记录不等于答案已引用，也不代表内容已经核实。</small>
    </section>
  );
}
