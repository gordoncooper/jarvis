"""Shared test helpers.

One repo root, one script loader, and the fixtures several modules used to
copy from each other. Ansible role renders stay in ansible_role_proof.
"""

from __future__ import annotations

import functools
import importlib
import re
import shutil
import subprocess
import tempfile
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE_APPS_REPO = REPO_ROOT / "ci" / "tests" / "fixtures" / "app-hosting" / "jarvis-apps"


def instance_apps_repo_for_checks(instance_root: Path) -> Path:
    """Apps checkout beside a tmp instance — same content as CI fixtures, not the forbidden core path."""
    dest = instance_root / "example-apps"
    if not dest.is_dir():
        shutil.copytree(_FIXTURE_APPS_REPO, dest)
    return dest


_RENOVATE_NAMED_GROUP = re.compile(r"\(\?<([A-Za-z_]\w*)>")


def renovate_python_pattern(pattern: str) -> str:
    """Renovate's RE2 named group `(?<name>...)` as Python `re` `(?P<name>...)`.

    This is not Renovate's engine. A pattern RE2 accepts and Python reads
    differently still matches here.
    """
    return _RENOVATE_NAMED_GROUP.sub(r"(?P<\1>", pattern)


def _kustomize_build(rel_path: str, *, root: Path | None = None) -> str:
    """Chart defaults the values don't set pass unchecked."""
    repo = REPO_ROOT if root is None else root
    kustomize = shutil.which("kustomize")
    if not kustomize:
        pytest.fail("kustomize not found on PATH (run jarvis doctor)")
    proc = subprocess.run(
        [
            kustomize,
            "build",
            "--load-restrictor",
            "LoadRestrictionsNone",
            str(repo / rel_path),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=repo,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return proc.stdout


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


# A sealed file's shape for the schema: SOPS metadata naming the recipient _write_env's rule names.
SEALED_PLACEHOLDER = "sops:\n  age:\n    - recipient: age1fixture\n"


def _ensure_sops_creation_rule(root: Path, env_name: str) -> None:
    import yaml

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
    """The sample env's cluster-vars, as jarvis init writes them plus the sample's LAN range."""
    import sample
    import yaml

    from jarvis.env import scaffold

    reference = yaml.safe_load(scaffold.cluster_vars(sample.spec()))
    reference["lan_cidrs"] = ["10.200.0.0/24"]
    return dict(reference)


def _write_env(
    root: Path,
    env_name: str = "env-test",
    *,
    cluster_vars: dict | None = None,
    omit_ssh_user: bool = False,
    hosts_yml: str | None = None,
) -> Path:
    import yaml

    env = root / "envs" / env_name
    (env / "values").mkdir(parents=True)
    (env / "inventory").mkdir(parents=True)

    cv = cluster_vars if cluster_vars is not None else _valid_cluster_vars()
    if omit_ssh_user:
        cv = {k: v for k, v in cv.items() if k != "ssh_user"}
    if cv.get("jarvis_env") in (None, "env-00"):
        cv["jarvis_env"] = env_name

    (env / "values" / "cluster-vars.yaml").write_text(
        yaml.safe_dump(cv, sort_keys=False), encoding="utf-8"
    )
    (env / "inventory" / "hosts.yml").write_text(
        hosts_yml if hosts_yml is not None else _default_hosts(),
        encoding="utf-8",
    )
    _ensure_sops_creation_rule(root, env_name)
    return env


@dataclass(frozen=True)
class ExampleCopy:
    """A throwaway sample env, built as jarvis init builds one. The age private key stays in tmp."""

    root: Path
    name: str

    @property
    def env_dir(self) -> Path:
        return self.root / "envs" / self.name


def real_and_copy_envs(copy: ExampleCopy) -> list[tuple[Path, str]]:
    """The envs a whole-env rule runs on: core ships none, so the sample copy."""
    return [(copy.root, copy.name)]


def bootstrap_example_copy(
    tmp_path: Path,
    *,
    name: str = "env-copy",
    hosts_text: str | None = None,
    core_only: bool = False,
) -> ExampleCopy:
    """A sample instance in tmp_path/repo holding one env, <name> (ci/tests/sample.py): written by
    jarvis init's code, edited as an owner would, sealed to a throwaway key, and rendered. With
    core_only it lists no add-on; hosts_text replaces its inventory."""
    import sample

    root = (tmp_path / "repo").resolve()
    repo = REPO_ROOT.resolve()
    if (root == repo or repo in root.parents) and not _git_ignored(repo, root):
        raise RuntimeError("refusing to write the example copy at a tracked path in the repo")
    sample.build(root, name, hosts_text=hosts_text or sample.HOSTS, with_addons=not core_only)
    return ExampleCopy(root, name)


@functools.cache
def sample_root() -> Path:
    """A pristine sample instance holding env-00 (no add-on), built once per test process. Read
    it; to change it, copy it (copy_sample) first."""
    import atexit

    import sample

    base = Path(tempfile.mkdtemp(prefix="jarvis-sample-"))
    atexit.register(shutil.rmtree, base, True)
    # No add-on, so a copy of one env builds in any tree; example_copy lists the fixture add-on.
    return sample.build(base / "instance", with_addons=False)


def copy_sample(dest: Path) -> Path:
    """A copy of the sample instance at dest (which may exist), its core links kept."""
    shutil.copytree(sample_root(), dest, symlinks=True, dirs_exist_ok=True)
    return dest


def link_core_trees(instance: Path, product: Path) -> None:
    """Lay instance out like the root artifact: product's core/ and addons/ beside its envs/."""
    instance.mkdir(parents=True, exist_ok=True)
    for tree in ("core", "addons"):
        if (product / tree).is_dir():
            (instance / tree).symlink_to(product / tree, target_is_directory=True)


def set_switches(env_dir: Path, **sections: list[str]) -> None:
    """Replace these sections of the env's values/switches.yaml, as an owner edits the file."""
    import yaml

    path = env_dir / "values" / "switches.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    data.update(sections)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def render_switches(env_dir: Path, root: Path) -> None:
    """Render the env's Flux wiring from its switches, as an owner would."""
    from jarvis.render.switches import render_env

    render_env(env_dir, root)


def link_core_with_fixture_addons(root: Path) -> None:
    """root/core links to core, and root/addons holds core's add-ons and the fixture ones."""
    import sample

    sample.link_core(root)


def _git_ignored(repo: Path, path: Path) -> bool:
    """True when git ignores path, so a copy there cannot touch tracked files.

    An agent bound to its worktree runs the suite with --basetemp=.scratch/tmp.
    """
    proc = subprocess.run(
        ["git", "check-ignore", "-q", str(path)], cwd=repo, check=False, capture_output=True
    )
    return proc.returncode == 0


def _rename_copied_env(env_dir: Path, old: str, new: str) -> None:
    for path in sorted(item for item in env_dir.rglob("*") if item.is_file()):
        text = path.read_text(encoding="utf-8")
        updated = text.replace(f"envs/{old}/", f"envs/{new}/").replace(
            f"jarvis_env: {old}", f"jarvis_env: {new}"
        )
        if updated != text:
            path.write_text(updated, encoding="utf-8")


def register_copy_app(copy: ExampleCopy, namespace: str) -> None:
    """Turn on backup for one app in the copy: selection, render, bootstrap-secrets, as an owner would."""
    import yaml

    app = namespace.removeprefix("app-")
    apps_path = copy.env_dir / "values" / "apps.yaml"
    data = yaml.safe_load(apps_path.read_text(encoding="utf-8"))
    data["backups"] = {"mode": "selected", "selected": [app]}
    apps_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    switches = yaml.safe_load((copy.env_dir / "values" / "switches.yaml").read_text(encoding="utf-8"))
    set_switches(
        copy.env_dir,
        apps=list(dict.fromkeys([*switches["apps"], app])),
        features=list(dict.fromkeys([*switches["features"], "apps-backup-server"])),
    )
    render_switches(copy.env_dir, copy.root)
    sops = shutil.which("sops")
    if not sops:
        raise RuntimeError("sops not found on PATH (run make deps)")
    mod = importlib.import_module("jarvis.secrets.mint")
    previous_root, previous_repo_root = mod.REPO_ROOT, mod._repo_root
    mod.REPO_ROOT = copy.root
    mod._repo_root = lambda: copy.root
    try:
        mod.bootstrap_app(copy.name, namespace, sops_bin=sops)
    finally:
        mod.REPO_ROOT, mod._repo_root = previous_root, previous_repo_root


def kyverno_generated(work: Path, policies: list[dict], triggers: list[dict], cluster: list[dict] | None = None) -> list[dict]:
    """Every object the GeneratingPolicies make for the triggers, with the kyverno CLI (kyverno
    apply -o). cluster holds what resource.get and resource.list read, and each trigger's
    Namespace. kyverno test cannot be used: it passes a generatedResource that was never made."""
    kyverno = shutil.which("kyverno")
    if kyverno is None:
        pytest.fail("kyverno CLI not found on PATH (run make deps)")
    import yaml

    work.mkdir(parents=True, exist_ok=True)
    (work / "policies.yaml").write_text(yaml.safe_dump_all(policies), encoding="utf-8")
    (work / "triggers.yaml").write_text(yaml.safe_dump_all(triggers), encoding="utf-8")
    context = {"apiVersion": "cli.kyverno.io/v1alpha1", "kind": "Context", "metadata": {"name": "c"},
               "spec": {"resources": cluster or []}}
    (work / "context.yaml").write_text(yaml.safe_dump(context), encoding="utf-8")
    out = work / "out"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir()
    proc = subprocess.run(
        [kyverno, "apply", str(work / "policies.yaml"), "--resource", str(work / "triggers.yaml"),
         "--context-file", str(work / "context.yaml"), "-o", str(out), "--remove-color"],
        capture_output=True, text=True, check=False,
    )
    report = proc.stdout + proc.stderr
    assert proc.returncode == 0 and ", error: 0," in report, report
    assert "Invalid Policy" not in report and "failed to" not in report, report
    return [
        doc
        for path in sorted(out.iterdir())
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8"))
        if isinstance(doc, dict)
    ]
