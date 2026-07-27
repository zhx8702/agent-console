"""Runtime plugin dependency parsing and deterministic DAG resolution."""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from heapq import heappop, heappush
from typing import TYPE_CHECKING

from packaging.version import InvalidVersion, Version

if TYPE_CHECKING:
    from app.plugin.base import Plugin


_DEPENDENCY_RE = re.compile(r"^(?P<name>[a-z0-9_]+)(?:\s*>=\s*(?P<minimum>\S+))?$")


class PluginDependencyError(RuntimeError):
    """Base exception for runtime plugin dependency failures."""


class PluginDependencyGraphError(PluginDependencyError):
    """Raised before initialization when the selected plugin DAG is invalid."""

    def __init__(self, failures: Mapping[str, str]) -> None:
        self.failures = dict(failures)
        details = "; ".join(f"{name}: {reason}" for name, reason in self.failures.items())
        super().__init__(f"invalid plugin dependency graph: {details}")


class PluginDependencyBlockedError(PluginDependencyError):
    """Raised when a plugin is activated before a required dependency."""


@dataclass(frozen=True)
class PluginDependencySpec:
    name: str
    minimum_version: Version | None = None
    raw: str = ""

    @property
    def label(self) -> str:
        if self.minimum_version is None:
            return self.name
        return f"{self.name}>={self.minimum_version}"


@dataclass(frozen=True)
class PluginDependencyGraph:
    order: tuple[str, ...]
    requirements: dict[str, tuple[PluginDependencySpec, ...]]


def parse_plugin_dependency(raw: str, *, owner: str = "") -> PluginDependencySpec:
    """Parse ``name`` or ``name>=minimum-version`` dependency syntax."""

    if not isinstance(raw, str):
        raise PluginDependencyError(
            _invalid_spec_message(owner, repr(raw), "dependency must be a string")
        )
    cleaned = raw.strip()
    match = _DEPENDENCY_RE.fullmatch(cleaned)
    if match is None:
        raise PluginDependencyError(
            _invalid_spec_message(
                owner,
                raw,
                "expected 'name' or 'name>=minimum-version'",
            )
        )

    minimum_raw = match.group("minimum")
    minimum: Version | None = None
    if minimum_raw:
        try:
            minimum = Version(minimum_raw)
        except InvalidVersion as exc:
            raise PluginDependencyError(
                _invalid_spec_message(owner, raw, f"invalid minimum version {minimum_raw!r}")
            ) from exc
    return PluginDependencySpec(
        name=match.group("name"),
        minimum_version=minimum,
        raw=cleaned,
    )


def resolve_plugin_dependency_graph(
    plugins: Mapping[str, Plugin],
    selected_names: Collection[str] | None = None,
) -> PluginDependencyGraph:
    """Validate dependencies and return a stable dependency-first order.

    Registration order breaks ties between otherwise independent plugins.  A
    dependency that is loaded but not selected (for example, disabled by state)
    is unavailable to selected dependents and fails the same preflight.
    """

    selected = set(plugins) if selected_names is None else set(selected_names)
    ordered_names = tuple(name for name in plugins if name in selected)
    issues: dict[str, list[str]] = {}
    requirements: dict[str, tuple[PluginDependencySpec, ...]] = {}

    unknown_selected = sorted(selected.difference(plugins))
    for name in unknown_selected:
        _append_issue(issues, name, f"selected plugin {name!r} is not loaded")

    for owner in ordered_names:
        parsed: list[PluginDependencySpec] = []
        for raw in plugins[owner].meta.dependencies:
            try:
                parsed.append(parse_plugin_dependency(raw, owner=owner))
            except PluginDependencyError as exc:
                _append_issue(issues, owner, str(exc))
        requirements[owner] = tuple(parsed)

        for dependency in parsed:
            dependency_plugin = plugins.get(dependency.name)
            if dependency_plugin is None:
                _append_issue(
                    issues,
                    owner,
                    f"missing dependency {dependency.label!r}",
                )
                continue
            if dependency.name not in selected:
                _append_issue(
                    issues,
                    owner,
                    f"dependency {dependency.name!r} is not enabled for initialization",
                )
                continue
            if dependency.minimum_version is None:
                continue
            try:
                actual_version = Version(dependency_plugin.meta.version)
            except InvalidVersion:
                _append_issue(
                    issues,
                    owner,
                    f"dependency {dependency.name!r} has invalid version "
                    f"{dependency_plugin.meta.version!r}",
                )
                continue
            if actual_version < dependency.minimum_version:
                _append_issue(
                    issues,
                    owner,
                    f"dependency {dependency.name!r} requires >="
                    f"{dependency.minimum_version}, found {actual_version}",
                )

    order = _stable_topological_order(ordered_names, requirements)
    if len(order) != len(ordered_names):
        _record_dependency_cycles(ordered_names, requirements, issues)

    # A plugin downstream of an invalid node is not itself a cycle or missing
    # dependency owner, but it is still unusable.  Record that causal blocker so
    # state and diagnostics do not leave it looking merely skipped.
    changed = True
    while changed:
        changed = False
        for owner in ordered_names:
            if owner in issues:
                continue
            for dependency in requirements.get(owner, ()):
                if dependency.name in issues:
                    _append_issue(
                        issues,
                        owner,
                        f"dependency {dependency.name!r} has an invalid dependency graph",
                    )
                    changed = True
                    break

    if issues:
        raise PluginDependencyGraphError(
            {name: "; ".join(reasons) for name, reasons in issues.items()}
        )
    return PluginDependencyGraph(order=tuple(order), requirements=requirements)


def plugin_dependency_closure(
    plugins: Mapping[str, Plugin],
    roots: Collection[str],
) -> set[str]:
    """Return loaded transitive dependencies for targeted activation.

    Invalid and missing specifications are deliberately left for the graph
    resolver, which produces the owner-specific diagnostics.
    """

    selected: set[str] = set()
    pending = list(roots)
    while pending:
        name = pending.pop()
        if name in selected:
            continue
        selected.add(name)
        plugin = plugins.get(name)
        if plugin is None:
            continue
        for raw in plugin.meta.dependencies:
            try:
                dependency = parse_plugin_dependency(raw, owner=name)
            except PluginDependencyError:
                continue
            if dependency.name in plugins and dependency.name not in selected:
                pending.append(dependency.name)
    return selected


def _stable_topological_order(
    ordered_names: tuple[str, ...],
    requirements: Mapping[str, tuple[PluginDependencySpec, ...]],
) -> list[str]:
    selected = set(ordered_names)
    registration_index = {name: index for index, name in enumerate(ordered_names)}
    indegree = {name: 0 for name in ordered_names}
    dependents: dict[str, list[str]] = {name: [] for name in ordered_names}

    for owner in ordered_names:
        seen: set[str] = set()
        for dependency in requirements.get(owner, ()):
            if dependency.name not in selected or dependency.name in seen:
                continue
            seen.add(dependency.name)
            indegree[owner] += 1
            dependents[dependency.name].append(owner)

    ready: list[tuple[int, str]] = []
    for name in ordered_names:
        if indegree[name] == 0:
            heappush(ready, (registration_index[name], name))

    order: list[str] = []
    while ready:
        _, name = heappop(ready)
        order.append(name)
        for dependent in dependents[name]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                heappush(ready, (registration_index[dependent], dependent))
    return order


def _record_dependency_cycles(
    ordered_names: tuple[str, ...],
    requirements: Mapping[str, tuple[PluginDependencySpec, ...]],
    issues: dict[str, list[str]],
) -> None:
    selected = set(ordered_names)
    visit_state: dict[str, int] = {}
    stack: list[str] = []

    def visit(name: str) -> None:
        state = visit_state.get(name, 0)
        if state == 2:
            return
        if state == 1:
            start = stack.index(name)
            cycle = [*stack[start:], name]
            reason = f"dependency cycle detected: {' -> '.join(cycle)}"
            for member in dict.fromkeys(cycle[:-1]):
                _append_issue(issues, member, reason)
            return

        visit_state[name] = 1
        stack.append(name)
        for dependency in requirements.get(name, ()):
            if dependency.name in selected:
                visit(dependency.name)
        stack.pop()
        visit_state[name] = 2

    for name in ordered_names:
        visit(name)


def _append_issue(issues: dict[str, list[str]], owner: str, reason: str) -> None:
    owner_issues = issues.setdefault(owner, [])
    if reason not in owner_issues:
        owner_issues.append(reason)


def _invalid_spec_message(owner: str, raw: object, reason: str) -> str:
    prefix = f"plugin {owner!r} " if owner else ""
    return f"{prefix}has invalid dependency spec {raw!r}: {reason}"
