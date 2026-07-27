import { describe, expect, it } from "vitest";

import { emptyEffectSummary, summarizeEffectLogRows } from "./models";

describe("plugin flow summary helpers", () => {
  it("keeps the effect-audit grouping used by the runtime panel", () => {
    const summary = summarizeEffectLogRows([
      { owner: "memory", type: "write", status: "committed", dry_run: false },
      { owner: "memory", type: "write", status: "committed", dry_run: false },
      { owner: "wxbot", type: "send", status: "duplicate", dry_run: true },
    ]);

    expect(summary.total).toBe(3);
    expect(summary.by_status).toEqual([
      { status: "committed", count: 2 },
      { status: "duplicate", count: 1 },
    ]);
    expect(summary.by_owner).toEqual([
      { owner: "memory", count: 2 },
      { owner: "wxbot", count: 1 },
    ]);
    expect(summary.by_dry_run).toEqual([
      { dry_run: false, count: 2 },
      { dry_run: true, count: 1 },
    ]);
    expect(summary.matrix).toEqual([
      { owner: "memory", type: "write", status: "committed", dry_run: false, count: 2 },
      { owner: "wxbot", type: "send", status: "duplicate", dry_run: true, count: 1 },
    ]);
  });

  it("returns stable empty collections before audit data loads", () => {
    expect(emptyEffectSummary()).toEqual({
      total: 0,
      by_status: [],
      by_owner: [],
      by_type: [],
      by_dry_run: [],
      matrix: [],
    });
  });
});
