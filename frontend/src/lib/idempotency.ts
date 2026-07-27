import { useCallback, useRef } from "react";

function createKey() {
  const randomId =
    typeof globalThis.crypto?.randomUUID === "function"
      ? globalThis.crypto.randomUUID()
      : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
  return `agent-console:${randomId}`;
}

/**
 * Keep one idempotency key for the lifetime of a user intent.
 *
 * A failed request reuses the same key when the confirmation dialog retries.
 * Call ``clear`` only after the mutation and its local refresh both succeed so
 * a later, deliberate operation receives a new key.
 */
export function useStableIdempotencyKeys() {
  const keysRef = useRef(new Map<string, string>());

  const keyFor = useCallback((intent: string) => {
    const existing = keysRef.current.get(intent);
    if (existing) {
      return existing;
    }
    const key = createKey();
    keysRef.current.set(intent, key);
    return key;
  }, []);

  const clear = useCallback((intent: string) => {
    keysRef.current.delete(intent);
  }, []);

  return { keyFor, clear };
}
