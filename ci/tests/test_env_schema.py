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
    DEFAULT_SSH_USER,
    ValidationError,
    validate_env_tree,
)
from support import REPO_ROOT, RUN_MODULE, SEALED_PLACEHOLDER, _valid_cluster_vars, _write_env, copy_sample, sample_root


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


def test_ssh_user_defaults_to_ubuntu_when_omitted(tmp_path: Path):
    _write_env(tmp_path, omit_ssh_user=True)
    resolved = validate_env_tree(tmp_path)
    assert resolved["env-test"]["ssh_user"] == DEFAULT_SSH_USER


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("backup_path", "srv/backups"),
        ("backup_path", "/srv/../etc"),
        ("local_path_data", "/var/lib/jarvis"),
        ("local_path_data", "/var/lib/jarvis/../etc"),
    ],
    ids=["backup-relative", "backup-dot-segment", "data-is-the-mount", "data-dot-segment"],
)
def test_a_data_path_that_could_land_off_its_disk_fails(tmp_path: Path, key: str, value: str):
    """Rule: backup_path is absolute with no '.' or '..' segment, and local_path_data is a
    subdirectory of /var/lib/jarvis, so neither hostPath can resolve onto another disk."""
    cv = _valid_cluster_vars()
    cv[key] = value
    _write_env(tmp_path, cluster_vars=cv)
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert any(f"cluster-vars.yaml: {key}: " in item for item in excinfo.value.messages), excinfo.value.messages


def test_jarvis_env_must_match_env_directory(tmp_path: Path):
    cv = _valid_cluster_vars()
    cv["jarvis_env"] = "env-other"
    _write_env(tmp_path, cluster_vars=cv)
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert "jarvis_env" in "\n".join(excinfo.value.messages)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("not-an-ip", "must be an IP address"),
        # The sample's pod_cidr is 10.42.0.0/16 and its service_cidr 10.43.0.0/16 (k3s's own).
        ("10.42.0.53", "must be a host address inside service_cidr"),
        ("10.43.0.0", "must be a host address inside service_cidr"),
        ("10.43.0.1", "must not be the first or tenth address of service_cidr"),
        ("10.43.0.10", "must not be the first or tenth address of service_cidr"),
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
        (["10.200.0.1/24"], "is not an IPv4 CIDR"),
        (["198.51.100.0/24"], "outside the private ranges"),
        (["10.42.0.0/24"], "overlaps pod_cidr or service_cidr"),
    ],
    ids=["host-bits", "public", "cluster-range"],
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


def test_time_zone_must_be_an_iana_zone(tmp_path: Path):
    cv = _valid_cluster_vars()
    cv["time_zone"] = "Not/AZone"
    _write_env(tmp_path, cluster_vars=cv)
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert "time_zone must be an IANA time zone" in "\n".join(excinfo.value.messages)


def test_the_apps_repository_may_be_another_owners(tmp_path: Path):
    """Rule: an env may read hosted apps from anyone's repository (the example apps beside core,
    another team's); the apps are untrusted and held by admission wherever they come from."""
    cv = _valid_cluster_vars()
    cv["apps_git_repo_url"] = "https://github.com/other-owner/example-apps"
    _write_env(tmp_path, cluster_vars=cv)
    validate_env_tree(tmp_path)


def test_ssh_root_requires_the_sealed_deploy_key(tmp_path: Path) -> None:
    """An ssh:// root reads with the deploy key, so its sealed file is required."""
    cv = _valid_cluster_vars()
    cv["flux_git_repo_url"] = "ssh://git@github.com/example-owner/example-cluster"
    env = _write_env(tmp_path, cluster_vars=cv)
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert "missing required file envs/env-test/secrets/flux-deploy-key.sops.yaml" in str(excinfo.value)
    (env / "secrets").mkdir()
    (env / "secrets" / "flux-deploy-key.sops.yaml").write_text(SEALED_PLACEHOLDER, encoding="utf-8")
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert "flux-deploy-key" not in str(excinfo.value)


_OPTIONAL_CLUSTER_KEYS = (
    "apps_git_repo_url",
    "lan_cidrs",
)


def test_optional_cluster_keys_may_all_be_omitted(tmp_path: Path):
    """An env can deploy with none of the optional keys."""
    cv = _valid_cluster_vars()
    for key in _OPTIONAL_CLUSTER_KEYS:
        del cv[key]
    _write_env(tmp_path, cluster_vars=cv)
    resolved = validate_env_tree(tmp_path)
    for key in _OPTIONAL_CLUSTER_KEYS:
        assert key not in resolved["env-test"]


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


def test_env_needs_only_cluster_vars_and_inventory(tmp_path: Path) -> None:
    """An env with no hermes.yaml or apps.yaml validates."""
    env = _write_env(tmp_path)
    assert sorted(p.name for p in (env / "values").iterdir()) == ["cluster-vars.yaml"]
    validate_env_tree(tmp_path)


def test_bootstrapped_env_requires_the_sealed_k3s_tokens(tmp_path: Path) -> None:
    """Once an env has sealed files, k3s-token.sops.yaml is required. Before that it is not."""
    env = _write_env(tmp_path)
    secrets = env / "secrets"
    secrets.mkdir(exist_ok=True)
    validate_env_tree(tmp_path)
    _copy_flux_entry(env)
    (secrets / "cert-manager-ca.sops.yaml").write_text(SEALED_PLACEHOLDER, encoding="utf-8")
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert "missing required file envs/env-test/secrets/k3s-token.sops.yaml" in excinfo.value.messages[0]
    for name in ("k3s-token",):
        (secrets / f"{name}.sops.yaml").write_text(SEALED_PLACEHOLDER, encoding="utf-8")
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert "missing required file envs/env-test/secrets/backups-restic.sops.yaml" in excinfo.value.messages[0]
    (secrets / "backups-restic.sops.yaml").write_text(SEALED_PLACEHOLDER, encoding="utf-8")
    validate_env_tree(tmp_path)


def _copy_flux_entry(env: Path) -> None:
    """env-00's flux/ tree under a test env, enough to satisfy the must-exist files."""
    shutil.copytree(
        sample_root() / "envs" / "env-00" / "flux",
        env / "flux",
        ignore=shutil.ignore_patterns("generated"),
    )
    for path in (env / "flux").rglob("*.yaml"):
        text = path.read_text(encoding="utf-8")
        text = text.replace(f"envs/{"env-00"}/", f"envs/{env.name}/")
        path.write_text(text.replace(f"jarvis_env: {"env-00"}", f"jarvis_env: {env.name}"), encoding="utf-8")
    # A new env starts core-only, as docs/deploy.md has a copy empty the example's lists. No
    # off-host copy unless a test lists it, and with no hosted app no rendered app wiring either.
    _render_core_only(env)


def _render_core_only(env: Path, *features: str) -> None:
    from jarvis.apps import values as _apps
    from jarvis.env import switches as _switches

    data = {"features": list(features), "addons": [], "apps": []}
    (env / _switches.SWITCHES_FILE).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    flux = env / "flux"
    (flux / _switches.ROOT_REL).write_text(
        _switches.root_kustomization(data, apps_generated=_apps.GENERATED_REL), encoding="utf-8"
    )
    (flux / _switches.SERVICES_REL).write_text(_switches.services(data), encoding="utf-8")
    (flux / _switches.PLATFORM_CONFIGS_REL).write_text(_switches.platform_configs(data), encoding="utf-8")


def test_env_that_lists_the_offsite_copy_requires_its_sealed_secret(tmp_path: Path) -> None:
    """Once bootstrapped, an env listing core/feature/offsite-backup-copy requires backups-offsite.sops.yaml."""
    env = _write_env(tmp_path)
    _copy_flux_entry(env)
    _render_core_only(env, "offsite-backup-copy")
    validate_env_tree(tmp_path)
    (env / "secrets").mkdir(exist_ok=True)
    for name in ("k3s-token", "cert-manager-ca", "backups-restic"):
        (env / "secrets" / f"{name}.sops.yaml").write_text(SEALED_PLACEHOLDER, encoding="utf-8")
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert "missing required file envs/env-test/secrets/backups-offsite.sops.yaml" in "\n".join(
        excinfo.value.messages
    )
    (env / "secrets" / "backups-offsite.sops.yaml").write_text(SEALED_PLACEHOLDER, encoding="utf-8")
    validate_env_tree(tmp_path)


def test_cli_requires_env() -> None:
    commands = (
        [sys.executable, "jarvis/secrets/mint.py"],
        [sys.executable, "jarvis/secrets/encrypt.py", "--name", "example"],
        [*RUN_MODULE, "jarvis.render.env"],
    )
    for cmd in commands:
        proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
        assert proc.returncode != 0, cmd
        assert "--env" in proc.stderr + proc.stdout


def test_validate_one_env_runs_sops_creation_rules(tmp_path: Path) -> None:
    """An existing env with a .sops.yaml creation-rule error fails validate_one_env."""
    copy_sample(tmp_path)
    rule = "  - path_regex: envs/env-00/secrets/.*\\.sops\\.yaml$\n    age: age1fixture\n"
    (tmp_path / ".sops.yaml").write_text("creation_rules:\n" + rule + rule)
    with pytest.raises(ValidationError) as excinfo:
        env_schema.validate_one_env(tmp_path, "env-00")
    assert "expected exactly one creation rule" in str(excinfo.value)


def _run_site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *args: str) -> tuple[int, list[str]]:
    """jarvis site with a stub ansible-playbook first on PATH; returns its exit code and the stub's argv."""
    from jarvis.node import site

    log = tmp_path / "ap.log"
    log.unlink(missing_ok=True)
    stub = tmp_path / "bin" / "ansible-playbook"
    stub.parent.mkdir(exist_ok=True)
    stub.write_text("#!/bin/sh\nprintf '%s\\n' \"$0\" \"$@\" > \"${CALL_LOG}\"\n")
    stub.chmod(0o755)
    monkeypatch.setenv("CALL_LOG", str(log))
    monkeypatch.setenv("PATH", f"{stub.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("INSTANCE", str(sample_root()))
    code = site.main(["--env", "env-00", *args])
    return code, log.read_text().splitlines() if log.exists() else []


def test_site_passes_inventories_tags_limit_and_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid env passes -i hosts then roles, -e, --tags, --limit, and --check --diff."""
    code, args = _run_site(tmp_path, monkeypatch)
    assert code == 0
    assert args[1] == "site.yml"
    dash_i = [i for i, a in enumerate(args) if a == "-i"]
    assert args[dash_i[0] + 1] == f"{sample_root()}/envs/env-00/inventory/hosts.yml"
    assert args[dash_i[1] + 1] == "../inventory/roles"
    assert f"jarvis_cluster_vars={sample_root()}/envs/env-00/values/cluster-vars.yaml" in args
    assert f"jarvis_instance_root={sample_root()}" in args
    assert "--tags" not in args and "--limit" not in args and "--check" not in args
    assert "--diff" not in args
    code, args = _run_site(tmp_path, monkeypatch, "--tags", "base-os,k3s", "--limit", "cp-1", "--check")
    assert code == 0
    assert args[args.index("--tags") + 1] == "base-os,k3s"
    assert args[args.index("--limit") + 1] == "cp-1" and "--check" in args
    assert "--diff" in args


def test_site_refuses_an_invalid_env_before_ansible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """Rule: the env schema runs first; an invalid env runs no playbook and exits 3."""
    from jarvis.node import site

    def invalid(root, name):
        raise ValidationError(["envs/env-00: broken"])

    monkeypatch.setattr(site, "validate_one_env", invalid)
    code, args = _run_site(tmp_path, monkeypatch)
    assert code == 3 and args == []
    assert "REFUSE site" in capsys.readouterr().out


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
        ("ssh://git@git.example.lan/o/r", _GITHUB_KEY, "no key for git.example.lan"),
        ("ssh://git@git.example.lan/o/r", "git.example.lan ssh-ed25519 AAAAC3Nza=", None),
        ("ssh://git@github.com/o/r", _GITHUB_KEY + "\ngitlab.com ssh-ed25519 AAAAC3NzaB", None),
        ("ssh://git@git.example.lan/o/r", None, "set flux_known_hosts"),
        ("ssh://git@github.com/o/r", None, None),
    ],
    ids=["root-host-missing", "own-forge", "several-lines", "unset-off-github", "unset-on-github"],
)
def test_flux_known_hosts_trusts_the_root_host(tmp_path: Path, root: str, known_hosts: str | None, problem: str | None):
    """Rule: flux_known_hosts holds '<host> <type> <key>' lines, and an ssh:// root's host is one of them;
    unset, it is GitHub's key, so an ssh:// root elsewhere must set it."""
    cv = _valid_cluster_vars()
    cv["flux_known_hosts"] = known_hosts
    if known_hosts is None:
        del cv["flux_known_hosts"]
    cv["flux_git_repo_url"] = root
    found = env_schema._known_hosts_problems(cv.get("flux_known_hosts"), cv["flux_git_repo_url"])
    if problem is None:
        assert found == []
    else:
        assert any(problem in item for item in found), found


def test_app_dns_cluster_ip_is_required_only_while_app_dns_is_listed(tmp_path: Path) -> None:
    """Rule: app_dns_cluster_ip is optional, and required once values/switches.yaml lists app-dns."""
    from support import set_switches

    cv = _valid_cluster_vars()
    cv.pop("app_dns_cluster_ip", None)
    env = _write_env(tmp_path, cluster_vars=dict(cv))
    (env / "secrets").mkdir(exist_ok=True)
    _copy_flux_entry(env)
    validate_env_tree(tmp_path)
    set_switches(env, features=["app-dns"])
    with pytest.raises(ValidationError) as excinfo:
        validate_env_tree(tmp_path)
    assert "app_dns_cluster_ip is required while values/switches.yaml lists app-dns" in str(excinfo.value)


def test_every_sealed_file_opens_for_exactly_the_env_recipients(tmp_path: Path) -> None:
    """Rule: a sealed file that is not SOPS-encrypted, or is encrypted to other recipients than the
    env's creation rule names, fails; jarvis secrets --update-recipients is the fix it names."""
    env = copy_sample(tmp_path) / "envs" / "env-00"
    env_schema.validate_one_env(tmp_path, "env-00")
    token = env / "secrets" / "k3s-token.sops.yaml"
    sealed = token.read_text(encoding="utf-8")
    token.write_text("stringData:\n  server_token: plain\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="k3s-token.sops.yaml: not SOPS-encrypted"):
        env_schema.validate_one_env(tmp_path, "env-00")
    doc = yaml.safe_load(sealed)
    doc["sops"]["age"] = doc["sops"]["age"][:1] + [{"recipient": "age1" + "q" * 58, "enc": "x"}]
    token.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(ValidationError, match="run jarvis secrets --update-recipients"):
        env_schema.validate_one_env(tmp_path, "env-00")
