import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { Alert } from "../../components";
import { apiRequest } from "../../lib/api";
import { useConsoleConfig } from "../../state/console-config";

export type JevDesk = {
  session_id: string;
  in_help_sessions: boolean;
  participation_enabled: boolean;
  participation_shadow_only: boolean;
  min_confidence: number;
  social?: {
    effective_enabled: boolean;
    group_enabled: boolean;
    proactive_enabled: boolean;
  } | null;
  channel: {
    reply_mode: string;
    configured_reply_mode: string;
    inherits_global_keywords: boolean;
    keyword_count: number;
    mention_sender: boolean;
    has_session_row: boolean;
  } | null;
  conflicts: string[];
};

const CONFLICTS: Record<string, string> = {
  participation_disabled: "群内求助评估已关闭",
  not_in_help_sessions: "该群不在主动答疑白名单，Jev 不会判断求助",
  jev_observe_only: "求助判断仍是观察模式，只记录建议，不会主动开口",
  social_disabled: "群参与总开关未打开，Jev 判断了也不会发出去",
  social_proactive_disabled: "该群主动参与未打开，Jev 不会判断求助",
  channel_off: "微信回复策略是关闭，关键词和 Jev 都不会发出去",
  contains_without_keywords: "回复模式是包含关键词，但当前没有关键词",
  no_session_policy: "该群没有独立回复策略，继承全局默认",
};

const REPLY_MODES: Record<string, string> = {
  off: "关闭",
  contains: "包含关键词",
  all: "全部回复",
  inherit: "继承全局",
};

export function JevDeskStatus({ sessionId }: { sessionId: string }) {
  const { config } = useConsoleConfig();
  const [desk, setDesk] = useState<JevDesk | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const generation = useRef(0);

  useEffect(() => {
    const current = ++generation.current;
    if (!config.tenantId || !sessionId) {
      setDesk(null);
      setError("");
      setBusy(false);
      return;
    }
    setBusy(true);
    setError("");
    void apiRequest<{ desk: JevDesk }>(config, "/v1/admin/jev/desk", {
      auth: true,
      query: { tenant_id: config.tenantId, session_id: sessionId },
    }).then((result) => {
      if (current !== generation.current) return;
      setDesk(result.desk);
    }).catch((caught) => {
      if (current !== generation.current) return;
      setDesk(null);
      setError(String(caught));
    }).finally(() => {
      if (current === generation.current) setBusy(false);
    });
    return () => {
      generation.current += 1;
    };
  }, [config, sessionId]);

  if (!sessionId) return null;

  const pillClass = desk?.in_help_sessions && desk.participation_enabled && !desk.participation_shadow_only && desk.social?.proactive_enabled && desk.conflicts.length === 0
    ? "pill-ok"
    : desk?.conflicts.length
      ? "pill-danger"
      : "pill-warning";
  const pillLabel = !desk
    ? (busy ? "读取中" : "未读取")
    : !desk.participation_enabled
      ? "求助评估关闭"
      : desk.social && !desk.social.proactive_enabled
        ? "主动参与关闭"
        : !desk.in_help_sessions
          ? "未接入答疑"
          : desk.participation_shadow_only
            ? "观察模式"
            : "参与决策";

  return (
    <section className="panel span-3" aria-label="Jev 答疑状态">
      <div className="panel-header">
        <div>
          <p className="section-kicker">求助判断</p>
          <h2>Jev 答疑状态</h2>
        </div>
        <span className={`pill ${pillClass}`}>{pillLabel}</span>
      </div>
      <p className="muted-copy">
        这行只对照当前群会不会交给 Jev 判断求助。改白名单、阈值和观察模式仍在成员记忆里。
      </p>
      {error ? (
        <Alert variant="warning" title="Jev 状态读取失败">
          群参与策略仍可编辑。{error}
        </Alert>
      ) : null}
      {desk ? (
        <>
          <p>
            {desk.in_help_sessions ? "已在主动答疑白名单" : "不在主动答疑白名单"}
            {" · "}
            求助评估{desk.participation_enabled ? "已开启" : "已关闭"}
            {" · "}
            {desk.participation_shadow_only ? "观察模式" : "参与决策"}
            {" · "}
            主动参与{desk.social ? (desk.social.proactive_enabled ? "已开启" : "未开启") : "未读取"}
            {" · "}
            阈值 {desk.min_confidence.toFixed(2)}
          </p>
          {desk.channel ? (
            <p>
              微信策略：{REPLY_MODES[desk.channel.reply_mode] || desk.channel.reply_mode}
              {desk.channel.configured_reply_mode === "inherit" ? "（继承）" : ""}
              {" · "}
              关键词 {desk.channel.keyword_count} 个
              {desk.channel.inherits_global_keywords ? "（继承全局）" : ""}
            </p>
          ) : null}
          {desk.conflicts.map((code) => (
            <Alert key={code} variant="warning" title="与当前参与策略不一致">
              {CONFLICTS[code] || code}
            </Alert>
          ))}
        </>
      ) : null}
      <div className="action-row">
        <Link className="button button-secondary" to="/memory?tab=jev">打开 Jev 评估</Link>
      </div>
    </section>
  );
}
