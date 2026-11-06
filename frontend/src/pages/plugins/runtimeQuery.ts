import { apiRequest } from "../../lib/api";
import type { ConsoleConfig } from "../../state/console-config";
import type { PluginRuntimeEnvelope } from "./models";

export const PLUGIN_RUNTIME_TIMEOUT_MS = 8000;

export async function queryPluginRuntime(config: ConsoleConfig, name: string) {
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const result = await Promise.race([
      apiRequest<PluginRuntimeEnvelope>(config, `/v1/admin/plugins/${encodeURIComponent(name)}/runtime`, {
        auth: true,
        init: { signal: controller.signal },
      }),
      new Promise<never>((_, reject) => {
        timer = setTimeout(() => {
          reject(new Error("运行状态查询超时"));
          controller.abort();
        }, PLUGIN_RUNTIME_TIMEOUT_MS);
      }),
    ]);
    if (result.runtime_query?.status === "error" || result.runtime_query?.status === "timeout") {
      throw new Error(result.runtime_query.status === "timeout" ? "运行状态查询超时" : "运行状态查询失败");
    }
    const facts = result.runtime_status ?? result.runtime;
    if (!facts || typeof facts !== "object" || Array.isArray(facts)) {
      throw new Error("运行状态响应无效");
    }
    return facts;
  } finally {
    clearTimeout(timer);
  }
}
