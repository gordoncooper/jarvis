"""Read Flux Kustomization paths from envs/<env>/flux (p0a-14)."""

from __future__ import annotations

from pathlib import Path

import yaml

from _core_names import sole_env_directory_name


def flux_kustomization_docs(repo_root: Path | str) -> list[dict[str, object]]:
    """Return Flux Kustomization manifests in dependsOn order."""
    root = Path(repo_root).resolve()
    env = sole_env_directory_name(root)
    kdir = root / "envs" / env / "flux" / "kustomizations"
    by_name: dict[str, dict[str, object]] = {}
    for path in sorted(kdir.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            raise ValueError(f"{path}: expected one mapping")
        name = doc.get("metadata", {}).get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"{path}: missing metadata.name")
        by_name[name] = doc

    ordered: list[dict[str, object]] = []
    remaining = set(by_name)
    while remaining:
        progressed = False
        for name in sorted(remaining):
            doc = by_name[name]
            depends = doc.get("spec", {}).get("dependsOn") or []
            dep_names = [
                item["name"]
                for item in depends
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            ]
            ordered_names = {
                d["metadata"]["name"]
                for d in ordered
                if isinstance(d.get("metadata"), dict)
                and isinstance(d["metadata"].get("name"), str)
            }
            if all(dep in ordered_names for dep in dep_names):
                ordered.append(doc)
                remaining.remove(name)
                progressed = True
                break
        if not progressed:
            raise ValueError(f"cycle or missing dependsOn in Flux kustomizations: {remaining}")

    return ordered


def flux_kustomization_layer_paths(repo_root: Path | str) -> dict[str, str]:
    """Map Kustomization metadata.name to spec.path (repo-relative, no leading ./)."""
    layers: dict[str, str] = {}
    for doc in flux_kustomization_docs(repo_root):
        name = doc["metadata"]["name"]
        assert isinstance(name, str)
        spec = doc.get("spec")
        assert isinstance(spec, dict)
        path = spec.get("path")
        assert isinstance(path, str) and path.startswith("./")
        layers[name] = path.removeprefix("./")
    return layers
