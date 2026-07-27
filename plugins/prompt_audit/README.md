# Prompt Audit core

This directory is a standalone, provider-neutral prompt-audit engine. It is
deliberately **not connected** to the application plugin registry or message
pipeline yet: there is no `plugin.py`, API router, flow step, hook, database
migration, or background worker.

The public embedding surface is:

- `AuditRequest` / `AuditDecision` for stable input and output contracts.
- `PromptAuditConfig` / `ConfigSnapshot` for immutable, versioned config.
- `Qwen3GuardScanner` for OpenAI-compatible Guard endpoints.
- `PromptAuditService.evaluate()` for `off`, `observe`, and `blocking` modes.
- `PromptAuditComponent` for explicit lifecycle ownership. Injected scanners
  remain caller-owned unless `close_scanner=True` is selected explicitly.

Future application integration should be an adapter layer. It must provide a
durable observe queue, event sink, encrypted endpoint secrets, configuration
reload, and an inbound flow gate after preprocessing but before session writes.
It must not add those concerns to this core package.

Queue and event ports receive only a configuration version, never endpoint
credentials. The observe queue is the sole port that receives raw request text;
an adapter must place it in an explicitly bounded sensitive-payload store.
