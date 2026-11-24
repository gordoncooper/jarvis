"""Flux Git entry: cluster-vars, sealed secrets, and what each Flux path applies."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from ansible_role_proof import (
    load_cluster_vars,
)

from jarvis.checks.flux_build import env_tree
from jarvis.render.cluster_vars import (
    CLUSTER_VARS_PUBLISHED,
    cluster_vars_configmap,
)
from support import ExampleCopy, real_and_copy_envs, sample_root


def test_cluster_vars_configmap_publishes_only_the_allow_list() -> None:
    """The live ConfigMap is the allow-list. Host-only keys stay in the values file."""
    raw = load_cluster_vars(sample_root("app-dns"))
    data = cluster_vars_configmap(raw)["data"]
    assert isinstance(data, dict)
    assert set(data) == set(CLUSTER_VARS_PUBLISHED)
    assert data["jarvis_env"] == raw["jarvis_env"]
    assert "ssh_user" not in data
    assert "ntp_pools" not in data


def test_cluster_vars_configmap_flattens_ntp_pools() -> None:
    """A list value is JSON-encoded. ntp_pools is host-only, so the proof uses a published key."""
    raw = load_cluster_vars()
    pools = raw["ntp_pools"]
    assert isinstance(pools, list) and pools
    published = cluster_vars_configmap(raw)["data"]
    assert "ntp_pools" not in published
    data = cluster_vars_configmap({**raw, "dns_suffix": pools})["data"]
    assert data["dns_suffix"] == json.dumps(pools, separators=(",", ":"))


def _assert_sops_leaf(path: Path, section: str, node: object) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            _assert_sops_leaf(path, f"{section}.{key}", value)
        return
    if isinstance(node, list):
        for index, value in enumerate(node):
            _assert_sops_leaf(path, f"{section}[{index}]", value)
        return
    if isinstance(node, str) and node.startswith("ENC["):
        return
    raise AssertionError(f"{path}: {section} is not an ENC value")


def test_sops_secret_leaves_stay_encrypted(example_copy: ExampleCopy) -> None:
    """SOPS secret leaves stay encrypted in a bootstrapped copy, an add-on's folder included."""
    # An add-on's Secrets sit in its namespace's folder.
    paths = sorted((example_copy.env_dir / "secrets").rglob("*.sops.yaml"))
    assert any(path.parent != example_copy.env_dir / "secrets" for path in paths)
    assert paths
    for path in paths:
        docs = [
            doc
            for doc in yaml.safe_load_all(path.read_text(encoding="utf-8"))
            if doc is not None
        ]
        assert docs, path
        for doc in docs:
            assert isinstance(doc, dict), path
            assert isinstance(doc.get("sops"), dict), path
            sections = [key for key in ("data", "stringData") if key in doc]
            assert sections, path
            for key in sections:
                _assert_sops_leaf(path, key, doc[key])


def test_secret_scan_rejects_planted_age_key(tmp_path: Path) -> None:
    gitleaks = shutil.which("gitleaks")
    age_keygen = shutil.which("age-keygen")
    if gitleaks is None or age_keygen is None:
        pytest.fail("gitleaks and age-keygen are required (run make deps)")
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        [age_keygen, "-o", str(tmp_path / "key.txt")],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "add", "key.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=fixture@example.com",
            "-c",
            "user.name=fixture",
            "commit",
            "-m",
            "plant",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    proc = subprocess.run(
        [str(gitleaks), "detect", "--source", str(tmp_path), "--redact"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0


def test_no_object_is_applied_by_two_flux_paths(example_copy: ExampleCopy) -> None:
    """Each full env's Flux paths build, and no object is applied by two of them (they would fight)."""
    for root, env in real_and_copy_envs(example_copy):
        tree = env_tree(root, f"envs/{env}/flux")
        assert tree.error is None, tree.error
        seen: dict[tuple[str, str, str], str] = {}
        for path in tree.paths:
            name = path.kustomization["metadata"]["name"]
            assert path.error is None, f"{name}: {path.error}"
            for doc in path.docs:
                meta = doc.get("metadata") or {}
                ident = (str(doc.get("kind")), str(meta.get("namespace") or ""), str(meta.get("name")))
                assert ident not in seen, f"{ident} applied by both {seen[ident]} and {name}"
                seen[ident] = name
