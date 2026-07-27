"""Dispatch outbound messages from local queue via wechat_sender.

Reads pending send requests from queue_store, dispatches through
the WeChat UI automation driver, and marks results.
"""

import events
import queue_store as qs

import config
from sealed_core import ingest_loader as ingest
from sealed_core import sender_loader as wechat_sender
from sealed_core.runtime import require_capability


def tick():
    require_capability("dispatch_messages")
    identity = ingest.identity_status(refresh=True)
    if identity.get("ready") is not True:
        print(
            "[send] self identity unavailable; outbound dispatch remains disabled "
            f"(reason={identity.get('reason') or 'self_identity_unavailable'})"
        )
        return None
    recovered = qs.recover_stale_outbound(stale_seconds=120)
    if recovered:
        print(f"[send] quarantined {recovered} interrupted tasks for reconciliation")
    tasks = qs.claim_outbound_pending(limit=50)
    if not tasks:
        return None

    print(f"[send] got {len(tasks)} tasks from local queue")

    results = wechat_sender.send_batch(tasks)
    for tid, ok, err in results:
        task = next((item for item in tasks if int(item["id"]) == int(tid)), {})
        claim_token = str(task.get("claim_token") or "")
        if ok:
            qs.mark_outbound_sent(tid, claim_token=claim_token)
            print(f"  [ok] id={tid}")
        else:
            qs.mark_outbound_uncertain(tid, str(err), claim_token=claim_token)
            print(f"  [uncertain] id={tid}: {err}")

    return None


def run_forever():
    print(f"[send] dispatching from local queue every {config.SEND_INTERVAL}s")
    while True:
        try:
            tick()
        except Exception as e:
            print(f"[send] error: {e}")
        events.wait_or_timeout(events.send_wakeup, config.SEND_INTERVAL)
