import { Link, useParams } from "react-router-dom";

import { PageHeader } from "../../components/PageHeader";
import { deliveryTone } from "./model";
import { useMessageStory } from "./useMessageStory";

const TONE_PILL: Record<"ok" | "danger" | "warning" | "muted", string> = {
  ok: "pill pill-ok",
  danger: "pill pill-danger",
  warning: "pill pill-warning",
  muted: "pill pill-muted",
};

export function MessageStoryPage() {
  const { traceId = "" } = useParams<{ traceId: string }>();
  const decodedTraceId = decodeURIComponent(traceId).trim();
  const { loading, story, errors, refresh } = useMessageStory(decodedTraceId);

  return (
    <div className="page-grid message-story-page">
      <section className="panel span-3 message-story-panel">
        <PageHeader
          eyebrow="消息跟踪"
          title="这条消息"
          description="按一条群消息看：看到了什么、为什么回或不回、回了什么、发出去了没有。"
          actions={(
            <div className="action-row">
              <Link className="button button-secondary" to="/queues">返回消息队列</Link>
              <button
                className="button button-secondary"
                type="button"
                onClick={() => void refresh()}
                disabled={loading || !decodedTraceId}
              >
                {loading ? "刷新中…" : "刷新"}
              </button>
            </div>
          )}
        />

        {!decodedTraceId ? (
          <p className="muted-copy">缺少追踪标识。从消息队列、发送队列或决策事件点进来。</p>
        ) : null}

        {errors.length ? (
          <div className="alert alert-warning" role="status">
            <span className="alert-icon" aria-hidden="true">!</span>
            <div className="alert-content">
              <strong>有些层没有读到</strong>
              <div>{errors.join("；")}</div>
            </div>
          </div>
        ) : null}

        {story ? (
          <>
            <div className={`message-story-verdict is-${deliveryTone(story.delivery)}`}>
              <div>
                <p className="section-kicker">结论</p>
                <h2>{story.headline}</h2>
                <p className="muted-copy">
                  {[story.timeLabel, story.sessionName, story.sender && `发送人 ${story.sender}`]
                    .filter(Boolean)
                    .join(" · ") || "身份还不完整"}
                </p>
              </div>
              <div className="message-story-verdict-pills">
                <span className={TONE_PILL[deliveryTone(story.delivery)]}>{story.deliveryLabel}</span>
                {story.decisionLabel ? (
                  <span className={story.decision === "cancel" ? "pill pill-danger" : "pill pill-ok"}>
                    {story.decisionLabel}
                  </span>
                ) : null}
                {story.score !== null ? <span className="pill pill-muted">得分 {story.score}</span> : null}
              </div>
            </div>

            <div className="message-story-speech">
              <article className="message-story-bubble">
                <p className="section-kicker">群里</p>
                <h3>{story.sender || "未识别发送人"}</h3>
                <p className="message-story-copy">
                  {story.inboundText || "入站原文已经滚出最近消息流。如果回复队列还在，刷新后可能补回来。"}
                </p>
              </article>
              <article className="message-story-bubble is-bot">
                <p className="section-kicker">机器人</p>
                <h3>{story.replyText ? "回复" : "没有回复正文"}</h3>
                <p className="message-story-copy">
                  {story.replyText || (
                    story.delivery === "no_reply"
                      ? "按当前参与策略，这条不需要回。"
                      : "还没有生成回复，或回复还没写入发送队列。"
                  )}
                </p>
              </article>
            </div>

            <section className="message-story-reason" aria-labelledby="message-story-reason-heading">
              <p className="section-kicker">为什么</p>
              <h3 id="message-story-reason-heading">参与决策</h3>
              <p>
                {story.reasonText
                  ? story.reasonText
                  : story.decision
                    ? "有决策结果，但没有额外原因码。"
                    : "还没有查到这条消息的参与决策。可能消息流已过期，或当时没有写入事件。"}
              </p>
            </section>

            <section className="message-story-timeline" aria-labelledby="message-story-timeline-heading">
              <p className="section-kicker">时间线</p>
              <h3 id="message-story-timeline-heading">发生了什么</h3>
              {story.timeline.length ? (
                <ol className="message-story-timeline-list">
                  {story.timeline.map((item) => (
                    <li className={`message-story-timeline-item is-${item.tone}`} key={item.key}>
                      <time>{item.time || "—"}</time>
                      <div>
                        <strong>{item.title}</strong>
                        <p>{item.detail}</p>
                      </div>
                    </li>
                  ))}
                </ol>
              ) : (
                <p className="muted-copy">
                  {loading ? "正在读取这条消息…" : "这条追踪下还没有入站、决策或回复记录。"}
                </p>
              )}
            </section>

            <div className="message-story-advanced">
              <p className="muted-copy mono">{decodedTraceId}</p>
              <Link
                className="link-button"
                to={`/plugins?trace_id=${encodeURIComponent(decodedTraceId)}`}
              >
                查看 Flow / Effect 细节
              </Link>
            </div>
          </>
        ) : (
          <p className="muted-copy">{loading ? "正在读取这条消息…" : "还没有可展示的故事。"}</p>
        )}
      </section>
    </div>
  );
}
