"""core-names CI check (p0a-07)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from _core_names import scan_core_names
from services.hermes.core.schema.env import validate_env_tree
from test_env_schema import _write_env

REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_NAMES_CHECK = REPO_ROOT / "tools" / "checks" / "core-names"


def test_repo_tree_passes_core_names_check():
    violations = scan_core_names(REPO_ROOT)
    assert violations == []


def test_core_names_check_executable_passes_in_repo():
    subprocess.run([str(CORE_NAMES_CHECK)], cwd=REPO_ROOT, check=True)


def test_hardcoded_dns_suffix_under_platform_fails(tmp_path: Path):
    _init_git_repo(tmp_path)
    _write_env(tmp_path, env_name="env-test")
    validate_env_tree(tmp_path)

    dns_suffix = yaml.safe_load(
        (tmp_path / "envs/env-test/values/cluster-vars.yaml").read_text(encoding="utf-8")
    )["dns_suffix"]

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


def test_flux_git_repo_url_outside_envs_fails(tmp_path: Path):
    _init_git_repo(tmp_path)
    _write_env(tmp_path, env_name="env-test")
    validate_env_tree(tmp_path)
    cluster = yaml.safe_load(
        (tmp_path / "envs/env-test/values/cluster-vars.yaml").read_text(encoding="utf-8")
    )
    repo_url = cluster["flux_git_repo_url"]

    bad = tmp_path / "platform" / "bad.txt"
    bad.parent.mkdir(parents=True)
    bad.write_text(f"sync: {repo_url}\n", encoding="utf-8")
    _git_add_all(tmp_path)

    violations = scan_core_names(tmp_path)
    assert any(v.kind == "flux_git_repo_url" and v.value == repo_url for v in violations)


def test_github_owner_segment_outside_envs_fails(tmp_path: Path):
    _init_git_repo(tmp_path)
    _write_env(tmp_path, env_name="env-test")
    validate_env_tree(tmp_path)
    cluster = yaml.safe_load(
        (tmp_path / "envs/env-test/values/cluster-vars.yaml").read_text(encoding="utf-8")
    )
    owner = cluster["flux_git_repo_url"].split("github.com/", 1)[1].split("/", 1)[0]
    segment = f"github.com/{owner}/"

    bad = tmp_path / "platform" / "bad.txt"
    bad.parent.mkdir(parents=True)
    bad.write_text(f"remote: {segment}jarvis.git\n", encoding="utf-8")
    _git_add_all(tmp_path)

    violations = scan_core_names(tmp_path)
    assert any(v.kind == "github_owner" and v.value == segment for v in violations)


def test_inventory_hostname_outside_envs_fails(tmp_path: Path):
    _init_git_repo(tmp_path)
    _write_env(tmp_path, env_name="env-test")
    validate_env_tree(tmp_path)
    hosts = yaml.safe_load(
        (tmp_path / "envs/env-test/inventory/hosts.yml").read_text(encoding="utf-8")
    )
    hostname = next(iter(hosts["all"]["hosts"]))

    bad = tmp_path / "platform" / "bad.txt"
    bad.parent.mkdir(parents=True)
    bad.write_text(f"ssh {hostname}\n", encoding="utf-8")
    _git_add_all(tmp_path)

    violations = scan_core_names(tmp_path)
    assert any(
        v.kind == "inventory_hostname" and v.value == hostname for v in violations
    )


def test_ssh_user_at_outside_envs_fails(tmp_path: Path):
    _init_git_repo(tmp_path)
    _write_env(tmp_path, env_name="env-test")
    validate_env_tree(tmp_path)
    cluster = yaml.safe_load(
        (tmp_path / "envs/env-test/values/cluster-vars.yaml").read_text(encoding="utf-8")
    )
    user = cluster["ssh_user"]

    bad = tmp_path / "platform" / "bad.txt"
    bad.parent.mkdir(parents=True)
    bad.write_text(f"ssh {user}@node.example\n", encoding="utf-8")
    _git_add_all(tmp_path)

    violations = scan_core_names(tmp_path)
    assert any(v.kind == "ssh_user_at" and v.value == f"{user}@" for v in violations)


def test_home_path_outside_envs_fails(tmp_path: Path):
    _init_git_repo(tmp_path)
    _write_env(tmp_path, env_name="env-test")
    validate_env_tree(tmp_path)

    bad = tmp_path / "platform" / "bad.txt"
    bad.parent.mkdir(parents=True)
    bad.write_text("path: /home/agent/.kube\n", encoding="utf-8")
    _git_add_all(tmp_path)

    violations = scan_core_names(tmp_path)
    assert any(v.kind == "home_path" for v in violations)


def test_bastion_word_outside_envs_fails(tmp_path: Path):
    _init_git_repo(tmp_path)
    _write_env(tmp_path, env_name="env-test")
    validate_env_tree(tmp_path)

    bad = tmp_path / "platform" / "bad.txt"
    bad.parent.mkdir(parents=True)
    bad.write_text("Run site.yml on the Bastion host\n", encoding="utf-8")
    _git_add_all(tmp_path)

    violations = scan_core_names(tmp_path)
    assert any(v.kind == "bastion" for v in violations)


def test_inventory_hostname_in_docs_fails(tmp_path: Path):
    _init_git_repo(tmp_path)
    _write_env(tmp_path, env_name="env-test")
    validate_env_tree(tmp_path)
    hosts = yaml.safe_load(
        (tmp_path / "envs/env-test/inventory/hosts.yml").read_text(encoding="utf-8")
    )
    hostname = next(iter(hosts["all"]["hosts"]))

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "bad.md").write_text(f"SSH target: {hostname}\n", encoding="utf-8")
    _git_add_all(tmp_path)

    violations = scan_core_names(tmp_path)
    assert any(
        v.path == "docs/bad.md"
        and v.kind == "inventory_hostname"
        and v.value == hostname
        for v in violations
    )


def test_github_owner_word_outside_docs_fails(tmp_path: Path):
    _init_git_repo(tmp_path)
    _write_env(tmp_path, env_name="env-test")
    validate_env_tree(tmp_path)
    cluster = yaml.safe_load(
        (tmp_path / "envs/env-test/values/cluster-vars.yaml").read_text(encoding="utf-8")
    )
    owner = cluster["flux_git_repo_url"].split("github.com/", 1)[1].split("/", 1)[0]

    bad = tmp_path / "platform" / "bad.txt"
    bad.parent.mkdir(parents=True)
    bad.write_text(f"Org: {owner} owns the repo\n", encoding="utf-8")
    _git_add_all(tmp_path)

    violations = scan_core_names(tmp_path)
    assert any(
        v.kind == "github_owner_word" and v.value == owner for v in violations
    )


def test_docs_and_readme_planning_links_allowed(tmp_path: Path):
    _init_git_repo(tmp_path)
    _write_env(tmp_path, env_name="env-test")
    validate_env_tree(tmp_path)
    cluster = yaml.safe_load(
        (tmp_path / "envs/env-test/values/cluster-vars.yaml").read_text(encoding="utf-8")
    )
    repo_url = cluster["flux_git_repo_url"]
    owner = repo_url.split("github.com/", 1)[1].split("/", 1)[0]

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "plan.md").write_text(
        f"Plans: https://github.com/{owner}/project-plans\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text(
        f"See {repo_url} and github.com/{owner}/\n",
        encoding="utf-8",
    )
    _git_add_all(tmp_path)

    violations = scan_core_names(tmp_path)
    assert violations == []


def _init_git_repo(root: Path) -> None:
    subprocess.check_call(["git", "init", "-q"], cwd=root)
    subprocess.check_call(["git", "config", "user.email", "test@example.com"], cwd=root)
    subprocess.check_call(["git", "config", "user.name", "test"], cwd=root)


def _git_add_all(root: Path) -> None:
    subprocess.check_call(["git", "add", "-A"], cwd=root)
