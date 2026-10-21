"""core-names CI check (p0a-07)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from _core_names import OwnerNameUnset, format_violations, scan_core_names
from schema.env import EXAMPLE_ENV, repo_owner_handle
from test_env_schema import _valid_cluster_vars, _write_env

_FIXTURE_OWNER_NAME = "Zzownername"
_HOME_PREFIX = "/" + "home" + "/"
_PRODUCT_WORD = "bas" + "tion"


@pytest.fixture(autouse=True)
def _owner_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OWNER_NAME", _FIXTURE_OWNER_NAME)


def test_owner_name_unset_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OWNER_NAME", raising=False)
    with pytest.raises(OwnerNameUnset, match="OWNER_NAME is unset"):
        scan_core_names(tmp_path)
    monkeypatch.setenv("OWNER_NAME", "   ")
    with pytest.raises(OwnerNameUnset, match="OWNER_NAME is unset"):
        scan_core_names(tmp_path)


def test_hardcoded_dns_suffix_under_platform_fails(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    _write_env(tmp_path, env_name="env-test")
    dns_suffix = _dns_suffix(tmp_path)

    platform = tmp_path / "platform" / "fixture"
    platform.mkdir(parents=True)
    bad = platform / "bad.txt"
    bad.write_text(f"host: app.{dns_suffix}\n", encoding="utf-8")
    _git_add_all(tmp_path)

    violations = scan_core_names(tmp_path)
    assert len(violations) == 1
    assert violations[0].path == "platform/fixture/bad.txt"
    assert violations[0].kind == "dns_suffix"
    assert violations[0].value == dns_suffix


def test_value_inside_longer_token_passes(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    _write_env(tmp_path, env_name="env-test")
    dns_suffix = _dns_suffix(tmp_path)
    hosts = _hosts(tmp_path)
    hostname = next(iter(hosts["all"]["hosts"]))

    platform = tmp_path / "platform"
    platform.mkdir()
    (platform / "ok.txt").write_text(
        f"token {dns_suffix}extra and pre{hostname}\n",
        encoding="utf-8",
    )
    _git_add_all(tmp_path)

    assert scan_core_names(tmp_path) == []


def test_ubuntu_image_tag_passes_when_ssh_user_is_ubuntu(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    cluster = _valid_cluster_vars()
    cluster["ssh_user"] = "ubuntu"
    _write_env(tmp_path, env_name="env-test", cluster_vars=cluster)

    platform = tmp_path / "platform"
    platform.mkdir()
    (platform / "ok.txt").write_text("image: ubuntu:26.04\n", encoding="utf-8")
    _git_add_all(tmp_path)

    assert scan_core_names(tmp_path) == []


def test_inventory_hostname_outside_envs_fails(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    _write_env(tmp_path, env_name="env-test")
    hostname = next(iter(_hosts(tmp_path)["all"]["hosts"]))

    bad = tmp_path / "platform" / "bad.txt"
    bad.parent.mkdir(parents=True)
    bad.write_text(f"ssh {hostname}\n", encoding="utf-8")
    _git_add_all(tmp_path)

    violations = scan_core_names(tmp_path)
    assert any(
        v.kind == "inventory_hostname" and v.value == hostname for v in violations
    )


def test_ssh_user_at_outside_envs_fails(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    _write_env(tmp_path, env_name="env-test")
    user = _cluster(tmp_path)["ssh_user"]

    bad = tmp_path / "platform" / "bad.txt"
    bad.parent.mkdir(parents=True)
    bad.write_text(f"ssh {user}@node.example\n", encoding="utf-8")
    _git_add_all(tmp_path)

    violations = scan_core_names(tmp_path)
    assert any(v.kind == "ssh_user_at" and v.value == f"{user}@" for v in violations)


def test_ssh_user_tilde_outside_envs_fails(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    _write_env(tmp_path, env_name="env-test")
    user = _cluster(tmp_path)["ssh_user"]

    bad = tmp_path / "platform" / "bad.txt"
    bad.parent.mkdir(parents=True)
    bad.write_text(f"home ~{user}\n", encoding="utf-8")
    _git_add_all(tmp_path)

    violations = scan_core_names(tmp_path)
    assert any(v.kind == "ssh_user_tilde" and v.value == f"~{user}" for v in violations)


def test_home_path_outside_envs_fails(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    _write_env(tmp_path, env_name="env-test")

    bad = tmp_path / "platform" / "bad.txt"
    bad.parent.mkdir(parents=True)
    bad.write_text(f"path: {_HOME_PREFIX}someuser/.kube\n", encoding="utf-8")
    _git_add_all(tmp_path)

    violations = scan_core_names(tmp_path)
    assert any(v.kind == "home_path" for v in violations)


def test_product_word_outside_envs_fails(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    _write_env(tmp_path, env_name="env-test")

    bad = tmp_path / "platform" / "bad.txt"
    bad.parent.mkdir(parents=True)
    bad.write_text(f"Run site.yml on the {_PRODUCT_WORD.capitalize()} host\n", encoding="utf-8")
    _git_add_all(tmp_path)

    violations = scan_core_names(tmp_path)
    assert any(v.kind == _PRODUCT_WORD for v in violations)


def test_inventory_hostname_in_docs_fails(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    _write_env(tmp_path, env_name="env-test")
    hostname = next(iter(_hosts(tmp_path)["all"]["hosts"]))

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "bad.md").write_text(f"SSH target: {hostname}\n", encoding="utf-8")
    _git_add_all(tmp_path)

    violations = scan_core_names(tmp_path)
    assert any(
        v.path == "docs/bad.md" and v.kind == "inventory_hostname" and v.value == hostname
        for v in violations
    )


def test_owner_handle_outside_envs_fails(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    _write_env(tmp_path, env_name="env-test")
    owner = _owner(tmp_path)

    bad = tmp_path / "platform" / "bad.txt"
    bad.parent.mkdir(parents=True)
    bad.write_text(f"Org: {owner} owns the repo\n", encoding="utf-8")
    _git_add_all(tmp_path)

    violations = scan_core_names(tmp_path)
    assert any(v.kind == "owner_handle" and v.value == owner for v in violations)


def test_owner_handle_in_docs_url_fails(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    _write_env(tmp_path, env_name="env-test")
    owner = _owner(tmp_path)

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "bad.md").write_text(
        f"Plans: https://github.com/{owner}/project-plans\n",
        encoding="utf-8",
    )
    _git_add_all(tmp_path)

    violations = scan_core_names(tmp_path)
    assert any(
        v.path == "docs/bad.md" and v.kind == "owner_handle" and v.value == owner
        for v in violations
    )


def test_env_name_in_docs_passes(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    _write_env(tmp_path, env_name="env-test")

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "ok.md").write_text("Copy env-test when adding a deployment.\n", encoding="utf-8")
    _git_add_all(tmp_path)

    assert scan_core_names(tmp_path) == []


def test_env_name_under_platform_fails(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    _write_env(tmp_path, env_name="env-test")

    platform = tmp_path / "platform"
    platform.mkdir()
    (platform / "bad.txt").write_text("deploy env-test\n", encoding="utf-8")
    _git_add_all(tmp_path)

    violations = scan_core_names(tmp_path)
    assert any(v.kind == "env_name" and v.value == "env-test" for v in violations)


def test_example_env_values_are_not_sources(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    _write_env(tmp_path, env_name=EXAMPLE_ENV)
    live = _valid_cluster_vars()
    live["dns_suffix"] = "live.example"
    live["flux_git_repo_url"] = "https://github.com/live-owner/live-cluster"
    live["apps_git_repo_url"] = "https://github.com/live-owner/live-apps"
    live["ssh_user"] = "liveuser"
    _write_env(tmp_path, env_name="env-live", cluster_vars=live)
    (tmp_path / "envs" / EXAMPLE_ENV / "inventory" / "hosts.yml").write_text(
        "all:\n  hosts:\n    example-host:\n"
        "      node_roles: [control_plane, workers, storage, backup, gpu, edge]\n",
        encoding="utf-8",
    )
    (tmp_path / "envs/env-live/inventory/hosts.yml").write_text(
        "all:\n  hosts:\n    live-host:\n      node_roles: [control_plane, backup, edge]\n",
        encoding="utf-8",
    )
    platform = tmp_path / "platform"
    platform.mkdir()
    example_dns = _cluster(tmp_path, EXAMPLE_ENV)["dns_suffix"]
    (platform / "ok.txt").write_text(
        f"example-host uses {example_dns}\n",
        encoding="utf-8",
    )
    _git_add_all(tmp_path)

    assert scan_core_names(tmp_path) == []


def test_file_outside_scan_roots_is_ignored(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    _write_env(tmp_path, env_name="env-test")
    dns_suffix = _dns_suffix(tmp_path)

    apps = tmp_path / "apps"
    apps.mkdir()
    (apps / "leak.txt").write_text(f"host: {dns_suffix}\n", encoding="utf-8")
    _git_add_all(tmp_path)

    assert scan_core_names(tmp_path) == []


def test_personal_name_in_env_readme_fails(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    _write_env(tmp_path, env_name="env-test")
    readme = tmp_path / "envs" / "env-test" / "README.md"
    readme.write_text(f"Maintained by {_FIXTURE_OWNER_NAME}.\n", encoding="utf-8")
    _git_add_all(tmp_path)

    violations = scan_core_names(tmp_path)
    assert any(v.path == "envs/env-test/README.md" and v.kind == "owner_name" for v in violations)
    rendered = "\n".join(format_violations(violations))
    assert _FIXTURE_OWNER_NAME not in rendered
    assert "forbidden owner_name" in rendered


def _dns_suffix(root: Path) -> str:
    return _cluster(root, "env-test")["dns_suffix"]


def _owner(root: Path) -> str:
    url = _cluster(root)["flux_git_repo_url"]
    assert isinstance(url, str)
    owner = repo_owner_handle(url)
    assert isinstance(owner, str)
    return owner


def _cluster(root: Path, env_name: str = "env-test") -> dict:
    data = yaml.safe_load(
        (root / "envs" / env_name / "values" / "cluster-vars.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(data, dict)
    return data


def _hosts(root: Path) -> dict:
    data = yaml.safe_load(
        (root / "envs/env-test/inventory/hosts.yml").read_text(encoding="utf-8")
    )
    assert isinstance(data, dict)
    return data


def _init_git_repo(root: Path) -> None:
    subprocess.check_call(["git", "init", "-q"], cwd=root)
    subprocess.check_call(["git", "config", "user.email", "test@example.com"], cwd=root)
    subprocess.check_call(["git", "config", "user.name", "test"], cwd=root)


def _git_add_all(root: Path) -> None:
    subprocess.check_call(["git", "add", "-A"], cwd=root)
