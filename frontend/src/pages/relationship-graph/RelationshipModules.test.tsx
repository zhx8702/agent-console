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
import {
  GRAPH_CANVAS_WIDTH,
  GRAPH_LANES,
  buildGraphLayout,
  edgeRecencyOpacity,
  edgeStrokeWidth,
  evidenceCountsLabel,
  evidenceObservedRange,
  evidenceSourceLabel,
  populatedGraphLanes,
  sanitizeEdgeEvidence,
  shouldKeepEdgeForMode,
} from "./graphModel";

function personNode(id: string, label: string): GroupGraphNode {
  return { id, type: "person", label, display_label: label, acceptance_status: "accepted", confidence: 1 };
}

function edgeBetween(id: string, source: string, target: string, type: string, evidence = 1): GroupGraphEdge {
  return { id, source, target, type, label: type, confidence: 0.9, acceptance_status: "accepted", evidence_count: evidence };
}

describe("relationship graph layout and encodings", () => {
  it("spreads people across the canvas instead of one vertical lane", () => {
    const people = Array.from({ length: 12 }, (_, index) => personNode(`p${index}`, `成员${index}`));
    const edges = [
      edgeBetween("e1", "p0", "p1", "replied_to", 40),
      edgeBetween("e2", "p0", "p2", "replied_to", 12),
      edgeBetween("e3", "p1", "p2", "addressed", 3),
      edgeBetween("e4", "p3", "p4", "replied_to", 2),
      edgeBetween("e5", "p5", "p6", "replied_to", 1),
    ];

    const layout = buildGraphLayout(people, edges);
    const points = people.map((node) => layout.get(node.id)!);

    expect(points.every((point) => Number.isFinite(point.x) && Number.isFinite(point.y))).toBe(true);
    const xs = points.map((point) => point.x);
    expect(Math.max(...xs) - Math.min(...xs)).toBeGreaterThan(GRAPH_CANVAS_WIDTH * 0.35);
    // Strongly connected pair ends up closer than an unconnected pair.
    const distance = (a: string, b: string) => Math.hypot(layout.get(a)!.x - layout.get(b)!.x, layout.get(a)!.y - layout.get(b)!.y);
    expect(distance("p0", "p1")).toBeLessThan(distance("p0", "p9"));
    // Deterministic: the same input always yields the same picture.
    expect(buildGraphLayout(people, edges).get("p7")).toEqual(layout.get("p7"));
  });

  it("keeps topics in their own lane only when topic nodes exist", () => {
    const people = [personNode("p0", "洋白"), personNode("p1", "小海")];
    const topic: GroupGraphNode = { id: "t1", type: "topic", label: "并发", display_label: "并发", acceptance_status: "accepted", confidence: 0.8 };
    const edges = [edgeBetween("e1", "p0", "p1", "replied_to", 5), edgeBetween("e2", "p0", "t1", "mentioned", 2)];

    expect(populatedGraphLanes(people)).toEqual(new Set());
    expect(populatedGraphLanes([...people, topic])).toEqual(new Set(["topic"]));
    const layout = buildGraphLayout([...people, topic], edges);
    expect(layout.get("t1")!.x).toBeGreaterThan(GRAPH_LANES.topic.x - 60);
    expect(layout.get("p0")!.x).toBeLessThan(560);
  });

  it("folds same-window co-participation out of the default view only", () => {
    const nodes = new Map([["p0", personNode("p0", "洋白")], ["p1", personNode("p1", "小海")]]);
    const coParticipated = edgeBetween("e1", "p0", "p1", "co_participated");
    const reply = edgeBetween("e2", "p0", "p1", "replied_to");

    expect(shouldKeepEdgeForMode(coParticipated, nodes, "readable", new Set())).toBe(false);
    expect(shouldKeepEdgeForMode(reply, nodes, "readable", new Set())).toBe(true);
    expect(shouldKeepEdgeForMode(coParticipated, nodes, "people", new Set())).toBe(true);
    expect(shouldKeepEdgeForMode(coParticipated, nodes, "all", new Set())).toBe(true);
    // Selecting a node brings its co-participation edges back.
    expect(shouldKeepEdgeForMode(coParticipated, nodes, "readable", new Set(["p0"]))).toBe(true);
  });

  it("scales stroke width by evidence on a log scale and opacity by the last evidence date", () => {
    const widths = [1, 4, 20, 100].map((evidence) => edgeStrokeWidth(edgeBetween("e", "a", "b", "replied_to", evidence)));
    expect(widths[0]).toBeLessThan(widths[1]);
    expect(widths[1]).toBeLessThan(widths[2]);
    expect(widths[2]).toBeLessThan(widths[3]);
    expect(widths[3]).toBeLessThanOrEqual(5.5);

    const today = new Date().toISOString().slice(0, 10);
    const fresh = { ...edgeBetween("e", "a", "b", "replied_to"), last_seen: "2026-01-01T00:00:00", last_seen_date: today };
    const stale = { ...edgeBetween("e", "a", "b", "replied_to"), last_seen: "2026-01-01T00:00:00" };
    expect(edgeRecencyOpacity(fresh)).toBeGreaterThan(edgeRecencyOpacity(stale));
  });
});

describe("relationship graph evidence labels", () => {
  it("adds live group-message evidence to the counts label without exposing text", () => {
    const evidence = sanitizeEdgeEvidence({
      edge: {
        id: "fact:1",
        type: "replied_to",
        first_observed_at: "2026-08-09T01:02:03",
        last_observed_at: "2026-09-04T04:05:06",
      },
      evidence_counts: { memory_items: 1, events: 0, episodes: 0, observations: 12, evidence_days: 2 },
      evidence_source: "observation",
      observations: [
        { id: 154604, sender_label: "小海", occurred_at: "2026-08-09T01:02:03", content: "private text" },
      ],
    });

    expect(evidenceCountsLabel(evidence)).toBe("记忆项 1 / 事件 0 / 片段 0 / 群消息 12");
    expect(evidenceSourceLabel(evidence.evidence_source)).toBe("实时群消息");
    expect(evidenceObservedRange(evidence)).toContain("→");
    expect(JSON.stringify(evidence)).not.toContain("private text");
  });

  it("keeps the legacy label when no observation evidence exists", () => {
    expect(evidenceCountsLabel({ evidence_counts: { memory_items: 2, events: 1, episodes: 1 } })).toBe(
      "记忆项 2 / 事件 1 / 片段 1",
    );
    expect(evidenceSourceLabel("memory_event")).toBe("导入事件");
    expect(evidenceSourceLabel(undefined)).toBe("");
    expect(evidenceObservedRange(null)).toBe("");
  });
});

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
      anonymousGraphLabels: new Map<string, string>(),
      nodesById: new Map([[node.id, node], [otherNode.id, otherNode]]),
      modeFilteredNodes: [node, otherNode],
      graphRangeDays: 7,
      applyGraphRangeDays,
      playbackDate: "",
      applyPlaybackDate: vi.fn(),
      playbackDates: ["2026-08-25", "2026-08-26", "2026-08-27"],
      pendingEdges: [pendingEdge],
      pendingTotal: 637,
      pendingReviewError: "",
      pendingReviewLoading: false,
      reviewing: false,
      reviewEdge,
    } satisfies RelationshipGraphPresentationProps;

    const { rerender } = render(<RelationshipGraphPresentation {...controller} />);
    const nodeList = screen.getByRole("region", { name: "这些人" });
    const edgeList = screen.getByRole("region", { name: "这些互动" });
    const queue = screen.getByRole("region", { name: "待审核队列" });
    expect(within(queue).getByText(/相互印证.*自动通过/)).toBeInTheDocument();
    // The list is capped; the badge tells how many are really waiting.
    expect(within(queue).getByText("1 / 637")).toBeInTheDocument();
    const nodeRow = within(nodeList).getByRole("button", { name: /成员甲/ });
    await user.click(nodeRow);

    expect(setSelection).toHaveBeenCalledWith({ kind: "node", item: node });
    expect(nodeRow).toHaveAttribute("aria-pressed", "false");
    await user.click(within(edgeList).getByRole("button"));
    expect(setSelection).toHaveBeenLastCalledWith({ kind: "edge", item: edge });
    expect(screen.getByText(/可在画布上点选/)).toBeInTheDocument();
    expect(within(queue).getByRole("button", { name: /成员甲 回复 项目甲/ })).toBeInTheDocument();

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

    rerender(
      <RelationshipGraphPresentation
        {...controller}
        pendingEdges={[]}
        pendingReviewError="待审核关系加载失败：401 admin session required"
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("待审核关系加载失败");
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
      anonymousGraphLabels: new Map<string, string>(),
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
