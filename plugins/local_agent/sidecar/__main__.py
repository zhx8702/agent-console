"""Stdlib HTTP sidecar that probes and runs host grok / Codex CLIs."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    from plugins.local_agent.sidecar.backends import BACKENDS, probe_all, run_backend
except ImportError:  # pragma: no cover - host checkout run as a loose script
    from backends import BACKENDS, probe_all, run_backend


DEFAULT_HOST = os.environ.get("LOCAL_AGENT_SIDECAR_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("LOCAL_AGENT_SIDECAR_PORT", "8787"))
DEFAULT_TOKEN = os.environ.get("LOCAL_AGENT_TOKEN", "")
DEFAULT_CWD = os.environ.get(
    "LOCAL_AGENT_WORKSPACE",
    str(Path.home() / "agent-workspaces" / "default"),
)
DEFAULT_TIMEOUT = float(os.environ.get("LOCAL_AGENT_TASK_TIMEOUT_SECONDS", "600"))


class TaskStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._tasks: dict[str, dict[str, Any]] = {}

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        task_id = uuid.uuid4().hex
        now = time.time()
        record = {
            "id": task_id,
            "backend": payload["backend"],
            "prompt": payload["prompt"],
            "cwd": payload["cwd"],
            "timeout_seconds": payload["timeout_seconds"],
            "max_turns": payload.get("max_turns"),
            "status": "queued",
            "result_text": "",
            "error": "",
            "created_at": now,
            "updated_at": now,
        }
        with self._lock:
            self._tasks[task_id] = record
        worker = threading.Thread(target=self._run, args=(task_id,), daemon=True)
        worker.start()
        return self.get(task_id) or record

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._tasks.get(task_id)
            return dict(record) if record is not None else None

    def _update(self, task_id: str, **changes: Any) -> None:
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                return
            record.update(changes)
            record["updated_at"] = time.time()

    def _run(self, task_id: str) -> None:
        record = self.get(task_id)
        if record is None:
            return
        self._update(task_id, status="running")
        with self._run_lock:
            try:
                result = run_backend(
                    str(record["backend"]),
                    str(record["prompt"]),
                    cwd=str(record["cwd"]),
                    timeout_seconds=float(record["timeout_seconds"]),
                    max_turns=record.get("max_turns"),
                )
            except Exception as exc:
                self._update(task_id, status="failed", error=str(exc)[:2000])
                return
        self._update(task_id, status="succeeded", result_text=result, error="")


STORE = TaskStore()


def _json_bytes(payload: object, status: int = 200) -> tuple[int, bytes]:
    return status, json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _authorized(handler: BaseHTTPRequestHandler) -> bool:
    expected = str(DEFAULT_TOKEN or "").strip()
    if not expected:
        return True
    provided = str(handler.headers.get("Authorization") or "").strip()
    if provided == f"Bearer {expected}":
        return True
    return str(handler.headers.get("X-Local-Agent-Token") or "").strip() == expected


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or "0")
    if length <= 0 or length > 4_000_000:
        raise ValueError("invalid_body")
    raw = handler.rfile.read(length)
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("invalid_body")
    return payload


class Handler(BaseHTTPRequestHandler):
    server_version = "local-agent-sidecar/0.1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send(self, status: int, payload: object) -> None:
        body_status, body = _json_bytes(payload, status)
        self.send_response(body_status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if not _authorized(self):
            self._send(401, {"error": "unauthorized"})
            return
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/health":
            self._send(200, {"ok": True})
            return
        if path == "/v1/backends":
            backends = {
                name: probe.as_dict()
                for name, probe in probe_all().items()
            }
            self._send(200, {"ok": True, "backends": backends})
            return
        if path.startswith("/v1/tasks/"):
            task = STORE.get(path.rsplit("/", 1)[-1])
            if task is None:
                self._send(404, {"error": "task_not_found"})
                return
            self._send(200, task)
            return
        self._send(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if not _authorized(self):
            self._send(401, {"error": "unauthorized"})
            return
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path != "/v1/tasks":
            self._send(404, {"error": "not_found"})
            return
        try:
            payload = _read_json(self)
        except Exception:
            self._send(400, {"error": "invalid_json"})
            return
        backend = str(payload.get("backend") or "").strip().lower()
        prompt = str(payload.get("prompt") or "")
        if backend not in BACKENDS:
            self._send(400, {"error": "unknown_backend"})
            return
        if not prompt.strip():
            self._send(400, {"error": "prompt_required"})
            return
        cwd = str(payload.get("cwd") or DEFAULT_CWD).strip() or DEFAULT_CWD
        Path(cwd).mkdir(parents=True, exist_ok=True)
        timeout_seconds = float(payload.get("timeout_seconds") or DEFAULT_TIMEOUT)
        max_turns = payload.get("max_turns")
        try:
            max_turns = None if max_turns in (None, "") else int(max_turns)
        except (TypeError, ValueError):
            max_turns = None
        task = STORE.create(
            {
                "backend": backend,
                "prompt": prompt,
                "cwd": cwd,
                "timeout_seconds": timeout_seconds,
                "max_turns": max_turns,
            }
        )
        self._send(202, task)


def main() -> None:
    Path(DEFAULT_CWD).mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((DEFAULT_HOST, DEFAULT_PORT), Handler)
    print(f"local-agent-sidecar listening on {DEFAULT_HOST}:{DEFAULT_PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
