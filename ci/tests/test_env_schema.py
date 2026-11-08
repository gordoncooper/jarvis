"""Env schema validator."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

import jarvis.schema.env as env_schema
from jarvis.schema.env import (
    DEFAULT_AGE_KEY_FILE,
    DEFAULT_BACKUP_CHECK_SCHEDULE,
    DEFAULT_SSH_USER,
    EXAMPLE_ENV,
    ValidationError,
    validate_env_tree,
)
from support import REPO_ROOT, _valid_cluster_vars, _write_env


def test_sops_creation_rule_missing_for_non_example_env(tmp_path: Path):
    _write_env(tmp_path)
    (tmp_path / ".sops.yaml").write_text("creation_rules: []\n", encoding="utf-8")
    validate_env_tree(tmp_path, pre_host=True)
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert "envs/env-test/secrets/" in "\n".join(excinfo.value.messages)


def test_pre_host_rejects_two_sops_rules_for_one_env(tmp_path: Path):
    """A duplicate creation rule fails before a host is touched."""
    _write_env(tmp_path)
    path = tmp_path / ".sops.yaml"
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    loaded["creation_rules"].append(dict(loaded["creation_rules"][0]))
    path.write_text(yaml.safe_dump(loaded, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path, pre_host=True)
    assert "found 2" in "\n".join(excinfo.value.messages)


def test_pre_host_rejects_invalid_sops_yaml(tmp_path: Path):
    """Invalid .sops.yaml is reported in pre-host, not swallowed."""
    _write_env(tmp_path)
    (tmp_path / ".sops.yaml").write_text("{\n", encoding="utf-8")
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path, pre_host=True)
    assert "invalid YAML" in "\n".join(excinfo.value.messages)


def test_sops_creation_rule_for_missing_env_fails(tmp_path: Path):
    """A .sops.yaml rule must name an env folder that exists."""
    _write_env(tmp_path)
    path = tmp_path / ".sops.yaml"
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    loaded["creation_rules"].append(
        {
            "path_regex": "envs/env-ghost/secrets/.*\\.sops\\.yaml$",
            "age": "age1fixture",
        }
    )
    path.write_text(yaml.safe_dump(loaded, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    msg = "\n".join(excinfo.value.messages)
    assert "env-ghost" in msg
    assert "does not exist" in msg


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
        validate_env_tree(tmp_path, pre_host=True)
    assert f"{EXAMPLE_ENV} must not have a creation rule" in "\n".join(
        excinfo.value.messages
    )


def test_build_agent_is_an_unknown_key(tmp_path: Path):
    cv = _valid_cluster_vars()
    cv["build_agent"] = "cursor"
    _write_env(tmp_path, cluster_vars=cv)
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert "unknown key 'build_agent'" in "\n".join(excinfo.value.messages)


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


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("10.200.0.0/24", "lan_cidrs must be a non-empty list"),
        (["10.200.0.1/24"], "is not an IPv4 CIDR"),
        (["198.51.100.0/24"], "outside the private ranges"),
        (["10.80.0.0/24"], "overlaps pod_cidr or service_cidr"),
    ],
    ids=["not-a-list", "host-bits", "public", "cluster-range"],
)
def test_lan_cidrs_are_private_ranges_clear_of_the_cluster(tmp_path: Path, value, message: str):
    """Rule: the env's LAN ranges are private and outside its pod and service CIDRs, so a LAN
    grant inside them opens neither the internet nor the cluster."""
    cv = _valid_cluster_vars()
    cv["lan_cidrs"] = value
    _write_env(tmp_path, cluster_vars=cv)
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert message in "\n".join(excinfo.value.messages)


def test_flux_paths_must_use_own_env_name(tmp_path: Path):
    env = _write_env(tmp_path, "env-test")
    cv = yaml.safe_load((env / "values" / "cluster-vars.yaml").read_text(encoding="utf-8"))
    flux = env / "flux"
    flux.mkdir()
    (flux / "flux-instance.yaml").write_text(
        textwrap.dedent(
            f"""
            spec:
              sync:
                url: "{cv["flux_git_repo_url"]}"
                ref: "{cv["flux_git_ref"]}"
                path: "envs/env-other/flux"
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    msg = "\n".join(excinfo.value.messages)
    assert "envs/env-other/" in msg
    assert "env-test" in msg


@pytest.mark.parametrize("key", ["backup_schedule", "backup_check_schedule"])
def test_backup_schedules_must_be_five_fields(tmp_path: Path, key: str):
    cv = _valid_cluster_vars()
    cv[key] = "nightly"
    _write_env(tmp_path, cluster_vars=cv)
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert f"{key} must be a five-field cron" in "\n".join(excinfo.value.messages)


def test_backup_check_schedule_defaults_when_omitted(tmp_path: Path):
    cv = _valid_cluster_vars()
    cv.pop("backup_check_schedule", None)
    _write_env(tmp_path, cluster_vars=cv)
    resolved = validate_env_tree(tmp_path)
    assert resolved["env-test"]["backup_check_schedule"] == DEFAULT_BACKUP_CHECK_SCHEDULE


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


def test_removed_code_delivery_is_an_unknown_key(tmp_path: Path):
    cv = _valid_cluster_vars()
    cv["code_delivery"] = "github"
    _write_env(tmp_path, cluster_vars=cv)
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert "code_delivery" in "\n".join(excinfo.value.messages)


def test_unknown_cluster_var_fails(tmp_path: Path):
    cv = _valid_cluster_vars()
    cv["extra_setting"] = "nope"
    _write_env(tmp_path, cluster_vars=cv)
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    msg = "\n".join(excinfo.value.messages)
    assert "unknown key" in msg
    assert "extra_setting" in msg


@pytest.mark.parametrize("value", ["off", 0, ["true"]])
def test_chrony_nts_must_be_a_boolean(tmp_path: Path, value: object):
    cv = _valid_cluster_vars()
    cv["chrony_nts"] = value
    _write_env(tmp_path, cluster_vars=cv)
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert "chrony_nts must be true or false" in "\n".join(excinfo.value.messages)


@pytest.mark.parametrize("value", [51, -1, "10", True])
def test_node_memory_reserve_is_a_percent_from_0_to_50(tmp_path: Path, value: object):
    """Rule: the node memory reserve is a whole percent from 0 to 50."""
    cv = _valid_cluster_vars()
    cv["node_memory_reserve_percent"] = value
    _write_env(tmp_path, cluster_vars=cv)
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert "node_memory_reserve_percent must be an integer from 0 to 50" in "\n".join(excinfo.value.messages)


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


@pytest.mark.parametrize("key", ("flux_git_repo_url", "apps_git_repo_url"))
@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("https://user@github.com/example-owner/example-cluster", "userinfo"),
        ("https://github.com/example-owner/example-cluster?foo=1", "query"),
        ("https://github.com/example-owner/example-cluster?", "query"),
        ("https://github.com/example-owner/example-cluster#frag", "fragment"),
        ("https://github.com/example-owner/example-cluster#", "fragment"),
    ],
)
def test_repo_urls_reject_userinfo_query_and_fragment(
    tmp_path: Path, key: str, url: str, message: str
) -> None:
    """Repo URL keys are https owner/repo only: no userinfo, query, or fragment."""
    cv = _valid_cluster_vars()
    cv[key] = url
    if key == "apps_git_repo_url":
        cv["flux_git_repo_url"] = "https://github.com/example-owner/example-cluster"
    _write_env(tmp_path, cluster_vars=cv)
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert message in "\n".join(excinfo.value.messages)


def test_dns_suffix_must_be_a_lowercase_dns_name(tmp_path: Path) -> None:
    """dns_suffix is a lowercase DNS name; Home_Lab is not."""
    cv = _valid_cluster_vars()
    cv["dns_suffix"] = "Home_Lab"
    _write_env(tmp_path, cluster_vars=cv)
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert "dns_suffix must be a lowercase DNS name" in "\n".join(excinfo.value.messages)


def test_env_00_source_pair_matches_cluster_vars_and_drift_fails(tmp_path: Path) -> None:
    """sync.yaml renders from cluster-vars (the root and core sources' url and ref), so an edit fails."""
    shutil.copytree(REPO_ROOT / "envs" / EXAMPLE_ENV, tmp_path / "envs" / EXAMPLE_ENV)
    env_schema.validate_one_env(tmp_path, EXAMPLE_ENV)
    sync = tmp_path / "envs" / EXAMPLE_ENV / "flux" / "sync.yaml"
    sync.write_text(sync.read_text(encoding="utf-8").replace("example-core", "other-core"), encoding="utf-8")
    with pytest.raises(ValidationError) as excinfo:
        env_schema.validate_one_env(tmp_path, EXAMPLE_ENV)
    assert "sync.yaml: does not match values/cluster-vars.yaml" in str(excinfo.value)


def test_ssh_root_requires_the_sealed_deploy_key(tmp_path: Path) -> None:
    """An ssh:// root reads with the deploy key, so its sealed file is required."""
    cv = _valid_cluster_vars()
    cv["flux_git_repo_url"] = "ssh://git@github.com/example-owner/example-cluster"
    env = _write_env(tmp_path, cluster_vars=cv)
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert "missing required file envs/env-test/secrets/flux-deploy-key.sops.yaml" in str(excinfo.value)
    (env / "secrets").mkdir()
    (env / "secrets" / "flux-deploy-key.sops.yaml").write_text("fixture: 1\n", encoding="utf-8")
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert "flux-deploy-key" not in str(excinfo.value)


def test_stale_configmap_fails_full_schema_and_passes_pre_host(tmp_path: Path) -> None:
    """A copied env-00 with edited values and a stale ConfigMap passes pre-host only."""
    dest = tmp_path / "envs" / "env-00"
    shutil.copytree(REPO_ROOT / "envs" / "env-00", dest)
    cluster = dest / "values" / "cluster-vars.yaml"
    original = cluster.read_text(encoding="utf-8")
    edited = original.replace("dns_suffix: env-00.example", "dns_suffix: lab.example", 1)
    assert edited != original
    cluster.write_text(edited, encoding="utf-8")
    validate_env_tree(tmp_path, pre_host=True)
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    msg = "\n".join(excinfo.value.messages)
    assert "cluster-vars-configmap.yaml" in msg
    assert "re-render" in msg
    assert "tools/render" in msg


_OPTIONAL_CLUSTER_KEYS = (
    "apps_git_repo_url",
    "lan_cidrs",
    "flux_known_hosts",
)


@pytest.mark.parametrize("key", _OPTIONAL_CLUSTER_KEYS)
def test_optional_cluster_key_may_be_omitted(tmp_path: Path, key: str):
    """An unused key may be omitted."""
    cv = _valid_cluster_vars()
    assert key in cv
    del cv[key]
    _write_env(tmp_path, cluster_vars=cv)
    resolved = validate_env_tree(tmp_path)
    assert key not in resolved["env-test"]


def test_optional_cluster_keys_may_all_be_omitted(tmp_path: Path):
    """An env can deploy with none of the optional keys."""
    cv = _valid_cluster_vars()
    for key in _OPTIONAL_CLUSTER_KEYS:
        del cv[key]
    _write_env(tmp_path, cluster_vars=cv)
    resolved = validate_env_tree(tmp_path)
    for key in _OPTIONAL_CLUSTER_KEYS:
        assert key not in resolved["env-test"]


def test_non_example_envs_may_use_different_owners(tmp_path: Path):
    """Each env may use its own flux owner handle."""
    first = _valid_cluster_vars()
    first["flux_git_repo_url"] = "https://github.com/owner-a/cluster"
    first["apps_git_repo_url"] = "https://github.com/owner-a/apps"
    second = _valid_cluster_vars()
    second["flux_git_repo_url"] = "https://github.com/owner-b/cluster"
    second["apps_git_repo_url"] = "https://github.com/owner-b/apps"
    _write_env(tmp_path, "env-a", cluster_vars=first)
    _write_env(tmp_path, "env-b", cluster_vars=second)
    resolved = validate_env_tree(tmp_path)
    assert "env-a" in resolved
    assert "env-b" in resolved


def test_different_owners_still_pass_when_apps_url_is_omitted(tmp_path: Path):
    """The within-env owner rule does not apply across envs when apps URL is unset."""
    first = _valid_cluster_vars()
    second = _valid_cluster_vars()
    for cv, owner in ((first, "owner-a"), (second, "owner-b")):
        cv.pop("apps_git_repo_url")
        cv["flux_git_repo_url"] = f"https://github.com/{owner}/cluster"
    _write_env(tmp_path, "env-a", cluster_vars=first)
    _write_env(tmp_path, "env-b", cluster_vars=second)
    resolved = validate_env_tree(tmp_path)
    assert "env-a" in resolved
    assert "env-b" in resolved


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
    """data_disk: true is the only accepted value."""
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
    """Any value other than true, including the old mapping, fails."""
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


def _one_host(tmp_path: Path, extra: str) -> None:
    _write_env(
        tmp_path,
        hosts_yml=_hosts(
            "all:\n  hosts:\n    node.example:\n"
            f"      node_roles: [control_plane, backup, edge]\n      {extra}\n"
        ),
    )


def test_per_host_keys_valid_pass(tmp_path: Path) -> None:
    """Quoted per-host override keys are accepted as the scalar value."""
    _one_host(
        tmp_path,
        'ssh_user: "ops"\n      k3s_kubelet_kube_reserved: "cpu=50m"\n'
        '      k3s_kubelet_system_reserved: "cpu=50m"\n'
        '      k3s_kubelet_eviction_hard: "memory.available<50Mi"',
    )
    assert "env-test" in validate_env_tree(tmp_path)
    host = yaml.safe_load(
        (tmp_path / "envs/env-test/inventory/hosts.yml").read_text()
    )["all"]["hosts"]["node.example"]
    assert host["ssh_user"] == "ops"
    assert host["k3s_kubelet_kube_reserved"] == "cpu=50m"
    assert host["k3s_kubelet_system_reserved"] == "cpu=50m"
    assert host["k3s_kubelet_eviction_hard"] == "memory.available<50Mi"


def test_per_host_keys_invalid_fail(tmp_path: Path) -> None:
    """Whitespace-only per-host keys and an unknown key are refused."""
    _one_host(
        tmp_path,
        "ssh_user: '   '\n      k3s_kubelet_kube_reserved: ' '\n"
        "      k3s_kubelet_system_reserved: []\n      k3s_kubelet_eviction_hard: ''\n"
        "      gpu_reserved: cpu=1",
    )
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    prefix = "envs/env-test/inventory/hosts.yml: host node.example: "
    allowed = ", ".join(sorted(env_schema._HOST_KEYS))
    assert excinfo.value.messages == [
        f"{prefix}unknown key 'gpu_reserved' (allowed: {allowed})",
        f"{prefix}ssh_user must be a non-empty string",
        f"{prefix}k3s_kubelet_kube_reserved must be a non-empty string",
        f"{prefix}k3s_kubelet_system_reserved must be a non-empty string",
        f"{prefix}k3s_kubelet_eviction_hard must be a non-empty string",
    ]


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


_ABSENT = object()


@pytest.mark.parametrize("key", sorted(env_schema._CLUSTER_VARS_REQUIRED))
def test_required_key_set_to_null_counts_as_unset(tmp_path: Path, key: str) -> None:
    """A required key set to null fails env-schema with the same message as an absent key."""
    absent_root = tmp_path / "absent"
    null_root = tmp_path / "null"
    _write_env(absent_root)
    _write_env(null_root)

    def rewrite(root: Path, value: object) -> None:
        path = root / "envs" / "env-test" / "values" / "cluster-vars.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        if value is _ABSENT:
            data.pop(key)
        else:
            data[key] = value
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    rewrite(absent_root, _ABSENT)
    rewrite(null_root, None)

    def messages(root: Path) -> list[str]:
        with pytest.raises(ValidationError) as excinfo:
            validate_env_tree(root)
        return excinfo.value.messages

    null_messages = messages(null_root)
    assert null_messages == messages(absent_root)
    assert any(f"missing required key: {key}" in message for message in null_messages)


def test_env_needs_only_cluster_vars_and_inventory(tmp_path: Path) -> None:
    """An env with no hermes.yaml or apps.yaml validates."""
    env = _write_env(tmp_path)
    assert sorted(p.name for p in (env / "values").iterdir()) == ["cluster-vars.yaml"]
    validate_env_tree(tmp_path)


def test_storage_is_not_a_node_role(tmp_path: Path) -> None:
    """A role no manifest schedules on is not accepted."""
    _write_env(
        tmp_path,
        hosts_yml=_hosts(
            """
            all:
              hosts:
                node.example:
                  node_roles: [control_plane, backup, edge, storage]
            """
        ),
    )
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert "unknown role 'storage'" in "\n".join(excinfo.value.messages)


def test_bootstrapped_env_requires_the_sealed_k3s_tokens(tmp_path: Path) -> None:
    """Once an env has sealed files, k3s-token.sops.yaml is required. Before that it is not."""
    env = _write_env(tmp_path)
    secrets = env / "secrets"
    secrets.mkdir(exist_ok=True)
    validate_env_tree(tmp_path)
    _copy_flux_entry(env)
    (secrets / "cert-manager-ca.sops.yaml").write_text("fixture: 1\n", encoding="utf-8")
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert "missing required file envs/env-test/secrets/k3s-token.sops.yaml" in excinfo.value.messages[0]
    for name in ("k3s-token",):
        (secrets / f"{name}.sops.yaml").write_text("fixture: 1\n", encoding="utf-8")
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert "missing required file envs/env-test/secrets/backups-restic.sops.yaml" in excinfo.value.messages[0]
    (secrets / "backups-restic.sops.yaml").write_text("fixture: 1\n", encoding="utf-8")
    validate_env_tree(tmp_path)


def _copy_flux_entry(env: Path) -> None:
    """env-00's flux/ tree under a test env, enough to satisfy the must-exist files."""
    shutil.copytree(
        REPO_ROOT / "envs" / EXAMPLE_ENV / "flux",
        env / "flux",
        ignore=shutil.ignore_patterns("generated"),
    )
    for path in (env / "flux").rglob("*.yaml"):
        text = path.read_text(encoding="utf-8")
        text = text.replace(f"envs/{EXAMPLE_ENV}/", f"envs/{env.name}/")
        path.write_text(text.replace(f"jarvis_env: {EXAMPLE_ENV}", f"jarvis_env: {env.name}"), encoding="utf-8")
    # A new env starts core-only, as docs/deploy.md has a copy empty the example's lists. No
    # off-host copy unless a test lists it, and with no hosted app no rendered app wiring either.
    _render_core_only(env)


def _render_core_only(env: Path, *optional: str) -> None:
    from jarvis.lib import apps as _apps
    from jarvis.lib import switches as _switches

    data = {"features": list(optional), "addons": [], "apps": []}
    (env / _switches.SWITCHES_FILE).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    flux = env / "flux"
    (flux / _switches.ROOT_REL).write_text(
        _switches.root_kustomization(data, apps_generated=_apps.GENERATED_REL), encoding="utf-8"
    )
    (flux / _switches.SERVICES_REL).write_text(_switches.services(data), encoding="utf-8")
    (flux / _switches.PLATFORM_CONFIGS_REL).write_text(_switches.platform_configs(data), encoding="utf-8")


def test_missing_flux_entry_file_fails(tmp_path: Path) -> None:
    """A flux/ folder without sync.yaml fails, even with flux-instance.yaml present."""
    env = _write_env(tmp_path)
    _copy_flux_entry(env)
    (env / "flux" / "sync.yaml").unlink()
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path, pre_host=True)
    assert "missing required file envs/env-test/flux/sync.yaml" in "\n".join(excinfo.value.messages)


def test_env_that_lists_the_offsite_copy_requires_its_sealed_secret(tmp_path: Path) -> None:
    """Once bootstrapped, an env listing core/feature/offsite-backup-copy requires backups-offsite.sops.yaml."""
    env = _write_env(tmp_path)
    _copy_flux_entry(env)
    _render_core_only(env, "offsite-backup-copy")
    validate_env_tree(tmp_path)
    (env / "secrets").mkdir(exist_ok=True)
    for name in ("k3s-token", "cert-manager-ca", "backups-restic"):
        (env / "secrets" / f"{name}.sops.yaml").write_text("fixture: 1\n", encoding="utf-8")
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert "missing required file envs/env-test/secrets/backups-offsite.sops.yaml" in "\n".join(
        excinfo.value.messages
    )
    (env / "secrets" / "backups-offsite.sops.yaml").write_text("fixture: 1\n", encoding="utf-8")
    validate_env_tree(tmp_path)


def test_cli_requires_env() -> None:
    commands = (
        [sys.executable, "tools/bootstrap-secrets"],
        [sys.executable, "tools/encrypt", "--name", "example"],
        [sys.executable, "tools/render"],
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
        ["make", "encrypt", "NAME=example"],
        ["make", "verify"],
        ["make", "drill"],
        ["make", "drill-cleanup"],
        ["make", "site"],
    )
    for cmd in commands:
        proc = subprocess.run(
            cmd, cwd=REPO_ROOT, capture_output=True, text=True, env=env
        )
        assert proc.returncode != 0, cmd
        assert "ENV is required" in proc.stderr


def _run_site(tmp_path, *extra: str):
    log, stub = tmp_path / "ap.log", tmp_path / "ansible-playbook"
    log.unlink(missing_ok=True)
    stub.write_text("#!/bin/sh\nprintf '%s\\n' \"$0\" \"$@\" > \"${CALL_LOG}\"\n")
    stub.chmod(0o755)
    # A throwaway VENV whose python is this interpreter, so the test needs no repo .venv.
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    (venv / "bin" / "python").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    (venv / "bin" / "python").chmod(0o755)
    env = os.environ.copy()
    env["CALL_LOG"] = str(log)
    for key in ("ENV", "TAGS", "LIMIT", "CHECK", "VENV", "INSTANCE"):
        env.pop(key, None)
    proc = subprocess.run(
        ["make", "site", f"ANSIBLE_PLAYBOOK={stub}", f"VENV={venv}", *extra],
        cwd=REPO_ROOT, capture_output=True, text=True, env=env,
    )
    return proc, log.read_text() if log.exists() else ""


def test_make_site_names_a_missing_venv(tmp_path) -> None:
    """A VENV without bin/python fails with the make deps hint and runs nothing."""
    proc, recorded = _run_site(tmp_path, "ENV=env-00", f"VENV={tmp_path / 'nope'}")
    assert proc.returncode != 0 and recorded == ""
    assert "make deps" in proc.stderr and "VENV=" in proc.stderr


def test_validate_one_env_runs_sops_creation_rules(tmp_path: Path) -> None:
    """An existing env with a .sops.yaml creation-rule error fails validate_one_env."""
    shutil.copytree(REPO_ROOT / "envs" / "env-00", tmp_path / "envs" / "env-00")
    (tmp_path / ".sops.yaml").write_text(
        "creation_rules:\n  - path_regex: envs/env-00/secrets/.*\\.sops\\.yaml$\n    age: age1fixture\n"
    )
    with pytest.raises(ValidationError) as excinfo:
        env_schema.validate_one_env(tmp_path, "env-00")
    assert "creation rule" in str(excinfo.value)


def test_make_site_schema_error_skips_playbook(tmp_path) -> None:
    """A schema error skips ansible-playbook."""
    proc, recorded = _run_site(tmp_path, "ENV=missing-env")
    assert proc.returncode != 0 and recorded == "" and "unknown env" in proc.stdout + proc.stderr
    proc, recorded = _run_site(tmp_path, "ENV=../etc")
    assert proc.returncode != 0 and recorded == "" and "invalid env name" in proc.stdout + proc.stderr


def test_make_site_passes_inventories_tags(tmp_path) -> None:
    """A valid env passes -i hosts then roles, -e, TAGS, LIMIT, and CHECK."""
    proc, recorded = _run_site(tmp_path, "ENV=env-00")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    args = recorded.splitlines()
    assert args[1] == "site.yml"
    dash_i = [i for i, a in enumerate(args) if a == "-i"]
    assert args[dash_i[0] + 1] == f"{REPO_ROOT}/envs/env-00/inventory/hosts.yml"
    assert args[dash_i[1] + 1] == "../inventory/roles"
    assert f"jarvis_cluster_vars={REPO_ROOT}/envs/env-00/values/cluster-vars.yaml" in args
    assert f"jarvis_instance_root={REPO_ROOT}" in args
    assert "--tags" not in args and "--limit" not in args and "--check" not in args
    assert "--diff" not in args
    flagged, recorded = _run_site(
        tmp_path, "ENV=env-00", "TAGS=base-os,k3s", "LIMIT=cp-1", "CHECK=1"
    )
    assert flagged.returncode == 0, flagged.stdout + flagged.stderr
    args = recorded.splitlines()
    assert args[args.index("--tags") + 1] == "base-os,k3s"
    assert args[args.index("--limit") + 1] == "cp-1" and "--check" in args
    assert "--diff" in args
    bad, recorded = _run_site(tmp_path, "ENV=env-00", "CHECK=true")
    assert bad.returncode != 0 and recorded == "" and "CHECK must be empty or 1" in bad.stderr


def test_envs_limits_the_env_checks_to_the_named_envs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Rule: ENVS names the envs validate_env_tree checks, so an instance CI leg checks one env
    against its own core pin; .sops.yaml rules still see every env folder."""
    _write_env(tmp_path, "env-test")
    _write_env(tmp_path, "env-broken")
    (tmp_path / "envs" / "env-broken" / "values" / "cluster-vars.yaml").write_text("not: valid\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        validate_env_tree(tmp_path)
    monkeypatch.setenv(env_schema.ENVS_VAR, "env-test")
    assert set(validate_env_tree(tmp_path)) == {"env-test"}
    monkeypatch.setenv(env_schema.ENVS_VAR, "env-broken")
    with pytest.raises(ValidationError):
        validate_env_tree(tmp_path)


_GITHUB_KEY = "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"


@pytest.mark.parametrize(
    ("root", "known_hosts", "problem"),
    [
        ("https://github.com/o/r", "github.com AAAA", "lines must be"),
        ("ssh://git@git.example.lan/o/r", _GITHUB_KEY, "no key for git.example.lan"),
        ("ssh://git@git.example.lan/o/r", "git.example.lan ssh-ed25519 AAAAC3Nza=", None),
        ("ssh://git@github.com/o/r", _GITHUB_KEY + "\ngitlab.com ssh-ed25519 AAAAC3NzaB", None),
        ("ssh://git@git.example.lan/o/r", None, "set flux_known_hosts"),
        ("ssh://git@github.com/o/r", None, None),
    ],
    ids=["malformed", "root-host-missing", "own-forge", "several-lines", "unset-off-github", "unset-on-github"],
)
def test_flux_known_hosts_trusts_the_root_host(tmp_path: Path, root: str, known_hosts: str | None, problem: str | None):
    """Rule: flux_known_hosts holds '<host> <type> <key>' lines, and an ssh:// root's host is one of them;
    unset, it is GitHub's key, so an ssh:// root elsewhere must set it."""
    cv = _valid_cluster_vars()
    cv["flux_known_hosts"] = known_hosts
    if known_hosts is None:
        del cv["flux_known_hosts"]
    cv["flux_git_repo_url"] = root
    errors = env_schema._validate_cluster_vars(cv, "cluster-vars.yaml", "env-test")[0]
    found = [item for item in errors if "flux_known_hosts" in item]
    if problem is None:
        assert found == []
    else:
        assert any(problem in item for item in found), errors


@pytest.mark.parametrize(
    "post_build",
    [None, {"substitute": {"jarvis_env": "env-00"}}, {"substitute": {"jarvis_env": "env-test", "extra": "x"}}],
    ids=["missing", "other-env", "extra-key"],
)
def test_root_kustomization_sets_only_its_own_jarvis_env(tmp_path: Path, post_build: dict | None) -> None:
    """Rule: the root Kustomization sets postBuild.substitute to jarvis_env: <env> and nothing else;
    sync.yaml is the render, so any other value fails."""
    env = _write_env(tmp_path)
    (env / "secrets").mkdir(exist_ok=True)
    _copy_flux_entry(env)
    validate_env_tree(tmp_path)
    sync = env / "flux" / "sync.yaml"
    docs = list(yaml.safe_load_all(sync.read_text(encoding="utf-8")))
    root = next(doc for doc in docs if doc and doc["kind"] == "Kustomization")
    if post_build is None:
        root["spec"].pop("postBuild")
    else:
        root["spec"]["postBuild"] = post_build
    sync.write_text(yaml.safe_dump_all(docs, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert "envs/env-test/flux/sync.yaml: does not match values/cluster-vars.yaml" in "\n".join(excinfo.value.messages)


def test_k3s_release_is_cores_unless_the_env_sets_both_keys(tmp_path: Path) -> None:
    """Rule: an env runs core's k3s release; k3s_version and k3s_sha256 override it together."""
    from jarvis.lib import k3s

    cv = _valid_cluster_vars()
    cv.pop("k3s_version", None)
    cv.pop("k3s_sha256", None)
    assert k3s.release(cv) == k3s.core_release()
    _write_env(tmp_path, cluster_vars=dict(cv))
    validate_env_tree(tmp_path)
    shutil.rmtree(tmp_path / "envs")
    _write_env(tmp_path, cluster_vars={**cv, "k3s_version": "v1.36.5+k3s1"})
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert "set both or neither" in str(excinfo.value)
    override = {**cv, "k3s_version": "v1.35.9+k3s1", "k3s_sha256": "0" * 64}
    assert k3s.release(override) == ("v1.35.9+k3s1", "0" * 64)


def test_app_dns_cluster_ip_is_required_only_while_app_dns_is_listed(tmp_path: Path) -> None:
    """Rule: app_dns_cluster_ip is optional, and required once values/switches.yaml lists app-dns."""
    from support import set_switches

    cv = _valid_cluster_vars()
    cv.pop("app_dns_cluster_ip", None)
    env = _write_env(tmp_path, cluster_vars=dict(cv))
    (env / "secrets").mkdir(exist_ok=True)
    _copy_flux_entry(env)
    validate_env_tree(tmp_path)
    set_switches(env, optional=["app-dns"])
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert "app_dns_cluster_ip is required while values/switches.yaml lists app-dns" in str(excinfo.value)
