"""R5: memory and CPU requests, a memory limit, no CPU limit on long-running containers, probes, stdout.

Plain pod specs are checked as written. HelmRelease values and HelmChartConfig
valuesContent are checked where they set resources, probes, or args. Charts
are not rendered: rendering would pull remote charts into CI.

Probes are required on long-running pods (Deployment, StatefulSet, DaemonSet,
ReplicaSet, Pod), including their app containers. Job and CronJob containers
and init containers are batch: they need the resource rules, not probes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from _manifests import helm_values, kind_of, load_documents, pod_spec, ref_of

# Flags that send logs to a file. Modest on purpose: no volume guessing.
_LOG_FILE = re.compile(
    r"(?:^|[\s\"'])(?:--log-file|--log_file|--log-dir|--log_dir|--log-output-file)(?:$|=|[\s\"'])"
)

_SERVICE_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Pod"}
_PROBE_KEYS = ("livenessProbe", "readinessProbe")
_ARG_KEYS = ("command", "args", "extraArgs")


@dataclass(frozen=True)
class ManifestLintViolation:
    path: str
    message: str


def scan_manifest_lint(repo_root: Path | str) -> list[ManifestLintViolation]:
    violations: list[ManifestLintViolation] = []
    for rel, doc, error in load_documents(repo_root):
        if error or doc is None:
            violations.append(ManifestLintViolation(rel, error or "unreadable YAML"))
            continue
        pod = pod_spec(doc)
        if pod is not None:
            violations.extend(_pod_violations(rel, doc, pod))
            continue
        values, values_error = helm_values(doc)
        if values_error:
            violations.append(ManifestLintViolation(rel, f"{ref_of(doc)}: {values_error}"))
            continue
        if values is not None:
            _walk_values(values, rel, ref_of(doc), "", violations)
    return sorted(violations, key=lambda item: (item.path, item.message))


def format_violations(violations: Iterable[ManifestLintViolation]) -> list[str]:
    return [f"{item.path}: {item.message}" for item in violations]


def _pod_violations(rel: str, doc: dict, pod: dict) -> list[ManifestLintViolation]:
    found: list[ManifestLintViolation] = []
    require_probes = kind_of(doc) in _SERVICE_KINDS
    for group in ("initContainers", "containers", "ephemeralContainers"):
        containers = pod.get(group)
        if not isinstance(containers, list):
            continue
        for container in containers:
            if not isinstance(container, dict):
                continue
            name = container.get("name")
            label = name.strip() if isinstance(name, str) and name.strip() else group
            where = f"{ref_of(doc)}: container {label}"
            resources = container.get("resources")
            if not isinstance(resources, dict):
                resources = {}
            found.extend(
                ManifestLintViolation(rel, f"{where}: {problem}")
                for problem in _resource_problems(resources)
            )
            if require_probes and group == "containers":
                for probe in _PROBE_KEYS:
                    if not _probe_set(container.get(probe)):
                        found.append(ManifestLintViolation(rel, f"{where}: {probe} is unset"))
            if _text_logs_to_file(container.get("command")) or _text_logs_to_file(
                container.get("args")
            ):
                found.append(ManifestLintViolation(rel, f"{where}: logs to a file"))
    return found


def _walk_values(
    node: object,
    rel: str,
    ref: str,
    path: str,
    violations: list[ManifestLintViolation],
) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            if key == "resources" and isinstance(value, dict) and _is_resource_block(value):
                for problem in _resource_problems(value):
                    violations.append(
                        ManifestLintViolation(rel, f"{ref}: {child}: {problem}")
                    )
                continue
            if key in _PROBE_KEYS and _probe_disabled(value):
                violations.append(ManifestLintViolation(rel, f"{ref}: {child} is disabled"))
                continue
            if key in _ARG_KEYS and _text_logs_to_file(value):
                violations.append(ManifestLintViolation(rel, f"{ref}: {child} logs to a file"))
            _walk_values(value, rel, ref, child, violations)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _walk_values(item, rel, ref, f"{path}[{index}]", violations)


def _is_resource_block(block: dict) -> bool:
    return isinstance(block.get("requests"), dict) or isinstance(block.get("limits"), dict)


def _resource_problems(block: dict) -> list[str]:
    requests = block.get("requests") if isinstance(block.get("requests"), dict) else {}
    limits = block.get("limits") if isinstance(block.get("limits"), dict) else {}
    problems: list[str] = []
    if not _quantity(requests.get("memory")):
        problems.append("memory request is unset")
    if not _quantity(limits.get("memory")):
        problems.append("memory limit is unset")
    if not _quantity(requests.get("cpu")):
        problems.append("cpu request is unset")
    if "cpu" in limits and limits.get("cpu") is not None:
        problems.append("cpu limit is set")
    return problems


def _quantity(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _probe_set(value: object) -> bool:
    return isinstance(value, dict) and bool(value) and value.get("enabled") is not False


def _probe_disabled(value: object) -> bool:
    if value is None or value is False:
        return True
    return isinstance(value, dict) and value.get("enabled") is False


def _text_logs_to_file(value: object) -> bool:
    if isinstance(value, str):
        return bool(_LOG_FILE.search(value))
    if isinstance(value, list):
        return any(isinstance(item, str) and _LOG_FILE.search(item) for item in value)
    return False
