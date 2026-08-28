"""Host-side adapters for the two supported local CLIs."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


BACKENDS = ("grok", "codex")


@dataclass(frozen=True)
class BackendProbe:
    name: str
    ok: bool
    executable: str = ""
    version: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "ok": self.ok,
            "executable": self.executable,
            "version": self.version,
            "error": self.error,
        }


def resolve_executable(name: str) -> str:
    candidates = (name,)
    if os.name == "nt":
        candidates = (f"{name}.cmd", f"{name}.exe", name)
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return found
    return ""


def probe_backend(name: str, *, timeout_seconds: float = 5.0) -> BackendProbe:
    if name not in BACKENDS:
        return BackendProbe(name=name, ok=False, error="unknown_backend")
    executable = resolve_executable(name)
    if not executable:
        return BackendProbe(name=name, ok=False, error="executable_not_found")
    try:
        completed = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1.0, float(timeout_seconds)),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return BackendProbe(
            name=name,
            ok=False,
            executable=executable,
            error=str(exc)[:300],
        )
    version = (completed.stdout or completed.stderr or "").strip().splitlines()
    version_text = version[0].strip() if version else ""
    if completed.returncode != 0:
        return BackendProbe(
            name=name,
            ok=False,
            executable=executable,
            version=version_text,
            error=(completed.stderr or completed.stdout or "version_failed").strip()[:300],
        )
    return BackendProbe(name=name, ok=True, executable=executable, version=version_text)


def probe_all(*, timeout_seconds: float = 5.0) -> dict[str, BackendProbe]:
    return {name: probe_backend(name, timeout_seconds=timeout_seconds) for name in BACKENDS}


def _grok_command(executable: str, prompt_path: str, *, max_turns: int = 8) -> list[str]:
    turns = max(1, min(int(max_turns or 8), 80))
    return [
        executable,
        "--prompt-file",
        prompt_path,
        "--output-format",
        "plain",
        "--always-approve",
        "--no-auto-update",
        "--max-turns",
        str(turns),
    ]


def _codex_command(executable: str, prompt_path: str) -> list[str]:
    _ = prompt_path
    return [
        executable,
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--ephemeral",
        "-s",
        "read-only",
        "-c",
        "approval_policy=never",
        "--disable",
        "memories",
    ]


def parse_codex_output(stdout: str) -> str:
    final_text = ""
    for line in str(stdout or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "item.completed":
            continue
        item = event.get("item") or {}
        if item.get("type") == "agent_message":
            final_text = str(item.get("text") or final_text)
    if final_text.strip():
        return final_text.strip()
    return str(stdout or "").strip()


def parse_grok_output(stdout: str) -> str:
    return str(stdout or "").strip()


_COMMAND_BUILDERS: dict[str, Callable[[str, str], list[str]]] = {
    "grok": _grok_command,
    "codex": _codex_command,
}
_OUTPUT_PARSERS: dict[str, Callable[[str], str]] = {
    "grok": parse_grok_output,
    "codex": parse_codex_output,
}


def run_backend(
    name: str,
    prompt: str,
    *,
    cwd: str,
    timeout_seconds: float,
    max_turns: int | None = None,
) -> str:
    if name not in BACKENDS:
        raise RuntimeError("unknown_backend")
    executable = resolve_executable(name)
    if not executable:
        raise RuntimeError("executable_not_found")
    prompt_file = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        delete=False,
        suffix=".txt",
        prefix=f"{name}-prompt-",
    )
    try:
        prompt_file.write(str(prompt or ""))
        prompt_file.close()
        if name == "grok":
            command = _grok_command(
                executable,
                prompt_file.name,
                max_turns=8 if max_turns is None else max_turns,
            )
        else:
            command = _COMMAND_BUILDERS[name](executable, prompt_file.name)
        completed = subprocess.run(
            command,
            input=str(prompt or "") if name == "codex" else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd or None,
            timeout=max(1.0, float(timeout_seconds)),
            check=False,
        )
    finally:
        Path(prompt_file.name).unlink(missing_ok=True)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "cli_failed").strip()
        raise RuntimeError(detail[:2000] or "cli_failed")
    return _OUTPUT_PARSERS[name](completed.stdout)
