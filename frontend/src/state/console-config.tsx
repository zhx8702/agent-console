import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type PropsWithChildren,
} from "react";

export type ConsoleConfig = {
  apiBaseUrl: string;
  adminToken: string;
  tenantId: string;
  sessionId: string;
  userId: string;
};

type ConsoleConfigContextValue = {
  config: ConsoleConfig;
  updateConfig: (patch: Partial<ConsoleConfig>) => void;
  verifiedGroupIds: ReadonlySet<string>;
  registerVerifiedGroups: (groupIds: readonly string[]) => void;
  selectVerifiedGroup: (groupId: string) => void;
  clearSelectedGroup: () => void;
};

const STORAGE_KEY = "agent-console-frontend-config";
export const AUTH_INVALID_EVENT = "agent-console-auth-invalid";
export const COOKIE_SESSION_MARKER = "__agent_console_cookie_session__";

function defaultApiBaseUrl() {
  return window.location.origin;
}

const defaultConfig: ConsoleConfig = {
  apiBaseUrl: defaultApiBaseUrl(),
  adminToken: "",
  tenantId: "",
  sessionId: "",
  userId: "",
};

export class GroupSelectionRequiredError extends Error {
  reason: "missing" | "unverified";

  constructor(reason: "missing" | "unverified") {
    super(
      reason === "missing"
        ? "请先从已同步的群聊列表中选择目标群"
        : "当前目标群未通过后端会话列表验证，请刷新后重新选择",
    );
    this.name = "GroupSelectionRequiredError";
    this.reason = reason;
  }
}

/**
 * Guard every group-scoped mutation with the authenticated roster snapshot.
 * Returning the normalized id makes it convenient to use directly in a URL or
 * request body; callers get a user-facing error instead of silently falling
 * back to a global/default scope.
 */
export function requireSelectedGroup(
  config: Pick<ConsoleConfig, "sessionId">,
  verifiedGroupIds: ReadonlySet<string>,
) {
  const groupId = config.sessionId.trim();
  if (!groupId) {
    throw new GroupSelectionRequiredError("missing");
  }
  if (!verifiedGroupIds.has(groupId)) {
    throw new GroupSelectionRequiredError("unverified");
  }
  return groupId;
}

const ConsoleConfigContext = createContext<ConsoleConfigContextValue | null>(null);

export function ConsoleConfigProvider({ children }: PropsWithChildren) {
  const [config, setConfig] = useState<ConsoleConfig>(() => {
    // Authentication identity is always re-established by /auth/me.  Purge
    // legacy state instead of letting a browser-controlled tenant/user/session
    // survive a reload and influence the first scoped request.
    window.localStorage.removeItem(STORAGE_KEY);
    return defaultConfig;
  });
  const [verifiedGroupIds, setVerifiedGroupIds] = useState<ReadonlySet<string>>(
    () => new Set(),
  );

  useEffect(() => {
    const clearInvalidToken = () => {
      setConfig((current) =>
        current.adminToken ? { ...current, adminToken: "" } : current,
      );
    };
    window.addEventListener(AUTH_INVALID_EVENT, clearInvalidToken);
    return () => window.removeEventListener(AUTH_INVALID_EVENT, clearInvalidToken);
  }, []);

  const updateConfig = useCallback((patch: Partial<ConsoleConfig>) => {
    setConfig((current) => ({ ...current, ...patch }));
  }, []);

  const registerVerifiedGroups = useCallback((groupIds: readonly string[]) => {
    const nextIds = new Set(groupIds.map((item) => item.trim()).filter(Boolean));
    setVerifiedGroupIds(nextIds);
    setConfig((current) =>
      current.sessionId && !nextIds.has(current.sessionId)
        ? { ...current, sessionId: "" }
        : current,
    );
  }, []);

  const selectVerifiedGroup = useCallback(
    (groupId: string) => {
      const normalized = groupId.trim();
      if (!normalized) {
        setConfig((current) => ({ ...current, sessionId: "" }));
        return;
      }
      if (!verifiedGroupIds.has(normalized)) {
        throw new GroupSelectionRequiredError("unverified");
      }
      setConfig((current) => ({ ...current, sessionId: normalized }));
    },
    [verifiedGroupIds],
  );

  const clearSelectedGroup = useCallback(() => {
    setConfig((current) =>
      current.sessionId ? { ...current, sessionId: "" } : current,
    );
  }, []);

  const contextValue = useMemo<ConsoleConfigContextValue>(
    () => ({
      config,
      updateConfig,
      verifiedGroupIds,
      registerVerifiedGroups,
      selectVerifiedGroup,
      clearSelectedGroup,
    }),
    [
      clearSelectedGroup,
      config,
      registerVerifiedGroups,
      selectVerifiedGroup,
      updateConfig,
      verifiedGroupIds,
    ],
  );

  return (
    <ConsoleConfigContext.Provider value={contextValue}>
      {children}
    </ConsoleConfigContext.Provider>
  );
}

export function useConsoleConfig() {
  const value = useContext(ConsoleConfigContext);
  if (!value) {
    throw new Error("useConsoleConfig must be used within ConsoleConfigProvider");
  }
  return value;
}
