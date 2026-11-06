"""Publish the moderation tunnel only after GPU models have been warmed.

Keep the complete Ollama protocol in the tunnel; this process does not proxy or
rewrite caller payloads. A missing model or failed readiness check withdraws the
tunnel so the production gateway returns an unavailable error.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import signal
import subprocess
import threading
import urllib.request


def gpu_models_ready(tags, running, required):
    available = {row["name"]: row["digest"] for row in tags.get("models", [])}
    loaded = {row["digest"] for row in running.get("models", []) if row.get("size_vram", 0) > 0}
    # Ollama can deduplicate aliases of identical manifests into one loaded entry.
    return bool(required) and all(name in available and available[name] in loaded for name in required)


def ssh_command(env):
    return [
        env.get("SSH_EXECUTABLE", "/usr/bin/ssh"), "-NT", "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=15",
        "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=10",
        "-o", "ServerAliveCountMax=3", "-o", "UserKnownHostsFile=" + env["SSH_KNOWN_HOSTS_FILE"],
        "-i", env["SSH_IDENTITY_FILE"], "-p", env["SSH_PORT"], "-R",
        ":".join(env[key] for key in ["SSH_REMOTE_BIND", "SSH_REMOTE_PORT",
                                     "MODERATION_LOCAL_HOST", "MODERATION_LOCAL_PORT"]),
        env["SSH_USER"] + "@" + env["SSH_HOST"],
    ]


class Supervisor:
    def __init__(self, env):
        self.base_url = env["MODERATION_OLLAMA_URL"].rstrip("/")
        self.models = tuple(name.strip() for name in env["MODERATION_MODELS"].split(",") if name.strip())
        if not 1 <= len(self.models) <= 8:
            raise ValueError("Configure one to eight moderation models")
        self.command = ssh_command(env)
        self.stop = threading.Event()
        self.tunnel = None
        self.primed = False

    def request(self, path, body=None, timeout=5):
        request = urllib.request.Request(
            self.base_url + path, data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=timeout) as response:
            return json.load(response)

    def ready(self):
        return gpu_models_ready(self.request("/api/tags"), self.request("/api/ps"), self.models)

    def warmup(self):
        tasks = [(model, text) for model in self.models for text in (
            "请介绍一下天气预报。", "请给出保护账号安全的建议。", "请介绍公园徒步的准备事项。" * 24)]

        def run(task):
            model, text = task
            result = self.request("/v1/chat/completions", {
                "model": model, "messages": [{"role": "user", "content": text}],
                "temperature": 0, "max_tokens": 128, "stream": False,
            }, timeout=180)
            if not result["choices"][0]["message"]["content"].strip():
                raise ValueError("warmup returned no completion")

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(run, tasks))

    def withdraw(self):
        if self.tunnel is not None:
            self.tunnel.terminate()
            try:
                self.tunnel.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.tunnel.kill()
                self.tunnel.wait()
            self.tunnel = None

    def tick(self):
        if not self.primed or not self.ready():
            self.withdraw()
            self.primed = False
            self.warmup()
            if not self.ready():
                raise RuntimeError("GPU models are not resident after warmup")
            self.primed = True
            print(json.dumps({"event": "moderation_gpu_ready"}), flush=True)
        if not self.stop.is_set() and (self.tunnel is None or self.tunnel.poll() is not None):
            self.tunnel = subprocess.Popen(self.command)

    def run(self):
        try:
            while not self.stop.is_set():
                try:
                    self.tick()
                except Exception as exc:
                    self.withdraw()
                    self.primed = False
                    print(json.dumps({"event": "moderation_tunnel_withdrawn", "error": type(exc).__name__}),
                          flush=True)
                    self.stop.wait(5)
                else:
                    self.stop.wait(2)
        finally:
            self.withdraw()


def main():
    supervisor = Supervisor(os.environ)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: supervisor.stop.set())
    supervisor.run()


if __name__ == "__main__":
    main()
