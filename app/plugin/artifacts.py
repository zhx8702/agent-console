from __future__ import annotations

import hashlib
from pathlib import Path


def compute_plugin_tree_digest(root: Path) -> str:
    """Hash one extracted plugin generation using stable relative paths.

    Interpreter caches are excluded because they are derived after import and
    are not part of the approved archive generation. Symlinks fail closed.
    """

    base = Path(root).resolve()
    if not base.is_dir():
        raise ValueError("plugin artifact root must be a directory")
    digest = hashlib.sha256()
    files: list[tuple[str, Path]] = []
    for path in base.rglob("*"):
        relative = path.relative_to(base)
        if path.is_symlink():
            raise ValueError(f"plugin artifact contains a symlink: {relative.as_posix()}")
        if not path.is_file():
            continue
        if "__pycache__" in relative.parts or path.suffix.lower() in {".pyc", ".pyo"}:
            continue
        files.append((relative.as_posix(), path))

    for relative, path in sorted(files, key=lambda item: item[0]):
        encoded_path = relative.encode("utf-8")
        size = path.stat().st_size
        digest.update(b"F")
        digest.update(len(encoded_path).to_bytes(4, "big"))
        digest.update(encoded_path)
        digest.update(size.to_bytes(8, "big"))
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


__all__ = ["compute_plugin_tree_digest"]
