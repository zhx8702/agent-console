import { describe, expect, it } from "vitest";

import { mergeSessions } from "./ModerationPage";

describe("moderation group scope", () => {
  it("only exposes groups returned by the verified wxbot roster", () => {
    const result = mergeSessions(
      [
        {
          session_id: "verified@chatroom",
          session_name: "已验证群",
          enabled: true,
          keyword_count: 2,
          event_count: 3,
        },
        {
          session_id: "forged@chatroom",
          session_name: "伪造群",
          enabled: true,
          keyword_count: 99,
          event_count: 99,
        },
      ],
      [
        { session_id: "verified@chatroom", session_name: "已验证群", kind: "group" },
        { session_id: "private-user", session_name: "私聊", kind: "private" },
      ],
    );

    expect(result).toHaveLength(1);
    expect(result[0]).toMatchObject({
      session_id: "verified@chatroom",
      enabled: true,
      keyword_count: 2,
      event_count: 3,
    });
  });
});
