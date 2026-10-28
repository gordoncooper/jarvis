"""Env schema validator (p0a-05)."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

import schema.env as env_schema
from schema.env import (
    DEFAULT_AGE_KEY_FILE,
    DEFAULT_SSH_USER,
    EXAMPLE_ENV,
    ValidationError,
    is_example_env,
    validate_env_tree,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _default_hosts() -> str:
    return (
        textwrap.dedent(
            """
            all:
              hosts:
                node.example:
                  node_roles: [control_plane, backup, edge]
            """
        ).strip()
        + "\n"
    )


def _write_env(
    root: Path,
    env_name: str = "env-test",
    *,
    cluster_vars: dict | None = None,
    omit_ssh_user: bool = False,
    hosts_yml: str | None = None,
) -> Path:
    env = root / "envs" / env_name
    (env / "values").mkdir(parents=True)
    (env / "inventory").mkdir(parents=True)

    cv = cluster_vars if cluster_vars is not None else _valid_cluster_vars()
    if omit_ssh_user:
        cv = {k: v for k, v in cv.items() if k != "ssh_user"}
    if cv.get("jarvis_env") in (None, EXAMPLE_ENV):
        cv["jarvis_env"] = env_name

    (env / "values" / "cluster-vars.yaml").write_text(
        yaml.safe_dump(cv, sort_keys=False), encoding="utf-8"
    )
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
        hosts_yml if hosts_yml is not None else _default_hosts(),
        encoding="utf-8",
    )
    if not is_example_env(env_name):
        _ensure_sops_creation_rule(root, env_name)
    return env


def _ensure_sops_creation_rule(root: Path, env_name: str) -> None:
    path = root / ".sops.yaml"
    rules: list[dict[str, object]] = []
    if path.is_file():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, dict) and isinstance(loaded.get("creation_rules"), list):
            rules = list(loaded["creation_rules"])
    needle = f"envs/{env_name}/secrets/"
    if any(
        isinstance(rule, dict) and needle in str(rule.get("path_regex", ""))
        for rule in rules
    ):
        return
    rules.append(
        {
            "path_regex": f"envs/{env_name}/secrets/.*\\.sops\\.yaml$",
            "age": "age1fixture",
        }
    )
    path.write_text(
        yaml.safe_dump({"creation_rules": rules}, sort_keys=False),
        encoding="utf-8",
    )


def _valid_cluster_vars() -> dict:
    cluster_vars_path = REPO_ROOT / "envs" / EXAMPLE_ENV / "values" / "cluster-vars.yaml"
    reference = yaml.safe_load(cluster_vars_path.read_text(encoding="utf-8"))
    assert isinstance(reference, dict)
    return dict(reference)


def test_sops_creation_rule_missing_for_non_example_env(tmp_path: Path):
    _write_env(tmp_path)
    (tmp_path / ".sops.yaml").write_text("creation_rules: []\n", encoding="utf-8")
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert "envs/env-test/secrets/" in "\n".join(excinfo.value.messages)


def test_sops_creation_rule_rejects_example_env(tmp_path: Path):
    _write_env(tmp_path)
    path = tmp_path / ".sops.yaml"
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    rules = loaded["creation_rules"]
    rules.append(
        {
            "path_regex": f"envs/{EXAMPLE_ENV}/secrets/.*\\.sops\\.yaml$",
            "age": "age1fixture",
        }
    )
    path.write_text(yaml.safe_dump(loaded, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert f"{EXAMPLE_ENV} must not have a creation rule" in "\n".join(
        excinfo.value.messages
    )


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


def test_age_key_file_defaults_when_omitted(tmp_path: Path):
    cv = _valid_cluster_vars()
    cv.pop("age_key_file", None)
    _write_env(tmp_path, cluster_vars=cv)
    resolved = validate_env_tree(tmp_path)
    assert resolved["env-test"]["age_key_file"] == DEFAULT_AGE_KEY_FILE


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


def test_cluster_cidrs_must_be_cidrs(tmp_path: Path):
    cv = _valid_cluster_vars()
    cv["pod_cidr"] = "not-a-pod-cidr"
    cv["service_cidr"] = "not-a-service-cidr"
    _write_env(tmp_path, cluster_vars=cv)
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    msg = "\n".join(excinfo.value.messages)
    assert "pod_cidr must be a CIDR" in msg
    assert "service_cidr must be a CIDR" in msg


def test_flux_paths_must_use_own_env_name(tmp_path: Path):
    env = _write_env(tmp_path, "env-test")
    flux = env / "flux"
    flux.mkdir()
    (flux / "flux-instance.yaml").write_text(
        'path: "envs/env-other/flux"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    msg = "\n".join(excinfo.value.messages)
    assert "envs/env-other/" in msg
    assert "env-test" in msg


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


def test_unknown_cluster_var_fails(tmp_path: Path):
    cv = _valid_cluster_vars()
    cv["extra_setting"] = "nope"
    _write_env(tmp_path, cluster_vars=cv)
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    msg = "\n".join(excinfo.value.messages)
    assert "unknown key" in msg
    assert "extra_setting" in msg


def test_llm_provider_host_must_be_bare_hostname(tmp_path: Path):
    cv = _valid_cluster_vars()
    cv["llm_provider_host"] = "https://llm.example/v1"
    _write_env(tmp_path, cluster_vars=cv)
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert "llm_provider_host" in "\n".join(excinfo.value.messages)


def test_repo_urls_must_share_owner_handle(tmp_path: Path):
    cv = _valid_cluster_vars()
    cv["apps_git_repo_url"] = "https://github.com/other-owner/example-apps"
    _write_env(tmp_path, cluster_vars=cv)
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert "owner handle" in "\n".join(excinfo.value.messages)


def test_owner_handle_must_match_across_non_example_envs(tmp_path: Path):
    first = _valid_cluster_vars()
    first["flux_git_repo_url"] = "https://github.com/owner-a/cluster"
    first["apps_git_repo_url"] = "https://github.com/owner-a/apps"
    second = _valid_cluster_vars()
    second["flux_git_repo_url"] = "https://github.com/owner-b/cluster"
    second["apps_git_repo_url"] = "https://github.com/owner-b/apps"
    _write_env(tmp_path, "env-a", cluster_vars=first)
    _write_env(tmp_path, "env-b", cluster_vars=second)
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    msg = "\n".join(excinfo.value.messages)
    assert "non-example envs" in msg
    assert "owner-a" in msg
    assert "owner-b" in msg


def _hosts(body: str) -> str:
    return textwrap.dedent(body).strip() + "\n"


def test_env_without_edge_host_fails(tmp_path: Path):
    _write_env(
        tmp_path,
        hosts_yml=_hosts(
            """
            all:
              hosts:
                node.example:
                  node_roles: [control_plane, backup]
            """
        ),
    )
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert "at least one edge host" in "\n".join(excinfo.value.messages)


def test_unknown_role_fails(tmp_path: Path):
    _write_env(
        tmp_path,
        hosts_yml=_hosts(
            """
            all:
              hosts:
                node.example:
                  node_roles: [control_plane, backup, database]
            """
        ),
    )
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    msg = "\n".join(excinfo.value.messages)
    assert "unknown role" in msg
    assert "database" in msg


@pytest.mark.parametrize(
    ("hosts_body", "role"),
    [
        (
            """
            all:
              hosts:
                node.example:
                  node_roles: [backup]
            """,
            "control_plane",
        ),
        (
            """
            all:
              hosts:
                a.example:
                  node_roles: [control_plane, backup]
                b.example:
                  node_roles: [control_plane]
            """,
            "control_plane",
        ),
        (
            """
            all:
              hosts:
                node.example:
                  node_roles: [control_plane]
            """,
            "backup",
        ),
        (
            """
            all:
              hosts:
                a.example:
                  node_roles: [control_plane, backup]
                b.example:
                  node_roles: [backup]
            """,
            "backup",
        ),
    ],
)
def test_singleton_role_must_be_exactly_one(tmp_path: Path, hosts_body: str, role: str):
    _write_env(tmp_path, hosts_yml=_hosts(hosts_body))
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert f"exactly one {role} host" in "\n".join(excinfo.value.messages)


def test_unknown_host_key_fails(tmp_path: Path):
    _write_env(
        tmp_path,
        hosts_yml=_hosts(
            """
            all:
              hosts:
                node.example:
                  node_roles: [control_plane, backup]
                  ansible_host: node.example
            """
        ),
    )
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    msg = "\n".join(excinfo.value.messages)
    assert "unknown key" in msg
    assert "ansible_host" in msg


@pytest.mark.parametrize(
    "disk_id",
    [
        "/dev/nvme0n1",
        "/dev/sda",
        "/dev/disk/by-path/pci-0000:01:00.0-nvme-1",
        "/dev/disk/by-id/example-disk-part1",
        "/dev/disk/by-id/nvme-example-part2",
    ],
)
def test_data_disk_id_must_be_whole_disk_by_id(tmp_path: Path, disk_id: str):
    _write_env(
        tmp_path,
        hosts_yml=_hosts(
            f"""
            all:
              hosts:
                node.example:
                  node_roles: [control_plane, backup]
                  data_disk:
                    id: {disk_id}
                    wipe_confirmed: false
            """
        ),
    )
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert "whole-disk /dev/disk/by-id path" in "\n".join(excinfo.value.messages)


def test_example_env_must_declare_a_new_known_role(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        env_schema,
        "_NODE_ROLES",
        env_schema._NODE_ROLES | {"drift_role"},
    )
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(REPO_ROOT)
    msg = "\n".join(excinfo.value.messages)
    assert msg.count("drift_role") == 1
    assert f"{EXAMPLE_ENV} must declare every known role: drift_role" in msg


def test_example_env_must_set_a_new_known_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        env_schema,
        "_CLUSTER_VARS_KNOWN",
        env_schema._CLUSTER_VARS_KNOWN | {"drift_key"},
    )
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(REPO_ROOT)
    msg = "\n".join(excinfo.value.messages)
    assert msg.count("drift_key") == 1
    assert f"{EXAMPLE_ENV} must set every known key: drift_key" in msg


def test_cli_requires_env() -> None:
    commands = (
        [sys.executable, "tools/bootstrap-secrets"],
        [sys.executable, "tools/seal", "--name", "example"],
        [sys.executable, "tools/render_cluster_vars_configmap.py"],
    )
    for cmd in commands:
        proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
        assert proc.returncode != 0, cmd
        assert "--env" in proc.stderr + proc.stdout


def test_make_targets_require_env() -> None:
    env = os.environ.copy()
    env.pop("ENV", None)
    commands = (
        ["make", "bootstrap-secrets"],
        ["make", "seal", "NAME=example"],
        ["make", "verify", "PHASE=p0a"],
    )
    for cmd in commands:
        proc = subprocess.run(
            cmd, cwd=REPO_ROOT, capture_output=True, text=True, env=env
        )
        assert proc.returncode != 0, cmd
        assert "ENV is required" in proc.stderr
