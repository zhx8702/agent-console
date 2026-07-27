from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from app.common.runtime_config import write_env_overrides_atomic
from plugins.amap import router as amap_router


def test_concurrent_runtime_config_writers_preserve_every_unrelated_key(tmp_path) -> None:
    env_path = tmp_path / "runtime.env"
    env_path.write_text("UNCHANGED=keep\n", encoding="utf-8")
    worker_count = 16
    barrier = threading.Barrier(worker_count)

    def update(index: int) -> None:
        barrier.wait(timeout=5)
        writer = (
            write_env_overrides_atomic if index % 2 == 0 else amap_router._write_env_overrides
        )
        writer(str(env_path), {f"CONCURRENT_KEY_{index}": f"value-{index}"})

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(update, index) for index in range(worker_count)]
        for future in futures:
            future.result(timeout=10)

    content = env_path.read_text(encoding="utf-8")
    assert "UNCHANGED=keep" in content
    for index in range(worker_count):
        assert f"CONCURRENT_KEY_{index}=value-{index}" in content
    assert list(tmp_path.glob(".runtime.env.*.tmp")) == []
