"""wx-bot SDK entry point.

1. Authenticates with remote auth server (Ed25519 signed sessions)
2. Installs RuntimeAuthGuard into sealed_core.runtime
3. Initializes local SQLite message queue
4. Starts ingest + send worker threads
5. Starts Flask HTTP API server for external consumers
"""
import sys
import threading
from urllib.parse import urlsplit

import queue_store as qs
from client.runtime_guard import AuthorizationError, RuntimeAuthGuard
from sealed_core import runtime

import config


def _auth_server_status(raw_url: object) -> str:
    """Return a startup-safe auth origin summary without echoing credentials."""

    value = str(raw_url or "").strip()
    if not value:
        return "configured=no, validated_host=none"
    try:
        parsed = urlsplit(value)
        hostname = str(parsed.hostname or "").strip().lower().rstrip(".")
        normalized_host = hostname.encode("idna").decode("ascii") if hostname else ""
        _validated_port = parsed.port  # validate without exposing it
        valid = (
            parsed.scheme.lower() == "https"
            and bool(normalized_host)
            and parsed.username is None
            and parsed.password is None
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
        )
    except (UnicodeError, ValueError):
        valid = False
        normalized_host = ""
    host_status = normalized_host if valid else "invalid"
    return f"configured=yes, validated_host={host_status}"


def _run_ingest():
    from sealed_core import ingest_loader as ingest
    ingest.run_forever()


def _run_send():
    from sealed_core import send_loop
    send_loop.run_forever()


def _run_api():
    from api.server import run_server
    run_server(host=config.API_HOST, port=config.API_PORT)


def _identity_preflight():
    try:
        from sealed_core import ingest_loader as ingest

        identity = ingest.resolve_self_identity()
    except Exception:
        identity = {
            "ready": False,
            "self_wxid": "",
            "self_rowid": None,
            "reason": "self_identity_preflight_failed",
        }
    if not isinstance(identity, dict) or identity.get("ready") is not True:
        reason = (
            str(identity.get("reason") or "self_identity_unavailable")
            if isinstance(identity, dict)
            else "self_identity_unavailable"
        )
        return {
            "ready": False,
            "self_wxid": "",
            "self_rowid": None,
            "reason": reason[:64],
        }
    try:
        rowid = int(identity.get("self_rowid"))
    except (TypeError, ValueError):
        rowid = -1
    wxid = str(identity.get("self_wxid") or "").strip()
    if not wxid or rowid <= 0:
        return {
            "ready": False,
            "self_wxid": "",
            "self_rowid": None,
            "reason": "self_identity_invalid",
        }
    return {
        "ready": True,
        "self_wxid": wxid,
        "self_rowid": rowid,
        "reason": "",
    }


def main():
    print("=" * 60)
    print("  wx-bot SDK")
    print(f"  auth server:  {_auth_server_status(config.AUTH_BASE_URL)}")
    print(f"  api endpoint: http://{config.API_HOST}:{config.API_PORT}")
    print("  identity:     configured")
    print("=" * 60)

    # ── Initialize local queue ──
    qs.init(config.QUEUE_DB_PATH)
    print("[main] local queue initialized")

    identity = _identity_preflight()
    if identity["ready"] is not True:
        print(
            "[main] self identity unavailable; capture and proactive behavior "
            f"remain disabled (reason={identity['reason']})"
        )
    else:
        print("[main] self identity verified")

    # ── Authenticate with remote auth server ──
    print("[main] authenticating with remote auth server...")
    try:
        guard = RuntimeAuthGuard(config.auth_settings())
        guard.start()
        runtime.install_guard(guard)
    except AuthorizationError:
        print("[main] authorization failed")
        print("[main] cannot start without valid auth. Exiting.")
        sys.exit(1)
    except Exception:
        print("[main] auth error")
        sys.exit(1)

    print("[main] authorized — runtime capabilities activated")

    # ── Start worker threads ──
    workers = [
        threading.Thread(target=_run_ingest, daemon=True, name="ingest"),
        threading.Thread(target=_run_send, daemon=True, name="send"),
    ]
    for t in workers:
        t.start()
        print(f"[main] started {t.name} worker")

    # ── Start HTTP API (blocks main thread) ──
    print(f"[main] starting HTTP API on {config.API_HOST}:{config.API_PORT}")
    try:
        _run_api()
    except KeyboardInterrupt:
        print("\n[main] shutting down...")
        guard.stop()


if __name__ == "__main__":
    main()
