from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from yaml.constructor import ConstructorError

from app.common.config import get_settings
from app.plugin.base import PLUGIN_API_VERSION, PLUGIN_RESERVED_NAMES
from app.plugin.config_schema import PluginConfigSchemaError, validate_config_schema

_PLUGIN_NAME_RE = re.compile(r"^[a-z0-9_]+$")
_SOURCES = {"builtin", "marketplace", "local"}
_PACKAGE_TYPES = {"builtin", "local_archive", "git", "wheel", "container"}
_RESTART_POLICIES = {"none", "required_after_install", "required_after_upgrade", "always_required"}
_ROOT_FIELDS = frozenset({"items"})
_ITEM_FIELDS = frozenset(
    {
        "name",
        "display_name",
        "version",
        "description",
        "author",
        "source",
        "package",
        "compatibility",
        "dependencies",
        "permissions",
        "capabilities",
        "capability_digest",
        "config_schema",
        "restart_policy",
    }
)
_PACKAGE_FIELDS = frozenset({"type", "uri", "checksum", "signature"})
_COMPATIBILITY_FIELDS = frozenset({"core_api", "python"})
_DEPENDENCY_FIELDS = frozenset({"name", "version", "required"})
_PERMISSION_FIELDS = frozenset({"id", "level", "description"})
_CAPABILITY_FIELDS = frozenset({"routes", "hooks", "agent_tools", "commands"})
_SOURCE_PACKAGE_TYPES = {
    "builtin": frozenset({"builtin"}),
    "local": frozenset({"local_archive"}),
    "marketplace": frozenset({"local_archive", "git", "wheel", "container"}),
}


class MarketplaceManifestError(ValueError):
    pass


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True)
class PluginPackage:
    type: str
    uri: str = ""
    checksum: str = ""
    signature: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "uri": self.uri,
            "checksum": self.checksum,
            "signature": self.signature,
        }


@dataclass(frozen=True)
class PluginDependency:
    name: str
    version: str = ""
    required: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version, "required": self.required}


@dataclass(frozen=True)
class PluginPermission:
    id: str
    level: str = "local"
    description: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "level": self.level, "description": self.description}


@dataclass(frozen=True)
class MarketplaceItem:
    name: str
    display_name: str
    version: str
    description: str
    author: str
    source: str
    package: PluginPackage
    compatibility: dict[str, str]
    dependencies: list[PluginDependency] = field(default_factory=list)
    permissions: list[PluginPermission] = field(default_factory=list)
    capabilities: dict[str, list[str]] = field(default_factory=dict)
    capability_digest: str = ""
    config_schema: dict[str, Any] = field(default_factory=dict)
    restart_policy: str = "required_after_install"

    @property
    def compatible(self) -> bool:
        return is_core_api_compatible(self.compatibility.get("core_api", "")) and is_python_compatible(
            self.compatibility.get("python", "")
        )

    @property
    def permission_ids(self) -> list[str]:
        return [permission.id for permission in self.permissions]

    def as_manifest_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "source": self.source,
            "package": self.package.as_dict(),
            "compatibility": dict(self.compatibility),
            "dependencies": [dependency.as_dict() for dependency in self.dependencies],
            "permissions": [permission.as_dict() for permission in self.permissions],
            "capabilities": {key: list(value) for key, value in self.capabilities.items()},
            "capability_digest": self.capability_digest,
            "config_schema": dict(self.config_schema),
            "restart_policy": self.restart_policy,
        }


@dataclass(frozen=True)
class MarketplaceManifest:
    items: list[MarketplaceItem]

    def by_name(self) -> dict[str, MarketplaceItem]:
        return {item.name: item for item in self.items}


def load_marketplace_manifest(path: str | Path) -> MarketplaceManifest:
    manifest_path = _resolve_path(path)
    if not manifest_path.exists():
        return MarketplaceManifest(items=[])

    try:
        raw = yaml.load(
            manifest_path.read_text(encoding="utf-8"),
            Loader=_UniqueKeyLoader,
        ) or {}
    except yaml.YAMLError as exc:
        raise MarketplaceManifestError(f"invalid marketplace yaml: {exc}") from exc

    if not isinstance(raw, dict):
        raise MarketplaceManifestError("marketplace manifest must be a mapping")
    _reject_unknown_fields(raw, _ROOT_FIELDS, "marketplace manifest")
    items = raw.get("items") or []
    if not isinstance(items, list):
        raise MarketplaceManifestError("marketplace manifest 'items' must be a list")
    parsed = [_parse_item(item, index) for index, item in enumerate(items)]
    seen_names: set[str] = set()
    for item in parsed:
        if item.name in seen_names:
            raise MarketplaceManifestError(
                f"marketplace manifest contains duplicate item {item.name!r}"
            )
        seen_names.add(item.name)
    return MarketplaceManifest(items=parsed)


def validate_plugin_name(name: str) -> str:
    cleaned = str(name or "").strip()
    if not cleaned or not _PLUGIN_NAME_RE.fullmatch(cleaned):
        raise MarketplaceManifestError("invalid_plugin_name")
    if cleaned in PLUGIN_RESERVED_NAMES:
        raise MarketplaceManifestError("reserved_plugin_name")
    return cleaned


def is_core_api_compatible(spec: str) -> bool:
    return _matches_spec(spec, PLUGIN_API_VERSION)


def is_python_compatible(spec: str) -> bool:
    if not spec:
        return True
    return _matches_spec(spec, f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")


def permission_delta(current_permissions: list[str], target_permissions: list[str]) -> dict[str, list[str]]:
    current = set(current_permissions)
    target = set(target_permissions)
    return {
        "added": sorted(target - current),
        "removed": sorted(current - target),
    }


def _parse_item(item: Any, index: int) -> MarketplaceItem:
    if not isinstance(item, dict):
        raise MarketplaceManifestError(f"marketplace item #{index} must be a mapping")
    _reject_unknown_fields(item, _ITEM_FIELDS, f"marketplace item #{index}")
    name = validate_plugin_name(_required_str(item, "name", index))
    version = _required_str(item, "version", index)
    _parse_version(version, index)
    source = str(item.get("source") or "builtin").strip()
    if source not in _SOURCES:
        raise MarketplaceManifestError(f"marketplace item {name!r} has invalid source")
    package = _parse_package(item.get("package", {}), name)
    if package.type not in _SOURCE_PACKAGE_TYPES[source]:
        raise MarketplaceManifestError(
            f"marketplace item {name!r} source {source!r} cannot use "
            f"package.type {package.type!r}"
        )
    compatibility = _parse_compatibility(item.get("compatibility", {}), name)
    restart_policy = str(item.get("restart_policy") or "required_after_install").strip()
    if restart_policy not in _RESTART_POLICIES:
        raise MarketplaceManifestError(f"marketplace item {name!r} has invalid restart_policy")
    capabilities = _parse_capabilities(item.get("capabilities", {}), name)
    capability_digest = str(item.get("capability_digest") or "").strip().lower()
    if capability_digest and not re.fullmatch(
        r"sha256:[0-9a-f]{64}", capability_digest
    ):
        raise MarketplaceManifestError(
            f"marketplace item {name!r} capability_digest must be sha256"
        )
    config_schema = item.get("config_schema", {})
    if not isinstance(config_schema, dict):
        raise MarketplaceManifestError(f"marketplace item {name!r} config_schema must be a mapping")
    try:
        validate_config_schema(config_schema)
    except PluginConfigSchemaError as exc:
        raise MarketplaceManifestError(
            f"marketplace item {name!r} config_schema is invalid: {exc}"
        ) from exc
    return MarketplaceItem(
        name=name,
        display_name=str(item.get("display_name") or name),
        version=version,
        description=str(item.get("description") or ""),
        author=str(item.get("author") or "builtin"),
        source=source,
        package=package,
        compatibility=compatibility,
        dependencies=_parse_dependencies(item.get("dependencies", []), name),
        permissions=_parse_permissions(item.get("permissions", []), name),
        capabilities=capabilities,
        capability_digest=capability_digest,
        config_schema=dict(config_schema),
        restart_policy=restart_policy,
    )


def _parse_package(raw: Any, name: str) -> PluginPackage:
    if not isinstance(raw, dict):
        raise MarketplaceManifestError(f"marketplace item {name!r} package must be a mapping")
    _reject_unknown_fields(raw, _PACKAGE_FIELDS, f"marketplace item {name!r} package")
    package_type = str(raw.get("type") or "builtin").strip()
    if package_type not in _PACKAGE_TYPES:
        raise MarketplaceManifestError(f"marketplace item {name!r} has invalid package.type")
    uri = str(raw.get("uri") or "").strip()
    if package_type == "builtin" and PurePosixPath(uri).parts != ("plugins", name):
        raise MarketplaceManifestError(
            f"marketplace item {name!r} builtin uri must equal plugins/{name}"
        )
    signature = str(raw.get("signature") or "").strip()
    if signature:
        # Do not expose a decorative signature field as a trust signal.  The
        # current production trust root is the digest-pinned application image
        # and dynamic packages are disabled there.  A future remote package
        # format must add a configured trust store and cryptographic verifier
        # before signed manifests can be accepted.
        raise MarketplaceManifestError(
            f"marketplace item {name!r} package.signature is unsupported without a trust store"
        )
    return PluginPackage(
        type=package_type,
        uri=uri,
        checksum=str(raw.get("checksum") or ""),
        signature="",
    )


def _parse_compatibility(raw: Any, name: str) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise MarketplaceManifestError(f"marketplace item {name!r} compatibility must be a mapping")
    _reject_unknown_fields(
        raw,
        _COMPATIBILITY_FIELDS,
        f"marketplace item {name!r} compatibility",
    )
    core_api = str(raw.get("core_api") or "").strip()
    python = str(raw.get("python") or "").strip()
    _parse_specifier(core_api, name, "core_api")
    if python:
        _parse_specifier(python, name, "python")
    return {"core_api": core_api, "python": python}


def _parse_dependencies(raw: Any, name: str) -> list[PluginDependency]:
    if not isinstance(raw, list):
        raise MarketplaceManifestError(f"marketplace item {name!r} dependencies must be a list")
    dependencies: list[PluginDependency] = []
    seen_names: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise MarketplaceManifestError(f"marketplace item {name!r} dependency #{index} must be a mapping")
        _reject_unknown_fields(
            item,
            _DEPENDENCY_FIELDS,
            f"marketplace item {name!r} dependency #{index}",
        )
        dep_name = validate_plugin_name(str(item.get("name") or ""))
        if dep_name in seen_names:
            raise MarketplaceManifestError(
                f"marketplace item {name!r} contains duplicate dependency {dep_name!r}"
            )
        if dep_name == name:
            raise MarketplaceManifestError(
                f"marketplace item {name!r} cannot depend on itself"
            )
        seen_names.add(dep_name)
        version = str(item.get("version") or "").strip()
        if version:
            _parse_specifier(version, dep_name, "version")
        required = item.get("required", True)
        if type(required) is not bool:
            raise MarketplaceManifestError(
                f"marketplace item {name!r} dependency #{index} required must be boolean"
            )
        dependencies.append(
            PluginDependency(
                name=dep_name,
                version=version,
                required=required,
            )
        )
    return dependencies


def _parse_permissions(raw: Any, name: str) -> list[PluginPermission]:
    if not isinstance(raw, list):
        raise MarketplaceManifestError(f"marketplace item {name!r} permissions must be a list")
    permissions: list[PluginPermission] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw):
        if isinstance(item, str):
            permission_id = item.strip()
            level = "local"
            description = ""
        elif isinstance(item, dict):
            _reject_unknown_fields(
                item,
                _PERMISSION_FIELDS,
                f"marketplace item {name!r} permission #{index}",
            )
            permission_id = str(item.get("id") or "").strip()
            level = str(item.get("level") or "local")
            description = str(item.get("description") or "")
        else:
            raise MarketplaceManifestError(
                f"marketplace item {name!r} permission #{index} must be a mapping or string"
            )
        if not permission_id:
            raise MarketplaceManifestError(f"marketplace item {name!r} permission #{index} missing id")
        if permission_id in seen_ids:
            raise MarketplaceManifestError(
                f"marketplace item {name!r} contains duplicate permission {permission_id!r}"
            )
        seen_ids.add(permission_id)
        permissions.append(
            PluginPermission(
                id=permission_id,
                level=level,
                description=description,
            )
        )
    return permissions


def _parse_capabilities(raw: Any, name: str) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        raise MarketplaceManifestError(
            f"marketplace item {name!r} capabilities must be a mapping"
        )
    capabilities: dict[str, list[str]] = {}
    for raw_key, raw_values in raw.items():
        key = str(raw_key or "").strip()
        if not key:
            raise MarketplaceManifestError(
                f"marketplace item {name!r} capability name cannot be empty"
            )
        if key not in _CAPABILITY_FIELDS:
            raise MarketplaceManifestError(
                f"marketplace item {name!r} has unknown capability field {key!r}"
            )
        if not isinstance(raw_values, list):
            raise MarketplaceManifestError(
                f"marketplace item {name!r} capability {key!r} must be a list"
            )
        values: list[str] = []
        seen_values: set[str] = set()
        for index, raw_value in enumerate(raw_values):
            if not isinstance(raw_value, str):
                raise MarketplaceManifestError(
                    f"marketplace item {name!r} capability {key!r} "
                    f"entry #{index} must be a string"
                )
            value = raw_value.strip()
            if not value:
                raise MarketplaceManifestError(
                    f"marketplace item {name!r} capability {key!r} "
                    f"entry #{index} cannot be empty"
                )
            if value in seen_values:
                raise MarketplaceManifestError(
                    f"marketplace item {name!r} capability {key!r} "
                    f"contains duplicate entry {value!r}"
                )
            seen_values.add(value)
            values.append(value)
        capabilities[key] = values
    return capabilities


def _required_str(item: dict[str, Any], key: str, index: int) -> str:
    raw = item.get(key)
    if not isinstance(raw, str):
        raise MarketplaceManifestError(f"marketplace item #{index} {key} must be a string")
    value = raw.strip()
    if not value:
        raise MarketplaceManifestError(f"marketplace item #{index} missing {key}")
    return value


def _reject_unknown_fields(
    raw: dict[Any, Any],
    allowed: frozenset[str],
    context: str,
) -> None:
    unknown = sorted(str(key) for key in raw if key not in allowed)
    if unknown:
        raise MarketplaceManifestError(f"{context} has unknown fields: {unknown!r}")


def _parse_version(value: str, index: int) -> Version:
    try:
        return Version(value)
    except InvalidVersion as exc:
        raise MarketplaceManifestError(f"marketplace item #{index} has invalid version") from exc


def _parse_specifier(value: str, name: str, field_name: str) -> SpecifierSet:
    if not value:
        raise MarketplaceManifestError(f"marketplace item {name!r} missing compatibility.{field_name}")
    try:
        return SpecifierSet(normalize_specifier(value))
    except InvalidSpecifier as exc:
        raise MarketplaceManifestError(
            f"marketplace item {name!r} has invalid compatibility.{field_name}"
        ) from exc


def _matches_spec(spec: str, version: str) -> bool:
    if not spec:
        return False
    return Version(version) in SpecifierSet(normalize_specifier(spec))


def normalize_specifier(value: str) -> str:
    return re.sub(r"\s+(?=[<>=!~])", ",", value.strip())


def _resolve_path(path: str | Path) -> Path:
    manifest_path = Path(path)
    if not manifest_path.is_absolute():
        manifest_path = get_settings().project_root / manifest_path
    return manifest_path
