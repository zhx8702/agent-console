"""Loader for the ingest module.

Priority:
1. Compiled binary (ingest.pyd / ingest.so) in sealed_core/ — verified by hash
2. Python source fallback (sealed_core/ingest.py)

In production, set require_compiled_ingest=true in config to block
source fallback.
"""
from __future__ import annotations

import importlib
from pathlib import Path

_HERE = Path(__file__).parent


def _try_compiled():
    for suffix in (".pyd", ".so"):
        candidate = _HERE / f"ingest{suffix}"
        if candidate.exists():
            from sealed_core.binary_manifest import verify_binary
            ok, reason = verify_binary("ingest", candidate)
            if not ok:
                raise RuntimeError(f"ingest binary verification failed: {reason}")
            spec = importlib.util.spec_from_file_location(
                "sealed_core._ingest_compiled", str(candidate),
            )
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                print(f"[loader] ingest: binary verified ({reason})")
                return mod
    return None


_mod = _try_compiled()

if _mod is None:
    import config as _cfg
    _raw = getattr(_cfg, "_raw", {}) or {}
    if _raw.get("require_compiled_ingest", False):
        raise RuntimeError(
            "require_compiled_ingest is true but no compiled ingest binary found "
            "in sealed_core/. Cannot fall back to source."
        )
    from sealed_core import ingest as _mod  # type: ignore[assignment]
    print("[loader] ingest: using Python source (dev mode)")
else:
    print("[loader] ingest: using compiled binary")


scan_once = _mod.scan_once
run_forever = _mod.run_forever
build_session_mapping = _mod.build_session_mapping


def _normalized_identity(value, *, missing_reason):
    if not isinstance(value, dict):
        return {
            "ready": False,
            "self_wxid": "",
            "self_rowid": None,
            "reason": missing_reason,
            "checked_at": 0,
        }
    try:
        rowid = int(value.get("self_rowid"))
    except (TypeError, ValueError):
        rowid = -1
    wxid = str(value.get("self_wxid") or "").strip()
    ready = value.get("ready") is True and bool(wxid) and rowid > 0
    return {
        "ready": ready,
        "self_wxid": wxid if ready else "",
        "self_rowid": rowid if ready else None,
        "reason": "" if ready else str(
            value.get("reason") or missing_reason
        )[:64],
        "checked_at": int(value.get("checked_at") or 0),
    }


def resolve_self_identity():
    resolver = getattr(_mod, "resolve_self_identity", None)
    if not callable(resolver):
        return _normalized_identity(
            None,
            missing_reason="identity_contract_missing",
        )
    return _normalized_identity(
        resolver(),
        missing_reason="self_identity_unavailable",
    )


def identity_status(refresh=False):
    if refresh:
        return resolve_self_identity()
    loader = getattr(_mod, "identity_status", None)
    if not callable(loader):
        return _normalized_identity(
            None,
            missing_reason="identity_contract_missing",
        )
    return _normalized_identity(
        loader(),
        missing_reason="self_identity_unavailable",
    )
