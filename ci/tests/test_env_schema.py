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


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("not-an-ip", "must be an IP address"),
        ("10.80.0.53", "must be inside service_cidr"),
        ("10.81.0.0", "must be a host address inside service_cidr"),
        ("10.81.0.1", "must not be the first address of service_cidr"),
        ("10.81.0.10", "must not be the tenth address of service_cidr"),
    ],
)
def test_app_dns_cluster_ip_must_be_a_service_host_address(tmp_path: Path, value: str, message: str):
    cv = _valid_cluster_vars()
    cv["app_dns_cluster_ip"] = value
    _write_env(tmp_path, cluster_vars=cv)
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert message in "\n".join(excinfo.value.messages)


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


def test_backup_schedule_must_be_five_fields(tmp_path: Path):
    cv = _valid_cluster_vars()
    cv["backup_schedule"] = "nightly"
    _write_env(tmp_path, cluster_vars=cv)
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert "backup_schedule must be a five-field cron" in "\n".join(excinfo.value.messages)


def test_time_zone_and_k3s_version_must_match_their_shapes(tmp_path: Path):
    cv = _valid_cluster_vars()
    cv["time_zone"] = "Not/AZone"
    cv["k3s_version"] = "v1.2.3"
    _write_env(tmp_path, cluster_vars=cv)
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    msg = "\n".join(excinfo.value.messages)
    assert "time_zone must be an IANA time zone" in msg
    assert "k3s_version must be a k3s version string" in msg


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


def test_apps_git_repo_url_must_be_https_when_set(tmp_path: Path):
    cv = _valid_cluster_vars()
    cv["apps_git_repo_url"] = "http://github.com/example-owner/example-apps"
    _write_env(tmp_path, cluster_vars=cv)
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert "apps_git_repo_url must be an https URL" in "\n".join(excinfo.value.messages)


_OPTIONAL_CLUSTER_KEYS = (
    "code_delivery",
    "build_agent",
    "apps_git_repo_url",
    "llm_provider_host",
)


@pytest.mark.parametrize("key", _OPTIONAL_CLUSTER_KEYS)
def test_optional_cluster_key_may_be_omitted(tmp_path: Path, key: str):
    """D45: an unused key may be omitted."""
    cv = _valid_cluster_vars()
    assert key in cv
    del cv[key]
    _write_env(tmp_path, cluster_vars=cv)
    resolved = validate_env_tree(tmp_path)
    assert key not in resolved["env-test"]


def test_optional_cluster_keys_may_all_be_omitted(tmp_path: Path):
    """An env can deploy with no AI vendor, apps repo, or LLM host (D45)."""
    cv = _valid_cluster_vars()
    for key in _OPTIONAL_CLUSTER_KEYS:
        del cv[key]
    _write_env(tmp_path, cluster_vars=cv)
    resolved = validate_env_tree(tmp_path)
    for key in _OPTIONAL_CLUSTER_KEYS:
        assert key not in resolved["env-test"]


def test_owner_comes_from_flux_when_apps_url_is_omitted(tmp_path: Path):
    """Two envs with different flux owners still fail when apps URL is unset."""
    first = _valid_cluster_vars()
    second = _valid_cluster_vars()
    for cv, owner in ((first, "owner-a"), (second, "owner-b")):
        cv.pop("apps_git_repo_url")
        cv["flux_git_repo_url"] = f"https://github.com/{owner}/cluster"
    _write_env(tmp_path, "env-a", cluster_vars=first)
    _write_env(tmp_path, "env-b", cluster_vars=second)
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    msg = "\n".join(excinfo.value.messages)
    assert "non-example envs" in msg
    assert "owner-a" in msg
    assert "owner-b" in msg


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
    "local_path",
    ["/var/lib/jarvis", "/var/lib/jarvis/", "/var/lib/other", "relative"],
)
def test_local_path_data_must_be_a_subdirectory(tmp_path: Path, local_path: str) -> None:
    """local_path_data is a directory under the data mount, not the mount itself."""
    cluster = _valid_cluster_vars()
    cluster["local_path_data"] = local_path
    _write_env(tmp_path, cluster_vars=cluster)
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    message = "\n".join(excinfo.value.messages)
    if local_path.startswith("/"):
        assert "subdirectory of /var/lib/jarvis" in message
    else:
        assert "absolute path" in message


@pytest.mark.parametrize("backup_path", ["backup", "var/lib/jarvis/backup"])
def test_backup_path_must_be_absolute(tmp_path: Path, backup_path: str) -> None:
    """A relative backup_path would build a hostPath of the wrong shape."""
    cluster = _valid_cluster_vars()
    cluster["backup_path"] = backup_path
    _write_env(tmp_path, cluster_vars=cluster)
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert "backup_path must be an absolute path" in "\n".join(excinfo.value.messages)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("local_path_data", "/var/lib/jarvis/../etc"),
        ("local_path_data", "/var/lib/jarvis/./local-path"),
        ("backup_path", "/var/lib/jarvis/../etc"),
        ("backup_path", "/var/lib/jarvis/./backup"),
    ],
)
def test_data_paths_reject_dot_segments(tmp_path: Path, key: str, value: str) -> None:
    """'.' and '..' path segments are refused in local_path_data and backup_path."""
    cluster = _valid_cluster_vars()
    cluster[key] = value
    _write_env(tmp_path, cluster_vars=cluster)
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    message = "\n".join(excinfo.value.messages)
    assert "'.'" in message or "'..'" in message or "subdirectory" in message


def test_data_disk_true_passes(tmp_path: Path) -> None:
    """data_disk: true is the only accepted value (p0a-11a)."""
    _write_env(
        tmp_path,
        hosts_yml=_hosts(
            """
            all:
              hosts:
                node.example:
                  node_roles: [control_plane, backup, edge]
                  data_disk: true
            """
        ),
    )
    resolved = validate_env_tree(tmp_path)
    assert "env-test" in resolved


@pytest.mark.parametrize(
    "disk_yml",
    [
        "false",
        '"true"',
        "1",
        "{id: /dev/disk/by-id/example, wipe_confirmed: true}",
    ],
)
def test_data_disk_other_than_true_fails(tmp_path: Path, disk_yml: str) -> None:
    """Any value other than true, including the old mapping, fails (p0a-11a)."""
    _write_env(
        tmp_path,
        hosts_yml=_hosts(
            f"""
            all:
              hosts:
                node.example:
                  node_roles: [control_plane, backup, edge]
                  data_disk: {disk_yml}
            """
        ),
    )
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert "data_disk must be true" in "\n".join(excinfo.value.messages)


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
        [sys.executable, "tools/lib/render_cluster_vars_configmap.py"],
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
        ["make", "verify"],
        ["make", "drill"],
        ["make", "drill-cleanup"],
    )
    for cmd in commands:
        proc = subprocess.run(
            cmd, cwd=REPO_ROOT, capture_output=True, text=True, env=env
        )
        assert proc.returncode != 0, cmd
        assert "ENV is required" in proc.stderr
