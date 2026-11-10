"""Write a new instance repo, or a new env in one: jarvis init and jarvis env add.

An env is decisions only: its values, its inventory and its sealed secrets. Everything under its
flux/ is render output, so a new env is these few files plus a render. The keys written are the
ones the schema requires (schema/cluster-vars.schema.json), with defaults that suit one small
cluster; every other key is optional and documented in schema/CONTRACT.md.

The working age key lives on the controller (age_key_file). Every env's secrets are also
encrypted to an offline recovery key: init prints its private half once and keeps no copy.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from jarvis.secrets import age as _age

# What init copies into a new instance as is: the agent kit, the devcontainer, the example apps.
# It lives in the package because init runs from the installed CLI, before any core checkout.
KIT = Path(__file__).resolve().parents[1] / "kit"
EXAMPLE_APPS = KIT / "examples-apps.yaml"

ROLES = ("control_plane", "workers", "backup", "edge", "gpu")
# One node with every role but gpu: the smallest whole cluster.
SINGLE_NODE_ROLES = ("control_plane", "workers", "backup", "edge")
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_HOST = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?")
_DOMAIN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")
_GITHUB = re.compile(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?")


class ScaffoldError(Exception):
    pass


@dataclass
class Node:
    name: str
    roles: tuple[str, ...]

    @classmethod
    def parse(cls, text: str, *, only: bool) -> Node:
        """<name>, or <name>:<role>,<role>. A lone node with no roles gets every role but gpu."""
        name, _, roles = text.partition(":")
        if not _HOST.fullmatch(name):
            raise ScaffoldError(f"not a host name: {name!r}")
        picked = tuple(role for role in roles.split(",") if role) if roles else ()
        unknown = [role for role in picked if role not in ROLES]
        if unknown:
            raise ScaffoldError(f"{name}: unknown role {', '.join(unknown)} (roles: {', '.join(ROLES)})")
        if not picked:
            if not only:
                raise ScaffoldError(f"{name}: give its roles, <name>:<role>,<role> (roles: {', '.join(ROLES)})")
            picked = SINGLE_NODE_ROLES
        return cls(name, picked)


@dataclass
class EnvSpec:
    """What one env decides at its start. Everything else is a later edit of its values."""

    name: str
    domain: str
    repo_url: str
    core_url: str
    core_tag: str
    nodes: list[Node]
    branch: str = "main"
    ssh_user: str = ""
    time_zone: str = "Etc/UTC"
    features: list[str] = field(default_factory=list)
    apps_repo_url: str = ""
    age_key_file: str = ""
    # The example hosted apps (jarvis/kit/examples-apps.yaml) on, for a first-run tour.
    examples: bool = False
    # The full core_git_ref when it is not refs/tags/<core_tag> (e2e pins the commit under test by
    # branch); a branch pin is refused by every jarvis command except under --core.
    core_ref: str = ""

    def check(self) -> None:
        if not _DNS_LABEL.fullmatch(self.name):
            raise ScaffoldError(f"env name {self.name!r} is not a DNS label (lowercase letters, digits, -)")
        if not _DOMAIN.fullmatch(self.domain):
            raise ScaffoldError(f"--domain {self.domain!r} is not a DNS name")
        if not self.repo_url.startswith(("ssh://", "https://")):
            raise ScaffoldError("--repo-url must be an ssh:// or https:// URL Flux can read")
        if not self.nodes:
            raise ScaffoldError("give at least one --node")
        held = {role for node in self.nodes for role in node.roles}
        for role in ("control_plane", "backup"):
            count = sum(role in node.roles for node in self.nodes)
            if count != 1:
                raise ScaffoldError(f"exactly one node needs the {role} role, found {count}")
        # The schema's rule: one control plane, one backup host, at least one edge host.
        if "edge" not in held:
            raise ScaffoldError("no node holds the edge role")
        if self.examples and ("app-hosting" not in self.features or not self.apps_repo_url):
            raise ScaffoldError("the example apps need the app-hosting feature and an apps repository")


def example_apps() -> dict:
    """The example apps' values/apps.yaml: their pins, grants and backup selection."""
    return yaml.safe_load(EXAMPLE_APPS.read_text(encoding="utf-8"))


def apps_repo_beside(core_url: str) -> str:
    """The examples' apps repository: jarvis-apps beside core, as core's own e2e reads it."""
    return core_url.rstrip("/").removesuffix(".git").rsplit("/", 1)[0] + "/jarvis-apps"


def cluster_vars(spec: EnvSpec) -> str:
    """values/cluster-vars.yaml: the schema's required keys, then the env's own choices."""
    app_dns = "app-dns" in spec.features
    lines = [*_schema_line(spec, "cluster-vars"),
        "# The env's flat settings (schema/CONTRACT.md in core lists every key).",
        f"jarvis_env: {spec.name}",
        f"dns_suffix: {spec.domain}",
        f"time_zone: {spec.time_zone}",
    ]
    if spec.ssh_user:
        lines.append(f"ssh_user: {spec.ssh_user}")
    if spec.age_key_file:
        lines.append(f"age_key_file: {spec.age_key_file}")
    lines += [
        "",
        "# Backups: local restic repositories on the backup node, and their retention.",
        "backup_path: /var/lib/jarvis/backup",
        "backup_retention_daily: 7",
        "backup_retention_weekly: 4",
        "backup_retention_monthly: 3",
        "backup_schedule: 0 1 * * *",
        "backup_check_schedule: 30 6 * * 3",
        "",
        "# k3s's own defaults.",
        "pod_cidr: 10.42.0.0/16",
        "service_cidr: 10.43.0.0/16",
    ]
    if app_dns:
        lines += ["# App DNS's address: inside service_cidr, not the Kubernetes API's.", "app_dns_cluster_ip: 10.43.0.53"]
    lines += [
        "local_path_data: /var/lib/jarvis/local-path",
        "",
        "# What each node keeps back from pods (the k3s role).",
        "k3s_kubelet_kube_reserved: cpu=250m,memory=512Mi",
        "k3s_kubelet_system_reserved: cpu=250m,memory=512Mi",
        "k3s_kubelet_eviction_hard: memory.available<500Mi,nodefs.available<10%",
        "",
        "ntp_pools:",
        "  - ntp.ubuntu.com",
        "",
        "# Flux reads this repo (the root source) and core at the release it pins.",
        f"flux_git_repo_url: {spec.repo_url}",
        f"flux_git_ref: refs/heads/{spec.branch}",
    ]
    if spec.apps_repo_url:
        lines.append(f"apps_git_repo_url: {spec.apps_repo_url}")
    lines += [f"core_git_repo_url: {spec.core_url}", f"core_git_ref: {spec.core_ref or 'refs/tags/' + spec.core_tag}", ""]
    return "\n".join(lines)


def _schema_line(spec: EnvSpec, name: str) -> list[str]:
    """The editor hint naming the file's schema, at the core release the env pins (GitHub only)."""
    core = _github(spec.core_url)
    if core is None:
        return []
    return [f"# yaml-language-server: $schema=https://raw.githubusercontent.com/{core}/{spec.core_tag}/schema/{name}.schema.json"]


def switches(spec: EnvSpec) -> str:
    features = "".join(f"  - {name}\n" for name in spec.features)
    names = list(example_apps()["versions"]) if spec.examples else []
    apps = "apps:\n" + "".join(f"  - {name}\n" for name in names) if names else "apps: []\n"
    return (
        "".join(line + "\n" for line in _schema_line(spec, "switches"))
        + "# What this env turns on, by name. Core is always on. jarvis render writes the Flux wiring\n"
        "# from this file; an unknown name fails.\n"
        + ("features:\n" + features if features else "features: []\n")
        + "addons: []\n"
        + apps
    )


def hosts(spec: EnvSpec) -> str:
    body = "".join(f"    {node.name}:\n      node_roles: [{', '.join(node.roles)}]\n" for node in spec.nodes)
    hint = "".join(line + "\n" for line in _schema_line(spec, "hosts"))
    return hint + "# Host names only; each must resolve from the controller and every node.\nall:\n  hosts:\n" + body


def _github(core_url: str) -> str | None:
    match = _GITHUB.fullmatch(core_url)
    return f"{match.group(1)}/{match.group(2)}" if match else None


def renovate_json(spec: EnvSpec) -> str:
    core = _github(spec.core_url)
    rules = ""
    if spec.apps_repo_url:
        rules = (
            ',\n  "packageRules": [\n    {\n'
            '      "description": "The apps repository whose <app>/v<version> tags the app pins follow.",\n'
            '      "matchDepTypes": ["jarvis-app"],\n'
            f'      "overridePackageName": "{spec.apps_repo_url}"\n'
            "    }\n  ]"
        )
    return (
        "{\n"
        '  "$schema": "https://docs.renovatebot.com/renovate-schema.json",\n'
        f'  "extends": ["github>{core}//renovate/instance.json#{spec.core_tag}"]'
        f"{rules}\n}}\n"
    )


def ci_workflow(spec: EnvSpec) -> str:
    core = _github(spec.core_url)
    return f"""name: ci

# Core's instance workflow runs jarvis validate for each env with the core it pins, and renders
# Renovate's pin bumps (RENDER_CLIENT_ID and RENDER_PRIVATE_KEY name the GitHub App that pushes
# the render; core's docs/deploy.md says how to make it).

on:
  pull_request:
  push:
    branches:
      - {spec.branch}
  workflow_dispatch:

permissions:
  contents: read

jobs:
  checks:
    uses: {core}/.github/workflows/instance.yml@{spec.core_tag}
    with:
      render-client-id: ${{{{ vars.RENDER_CLIENT_ID }}}}
    secrets:
      render-private-key: ${{{{ secrets.RENDER_PRIVATE_KEY }}}}

  # The required check, under a plain name: a called workflow's own jobs show as "checks / ...".
  ci:
    needs: checks
    if: always()
    runs-on: ubuntu-latest
    steps:
      - env:
          RESULT: ${{{{ needs.checks.result }}}}
        run: test "${{RESULT}}" = success
"""


PRE_COMMIT = """#!/bin/sh
# Installed by jarvis init: before each commit, the checks CI runs, for each env it touches.
envs=$(git diff --cached --name-only -- envs | cut -d/ -f2 | sort -u)
for env in $envs; do
  [ -d "envs/$env" ] || continue
  jarvis --env "$env" render --check || exit 1
  jarvis --env "$env" validate || exit 1
done
"""


def readme(instance: Path, core_url: str) -> str:
    return f"""# {instance.name}

Clusters run by [Jarvis]({core_url}). Each folder under `envs/` is
one cluster: its values, its inventory and its sealed secrets. Everything under an env's `flux/`
is written by `jarvis render`; change `values/` instead.

Run `jarvis --help` from this folder. The age key that opens `envs/*/secrets/` is not in this
repository.

No tools on this machine? Open the folder in its devcontainer (`.devcontainer/`): it installs
mise, uv and the jarvis CLI, and mounts your age key, kubeconfigs and SSH keys from the machine.
"""


def write_sops_rule(instance: Path, env: str, public_keys: list[str]) -> None:
    path = instance / ".sops.yaml"
    block = _age.creation_rule(env, public_keys)
    if not path.is_file():
        path.write_text("creation_rules:\n" + block, encoding="utf-8")
        return
    text = path.read_text(encoding="utf-8")
    if _age.recipients(path, env):
        raise ScaffoldError(f".sops.yaml already has a creation rule for envs/{env}/secrets/")
    path.write_text(text.rstrip("\n") + "\n" + block, encoding="utf-8")


def write_env(instance: Path, spec: EnvSpec) -> Path:
    """The env's own files: values and inventory. Its secrets and flux/ come after."""
    spec.check()
    env_dir = instance / "envs" / spec.name
    if env_dir.exists():
        raise ScaffoldError(f"envs/{spec.name} already exists")
    (env_dir / "values").mkdir(parents=True)
    (env_dir / "inventory").mkdir()
    (env_dir / "secrets").mkdir()
    (env_dir / "values" / "cluster-vars.yaml").write_text(cluster_vars(spec), encoding="utf-8")
    (env_dir / "values" / "switches.yaml").write_text(switches(spec), encoding="utf-8")
    (env_dir / "inventory" / "hosts.yml").write_text(hosts(spec), encoding="utf-8")
    if spec.examples:
        text = EXAMPLE_APPS.read_text(encoding="utf-8")
        body = text[text.index("versions:") :]
        header = "".join(line + "\n" for line in _schema_line(spec, "apps"))
        header += "# The example apps jarvis init --with-examples turned on. Remove them from switches.yaml's apps\n"
        header += "# and from here once the tour is done; their volumes stay until you delete them.\n"
        (env_dir / "values" / "apps.yaml").write_text(header + body, encoding="utf-8")
    return env_dir


def _copy_kit(instance: Path, pairs: tuple[tuple[str, str], ...]) -> None:
    for source, dest in pairs:
        target = instance / dest
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(KIT / source, target)
        if source.endswith(".sh"):
            target.chmod(0o755)


DEVCONTAINER = (("devcontainer/devcontainer.json", ".devcontainer/devcontainer.json"), ("devcontainer/setup.sh", ".devcontainer/setup.sh"))
AGENTS = (
    ("agents/AGENTS.md", "AGENTS.md"),
    ("agents/CLAUDE.md", "CLAUDE.md"),
    ("agents/claude-settings.json", ".claude/settings.json"),
    ("agents/cursor-cli.json", ".cursor/cli.json"),
)


def write_repo_files(instance: Path, spec: EnvSpec, *, ci: bool, agents: bool = False) -> list[str]:
    """README and the devcontainer; with agents the agent kit; with ci the Renovate config and the
    CI caller. Returns what it skipped and why."""
    (instance / "README.md").write_text(readme(instance, spec.core_url), encoding="utf-8")
    _copy_kit(instance, DEVCONTAINER + (AGENTS if agents else ()))
    if not ci:
        return []
    if _github(spec.core_url) is None:
        return [f"CI and Renovate: core_git_repo_url {spec.core_url} is not a GitHub repository"]
    (instance / "renovate.json").write_text(renovate_json(spec), encoding="utf-8")
    workflow = instance / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(ci_workflow(spec), encoding="utf-8")
    return []


def hook_path(instance: Path) -> Path | None:
    """Where git runs this repository's pre-commit hook, or None outside a git repository. git
    says: in a worktree .git is a file, and the hooks are the main repository's."""
    proc = subprocess.run(
        ["git", "-C", str(instance), "rev-parse", "--git-path", "hooks/pre-commit"], capture_output=True, text=True, check=False
    )
    if proc.returncode or not proc.stdout.strip():
        return None
    return (instance / proc.stdout.strip()).resolve()


def install_hook(instance: Path) -> Path:
    hook = hook_path(instance) or instance / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(PRE_COMMIT, encoding="utf-8")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return hook


def deploy_key(env: str) -> tuple[str, str]:
    """A new read-only deploy key for Flux: (private key, public key). Made in a 0700 folder
    that is removed before this returns."""
    with tempfile.TemporaryDirectory(prefix="jarvis-deploy-key-") as folder:
        os.chmod(folder, 0o700)
        key = Path(folder) / "id"
        proc = subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"flux-{env}", "-f", str(key)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode:
            raise ScaffoldError(f"ssh-keygen failed: {proc.stderr.strip()}")
        return key.read_text(encoding="utf-8"), (key.with_suffix(".pub")).read_text(encoding="utf-8").strip()


def deploy_key_secret(private_key: str) -> str:
    indented = "".join(f"    {line}\n" for line in private_key.strip().splitlines())
    return (
        "apiVersion: v1\nkind: Secret\nmetadata:\n  name: flux-deploy-key\n  namespace: flux-system\n"
        "type: Opaque\nstringData:\n  identity: |\n" + indented
    )


def git_init(instance: Path, branch: str) -> None:
    if shutil.which("git") is None:
        raise ScaffoldError("git is not on PATH")
    subprocess.run(["git", "init", "-q", "-b", branch, str(instance)], check=True)
