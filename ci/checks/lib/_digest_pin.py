"""R33: container images the repo adds are pinned by digest.

Pod image fields and HelmRelease image values are checked. A pin is
`@sha256:` on the reference, `tag: <version>@sha256:...`, or the chart
`digest:` field. Chart versions and OCI chart tags are not images.
HelmChartConfig is the k3s chart and is not scanned.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from _manifests import kind_of, load_documents, pod_spec, ref_of

_SHA = re.compile(r"sha256:[a-f0-9]{64}\Z")
_SHA_REF = re.compile(r"@sha256:[a-f0-9]{64}\Z")


@dataclass(frozen=True)
class DigestPinViolation:
    path: str
    message: str


def scan_digest_pin(repo_root: Path | str) -> list[DigestPinViolation]:
    violations: list[DigestPinViolation] = []
    for rel, doc, error in load_documents(repo_root):
        if error or doc is None:
            violations.append(DigestPinViolation(rel, error or "unreadable YAML"))
            continue
        pod = pod_spec(doc)
        if pod is not None:
            _pod_images(rel, doc, pod, violations)
        if kind_of(doc) == "HelmRelease":
            values = (doc.get("spec") or {}).get("values") if isinstance(doc.get("spec"), dict) else None
            if isinstance(values, dict):
                _walk_images(values, rel, ref_of(doc), "", violations)
    return sorted(violations, key=lambda item: (item.path, item.message))


def format_violations(violations: Iterable[DigestPinViolation]) -> list[str]:
    return [f"{item.path}: {item.message}" for item in violations]


def _pod_images(
    rel: str,
    doc: dict,
    pod: dict,
    violations: list[DigestPinViolation],
) -> None:
    for group in ("initContainers", "containers", "ephemeralContainers"):
        containers = pod.get(group)
        if not isinstance(containers, list):
            continue
        for container in containers:
            if not isinstance(container, dict):
                continue
            image = container.get("image")
            if not isinstance(image, str) or not image.strip():
                continue
            if _pinned_ref(image):
                continue
            name = container.get("name")
            label = name.strip() if isinstance(name, str) and name.strip() else group
            violations.append(
                DigestPinViolation(
                    rel,
                    f"{ref_of(doc)}: container {label} image {image.strip()} is not pinned by digest",
                )
            )


def _walk_images(
    node: object,
    rel: str,
    ref: str,
    path: str,
    violations: list[DigestPinViolation],
) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            if key == "image" and isinstance(value, str):
                if value.strip() and not _pinned_ref(value):
                    violations.append(
                        DigestPinViolation(
                            rel,
                            f"{ref}: {child} {value.strip()} is not pinned by digest",
                        )
                    )
                continue
            if key == "image" and isinstance(value, dict):
                state = _image_spec_pinned(value)
                if state is False:
                    violations.append(
                        DigestPinViolation(
                            rel,
                            f"{ref}: {child} {_image_label(value)} is not pinned by digest",
                        )
                    )
                    continue
            _walk_images(value, rel, ref, child, violations)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _walk_images(item, rel, ref, f"{path}[{index}]", violations)


def _image_spec_pinned(spec: dict) -> bool | None:
    """True when pinned, False when this is an image spec without a digest, None otherwise."""
    digest = spec.get("digest")
    tag = spec.get("tag")
    image = spec.get("image")
    repository = spec.get("repository")
    registry = spec.get("registry")
    if not any(_text(item) for item in (digest, tag, image, repository, registry)):
        return None
    if _pinned_digest(digest) or _pinned_ref(tag) or _pinned_ref(image):
        return True
    return False


def _image_label(spec: dict) -> str:
    for key in ("image", "repository", "tag", "registry"):
        value = spec.get(key)
        if _text(value):
            return str(value).strip()
    return "image"


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _pinned_digest(value: object) -> bool:
    return isinstance(value, str) and bool(_SHA.fullmatch(value.strip()))


def _pinned_ref(value: object) -> bool:
    return isinstance(value, str) and bool(_SHA_REF.search(value.strip()))
