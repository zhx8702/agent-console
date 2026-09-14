export function messageStoryPath(traceId: string) {
  return `/queues/traces/${encodeURIComponent(traceId.trim())}`;
}
