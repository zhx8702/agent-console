import { useEffect, useMemo, useState } from "react";

import { apiRequest } from "../lib/api";
import { useConsoleConfig } from "../state/console-config";
import { SearchableSelect, type SearchableSelectOption } from "./SearchableSelect";

export type VerifiedGroupSession = {
  session_id: string;
  session_name: string;
  kind?: string;
};

function isGroupSession(session: Pick<VerifiedGroupSession, "session_id" | "kind">) {
  return (
    session.session_id.endsWith("@chatroom") ||
    session.kind === "group" ||
    session.kind === "chatroom"
  );
}

function sessionDisplayName(session: VerifiedGroupSession) {
  return session.session_name?.trim() || session.session_id;
}

function normalizeVerifiedGroups(items: VerifiedGroupSession[]) {
  const groups = new Map<string, VerifiedGroupSession>();
  items.forEach((item) => {
    const sessionId = item?.session_id?.trim();
    if (!sessionId || !isGroupSession(item)) {
      return;
    }
    groups.set(sessionId, { ...item, session_id: sessionId, kind: "group" });
  });
  return Array.from(groups.values()).sort((left, right) =>
    sessionDisplayName(left).localeCompare(sessionDisplayName(right), "zh-CN"),
  );
}

/**
 * Group-scoped pages mount this selector; global/system pages do not. Options
 * come exclusively from the authenticated wxbot group roster, so typed search
 * text can filter a group but can never become a session id by itself.
 */
export function GlobalSessionBar() {
  const {
    config,
    registerVerifiedGroups,
    selectVerifiedGroup,
    clearSelectedGroup,
  } = useConsoleConfig();
  const [groups, setGroups] = useState<VerifiedGroupSession[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let cancelled = false;

    const loadGroups = async () => {
      setLoading(true);
      setError("");
      try {
        const payload = await apiRequest<{ sessions?: VerifiedGroupSession[] }>(
          config,
          "/plugins/wxbot/admin/roster/groups",
          { auth: true },
        );
        if (cancelled) {
          return;
        }
        const nextGroups = normalizeVerifiedGroups(payload.sessions || []);
        setGroups(nextGroups);
        registerVerifiedGroups(nextGroups.map((item) => item.session_id));
        if (!nextGroups.length) {
          setError("当前租户还没有已同步的群聊");
        }
      } catch (caught) {
        if (cancelled) {
          return;
        }
        setGroups([]);
        registerVerifiedGroups([]);
        setError(caught instanceof Error ? caught.message : "群聊列表加载失败");
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    };

    void loadGroups();
    return () => {
      cancelled = true;
    };
  }, [config.apiBaseUrl, config.tenantId, registerVerifiedGroups, reloadKey]);

  const options = useMemo<SearchableSelectOption[]>(
    () =>
      groups.map((item) => {
        const displayName = sessionDisplayName(item);
        return {
          value: item.session_id,
          label: displayName,
          keywords: [displayName, item.session_id, "群聊", "chatroom"],
        };
      }),
    [groups],
  );

  const selectedGroup = useMemo(
    () => groups.find((item) => item.session_id === config.sessionId) || null,
    [config.sessionId, groups],
  );

  const statusText = error || (loading ? "正在验证可操作群聊…" : `已验证 ${groups.length} 个群聊`);

  return (
    <section className="global-session-bar" aria-label="当前操作群聊">
      <div className="global-session-summary">
        <div className="global-session-kicker">当前操作群聊</div>
        <div className="global-session-title-row">
          <strong>{selectedGroup ? sessionDisplayName(selectedGroup) : "尚未选择群聊"}</strong>
          <span className={`global-session-kind${selectedGroup ? " is-group" : " is-global"}`}>
            {selectedGroup ? "已验证" : "需要选择"}
          </span>
        </div>
        <div className="global-session-meta">
          <span className="mono">{selectedGroup?.session_id || "不会回退到全局或任意 sessionId"}</span>
          <span>群聊 {groups.length}</span>
        </div>
        <p
          className={`global-session-status${error ? " is-error" : ""}`}
          role="status"
          aria-live="polite"
        >
          {statusText}
        </p>
      </div>

      <div className="global-session-controls">
        <label className="field">
          <span>选择已同步群聊</span>
          <SearchableSelect
            value={config.sessionId}
            options={options}
            onChange={selectVerifiedGroup}
            placeholder="从后端群聊列表中选择"
            searchPlaceholder="按群名或群 ID 筛选"
            emptyText={loading ? "正在加载群聊…" : "暂无可选群聊"}
            noResultsText="没有匹配的已同步群聊"
            disabled={loading || !options.length}
          />
        </label>

        <div className="global-session-actions">
          <button
            type="button"
            className="button button-secondary"
            onClick={() => setReloadKey((current) => current + 1)}
            disabled={loading}
          >
            刷新群聊
          </button>
          <button
            type="button"
            className="button button-secondary"
            onClick={clearSelectedGroup}
            disabled={!config.sessionId}
          >
            清除选择
          </button>
        </div>
      </div>
    </section>
  );
}
