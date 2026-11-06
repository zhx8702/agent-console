import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { MemoryTraceSummary } from "./MemoryTraceSummary";

describe("memory trace summary", () => {
  it("distinguishes policy blocking from a missing session", () => {
    render(<MemoryTraceSummary memory={{
      load_status: "blocked", load_reason: "member_privacy_blocked",
      save_status: "skipped", save_reason: "member_privacy_blocked",
      member_recall_blocked: true,
    }} />);
    expect(screen.getByRole("region", { name: "本次记忆诊断" })).toHaveTextContent("成员记忆未获授权");
    expect(screen.queryByText(/没有可用会话/)).not.toBeInTheDocument();
    expect(screen.getByText(/选中记录不等于答案已引用/)).toBeInTheDocument();
  });

  it("keeps old traces compatible without inventing a successful load", () => {
    const { container } = render(<MemoryTraceSummary />);
    expect(container).toBeEmptyDOMElement();
  });
});
