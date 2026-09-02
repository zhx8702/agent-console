import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { GroupGraphEdge, GroupGraphNode } from "../../lib/api";
import {
  RelationshipDetailPanel,
  RelationshipMutationActions,
  RelationshipUnavailableReset,
  type RelationshipMutationController,
} from "./RelationshipDetailsAndDanger";
import type { RelationshipGraphController } from "./useRelationshipGraphController";
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
      runRecentCoverage: vi.fn(),
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

  it("lets the canvas and review queue select graph objects", async () => {
    const user = userEvent.setup();
    const setSelection = vi.fn();
    const reviewEdge = vi.fn().mockResolvedValue(undefined);
    const applyGraphRangeDays = vi.fn();
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
      extraction_method: "deterministic",
    };
    const pendingEdge: GroupGraphEdge = {
      id: "edge-pending",
      from: node.id,
      to: otherNode.id,
      type: "replied_to",
      label: "replied_to",
      acceptance_status: "needs_review",
      confidence: 0.61,
      evidence_count: 4,
      extraction_method: "llm_window",
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
      graphRangeDays: 7,
      applyGraphRangeDays,
      playbackDate: "",
      applyPlaybackDate: vi.fn(),
      playbackDates: ["2026-08-25", "2026-08-26", "2026-08-27"],
      pendingEdges: [pendingEdge],
      reviewing: false,
      reviewEdge,
    } satisfies RelationshipGraphPresentationProps;

    render(<RelationshipGraphPresentation {...controller} />);
    const nodeList = screen.getByRole("region", { name: "节点列表" });
    const edgeList = screen.getByRole("region", { name: "关系列表" });
    const queue = screen.getByRole("region", { name: "待审核队列" });
    const nodeRow = within(nodeList).getByRole("button", { name: /成员甲/ });
    await user.click(nodeRow);

    expect(setSelection).toHaveBeenCalledWith({ kind: "node", item: node });
    expect(nodeRow).toHaveAttribute("aria-pressed", "false");
    await user.click(within(edgeList).getByRole("button"));
    expect(setSelection).toHaveBeenLastCalledWith({ kind: "edge", item: edge });
    expect(screen.getByText(/可在画布上点选/)).toBeInTheDocument();
    expect(within(queue).getByRole("button", { name: /成员甲 — 回复 — 项目甲/ })).toBeInTheDocument();

    const canvas = screen.getByRole("img", { name: "群聊关系图" });
    expect(canvas).not.toHaveAttribute("pointer-events", "none");
    fireEvent.click(canvas.querySelector("[data-graph-item='node']") as SVGElement);
    expect(setSelection).toHaveBeenCalledWith({ kind: "node", item: node });
    fireEvent.click(canvas.querySelector("[data-graph-item='edge']") as SVGElement);
    expect(setSelection).toHaveBeenCalledWith({ kind: "edge", item: edge });

    await user.click(screen.getByRole("button", { name: "近14天" }));
    expect(applyGraphRangeDays).toHaveBeenCalledWith(14);
    expect(screen.getByRole("slider")).toBeInTheDocument();

    await user.click(within(queue).getByRole("button", { name: "接受" }));
    const dialog = screen.getByRole("dialog", { name: "确认接受该关系" });
    await user.click(within(dialog).getByRole("button", { name: "确认接受" }));
    expect(reviewEdge).toHaveBeenCalledWith("accept", pendingEdge);
  });

  it("confirms edge acceptance, expiry and return-to-review from the detail panel", async () => {
    const user = userEvent.setup();
    const reviewEdge = vi.fn().mockResolvedValue(undefined);
    const node: GroupGraphNode = {
      id: "person:member-a",
      type: "person",
      label: "成员甲",
      display_label: "成员甲",
    };
    const otherNode: GroupGraphNode = {
      id: "person:member-b",
      type: "person",
      label: "成员乙",
      display_label: "成员乙",
    };
    const edge: GroupGraphEdge = {
      id: "edge-review-1",
      from: node.id,
      to: otherNode.id,
      type: "replied_to",
      label: "replied_to",
      acceptance_status: "needs_review",
      confidence: 0.62,
      evidence_count: 2,
      extraction_method: "llm_window",
      acceptance_score: 0.62,
      acceptance_reason: "window_relation",
    };
    const controller = {
      selection: { kind: "edge", item: edge },
      selectedNode: null,
      selectedEdge: edge,
      nodesById: new Map([[node.id, node], [otherNode.id, otherNode]]),
      evidence: null,
      evidenceLoading: false,
      evidenceStatus: "选择一条关系后会自动加载证据来源。",
      loadEdgeEvidence: vi.fn(),
      reviewing: false,
      reviewEdge,
      graphEdges: [edge],
      pendingEdges: [],
    } as unknown as RelationshipGraphController;

    const { rerender } = render(<RelationshipDetailPanel {...controller} />);
    expect(screen.getByText("语义抽取")).toBeInTheDocument();
    expect(screen.getByText("验收分")).toBeInTheDocument();
    expect(screen.getByText("window_relation")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "接受关系" }));

    const dialog = screen.getByRole("dialog", { name: "确认接受该关系" });
    expect(within(dialog).getByText(/回复/)).toBeInTheDocument();
    expect(within(dialog).getByText(/待审核/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "确认接受" }));

    expect(reviewEdge).toHaveBeenCalledWith("accept");

    const acceptedEdge = { ...edge, acceptance_status: "accepted" };
    rerender(<RelationshipDetailPanel {...controller} selectedEdge={acceptedEdge} selection={{ kind: "edge", item: acceptedEdge }} />);
    await user.click(screen.getByRole("button", { name: "标记过期" }));
    await user.click(within(screen.getByRole("dialog", { name: "确认将该关系标记为过期" })).getByRole("button", { name: "确认过期" }));
    expect(reviewEdge).toHaveBeenCalledWith("expire");

    await user.click(screen.getByRole("button", { name: "退回待审" }));
    await user.click(within(screen.getByRole("dialog", { name: "确认退回待审核" })).getByRole("button", { name: "确认退回" }));
    expect(reviewEdge).toHaveBeenCalledWith("needs_review");
  });
});
