"""Fail CI when locked dependencies have missing or prohibited license metadata."""

from __future__ import annotations

import json
import re
import tomllib
from importlib import metadata
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
UV_LOCK = REPOSITORY_ROOT / "uv.lock"
NPM_LOCK = REPOSITORY_ROOT / "frontend" / "package-lock.json"

# Strong copyleft, source-available, and add-on restriction licenses require an
# explicit project-level review before they can enter a distributed build.
PROHIBITED_LICENSE = re.compile(
    r"\b(?:AGPL|GPL|SSPL|BUSL)(?:[-\s]?v?\d[\w.-]*)?\b"
    r"|GNU (?:Affero )?General Public License"
    r"|Commons Clause|Elastic License",
    re.IGNORECASE,
)


def _normalize_package_name(value: str) -> str:
    return value.casefold().replace("_", "-")


def _python_license(distribution: metadata.Distribution) -> str:
    expression = (distribution.metadata.get("License-Expression") or "").strip()
    if expression:
        return expression

    declared = (distribution.metadata.get("License") or "").strip()
    if declared and declared.casefold() != "unknown":
        return declared

    classifiers = [
        value.removeprefix("License :: ")
        for value in distribution.metadata.get_all("Classifier") or []
        if value.startswith("License :: ")
    ]
    return " OR ".join(classifiers)


def python_dependency_licenses() -> dict[str, str]:
    """Return license declarations for locked packages installed in this environment."""

    lock_data = tomllib.loads(UV_LOCK.read_text(encoding="utf-8"))
    locked_names = {
        _normalize_package_name(package["name"]) for package in lock_data["package"]
    }
    licenses: dict[str, str] = {}
    for distribution in metadata.distributions():
        raw_name = distribution.metadata.get("Name") or ""
        package_name = _normalize_package_name(raw_name)
        if not package_name or package_name not in locked_names:
            continue
        licenses[package_name] = _python_license(distribution)
    return licenses


def _npm_installed_license(package_path: str) -> str:
    manifest_path = REPOSITORY_ROOT / "frontend" / package_path / "package.json"
    if not manifest_path.is_file():
        return ""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    declared = manifest.get("license")
    if isinstance(declared, str):
        return declared.strip()
    declarations = manifest.get("licenses")
    if isinstance(declarations, list):
        values = [
            item.get("type", "").strip()
            for item in declarations
            if isinstance(item, dict) and isinstance(item.get("type"), str)
        ]
        return " OR ".join(value for value in values if value)
    return ""


def frontend_dependency_licenses() -> dict[str, str]:
    """Return license declarations for packages in package-lock.json."""

    lock_data: dict[str, Any] = json.loads(NPM_LOCK.read_text(encoding="utf-8"))
    licenses: dict[str, str] = {}
    for package_path, package in lock_data.get("packages", {}).items():
        if not package_path.startswith("node_modules/"):
            continue
        declared = package.get("license", "")
        license_text = declared.strip() if isinstance(declared, str) else ""
        licenses[package_path] = license_text or _npm_installed_license(package_path)
    return licenses


def validate_licenses(ecosystem: str, licenses: dict[str, str]) -> list[str]:
    errors: list[str] = []
    if not licenses:
        return [f"{ecosystem}: no dependency metadata was found"]
    for package_name, license_text in sorted(licenses.items()):
        if not license_text:
            errors.append(f"{ecosystem}: {package_name} has no license declaration")
        elif PROHIBITED_LICENSE.search(license_text):
            errors.append(
                f"{ecosystem}: {package_name} uses a prohibited license: {license_text}"
            )
    return errors


def main() -> int:
    ecosystems = {
        "python": python_dependency_licenses(),
        "frontend": frontend_dependency_licenses(),
    }
    errors = [
        error
        for ecosystem, licenses in ecosystems.items()
        for error in validate_licenses(ecosystem, licenses)
    ]
    if errors:
        print("Dependency license check failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    counts = ", ".join(
        f"{ecosystem}={len(licenses)}" for ecosystem, licenses in ecosystems.items()
    )
    print(f"Dependency license check passed ({counts}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
