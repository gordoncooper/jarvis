"""R28: the only LoadBalancer Services are Traefik and App DNS.

Traefik is the HelmChartConfig under platform/traefik/. App DNS is the
HelmRelease under platform/app-dns/. The allow-list is those two objects by
(kind, namespace, name), each in its own folder. The check does not match a
DNS name or an address.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from _manifests import helm_values, load_documents, ref_of

ALLOWED: dict[tuple[str, str, str], str] = {
    ("HelmChartConfig", "kube-system", "traefik"): "platform/traefik/",
    ("HelmRelease", "flux-system", "k8s-gateway"): "platform/app-dns/",
}


@dataclass(frozen=True)
class LoadBalancerViolation:
    path: str
    message: str


def scan_loadbalancer(repo_root: Path | str) -> list[LoadBalancerViolation]:
    violations: list[LoadBalancerViolation] = []
    for rel, doc, error in load_documents(repo_root):
        if error or doc is None:
            violations.append(LoadBalancerViolation(rel, error or "unreadable YAML"))
            continue
        if not _is_loadbalancer(doc):
            continue
        folder = ALLOWED.get(_identity(doc))
        if folder is not None and rel.startswith(folder):
            continue
        violations.append(
            LoadBalancerViolation(
                rel,
                f"{ref_of(doc)}: LoadBalancer is only allowed for Traefik and App DNS",
            )
        )
    return sorted(violations, key=lambda item: (item.path, item.message))


def format_violations(violations: Iterable[LoadBalancerViolation]) -> list[str]:
    return [f"{item.path}: {item.message}" for item in violations]


def _identity(doc: dict) -> tuple[str, str, str]:
    metadata = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
    return (
        str(doc.get("kind") or ""),
        str(metadata.get("namespace") or ""),
        str(metadata.get("name") or ""),
    )


def _is_loadbalancer(doc: dict) -> bool:
    kind = doc.get("kind")
    if kind == "Service":
        spec = doc.get("spec")
        return isinstance(spec, dict) and spec.get("type") == "LoadBalancer"
    if kind not in {"HelmRelease", "HelmChartConfig"}:
        return False
    values, error = helm_values(doc)
    if error or not isinstance(values, dict):
        return False
    if values.get("serviceType") == "LoadBalancer":
        return True
    service = values.get("service")
    if not isinstance(service, dict):
        return False
    if service.get("type") == "LoadBalancer":
        return True
    nested = service.get("spec")
    return isinstance(nested, dict) and nested.get("type") == "LoadBalancer"
