import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useRef, useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { DangerAction } from "./DangerAction";
import { DataTable } from "./DataTable";
import { Dialog } from "./Dialog";
import { OutputPanel } from "./OutputPanel";
import { PageHeader } from "./PageHeader";
import { RouteAnnouncer } from "./RouteAnnouncer";
import { SearchableSelect } from "./SearchableSelect";
import { Tabs } from "./Tabs";
import { UnsavedChangesGuard } from "./UnsavedChangesGuard";

describe("shared accessibility components", () => {
  it("renders the page title as h1 and keeps technical output collapsed by default", () => {
    render(
      <>
        <PageHeader eyebrow="系统" title="运行状态" description="查看服务状态" />
        <OutputPanel title="接口响应" value={'{"ok":true}'} />
      </>,
    );

    expect(screen.getByRole("heading", { level: 1, name: "运行状态" })).toBeInTheDocument();
    const disclosure = screen.getByText("接口响应").closest("details");
    expect(disclosure).not.toHaveAttribute("open");
    expect(screen.getByText("技术详情")).toBeInTheDocument();
  });

  it("traps dialog focus, closes with Escape, and restores the trigger focus", async () => {
    const user = userEvent.setup();

    function Harness() {
      const [open, setOpen] = useState(false);
      const firstAction = useRef<HTMLButtonElement>(null);
      return (
        <>
          <button type="button" onClick={() => setOpen(true)}>
            打开设置
          </button>
          <Dialog
            open={open}
            onClose={() => setOpen(false)}
            title="确认设置"
            initialFocusRef={firstAction}
            footer={<button type="button">最后一步</button>}
          >
            <button ref={firstAction} type="button">
              第一步
            </button>
          </Dialog>
        </>
      );
    }

    render(<Harness />);
    const trigger = screen.getByRole("button", { name: "打开设置" });
    await user.click(trigger);
    await waitFor(() => expect(screen.getByRole("button", { name: "第一步" })).toHaveFocus());

    screen.getByRole("button", { name: "关闭对话框" }).focus();
    await user.keyboard("{Shift>}{Tab}{/Shift}");
    expect(screen.getByRole("button", { name: "最后一步" })).toHaveFocus();
    await user.keyboard("{Escape}");

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it("supports combobox arrow navigation, active descendant, Enter, and Escape", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <SearchableSelect
        value=""
        options={[
          { value: "alpha", label: "Alpha 群" },
          { value: "beta", label: "Beta 群" },
        ]}
        onChange={onChange}
        placeholder="选择群聊"
      />,
    );

    const combobox = screen.getByRole("combobox");
    combobox.focus();
    await user.keyboard("{ArrowDown}");
    expect(combobox).toHaveAttribute("aria-expanded", "true");
    expect(combobox.getAttribute("aria-activedescendant")).toContain("option-0");
    await user.keyboard("{ArrowDown}{Enter}");
    expect(onChange).toHaveBeenCalledWith("beta");
    expect(combobox).toHaveAttribute("aria-expanded", "false");

    await user.click(combobox);
    await user.keyboard("{Escape}");
    expect(combobox).toHaveAttribute("aria-expanded", "false");
  });

  it("exposes table caption and scoped headers and activates rows from the keyboard", async () => {
    const user = userEvent.setup();
    const onActivate = vi.fn();
    render(
      <DataTable
        caption="成员列表"
        rows={[{ id: "u1", name: "小林" }]}
        rowKey={(row) => row.id}
        rowLabel={(row) => `打开成员 ${row.name}`}
        onRowActivate={onActivate}
        columns={[
          { id: "name", header: "成员", cell: (row) => row.name },
          { id: "id", header: "标识", cell: (row) => row.id },
        ]}
      />,
    );

    expect(screen.getByText("成员列表").tagName).toBe("CAPTION");
    expect(screen.getByRole("columnheader", { name: "成员" })).toHaveAttribute("scope", "col");
    const rowAction = screen.getByRole("button", { name: "打开成员 小林" });
    rowAction.focus();
    await user.keyboard("{Enter}");
    expect(onActivate).toHaveBeenCalledWith({ id: "u1", name: "小林" }, 0);
  });

  it("uses roving tab focus and arrow navigation for tabs", async () => {
    const user = userEvent.setup();

    function Harness() {
      const [active, setActive] = useState("one");
      return (
        <Tabs
          ariaLabel="配置区"
          activeId={active}
          onChange={setActive}
          tabs={[
            { id: "one", label: "基础", content: "基础内容" },
            { id: "two", label: "高级", content: "高级内容" },
          ]}
        />
      );
    }

    render(<Harness />);
    const first = screen.getByRole("tab", { name: "基础" });
    first.focus();
    await user.keyboard("{ArrowRight}");
    const second = screen.getByRole("tab", { name: "高级" });
    expect(second).toHaveFocus();
    expect(second).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tabpanel")).toHaveTextContent("高级内容");
  });

  it("shows impact and prevents duplicate dangerous actions while pending", async () => {
    const user = userEvent.setup();
    let finish: (() => void) | undefined;
    const onConfirm = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          finish = resolve;
        }),
    );
    render(
      <DangerAction label="删除记录" title="删除成员记录" impact="该成员的历史记录会被永久移除。" onConfirm={onConfirm} />,
    );

    await user.click(screen.getByRole("button", { name: "删除记录" }));
    expect(screen.getByText("该成员的历史记录会被永久移除。")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "确认执行" }));
    const pending = screen.getByRole("button", { name: "正在执行…" });
    expect(pending).toBeDisabled();
    expect(onConfirm).toHaveBeenCalledTimes(1);
    finish?.();
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("blocks same-origin links while changes are unsaved", () => {
    const confirmDiscard = vi.fn(() => false);
    render(
      <>
        <UnsavedChangesGuard when confirmDiscard={confirmDiscard} />
        <a href="/next-page">下一页</a>
      </>,
    );

    const link = screen.getByRole("link", { name: "下一页" });
    const allowed = fireEvent.click(link);
    expect(allowed).toBe(false);
    expect(confirmDiscard).toHaveBeenCalledTimes(1);
  });

  it("uses an accessible dialog when no synchronous discard callback is supplied", async () => {
    const user = userEvent.setup();
    render(
      <>
        <UnsavedChangesGuard when />
        <a href="/next-page">下一页</a>
      </>,
    );

    await user.click(screen.getByRole("link", { name: "下一页" }));

    expect(screen.getByRole("dialog", { name: "放弃未保存的修改？" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "继续编辑" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "放弃修改并离开" })).toBeInTheDocument();
  });

  it("announces route changes and focuses the main landmark", async () => {
    render(
      <>
        <main id="main-content">页面内容</main>
        <RouteAnnouncer label="群运营页面已加载" documentTitle="群运营 · Agent Console" />
      </>,
    );

    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("群运营页面已加载"));
    expect(document.getElementById("main-content")).toHaveFocus();
    expect(document.title).toBe("群运营 · Agent Console");
  });
});
