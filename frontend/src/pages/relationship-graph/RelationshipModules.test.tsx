import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { GroupGraphEdge, GroupGraphNode } from "../../lib/api";
import {
  RelationshipMutationActions,
  RelationshipUnavailableReset,
  type RelationshipMutationController,
} from "./RelationshipDetailsAndDanger";
import {
  RelationshipGraphPresentation,
  type RelationshipGraphPresentationProps,
} from "./RelationshipGraphPresentation";

describe("relationship graph modules", () => {
  it("keeps the unsupported destructive reset visibly unavailable", () => {
    render(<RelationshipUnavailableReset />);

    const reset = screen.getByRole("button", { name: "清空/重置不可用" });
    expect(reset).toBeDisabled();
    expect(reset).toHaveAttribute(
      "title",
      "后端没有安全的关系图清理端点；避免误删生产数据。",
    );
  });

  it("confirms a scoped daily extraction before invoking the controller", async () => {
    const user = userEvent.setup();
    const runDailyExtraction = vi.fn().mockResolvedValue(undefined);
    const controller = {
      syncing: false,
      missingHistorySyncFields: [],
      selectedGroupId: "room@chatroom",
      targetDate: "2026-07-18",
      enqueueLlmJobs: true,
      runHistorySync: vi.fn(),
      extracting: false,
      extractionMaxJobCount: 50,
      runDailyExtraction,
      windowExtractionCursor: 0,
      windowExtractionMaxWindowsValue: 1,
      windowExtractionDryRun: false,
      runWindowExtraction: vi.fn(),
      windowCatchupMaxWindowsValue: 20,
      runWindowCatchup: vi.fn(),
      loadGraphAndStatus: vi.fn(),
      dateLoading: false,
      jobStatsLoading: false,
      loading: false,
    } satisfies RelationshipMutationController;

    render(<RelationshipMutationActions controller={controller} />);
    await user.click(screen.getByRole("button", { name: "运行所选日期 AI 抽取" }));

    const dialog = screen.getByRole("dialog", { name: "确认运行所选日期抽取" });
    expect(within(dialog).getByText(/当前已验证群聊/)).toBeInTheDocument();
    expect(within(dialog).getByText(/2026-07-18/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "确认运行" }));

    expect(runDailyExtraction).toHaveBeenCalledTimes(1);
  });

  it("uses the accessible lists as the sole graph selection controls", async () => {
    const user = userEvent.setup();
    const setSelection = vi.fn();
    const node: GroupGraphNode = {
      id: "person:member-a",
      type: "person",
      label: "成员甲",
      display_label: "成员甲",
      acceptance_status: "accepted",
      confidence: 0.9,
      evidence_count: 3,
    };
    const otherNode: GroupGraphNode = {
      id: "topic:project-a",
      type: "topic",
      label: "项目甲",
      display_label: "项目甲",
      acceptance_status: "accepted",
      confidence: 0.8,
      evidence_count: 2,
    };
    const edge: GroupGraphEdge = {
      id: "edge-1",
      from: node.id,
      to: otherNode.id,
      type: "participates_in",
      label: "participates_in",
      acceptance_status: "accepted",
      confidence: 0.85,
      evidence_count: 2,
    };
    const controller = {
      modeHiddenNodeCount: 0,
      modeHiddenEdgeCount: 0,
      graphViewMode: "readable",
      setGraphViewMode: vi.fn(),
      hiddenGraphNodeCount: 0,
      hiddenGraphEdgeCount: 0,
      graphNodes: [node, otherNode],
      graphSummaryText: "摘要视图：显示 2 个核心节点 / 1 条高信号关系。",
      loading: false,
      visibleGraphEdges: [edge],
      layout: new Map([
        [node.id, { x: 120, y: 120 }],
        [otherNode.id, { x: 320, y: 120 }],
      ]),
      selectedEdge: null,
      selectedNode: null,
      selection: null,
      setSelection,
      visibleLabels: new Map([
        [node.id, { nodeId: node.id, text: "成员甲", x: 120, y: 96, anchor: "middle" }],
        [otherNode.id, { nodeId: otherNode.id, text: "项目甲", x: 320, y: 96, anchor: "middle" }],
      ]),
      neighborNodeIds: new Set<string>(),
      graph: { nodes: [node, otherNode], edges: [edge] },
      graphStateMessage: "",
      graphEdges: [edge],
      nodesById: new Map([[node.id, node], [otherNode.id, otherNode]]),
      modeFilteredNodes: [node, otherNode],
    } satisfies RelationshipGraphPresentationProps;

    render(<RelationshipGraphPresentation {...controller} />);
    const nodeList = screen.getByRole("region", { name: "节点列表" });
    const edgeList = screen.getByRole("region", { name: "关系列表" });
    const nodeRow = within(nodeList).getByRole("button", { name: /成员甲/ });
    await user.click(nodeRow);

    expect(setSelection).toHaveBeenCalledWith({ kind: "node", item: node });
    expect(nodeRow).toHaveAttribute("aria-pressed", "false");
    await user.click(within(edgeList).getByRole("button"));
    expect(setSelection).toHaveBeenLastCalledWith({ kind: "edge", item: edge });
    expect(screen.getByText(/画布仅作视觉概览/)).toBeInTheDocument();

    const canvas = document.querySelector("svg.relationship-canvas");
    expect(canvas).toHaveAttribute("aria-hidden", "true");
    expect(canvas).toHaveAttribute("focusable", "false");
    expect(canvas).toHaveAttribute("pointer-events", "none");
    expect(canvas?.querySelectorAll("[tabindex], [role='button']")).toHaveLength(0);
    setSelection.mockClear();
    fireEvent.click(canvas?.querySelector(".relationship-node") as SVGElement);
    fireEvent.click(canvas?.querySelector(".relationship-edge") as SVGElement);
    expect(setSelection).not.toHaveBeenCalled();
    expect(screen.queryByRole("img", { name: "群聊关系图" })).not.toBeInTheDocument();
  });
});
