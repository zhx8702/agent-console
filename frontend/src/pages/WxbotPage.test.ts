import { describe, expect, it } from "vitest";

import {
  DEFAULT_GROUP_PARTICIPATION_POLICY,
  WxbotPage,
  groupActivityReasonLabel,
  normalizeGroupParticipationPolicy,
  readWxbotTabFromLocation,
  resolveExplicitVerifiedGroupSessionId,
} from "./WxbotPage";

describe("wxbot public module boundary", () => {
  it("keeps the lazy-route component exported from the thin page entrypoint", () => {
    expect(typeof WxbotPage).toBe("function");
  });
});

describe("wxbot deep-link routing", () => {
  it.each([
    ["connect", "overview"],
    ["groups", "overview"],
    ["participation", "policy"],
    ["launch", "policy"],
    ["test", "send"],
  ] as const)("maps onboarding=%s to the %s tab", (step, tab) => {
    expect(readWxbotTabFromLocation(`?onboarding=${step}`, "")).toBe(tab);
  });

  it("supports onboarding links in the hash", () => {
    expect(readWxbotTabFromLocation("", "#onboarding=test")).toBe("send");
  });

  it("keeps an explicit tab authoritative over onboarding guidance", () => {
    expect(readWxbotTabFromLocation("?tab=reports&onboarding=connect", "")).toBe("reports");
  });

  it("falls back safely for an unknown onboarding step", () => {
    expect(readWxbotTabFromLocation("?onboarding=unknown", "")).toBe("overview");
  });
});

describe("wxbot humanized policy defaults", () => {
  it("keeps omitted participation fields on the conservative server defaults", () => {
    expect(normalizeGroupParticipationPolicy({ threshold: 75 })).toEqual({
      ...DEFAULT_GROUP_PARTICIPATION_POLICY,
      threshold: 75,
    });
    expect(DEFAULT_GROUP_PARTICIPATION_POLICY).toMatchObject({
      quiet_start_hour: 23,
      quiet_end_hour: 8,
      max_soft_replies_10m: 2,
      max_soft_replies_hour: 6,
      max_bot_ratio_last_40: 0.15,
      max_consecutive_bot_messages: 2,
    });
  });

  it("turns activity reason codes into actionable operator copy", () => {
    expect(groupActivityReasonLabel("awaiting_human_response")).toContain("没人回应");
    expect(groupActivityReasonLabel("would_trigger")).toContain("会发起暖场");
    expect(groupActivityReasonLabel("custom_reason")).toBe("custom_reason");
  });
});

describe("wxbot verified group selection", () => {
  const groups = [{ session_id: "first@chatroom", session_name: "第一个群", kind: "group" }];

  it("does not auto-select the first roster group on initial load", () => {
    expect(resolveExplicitVerifiedGroupSessionId("", "", groups)).toBe("");
  });

  it("accepts only an explicit selection present in the verified roster", () => {
    expect(resolveExplicitVerifiedGroupSessionId("first@chatroom", "first@chatroom", groups)).toBe("first@chatroom");
    expect(resolveExplicitVerifiedGroupSessionId("unknown@chatroom", "unknown@chatroom", groups)).toBe("");
  });
});
