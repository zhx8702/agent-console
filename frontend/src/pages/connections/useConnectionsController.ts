import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";

import {
  createChannelConnection,
  deleteChannelConnection,
  disableChannelConnection,
  enableChannelConnection,
  getChannelAdapters,
  getChannelConnection,
  getChannelConnections,
  probeChannelConnection,
  updateChannelConnection,
  validateChannelConnection,
  type ChannelAdapter,
  type ChannelConnection,
  type ChannelConnectionActionResult,
  type ChannelConnectionWrite,
} from "../../lib/channel-connections";
import { VersionConflictError } from "../../lib/api";
import { useStableIdempotencyKeys } from "../../lib/idempotency";
import { useConsoleConfig } from "../../state/console-config";
import {
  filterConnections,
  type ConnectionFilterState,
} from "./model";

type CollectionStatus = "idle" | "loading" | "refreshing" | "ready" | "error";
type DetailStatus = "idle" | "loading" | "ready" | "error" | "conflict";

type AdapterState = {
  status: CollectionStatus;
  items: ChannelAdapter[];
  readOnly: boolean;
  error: string;
};

type ConnectionState = {
  status: CollectionStatus;
  items: ChannelConnection[];
  readOnly: boolean;
  error: string;
};

type DetailState = {
  status: DetailStatus;
  value: ChannelConnection | null;
  etag: string | null;
  error: string;
};

const EMPTY_ADAPTER_STATE: AdapterState = {
  status: "idle",
  items: [],
  readOnly: false,
  error: "",
};

const EMPTY_CONNECTION_STATE: ConnectionState = {
  status: "idle",
  items: [],
  readOnly: false,
  error: "",
};

const EMPTY_DETAIL_STATE: DetailState = {
  status: "idle",
  value: null,
  etag: null,
  error: "",
};

function errorMessage(caught: unknown, fallback: string) {
  return caught instanceof Error ? caught.message : fallback;
}

export function useConnectionsController() {
  const { config } = useConsoleConfig();
  const { keyFor, clear } = useStableIdempotencyKeys();
  const [searchParams, setSearchParams] = useSearchParams();
  const selectedConnectionId = searchParams.get("connection")?.trim() || "";
  const adapterQuery = searchParams.get("adapter")?.trim() || "";
  const [adapters, setAdapters] = useState<AdapterState>(EMPTY_ADAPTER_STATE);
  const [connections, setConnections] = useState<ConnectionState>(EMPTY_CONNECTION_STATE);
  const [detail, setDetail] = useState<DetailState>(EMPTY_DETAIL_STATE);
  const [adapterFilter, setAdapterFilter] = useState(adapterQuery);
  const [stateFilter, setStateFilter] = useState<ConnectionFilterState>("all");
  const [actionKey, setActionKey] = useState("");
  const [actionError, setActionError] = useState("");
  const [notice, setNotice] = useState("");
  const [actionResult, setActionResult] = useState<ChannelConnectionActionResult | null>(null);
  const [feedbackConnectionId, setFeedbackConnectionId] = useState("");
  const collectionRequestRef = useRef(0);
  const detailRequestRef = useRef(0);

  const selectConnection = useCallback((connectionId: string) => {
    const next = new URLSearchParams(searchParams);
    if (connectionId) next.set("connection", connectionId);
    else next.delete("connection");
    setSearchParams(next, { replace: true });
  }, [searchParams, setSearchParams]);

  const loadAdapters = useCallback(async (refresh = false) => {
    setAdapters((current) => ({
      ...current,
      status: refresh && current.items.length ? "refreshing" : "loading",
      error: "",
    }));
    try {
      const result = await getChannelAdapters(config);
      setAdapters({ status: "ready", items: result.items, readOnly: result.readOnly, error: "" });
      return result;
    } catch (caught) {
      setAdapters((current) => ({
        ...current,
        status: "error",
        error: errorMessage(caught, "消息平台目录读取失败"),
      }));
      throw caught;
    }
  }, [config]);

  const loadConnections = useCallback(async (refresh = false) => {
    const requestId = ++collectionRequestRef.current;
    setConnections((current) => ({
      ...current,
      status: refresh && current.items.length ? "refreshing" : "loading",
      error: "",
    }));
    try {
      const result = await getChannelConnections(config);
      if (requestId !== collectionRequestRef.current) return result;
      setConnections({ status: "ready", items: result.items, readOnly: result.readOnly, error: "" });
      return result;
    } catch (caught) {
      if (requestId === collectionRequestRef.current) {
        setConnections((current) => ({
          ...current,
          status: "error",
          error: errorMessage(caught, "连接列表读取失败"),
        }));
      }
      throw caught;
    }
  }, [config]);

  const refreshAll = useCallback(async () => {
    setActionError("");
    await Promise.allSettled([
      loadAdapters(adapters.status !== "idle"),
      loadConnections(connections.status !== "idle"),
    ]);
  }, [adapters.status, connections.status, loadAdapters, loadConnections]);

  const loadSelected = useCallback(async (connectionId = selectedConnectionId) => {
    if (!connectionId) {
      detailRequestRef.current += 1;
      setDetail(EMPTY_DETAIL_STATE);
      return null;
    }
    const requestId = ++detailRequestRef.current;
    const listValue = connections.items.find((item) => item.id === connectionId) || null;
    setDetail((current) => ({
      status: "loading",
      value: current.value?.id === connectionId ? current.value : listValue,
      etag: current.value?.id === connectionId ? current.etag : null,
      error: "",
    }));
    try {
      const result = await getChannelConnection(config, connectionId);
      if (requestId !== detailRequestRef.current) return result;
      setDetail({ status: "ready", value: result.value, etag: result.etag, error: "" });
      return result;
    } catch (caught) {
      if (requestId === detailRequestRef.current) {
        setDetail({
          status: "error",
          value: listValue,
          etag: null,
          error: errorMessage(caught, "连接详情读取失败"),
        });
      }
      throw caught;
    }
  }, [config, connections.items, selectedConnectionId]);

  useEffect(() => {
    if (!config.adminToken) return;
    void Promise.allSettled([loadAdapters(), loadConnections()]);
  }, [config.adminToken, config.apiBaseUrl, config.tenantId]);

  useEffect(() => {
    if (adapterQuery) setAdapterFilter(adapterQuery);
  }, [adapterQuery]);

  useEffect(() => {
    if (connections.status !== "ready" && connections.status !== "error") return;
    if (!connections.items.length) {
      if (selectedConnectionId) selectConnection("");
      return;
    }
    const selectionExists = connections.items.some((item) => item.id === selectedConnectionId);
    if (!selectionExists) selectConnection(connections.items[0].id);
  }, [connections.items, connections.status, selectConnection, selectedConnectionId]);

  useEffect(() => {
    if (!selectedConnectionId) {
      setDetail(EMPTY_DETAIL_STATE);
      return;
    }
    void loadSelected(selectedConnectionId).catch(() => undefined);
  }, [config.tenantId, selectedConnectionId]);

  const runAction = useCallback(async (
    action: "probe" | "validate" | "enable" | "disable",
    connectionId: string,
  ) => {
    const intent = `channel-connection:${action}:${config.tenantId}:${connectionId}`;
    setFeedbackConnectionId(connectionId);
    setActionKey(`${action}:${connectionId}`);
    setActionError("");
    setNotice("");
    setActionResult(null);
    try {
      const etag = detail.value?.id === connectionId ? detail.etag : null;
      if (!etag) {
        throw new Error("连接版本尚未加载，请刷新详情后重试");
      }
      const runner = action === "probe"
        ? probeChannelConnection
        : action === "validate"
          ? validateChannelConnection
          : action === "enable"
            ? enableChannelConnection
            : disableChannelConnection;
      const result = await runner(config, connectionId, etag, keyFor(intent));
      setActionResult(result);
      setNotice(result.summary || "操作已完成");
      clear(intent);
      await Promise.allSettled([loadConnections(true), loadSelected(connectionId)]);
      return result;
    } catch (caught) {
      setActionError(errorMessage(caught, "连接操作失败"));
      throw caught;
    } finally {
      setActionKey("");
    }
  }, [clear, config, detail.etag, detail.value?.id, keyFor, loadConnections, loadSelected]);

  const saveConnection = useCallback(async (
    draft: ChannelConnectionWrite,
    editingId = "",
  ) => {
    const normalizedName = draft.displayName.trim();
    const intent = `channel-connection:${editingId ? "update" : "create"}:${config.tenantId}:${editingId || draft.adapterId}:${normalizedName}`;
    setActionKey(`${editingId ? "update" : "create"}:${editingId || "new"}`);
    setFeedbackConnectionId(editingId);
    setActionError("");
    setNotice("");
    try {
      const input = {
        ...draft,
        displayName: normalizedName,
        endpointUrl: draft.endpointUrl.trim(),
        secretRef: draft.secretRef.trim(),
      };
      const result = editingId
        ? await updateChannelConnection(
            config,
            editingId,
            input,
            detail.etag || "",
            keyFor(intent),
          )
        : await createChannelConnection(config, input, keyFor(intent));
      clear(intent);
      setNotice(editingId ? "连接配置已保存" : "连接草稿已创建");
      setFeedbackConnectionId(result.value.id || editingId);
      setDetail({ status: "ready", value: result.value, etag: result.etag, error: "" });
      await loadConnections(true).catch(() => undefined);
      selectConnection(result.value.id || editingId);
      return result.value;
    } catch (caught) {
      if (caught instanceof VersionConflictError) {
        setDetail((current) => ({ ...current, status: "conflict", error: caught.message }));
      }
      setActionError(errorMessage(caught, "连接保存失败"));
      throw caught;
    } finally {
      setActionKey("");
    }
  }, [clear, config, detail.etag, keyFor, loadConnections, selectConnection]);

  const removeConnection = useCallback(async (connectionId: string) => {
    const intent = `channel-connection:delete:${config.tenantId}:${connectionId}`;
    setFeedbackConnectionId(connectionId);
    setActionKey(`delete:${connectionId}`);
    setActionError("");
    try {
      await deleteChannelConnection(config, connectionId, detail.etag || "", keyFor(intent));
      clear(intent);
      setNotice("连接已删除");
      selectConnection("");
      setDetail(EMPTY_DETAIL_STATE);
      await loadConnections(true).catch(() => undefined);
    } catch (caught) {
      setActionError(errorMessage(caught, "连接删除失败"));
      throw caught;
    } finally {
      setActionKey("");
    }
  }, [clear, config, detail.etag, keyFor, loadConnections, selectConnection]);

  const filteredConnections = useMemo(
    () => filterConnections(connections.items, adapterFilter, stateFilter),
    [adapterFilter, connections.items, stateFilter],
  );
  const feedbackMatchesSelection = Boolean(feedbackConnectionId)
    && feedbackConnectionId === selectedConnectionId;

  return {
    actionError: feedbackMatchesSelection ? actionError : "",
    actionKey,
    actionResult: feedbackMatchesSelection ? actionResult : null,
    adapterQuery,
    adapterFilter,
    adapters,
    connections,
    detail,
    filteredConnections,
    loadAdapters,
    loadConnections,
    loadSelected,
    notice: feedbackMatchesSelection ? notice : "",
    refreshAll,
    removeConnection,
    runAction,
    saveConnection,
    selectConnection,
    selectedConnectionId,
    setActionError,
    setAdapterFilter,
    setNotice,
    setStateFilter,
    stateFilter,
  } as const;
}

export type ConnectionsController = ReturnType<typeof useConnectionsController>;
