"""Cross-worker wakeup signals."""
import threading

ingest_wakeup = threading.Event()
send_wakeup = threading.Event()


def wait_or_timeout(event, timeout):
    fired = event.wait(timeout=timeout)
    if fired:
        event.clear()
    return fired
