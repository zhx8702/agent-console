"""Shared helpers for building protected Windows modules with Nuitka."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ASCII_BUILD_PYTHON_ENV = "WXBOT_BUILD_PYTHON"
ASCII_BUILD_HOME_ENV = "WXBOT_BUILD_HOME"
ASCII_REEXEC_READY_ENV = "WXBOT_ASCII_BUILD_READY"
NUITKA_LTO_ENV = "WXBOT_NUITKA_LTO"


def _has_non_ascii(value: str) -> bool:
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        return True
    return False


def _windows_drive_root(path: Path) -> Path:
    return Path(path.drive + "\\")


def _default_windows_python(project_root: Path) -> Path:
    return _windows_drive_root(project_root) / "python310" / "python.exe"


def _default_windows_home(project_root: Path) -> Path:
    return _windows_drive_root(project_root) / "pyhome"


def ensure_ascii_windows_python(script_path: Path, project_root: Path) -> None:
    if os.name != "nt":
        return

    current_paths = [sys.executable, sys.prefix, getattr(sys, "base_prefix", sys.prefix)]
    if not any(_has_non_ascii(path) for path in current_paths):
        return

    if os.environ.get(ASCII_REEXEC_READY_ENV) == "1":
        raise SystemExit(
            "current Python path still contains non-ASCII characters. "
            f"Set {ASCII_BUILD_PYTHON_ENV} to an ASCII-path interpreter, "
            r"for example E:\python310\python.exe."
        )

    candidates = []
    env_python = os.environ.get(ASCII_BUILD_PYTHON_ENV)
    if env_python:
        candidates.append(Path(env_python))
    candidates.append(_default_windows_python(project_root))

    for candidate in candidates:
        if not candidate.exists():
            continue
        if _has_non_ascii(str(candidate)):
            continue
        env = os.environ.copy()
        env[ASCII_REEXEC_READY_ENV] = "1"
        subprocess.run([str(candidate), str(script_path), *sys.argv[1:]], check=True, env=env)
        raise SystemExit(0)

    raise SystemExit(
        "no ASCII-path Python interpreter found for Nuitka build. "
        f"Install or copy one to {_default_windows_python(project_root)} "
        f"or point {ASCII_BUILD_PYTHON_ENV} at it."
    )


def build_nuitka_module(source_relative_path: str, label: str) -> None:
    script_path = Path(__file__).resolve()
    project_root = script_path.parent.parent
    ensure_ascii_windows_python(script_path, project_root)

    source = project_root / source_relative_path
    output_dir = project_root / "build" / "protected"
    output_dir.mkdir(parents=True, exist_ok=True)

    env = prepare_nuitka_env(project_root)

    cmd = [
        sys.executable,
        "-m",
        "nuitka",
        "--module",
        str(source),
        "--output-dir=" + str(output_dir),
        "--remove-output",
        "--assume-yes-for-downloads",
        "--lto=" + env.get(NUITKA_LTO_ENV, "no"),
    ]
    print(f"[build] {label} using python: {sys.executable}")
    if os.name == "nt":
        print(f"[build] {label} build home: {env['USERPROFILE']}")
        print(f"[build] {label} nuitka cache: {env['NUITKA_CACHE_DIR']}")
    print("[build] running:", " ".join(cmd))
    subprocess.run(cmd, check=True, env=env)
    artifact = _find_built_artifact(output_dir, source.stem)
    manifest_path = output_dir / "module_hashes.json"
    _update_hash_manifest(manifest_path, label, artifact)
    print(f"[build] protected {label} written to {artifact}")
    print(f"[build] hash manifest updated: {manifest_path}")


def _find_built_artifact(output_dir: Path, module_stem: str) -> Path:
    candidates = sorted(output_dir.glob(f"{module_stem}*.pyd"))
    if not candidates:
        raise FileNotFoundError(f"compiled artifact not found for {module_stem} in {output_dir}")
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _sha256_file(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _update_hash_manifest(manifest_path: Path, module_key: str, artifact: Path) -> None:
    data = {}
    if manifest_path.exists():
        try:
            with open(manifest_path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}

    data[module_key] = {
        "filename": artifact.name,
        "sha256": _sha256_file(artifact),
        "updated_at": int(artifact.stat().st_mtime),
    }
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def prepare_nuitka_env(project_root: Path) -> dict:
    env = os.environ.copy()
    if os.name != "nt":
        return env

    build_home = Path(env.get(ASCII_BUILD_HOME_ENV, str(_default_windows_home(project_root))))
    cache_dir = Path(env.get("NUITKA_CACHE_DIR", str(_windows_drive_root(project_root) / "nuitka-cache")))
    temp_dir = Path(env.get("TEMP", str(_windows_drive_root(project_root) / "tmp")))
    roaming_dir = build_home / "AppData" / "Roaming"
    local_dir = build_home / "AppData" / "Local"
    for path in (cache_dir, temp_dir, roaming_dir, local_dir):
        path.mkdir(parents=True, exist_ok=True)

    drive, tail = os.path.splitdrive(str(build_home))
    env["PYTHONNOUSERSITE"] = "1"
    env["NUITKA_CACHE_DIR"] = str(cache_dir)
    env["TEMP"] = str(temp_dir)
    env["TMP"] = str(temp_dir)
    env["HOME"] = str(build_home)
    env["USERPROFILE"] = str(build_home)
    env["HOMEDRIVE"] = drive or project_root.drive
    env["HOMEPATH"] = tail or "\\"
    env["APPDATA"] = str(roaming_dir)
    env["LOCALAPPDATA"] = str(local_dir)
    return env
