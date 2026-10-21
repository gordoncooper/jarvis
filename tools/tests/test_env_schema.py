"""Env schema validator (p0a-05)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

import services.hermes.core.schema.env as env_schema

from services.hermes.core.schema.env import (
    DEFAULT_SSH_USER,
    ValidationError,
    known_cluster_vars_keys,
    validate_env_tree,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _deployable_env_name(root: Path = REPO_ROOT) -> str:
    names = sorted(
        p.name
        for p in (root / "envs").iterdir()
        if p.is_dir() and p.name != "env-00"
    )
    assert len(names) == 1
    return names[0]


def _write_env(
    root: Path,
    env_name: str = "env-test",
    *,
    cluster_vars: dict | None = None,
    omit_ssh_user: bool = False,
) -> Path:
    env = root / "envs" / env_name
    (env / "values").mkdir(parents=True)
    (env / "inventory").mkdir(parents=True)

    cv = cluster_vars if cluster_vars is not None else _valid_cluster_vars()
    if omit_ssh_user:
        cv = {k: v for k, v in cv.items() if k != "ssh_user"}

    (env / "values" / "cluster-vars.yaml").write_text(yaml.safe_dump(cv, sort_keys=False), encoding="utf-8")
    (env / "values" / "hermes.yaml").write_text(
        textwrap.dedent(
            """
            bots: []
            cron: {}
            bot_tier_map:
              concierge: mid
            tier_model_map:
              mid: example-model
            lane_model_map:
              builder:
                model: example-builder
                family: composer
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    (env / "values" / "apps.yaml").write_text(
        "app_hosting_enabled: false\napps: []\n",
        encoding="utf-8",
    )
    (env / "inventory" / "hosts.yml").write_text(
        textwrap.dedent(
            """
            all:
              hosts:
                node.example:
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    return env


def _valid_cluster_vars() -> dict:
    cluster_vars_path = (
        REPO_ROOT / "envs" / _deployable_env_name() / "values" / "cluster-vars.yaml"
    )
    reference = yaml.safe_load(cluster_vars_path.read_text(encoding="utf-8"))
    return {
        "jarvis_env": "env-test",
        "dns_suffix": reference["dns_suffix"],
        "time_zone": "America/Los_Angeles",
        "code_delivery": "github",
        "build_agent": "cursor",
        "ssh_user": "ubuntu",
        "backup_path": "/var/lib/jarvis/backup",
        "backup_retention_daily": 7,
        "backup_retention_weekly": 4,
        "backup_retention_monthly": 3,
        "k3s_version": "v1.36.5+k3s1",
        "k3s_sha256": "d73847bcd3c5fccef0115b372e2f9a91f3032dc84bbf71518a4617565294d313",
        "pod_cidr": "10.42.0.0/16",
        "service_cidr": "10.43.0.0/16",
        "local_path_data": "/var/lib/jarvis/local-path",
        "ntp_pools": reference["ntp_pools"],
        "k3s_kubelet_kube_reserved": reference["k3s_kubelet_kube_reserved"],
        "k3s_kubelet_system_reserved": reference["k3s_kubelet_system_reserved"],
        "k3s_kubelet_eviction_hard": reference["k3s_kubelet_eviction_hard"],
        "flux_git_repo_url": reference["flux_git_repo_url"],
        "apps_git_repo_url": reference["apps_git_repo_url"],
        "flux_git_ref": reference["flux_git_ref"],
        "llm_provider_host": reference["llm_provider_host"],
    }


def test_env_tree_in_repo_passes():
    resolved = validate_env_tree(REPO_ROOT)
    assert resolved[_deployable_env_name()]["ssh_user"] == DEFAULT_SSH_USER
    assert "env-00" in resolved


def test_env_00_sets_every_known_cluster_vars_key():
    path = REPO_ROOT / "envs/env-00/values/cluster-vars.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    assert set(raw.keys()) == set(known_cluster_vars_keys())


def test_unknown_cluster_vars_key_fails(tmp_path: Path):
    cv = _valid_cluster_vars()
    cv["experimental_flag"] = True
    _write_env(tmp_path, cluster_vars=cv)
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert "unknown key" in "\n".join(excinfo.value.messages)


def test_llm_provider_host_rejects_url(tmp_path: Path):
    cv = _valid_cluster_vars()
    cv["llm_provider_host"] = "https://api.x.ai/v1"
    _write_env(tmp_path, cluster_vars=cv)
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    msg = "\n".join(excinfo.value.messages)
    assert "llm_provider_host" in msg


def test_apps_and_flux_owner_mismatch_fails(tmp_path: Path):
    cv = _valid_cluster_vars()
    cv["apps_git_repo_url"] = "https://github.com/other-owner/jarvis-apps"
    _write_env(tmp_path, cluster_vars=cv)
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    msg = "\n".join(excinfo.value.messages)
    assert "same GitHub owner" in msg


def test_schema_drift_fails_when_env_00_missing_new_key(monkeypatch):
    extended_known = env_schema._CLUSTER_VARS_KNOWN | frozenset({"synthetic_fixture_key"})
    monkeypatch.setattr(env_schema, "_CLUSTER_VARS_KNOWN", extended_known)
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(REPO_ROOT)
    assert "env-00 must set every known cluster-vars key" in "\n".join(excinfo.value.messages)


def test_bad_build_agent_fails(tmp_path: Path):
    cv = _valid_cluster_vars()
    cv["build_agent"] = "grok-build"
    _write_env(tmp_path, cluster_vars=cv)
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    msg = "\n".join(excinfo.value.messages)
    assert "build_agent" in msg
    assert "cursor" in msg


def test_ssh_user_defaults_to_ubuntu_when_omitted(tmp_path: Path):
    _write_env(tmp_path, omit_ssh_user=True)
    resolved = validate_env_tree(tmp_path)
    assert resolved["env-test"]["ssh_user"] == DEFAULT_SSH_USER


def test_ssh_user_explicit_value_kept(tmp_path: Path):
    cv = _valid_cluster_vars()
    cv["ssh_user"] = "deploy"
    _write_env(tmp_path, cluster_vars=cv)
    resolved = validate_env_tree(tmp_path)
    assert resolved["env-test"]["ssh_user"] == "deploy"


def test_jarvis_env_must_match_env_directory(tmp_path: Path):
    cv = _valid_cluster_vars()
    cv["jarvis_env"] = "env-other"
    _write_env(tmp_path, cluster_vars=cv)
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert "jarvis_env" in "\n".join(excinfo.value.messages)


def test_bad_flux_git_repo_url_fails(tmp_path: Path):
    cv = _valid_cluster_vars()
    cv["flux_git_repo_url"] = "http://example.com/repo.git"
    _write_env(tmp_path, cluster_vars=cv)
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert "flux_git_repo_url" in "\n".join(excinfo.value.messages)


def test_bad_k3s_sha256_fails(tmp_path: Path):
    cv = _valid_cluster_vars()
    cv["k3s_sha256"] = "not-a-checksum"
    _write_env(tmp_path, cluster_vars=cv)
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert "k3s_sha256" in "\n".join(excinfo.value.messages)


def test_bad_code_delivery_fails(tmp_path: Path):
    cv = _valid_cluster_vars()
    cv["code_delivery"] = "forgejo"
    _write_env(tmp_path, cluster_vars=cv)
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    msg = "\n".join(excinfo.value.messages)
    assert "code_delivery" in msg
    assert "github" in msg
