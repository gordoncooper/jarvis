"""Env schema validator: the rules that span an env's fields or files, and .sops.yaml's."""

from __future__ import annotations

import json
import os
import runpy
import sys
from pathlib import Path

import pytest
import yaml

import jarvis.schema.env as env_schema
from jarvis.env.switches import FEATURE_COMPONENTS
from jarvis.node import site
from jarvis.render import env as render
from jarvis.schema import values as _values
from jarvis.schema.env import ValidationError, validate_env_tree, validate_one_env
from jarvis.secrets import age, encrypt, mint
from support import FIXTURE_RECIPIENT, SEALED_PLACEHOLDER, _valid_cluster_vars, _write_env, copy_sample, set_switches

CLUSTER_VARS = "envs/env-test/values/cluster-vars.yaml"
DEFAULTS = _values.defaults(_values.CLUSTER_VARS)
OPTIONAL_KEYS = ("apps_git_repo_url", "lan_cidrs")
LAN_ROOT = "ssh://git@git.example.lan/o/r"
NO_LAN_KEY = "flux_known_hosts holds no key for git.example.lan, the host of flux_git_repo_url"
RULE = age.creation_rule("env-test", [FIXTURE_RECIPIENT])
GHOST = age.creation_rule("env-ghost", [FIXTURE_RECIPIENT])
NOT_JARVIS = "is not the rule jarvis writes for an env folder under envs/; remove it"


def _problems(root: Path, env: str | None = None) -> list[str]:
    """What validate_env_tree refuses in root, or validate_one_env (jarvis site's) for env; [] when it passes."""
    try:
        validate_env_tree(root) if env is None else validate_one_env(root, env)
    except ValidationError as error:
        return error.messages
    return []


def _yaml_error(text: str) -> str:
    try:
        yaml.safe_load(text)
    except yaml.YAMLError as error:
        return str(error)
    raise AssertionError(f"{text!r} is valid YAML")


def _env_with(root: Path, change: dict) -> dict:
    """A valid env-test, its App DNS address set, with change made to its cluster-vars (None leaves a key out) and
    an ssh:// root's deploy key sealed. Returns the cluster-vars written."""
    base = {**_valid_cluster_vars(), "app_dns_cluster_ip": "10.43.0.53"}
    cluster_vars = {key: value for key, value in {**base, **change}.items() if value is not None}
    env = _write_env(root, cluster_vars=cluster_vars)
    if cluster_vars["flux_git_repo_url"].startswith("ssh://"):
        (env / "secrets" / env_schema.DEPLOY_KEY_FILE).write_text(SEALED_PLACEHOLDER, encoding="utf-8")
    return cluster_vars


def _no_match(key: str, value: str) -> str:
    return f"{key}: {value!r} does not match {_values.schema(_values.CLUSTER_VARS)['properties'][key]['pattern']!r}"


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        pytest.param({"backup_path": "srv/backups"}, _no_match("backup_path", "srv/backups"), id="backup-relative"),
        pytest.param({"backup_path": "/srv/../etc"}, _no_match("backup_path", "/srv/../etc"), id="backup-dot-segment"),
        pytest.param({"local_path_data": "/var/lib/jarvis"}, _no_match("local_path_data", "/var/lib/jarvis"),
                     id="data-is-the-mount"),
        pytest.param({"local_path_data": "/var/lib/jarvis/../etc"}, _no_match("local_path_data", "/var/lib/jarvis/../etc"),
                     id="data-dot-segment"),
        pytest.param({"jarvis_env": "env-other"}, "jarvis_env must match the env directory name (env-test)",
                     id="jarvis-env-not-the-folder"),
        pytest.param({"time_zone": "Not/AZone"}, "time_zone must be an IANA time zone", id="time-zone-unknown"),
        pytest.param({"pod_cidr": "not-a-pod-cidr"}, "pod_cidr must be a CIDR", id="pod-cidr-not-a-cidr"),
        pytest.param({"service_cidr": "not-a-service-cidr"}, "service_cidr must be a CIDR", id="service-cidr-not-a-cidr"),
        pytest.param({"app_dns_cluster_ip": "not-an-ip"}, "app_dns_cluster_ip must be an IP address", id="app-dns-ip-not-an-ip"),
        # The sample's pod_cidr is 10.42.0.0/16 and its service_cidr 10.43.0.0/16 (k3s's own).
        pytest.param({"app_dns_cluster_ip": "10.42.0.53"}, "app_dns_cluster_ip must be a host address inside service_cidr",
                     id="app-dns-ip-in-pod-cidr"),
        pytest.param({"app_dns_cluster_ip": "10.43.0.0"}, "app_dns_cluster_ip must be a host address inside service_cidr",
                     id="app-dns-ip-network-address"),
        pytest.param({"app_dns_cluster_ip": "10.43.0.1"},
                     "app_dns_cluster_ip must not be the first or tenth address of service_cidr", id="app-dns-ip-api"),
        pytest.param({"app_dns_cluster_ip": "10.43.0.10"},
                     "app_dns_cluster_ip must not be the first or tenth address of service_cidr", id="app-dns-ip-cluster-dns"),
        pytest.param({"lan_cidrs": ["10.200.0.1/24"]}, "lan_cidrs: '10.200.0.1/24' is not an IPv4 CIDR", id="lan-host-bits"),
        pytest.param({"lan_cidrs": ["198.51.100.0/24"]},
                     "lan_cidrs: 198.51.100.0/24 is outside the private ranges 10/8, 172.16/12 and 192.168/16", id="lan-public"),
        pytest.param({"lan_cidrs": ["10.42.0.0/24"]}, "lan_cidrs: 10.42.0.0/24 overlaps pod_cidr or service_cidr",
                     id="lan-cluster-range"),
        pytest.param({"flux_git_repo_url": LAN_ROOT, "flux_known_hosts": DEFAULTS["flux_known_hosts"]}, NO_LAN_KEY,
                     id="known-hosts-root-host-missing"),
        pytest.param({"flux_git_repo_url": LAN_ROOT}, NO_LAN_KEY, id="known-hosts-unset-off-github"),
    ],
)  # fmt: skip
def test_each_cluster_vars_rule_refuses_its_value(tmp_path: Path, change: dict, problem: str) -> None:
    """Rule: each cluster-vars rule refuses its value with its own message: data paths stay on their disk, CIDRs and
    the App DNS address fit the cluster, LAN ranges stay private and clear of it, an ssh:// root's host has a key."""
    _env_with(tmp_path, change)
    assert _problems(tmp_path) == [f"{CLUSTER_VARS}: {problem}"]


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"apps_git_repo_url": "https://github.com/other-owner/example-apps"}, id="apps-repo-another-owners"),
        pytest.param(dict.fromkeys(OPTIONAL_KEYS), id="optional-keys-omitted"),
        pytest.param({"ssh_user": None}, id="ssh-user-omitted"),
        pytest.param({"flux_git_repo_url": LAN_ROOT, "flux_known_hosts": "git.example.lan ssh-ed25519 AAAAC3Nza="},
                     id="known-hosts-own-forge"),
        pytest.param({"flux_git_repo_url": "ssh://git@github.com/o/r",
                      "flux_known_hosts": DEFAULTS["flux_known_hosts"] + "\ngitlab.com ssh-ed25519 AAAAC3NzaB"},
                     id="known-hosts-several-lines"),
        pytest.param({"flux_git_repo_url": "ssh://git@github.com/o/r"}, id="known-hosts-unset-on-github"),
    ],
)  # fmt: skip
def test_cluster_vars_resolve_with_the_schema_defaults(tmp_path: Path, change: dict) -> None:
    """Rule: an env may read apps from anyone's repository (the example apps' own, another team's; admission holds them),
    leave out the optional keys and trust its own forge; validation fills in the schema's defaults, none for an optional key."""
    cluster_vars = _env_with(tmp_path, change)
    resolved = validate_env_tree(tmp_path)
    assert resolved == {"env-test": {**DEFAULTS, **cluster_vars}}
    assert not set(OPTIONAL_KEYS) & (resolved["env-test"].keys() - cluster_vars.keys())


def test_app_dns_cluster_ip_is_required_only_while_app_dns_is_listed(tmp_path: Path) -> None:
    """Rule: app_dns_cluster_ip is optional, and required once values/switches.yaml lists app-dns."""
    cluster_vars = _valid_cluster_vars()
    cluster_vars.pop("app_dns_cluster_ip", None)
    env = _write_env(tmp_path, cluster_vars=cluster_vars)
    assert _problems(tmp_path) == []
    set_switches(env, features=["app-dns"])
    assert _problems(tmp_path) == [f"{CLUSTER_VARS}: app_dns_cluster_ip is required while values/switches.yaml lists app-dns"]


@pytest.mark.parametrize(
    ("hosts", "problem"),
    [
        pytest.param("{node.example: {node_roles: [control_plane, backup]}}", "expected at least one edge host", id="no-edge"),
        pytest.param("{node.example: {node_roles: [backup, edge]}}", "expected exactly one control_plane host, found 0",
                     id="no-control-plane"),
        pytest.param("{a.example: {node_roles: [control_plane, backup, edge]}, b.example: {node_roles: [control_plane]}}",
                     "expected exactly one control_plane host, found 2", id="two-control-planes"),
        pytest.param("{node.example: {node_roles: [control_plane, edge]}}", "expected exactly one backup host, found 0",
                     id="no-backup"),
        pytest.param("{a.example: {node_roles: [control_plane, backup, edge]}, b.example: {node_roles: [backup]}}",
                     "expected exactly one backup host, found 2", id="two-backups"),
    ],
)  # fmt: skip
def test_host_roles_are_held_across_hosts(tmp_path: Path, hosts: str, problem: str) -> None:
    """Rule: across hosts, exactly one control_plane and one backup host, and at least one edge host."""
    _write_env(tmp_path, hosts_yml=f"all: {{hosts: {hosts}}}\n")
    assert _problems(tmp_path) == [f"envs/env-test/inventory/hosts.yml: {problem}"]


@pytest.mark.parametrize(
    ("change", "features", "rel", "message"),
    [
        *(pytest.param({}, [], rel, f"env-test: missing required file envs/env-test/{rel}", id=Path(rel).name.split(".")[0])
          for rel in ("values/cluster-vars.yaml", "inventory/hosts.yml", "values/switches.yaml",
                      "secrets/k3s-token.sops.yaml", "secrets/cert-manager-ca.sops.yaml", "secrets/backups-restic.sops.yaml")),
        pytest.param({"flux_git_repo_url": "ssh://git@github.com/example-owner/example-cluster"}, [],
                     "secrets/flux-deploy-key.sops.yaml",
                     "env-test: missing required file envs/env-test/secrets/flux-deploy-key.sops.yaml", id="ssh-root-deploy-key"),
        *(pytest.param({}, [feature], f"secrets/{file}",
                       f"env-test: values/switches.yaml lists {feature}, missing required file envs/env-test/secrets/{file}",
                       id=feature)
          for feature, adds in FEATURE_COMPONENTS.items() for file in adds.get("sealed", ())),
    ],
)  # fmt: skip
def test_an_env_holds_each_file_something_reads(tmp_path: Path, change: dict, features: list[str], rel: str, message: str) -> None:
    """Rule: an env holds its values files, the secrets jarvis init seals, an ssh:// root's deploy key and each listed
    feature's sealed file: without one it fails with that file's message, and with it it passes."""
    env = _write_env(tmp_path, cluster_vars={**_valid_cluster_vars(), **change})
    set_switches(env, features=features)
    path = env / rel
    held = path.read_text(encoding="utf-8") if path.is_file() else SEALED_PLACEHOLDER
    path.unlink(missing_ok=True)
    assert _problems(tmp_path) == [message]
    path.write_text(held, encoding="utf-8")
    assert _problems(tmp_path) == []


def test_every_sealed_file_opens_for_exactly_the_env_recipients(tmp_path: Path) -> None:
    """Rule: a sealed file that is not SOPS-encrypted, or is encrypted to other recipients than the
    env's creation rule names, fails; jarvis secrets --update-recipients is the fix it names."""
    token = copy_sample(tmp_path) / "envs" / "env-00" / "secrets" / "k3s-token.sops.yaml"
    assert _problems(tmp_path, "env-00") == []
    doc = yaml.safe_load(token.read_text(encoding="utf-8"))
    token.write_text("stringData:\n  server_token: plain\n", encoding="utf-8")
    assert _problems(tmp_path, "env-00") == [
        "envs/env-00/secrets/k3s-token.sops.yaml: not SOPS-encrypted with age; seal it with jarvis encrypt"]
    doc["sops"]["age"] = doc["sops"]["age"][:1] + [{"recipient": FIXTURE_RECIPIENT, "enc": "x"}]
    token.write_text(yaml.safe_dump(doc), encoding="utf-8")
    assert _problems(tmp_path, "env-00") == [
        "envs/env-00/secrets/k3s-token.sops.yaml: encrypted to 2 age recipient(s) that are not the 1 in .sops.yaml's rule"
        " for envs/env-00/secrets/; run jarvis secrets --update-recipients"]


@pytest.mark.parametrize(
    ("sops_yaml", "message"),
    [
        pytest.param({"creation_rules": []}, "expected exactly one creation rule for envs/env-test/secrets/, found 0", id="no-rule"),
        pytest.param({"creation_rules": [RULE, RULE]}, "expected exactly one creation rule for envs/env-test/secrets/, found 2",
                     id="two-rules"),
        pytest.param({"creation_rules": [RULE, GHOST]}, f"creation rule {GHOST['path_regex']!r} {NOT_JARVIS}",
                     id="rule-for-a-missing-env"),
        pytest.param({"creation_rules": [RULE, {**RULE, "path_regex": "^envs/(env-test|env-x)/secrets/"}]},
                     f"creation rule '^envs/(env-test|env-x)/secrets/' {NOT_JARVIS}", id="rule-edited-by-hand"),
        pytest.param("{\n", "invalid YAML: " + _yaml_error("{\n"), id="invalid-yaml"),
    ],
)  # fmt: skip
def test_sops_yaml_holds_one_rule_per_env_and_no_other(tmp_path: Path, sops_yaml: dict | str, message: str) -> None:
    """Rule: .sops.yaml holds one creation rule per env, the one jarvis writes (SOPS would apply an edited one too),
    and none for a folder that does not exist; validate_env_tree and validate_one_env (#167) both refuse the rest."""
    _write_env(tmp_path)
    text = sops_yaml if isinstance(sops_yaml, str) else yaml.safe_dump(sops_yaml, sort_keys=False)
    (tmp_path / ".sops.yaml").write_text(text, encoding="utf-8")
    assert _problems(tmp_path) == _problems(tmp_path, "env-test") == [f".sops.yaml: {message}"]


def test_envs_limits_the_env_checks_to_the_named_envs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Rule: ENVS names the envs validate_env_tree checks, so an instance CI leg checks one env
    against its own core pin; .sops.yaml rules still see every env folder."""
    _write_env(tmp_path, "env-test")
    (_write_env(tmp_path, "env-broken") / "values" / "cluster-vars.yaml").write_text("not: valid\n", encoding="utf-8")
    broken = _values.problems(_values.CLUSTER_VARS, {"not": "valid"}, "envs/env-broken/values/cluster-vars.yaml")
    assert _problems(tmp_path) == broken != []
    monkeypatch.setenv(env_schema.ENVS_VAR, "env-broken")
    assert _problems(tmp_path) == broken
    monkeypatch.setenv(env_schema.ENVS_VAR, "env-test")
    assert set(validate_env_tree(tmp_path)) == {"env-test"}


@pytest.mark.parametrize(
    ("module", "argv", "prog"),
    [
        pytest.param(mint, [], "jarvis secrets", id="secrets"),
        pytest.param(encrypt, ["--name", "example"], "jarvis encrypt", id="encrypt"),
        pytest.param(render, [], "jarvis render", id="render"),
    ],
)
def test_cli_requires_env(module, argv: list[str], prog: str, monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Rule: jarvis secrets (jarvis/secrets/mint.py), jarvis encrypt (jarvis/secrets/encrypt.py) and jarvis render
    refuse to run without --env, also run as a module: each file's __main__ block runs its main()."""
    monkeypatch.setattr(sys, "argv", [module.__file__, *argv])
    with pytest.raises(SystemExit) as exited:
        runpy.run_path(module.__file__, run_name="__main__")
    out, err = capsys.readouterr()
    assert (exited.value.code, out, err.splitlines()[-1]) == (2, "", f"{prog}: error: the following arguments are required: --env")


def _run_site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *args: str) -> tuple[int, list[str]]:
    """jarvis site on env-00 of the instance at tmp_path/instance, a stub ansible-playbook first on PATH: its exit
    code and the stub's argv, the extra-vars file as @vars (the stub keeps a copy at <log>.vars)."""
    log = tmp_path / "ap.log"
    log.unlink(missing_ok=True)
    stub = tmp_path / "bin" / "ansible-playbook"
    stub.parent.mkdir(exist_ok=True)
    stub.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"${CALL_LOG}\"\n"
                    "for a; do case \"$a\" in @*) cp \"${a#@}\" \"${CALL_LOG}.vars\";; esac; done\n")
    stub.chmod(0o755)
    monkeypatch.setenv("CALL_LOG", str(log))
    monkeypatch.setenv("PATH", f"{stub.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("INSTANCE", str(tmp_path / "instance"))
    code = site.main(["--env", "env-00", *args])
    return code, ["@vars" if arg.startswith("@") else arg for arg in log.read_text().splitlines()] if log.exists() else []


def test_site_passes_inventories_tags_limit_and_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule: jarvis site runs site.yml on the env's hosts then the role groups, with the env's values (the schema's
    defaults filled in) and what the plays no longer work out as one extra-vars file; --check adds --diff."""
    root = copy_sample(tmp_path / "instance")
    command = ["site.yml", "-i", f"{root}/envs/env-00/inventory/hosts.yml", "-i", "../inventory/roles", "-e", "@vars"]
    assert _run_site(tmp_path, monkeypatch) == (0, command)
    cluster_vars = yaml.safe_load((root / "envs/env-00/values/cluster-vars.yaml").read_text(encoding="utf-8"))
    assert json.loads((tmp_path / "ap.log.vars").read_text(encoding="utf-8")) == {
        **DEFAULTS, **cluster_vars, "jarvis_instance_root": str(root),
        "sops_age_key_file": str(age.key_path(cluster_vars["age_key_file"])),
        "sops_age_recipients": [age.public_key(root / ".age" / "keys.txt")],
    }  # fmt: skip
    flags = ["--tags", "base-os,k3s", "--limit", "cp-1", "--check"]
    assert _run_site(tmp_path, monkeypatch, *flags) == (0, [*command, *flags, "--diff"])


def test_site_refuses_an_invalid_env_before_ansible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """Rule: the env schema runs first; an invalid env runs no playbook and exits 3."""

    def invalid(root, name):
        raise ValidationError(["envs/env-00: broken"])

    monkeypatch.setattr(site, "validate_one_env", invalid)
    assert _run_site(tmp_path, monkeypatch) == (3, [])
    assert capsys.readouterr().out == "REFUSE site: env-00 fails validation\nenvs/env-00: broken\n"
