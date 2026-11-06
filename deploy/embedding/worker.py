"""Private embedding worker: bounded queue, query priority, startup warmup.

Uses only the Python standard library. Never logs submitted text or credentials.
The model service stays on the private Docker network; expose this worker only
on loopback and forward it through the dedicated authenticated SSH tunnel.
"""
from __future__ import annotations

import concurrent.futures
import hmac
import itertools
import json
import math
import os
import queue
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class Scheduler:
    def __init__(self, endpoint: str, model: str, dimensions: int = 1024):
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.dimensions = dimensions
        self.queue: queue.PriorityQueue = queue.PriorityQueue(maxsize=64)
        self.sequence = itertools.count()
        self.ready = threading.Event()
        self.completed = 0
        self.failed = 0
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def infer(self, texts: list[str]) -> tuple[list[list[float]], int]:
        payload = json.dumps({"model": self.model, "input": texts, "keep_alive": -1,
                              "truncate": False}).encode()
        request = urllib.request.Request(self.endpoint + "/api/embed", data=payload,
                                         headers={"Content-Type": "application/json"})
        with self._opener.open(request, timeout=90) as response:
            body = json.load(response)
        vectors = body["embeddings"]
        if len(vectors) != len(texts) or any(
            len(v) != self.dimensions or not all(math.isfinite(x) for x in v)
            or sum(x * x for x in v) <= 0 for v in vectors
        ):
            raise ValueError("invalid model output")
        return vectors, int(body.get("prompt_eval_count", 0))

    def warmup(self) -> None:
        # BGE-M3 tokenizes "a " as one token and adds two special tokens.
        # Repeated Chinese phrases can advance two tokens at a time and leave
        # every odd sequence length cold. Cover each short-query length.
        warmed = set()
        for length in (*range(1, 129), 256, 512):
            _, tokens = self.infer(["a " * length])
            warmed.add(tokens)
        if not set(range(3, 131)).issubset(warmed):
            raise RuntimeError("short query warmup coverage incomplete")
        self.ready.set()

    def submit(self, texts: list[str], *, background: bool, timeout: float):
        if not self.ready.is_set():
            raise RuntimeError("model not ready")
        future: concurrent.futures.Future = concurrent.futures.Future()
        self.queue.put_nowait((1 if background else 0, next(self.sequence),
                               time.monotonic() + timeout, texts, future))
        return future

    def run(self) -> None:
        while True:
            if not self.ready.is_set():
                try:
                    self.warmup()
                except Exception as exc:
                    print(json.dumps({"event": "warmup_retry", "error": type(exc).__name__}), flush=True)
                    time.sleep(5)
                    continue
            try:
                _, _, deadline, texts, future = self.queue.get(timeout=30)
            except queue.Empty:
                # Detect upstream restarts while idle; never serve an unready model.
                try:
                    self.infer(["服务健康检查"])
                except Exception:
                    self.ready.clear()
                continue
            try:
                if future.cancelled() or time.monotonic() >= deadline:
                    if not future.done():
                        future.set_exception(TimeoutError("queue deadline"))
                    continue
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    result = self.infer(texts)
                except Exception as exc:
                    self.failed += 1
                    self.ready.clear()
                    future.set_exception(RuntimeError(type(exc).__name__))
                else:
                    self.completed += len(texts)
                    future.set_result(result)
            finally:
                self.queue.task_done()


def make_handler(scheduler: Scheduler, token: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def respond(self, status: int, body: dict):
            data = json.dumps(body, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            if self.path not in {"/healthz", "/readyz"}:
                return self.respond(404, {"error": "not_found"})
            self.respond(200 if scheduler.ready.is_set() else 503,
                         {"ready": scheduler.ready.is_set(), "model": scheduler.model,
                          "dimensions": scheduler.dimensions, "queued": scheduler.queue.qsize(),
                          "completed_texts": scheduler.completed, "failed_requests": scheduler.failed})

        def do_POST(self):
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                self.close_connection = True
                return self.respond(401, {"error": "unauthorized"})
            if self.path != "/v1/embeddings":
                self.close_connection = True
                return self.respond(404, {"error": "not_found"})
            futures = []
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 262144:
                    self.close_connection = True
                    return self.respond(413, {"error": "request_too_large"})
                body = json.loads(self.rfile.read(length))
                texts = body.get("input")
                if isinstance(texts, str):
                    texts = [texts]
                if (body.get("model") != scheduler.model or
                    body.get("encoding_format", "float") != "float" or
                    not isinstance(texts, list) or not 1 <= len(texts) <= 64 or
                    any(not isinstance(t, str) or not t.strip() or len(t) > 8000 for t in texts)):
                    return self.respond(400, {"error": "invalid_model_or_input"})
                background = self.headers.get("X-Embedding-Priority") == "background"
                timeout = 25.0 if background else 3.5
                deadline = time.monotonic() + timeout
                # Small jobs keep long indexing batches from monopolizing the GPU.
                for offset in range(0, len(texts), 4):
                    futures.append(scheduler.submit(texts[offset:offset + 4],
                                                    background=background, timeout=timeout))
                vectors, tokens = [], 0
                for future in futures:
                    values, count = future.result(timeout=max(.001, deadline - time.monotonic()))
                    vectors.extend(values)
                    tokens += count
                self.respond(200, {"object": "list", "model": scheduler.model,
                                   "data": [{"object": "embedding", "index": i, "embedding": v}
                                            for i, v in enumerate(vectors)],
                                   "usage": {"prompt_tokens": tokens, "total_tokens": tokens}})
            except (ValueError, TypeError, AttributeError):
                self.respond(400, {"error": "invalid_request"})
            except (TimeoutError, queue.Full):
                self.respond(503, {"error": "embedding_busy"})
            except Exception:
                self.respond(503, {"error": "embedding_unavailable"})
            finally:
                for future in futures:
                    future.cancel()

    return Handler


def main():
    token = Path(os.environ["EMBEDDING_TOKEN_FILE"]).read_text().strip()
    if len(token) < 32:
        raise ValueError("embedding token must contain at least 32 characters")
    scheduler = Scheduler(os.environ.get("OLLAMA_URL", "http://model:11434"),
                          os.environ.get("EMBEDDING_MODEL", "bge-m3:latest"),
                          dimensions=int(os.environ.get("EMBEDDING_DIMENSIONS", "1024")))
    if scheduler.dimensions <= 0:
        raise ValueError("EMBEDDING_DIMENSIONS must be positive")
    threading.Thread(target=scheduler.run, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", 8080), make_handler(scheduler, token))
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
