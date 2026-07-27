type TechnicalDetailsProps = {
  value: unknown;
  summary?: string;
  label?: string;
};

function technicalText(value: unknown) {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

export function TechnicalDetails({
  value,
  summary = "查看技术详情",
  label = "完整技术详情",
}: TechnicalDetailsProps) {
  return (
    <details className="route-list">
      <summary>{summary}</summary>
      <pre className="code-view" aria-label={label}>
        <code>{technicalText(value)}</code>
      </pre>
    </details>
  );
}

export function friendlyErrorMessage(error: unknown, fallback: string) {
  const value = String(error || "").trim();
  if (!value) return fallback;
  if (/[\u3400-\u9fff]/.test(value)) return value;
  const normalized = value.toLowerCase();
  if (
    normalized.includes("network")
    || normalized.includes("failed to fetch")
    || normalized.includes("temporary")
    || normalized.includes("connection")
  ) {
    return "网络请求未完成，请检查连接后重试；尚未提交的内容会继续保留。";
  }
  if (normalized.includes("timeout") || normalized.includes("timed out")) {
    return "请求等待超时，请稍后重试；尚未提交的内容会继续保留。";
  }
  if (normalized.includes("401") || normalized.includes("unauthorized")) {
    return "登录状态已失效，请重新登录后继续。";
  }
  if (normalized.includes("403") || normalized.includes("forbidden")) {
    return "当前账号没有执行此操作的权限。";
  }
  if (normalized.includes("409") || normalized.includes("conflict")) {
    return "服务器已有更新，当前草稿已保留，请重新读取后核对。";
  }
  return fallback;
}
