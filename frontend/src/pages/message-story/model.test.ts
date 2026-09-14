import { describe, expect, it } from "vitest";

import { deriveMessageStory, extractMessageText } from "./model";

describe("message story model", () => {
  it("prefers inbound and reply text over technical summaries", () => {
    const story = deriveMessageStory({
      inbound: [{
        id: "in-1",
        session_id: "room@chatroom",
        user_id: "wxid_a",
        channel: "wechat",
        created_ts_ms: 1_700_000_000_000,
        payload: {
          message: { content: "flutter 调 ui 怎么调都怪" },
          metadata: { sender_name: "张三", session_name: "收购腾讯讨论组" },
        },
      }],
      outbound: [],
      replyQueue: [{
        id: 9,
        session_id: "room@chatroom",
        reply_text: "那倒是，RN 先让 AI 铺一版。",
        status: "sent",
        sent_at: "2026-09-04T08:33:00Z",
      }],
      events: [{
        event_id: "evt-1",
        tenant_id: "default",
        session_id: "room@chatroom",
        policy_version: 3,
        event_kind: "runtime",
        status: "may_reply",
        score: 60,
        reason_codes: ["explicit_question_to_bot"],
        signal_summary: {},
        trace_id: "trace-1",
        created_at: "2026-09-04T08:32:00Z",
        runtime_stage: "decision",
      }],
    });

    expect(story.inboundText).toBe("flutter 调 ui 怎么调都怪");
    expect(story.replyText).toBe("那倒是，RN 先让 AI 铺一版。");
    expect(story.sender).toBe("张三");
    expect(story.sessionName).toBe("收购腾讯讨论组");
    expect(story.decisionLabel).toBe("可以回复");
    expect(story.reasonText).toContain("明确向机器人提问");
    expect(story.delivery).toBe("sent");
    expect(story.headline).toBe("这条消息已经发出去了");
    expect(story.timeline.map((item) => item.title)).toEqual([
      "收到群消息",
      "参与决策 · 可以回复",
      "回复队列 · 已发送",
    ]);
  });

  it("uses the reply-queue source message when inbound stream is gone", () => {
    const story = deriveMessageStory({
      inbound: [],
      outbound: [],
      replyQueue: [{
        id: 3,
        sender_name: "李四",
        session_name: "产品群",
        source_message: { message: { content: "帮我看看这段报错" } },
        status: "pending",
      }],
      events: [],
    });

    expect(story.inboundText).toBe("帮我看看这段报错");
    expect(story.delivery).toBe("queued");
    expect(story.headline).toBe("回复正在排队发送");
  });

  it("explains observe-only as no reply instead of a missing delivery", () => {
    const story = deriveMessageStory({
      inbound: [{
        id: "in-2",
        payload: { content: "今天天气不错" },
      }],
      outbound: [],
      replyQueue: [],
      events: [{
        event_id: "evt-2",
        tenant_id: "default",
        session_id: "room@chatroom",
        policy_version: 1,
        event_kind: "runtime",
        status: "observe_only",
        score: 10,
        reason_codes: ["score_below_threshold"],
        signal_summary: {},
        trace_id: "trace-2",
        created_at: "2026-09-04T08:32:00Z",
      }],
    });

    expect(story.delivery).toBe("no_reply");
    expect(story.headline).toBe("机器人看到了，但决定不回");
    expect(story.reasonText).toContain("得分未达到参与阈值");
  });

  it("reads nested inbound content", () => {
    expect(extractMessageText({ message: { content: "原文" } })).toBe("原文");
    expect(extractMessageText({ message: { type: "image" } })).toBe("[图片]");
  });
});
