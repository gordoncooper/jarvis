"""Env schema validator (p0a-05)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from services.hermes.core.schema.env import (
    DEFAULT_SSH_USER,
    ValidationError,
    validate_env_tree,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


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
            tier_model_map: {}
            lane_model_map: {}
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
                bench.lan:
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    return env


def _valid_cluster_vars() -> dict:
    return {
        "dns_suffix": "alpha.lan",
        "time_zone": "America/Los_Angeles",
        "code_delivery": "github",
        "build_agent": "cursor",
        "ssh_user": "ubuntu",
        "backup_path": "/var/lib/jarvis/backup",
        "backup_retention_daily": 7,
        "backup_retention_weekly": 4,
        "backup_retention_monthly": 3,
        "k3s_version": "v1.36.5+k3s1",
        "pod_cidr": "10.42.0.0/16",
        "service_cidr": "10.43.0.0/16",
    }


def test_env_01_in_repo_passes():
    resolved = validate_env_tree(REPO_ROOT)
    assert resolved["env-01"]["ssh_user"] == DEFAULT_SSH_USER


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


def test_bad_code_delivery_fails(tmp_path: Path):
    cv = _valid_cluster_vars()
    cv["code_delivery"] = "forgejo"
    _write_env(tmp_path, cluster_vars=cv)
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    msg = "\n".join(excinfo.value.messages)
    assert "code_delivery" in msg
    assert "github" in msg
