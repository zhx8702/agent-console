import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { BackfillPanel } from "./BackfillPanel";


describe("BackfillPanel connection scope", () => {
  it("shows the explicit legacy wxbot history connection as read-only", () => {
    render(
      <BackfillPanel
        connectionId="legacy-wechat-default"
        pickerSessionId=""
        pickerOptions={[]}
        daysLimit={180}
        maxMessagesPerSession={200}
        limit={50}
        runtimeProfile={null}
        userId="wxid-member"
        selectedSessions={[]}
        events={[]}
        onPickerSessionIdChange={vi.fn()}
        onSessionIdsTextChange={vi.fn()}
        onDaysLimitChange={vi.fn()}
        onMaxMessagesPerSessionChange={vi.fn()}
        onLimitChange={vi.fn()}
        onRuntimeOutputChange={vi.fn()}
        onAddSessions={vi.fn()}
        onRemoveSession={vi.fn()}
        onRunBackfill={vi.fn()}
        onLoadRuntimeProfile={vi.fn()}
        onLoadEvents={vi.fn()}
      />,
    );

    const connection = screen.getByRole("textbox", { name: "历史连接" });
    expect(connection).toHaveValue("legacy-wechat-default");
    expect(connection).toHaveAttribute("readonly");
    expect(screen.getByText("原始 SDK 历史只允许默认租户的 legacy 微信连接。")).toBeInTheDocument();
  });
});
