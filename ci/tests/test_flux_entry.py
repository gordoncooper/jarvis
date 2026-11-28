"""Flux Git entry: cluster-vars, sealed secrets, the secret scan, and what each Flux path applies."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from ansible_role_proof import load_cluster_vars

from jarvis.checks.flux_build import env_tree
from jarvis.env.validate import CHECKS
from jarvis.render.cluster_vars import CLUSTER_VARS_PUBLISHED, cluster_vars_configmap
from support import ExampleCopy, git, sample_root


def test_cluster_vars_configmap_publishes_only_the_allow_list() -> None:
    """Rule: the live ConfigMap is the allow-list, a list value JSON-encoded. Host-only keys stay in the values file."""
    raw = load_cluster_vars(sample_root("app-dns"))
    data = cluster_vars_configmap(raw)["data"]
    listed = cluster_vars_configmap({**raw, "dns_suffix": ["a.example", "b.example"]})["data"]["dns_suffix"]
    assert set(data) == set(CLUSTER_VARS_PUBLISHED) and {"ssh_user", "ntp_pools"} <= set(raw) - set(data)
    assert (data["jarvis_env"], listed) == (raw["jarvis_env"], '["a.example","b.example"]')


def test_sops_secret_leaves_stay_encrypted(example_copy: ExampleCopy) -> None:
    """Rule: every sealed file's data and stringData leaves are SOPS ciphertext, an add-on's folder included."""
    secrets = example_copy.env_dir / "secrets"
    paths = sorted(secrets.rglob("*.sops.yaml"))
    assert any(path.parent != secrets for path in paths), paths
    for path in paths:
        docs = [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if doc is not None]
        assert docs, path
        for doc in docs:
            leaves = [leaf for key in ("data", "stringData") if key in doc for leaf in doc[key].values()]
            assert isinstance(doc.get("sops"), dict) and leaves and all(str(leaf).startswith("ENC[") for leaf in leaves), path


def test_secret_scan_rejects_planted_age_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule: jarvis's secret scan reads the instance's git history: it passes a clean one, and an age key
    committed there fails it, even once deleted."""
    monkeypatch.setenv("NO_COLOR", "1")
    git(tmp_path, "init", "-q")
    git(tmp_path, "commit", "-q", "--allow-empty", "-m", "clean")
    clean = CHECKS["secret-scan"](tmp_path)
    subprocess.run(["age-keygen", "-o", str(tmp_path / "key.txt")], check=True, capture_output=True)
    git(tmp_path, "add", "key.txt")
    git(tmp_path, "commit", "-q", "-m", "plant")
    git(tmp_path, "rm", "-q", "key.txt")
    git(tmp_path, "commit", "-q", "-m", "remove")
    found = CHECKS["secret-scan"](tmp_path)
    # gitleaks's lines start with the time; its count varies, as its generic rule flags the public key line only at times.
    assert clean == [] and found and found[-1].split(" ", 1)[1].startswith("WRN leaks found: "), found


def test_no_object_is_applied_by_two_flux_paths(example_copy: ExampleCopy) -> None:
    """Rule: the full env's Flux paths build, those a built path applies (the add-on's) included, and no
    object is applied by two of them, the root included (they would fight)."""
    tree = env_tree(example_copy.root, f"envs/{example_copy.name}/flux")
    named = [(path.kustomization["metadata"]["name"], path) for path in tree.paths]
    assert (tree.error, {name: path.error for name, path in named if path.error}) == (None, {})
    assert any(path.parent and path.docs for _, path in named), [name for name, _ in named]
    seen: dict[tuple[str, str, str], str] = {}
    for name, docs in [("flux-system", tree.env_docs), *((name, path.docs) for name, path in named)]:
        for doc in docs:
            meta = doc.get("metadata") or {}
            ident = (str(doc.get("kind")), str(meta.get("namespace") or ""), str(meta.get("name")))
            assert ident not in seen, f"{ident} applied by both {seen[ident]} and {name}"
            seen[ident] = name
