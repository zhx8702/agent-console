"""Loader for the wechat_sender module.

Priority:
1. Compiled binary (wechat_sender.pyd / wechat_sender.so) — verified by hash
2. Python source fallback (sealed_core/wechat_sender.py)

In production, set require_compiled_sender=true in config to block
source fallback.
"""
from __future__ import annotations

import importlib
from pathlib import Path

_HERE = Path(__file__).parent


def _try_compiled():
    for suffix in (".pyd", ".so"):
        candidate = _HERE / f"wechat_sender{suffix}"
        if candidate.exists():
            from sealed_core.binary_manifest import verify_binary
            ok, reason = verify_binary("sender", candidate)
            if not ok:
                raise RuntimeError(f"sender binary verification failed: {reason}")
            spec = importlib.util.spec_from_file_location(
                "sealed_core._sender_compiled", str(candidate),
            )
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                print(f"[loader] sender: binary verified ({reason})")
                return mod
    return None


_mod = _try_compiled()

if _mod is None:
    import config as _cfg
    _raw = getattr(_cfg, "_raw", {}) or {}
    if _raw.get("require_compiled_sender", False):
        raise RuntimeError(
            "require_compiled_sender is true but no compiled sender binary found "
            "in sealed_core/. Cannot fall back to source."
        )
    from sealed_core import wechat_sender as _mod  # type: ignore[assignment]
    print("[loader] sender: using Python source (dev mode)")
else:
    print("[loader] sender: using compiled binary")


send = _mod.send
send_image = _mod.send_image
send_batch = _mod.send_batch
