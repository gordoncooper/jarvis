"""Shared test helpers.

One repo root, one script loader, and the fixtures several modules used to
copy from each other. Ansible role renders stay in ansible_role_proof.
"""

from __future__ import annotations

import contextlib
import functools
import importlib
import shutil
import subprocess
import sys
import tempfile
import textwrap
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def instance_apps_repo_for_checks(instance_root: Path) -> Path:
    """The fixture apps repository beside a tmp instance, tagged at the sample's pins."""
    import sample

    dest = instance_root / "example-apps"
    if not dest.is_dir():
        sample.apps_repo(dest)
    return dest


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


# The recipient _write_env's rule names, and a sealed file's shape for the schema: SOPS metadata
# naming it.
FIXTURE_RECIPIENT = "age1" + "q" * 58
SEALED_PLACEHOLDER = f"sops:\n  age:\n    - recipient: {FIXTURE_RECIPIENT}\n"


def _ensure_sops_creation_rule(root: Path, env_name: str) -> None:
    from jarvis.secrets import age

    path = root / ".sops.yaml"
    if not age.recipients(path, env_name):
        age.write_creation_rules(path, [*age.creation_rules(path), age.creation_rule(env_name, [FIXTURE_RECIPIENT])])


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
    (env / "values" / "switches.yaml").write_text("features: []\naddons: []\n", encoding="utf-8")
    # The secrets jarvis init seals, as placeholders that name the rule's recipient.
    (env / "secrets").mkdir()
    for name in ("k3s-token", "cert-manager-ca", "backups-restic"):
        (env / "secrets" / f"{name}.sops.yaml").write_text(SEALED_PLACEHOLDER, encoding="utf-8")
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


@contextlib.contextmanager
def core_at(core: Path) -> Iterator[Path]:
    """Read env trees against core (a folder holding core/ and/or addons/) instead of the test
    core, for a test that ships its own core trees."""
    from jarvis import paths

    previous = paths.core_root
    paths.core_root = lambda: core
    try:
        yield core
    finally:
        paths.core_root = previous


# A jarvis module run in a child process against the test core: [*RUN_MODULE, "jarvis.env.validate", ...].
RUN_MODULE = (sys.executable, str(Path(__file__).resolve().parent / "run_module.py"))


def checks_without_charts() -> list[str]:
    """Every env check but the two that render charts (kubeconform, policies). Those run on the
    sample instance (ci/checks/sample-instance) and on jarvis init's own env (test_init.py); a
    test of another env shape runs the rest."""
    from jarvis.env.validate import CHECKS

    return [name for name in CHECKS if name not in ("kubeconform", "policies")]


def flux_built(root: Path, env: str, name: str = "platform-configs") -> list[dict]:
    """The objects the env's Flux Kustomization name applies, as Flux builds them (substituted)."""
    from jarvis.checks.flux_build import env_tree

    tree = env_tree(root, f"envs/{env}/flux")
    assert tree.error is None, tree.error
    path = next((item for item in tree.paths if (item.kustomization.get("metadata") or {}).get("name") == name), None)
    assert path is not None, f"envs/{env}/flux applies no Kustomization {name}"
    assert path.error is None, path.error
    return path.docs


def flux_envsubst(docs: list[dict], values: dict[str, str]) -> list[dict]:
    """docs after Flux's own postBuild substitution of values (flux envsubst --strict): every
    variable they use must be among values, as on the cluster."""
    import os

    import yaml

    flux = shutil.which("flux")
    assert flux, "flux not found on PATH (run jarvis doctor)"
    proc = subprocess.run(
        [flux, "envsubst", "--strict"], input=yaml.safe_dump_all(docs, sort_keys=False), capture_output=True,
        text=True, check=False, env={**os.environ, **{key: str(value) for key, value in values.items()}},
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    return [doc for doc in yaml.safe_load_all(proc.stdout) if isinstance(doc, dict)]


def write_flux_root(env_dir: Path) -> Path:
    """The env's flux/sync.yaml as jarvis render writes it (the root and core sources, and the root
    Kustomization applying the env's flux/), for a test env written by hand."""
    from jarvis.env import switches

    cluster_vars = {
        "flux_git_repo_url": "https://github.com/example-owner/example-cluster",
        "flux_git_ref": "refs/heads/main",
        "core_git_repo_url": "https://github.com/example-owner/example-core",
        "core_git_ref": "refs/tags/v0.0.0",
    }
    path = env_dir / "flux" / switches.SYNC_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(switches.sync(cluster_vars, env_dir.name), encoding="utf-8")
    return path


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


def _git_ignored(repo: Path, path: Path) -> bool:
    """True when git ignores path, so a copy there cannot touch tracked files.

    An agent bound to its worktree runs the suite with --basetemp=.scratch/tmp.
    """
    proc = subprocess.run(
        ["git", "check-ignore", "-q", str(path)], cwd=repo, check=False, capture_output=True
    )
    return proc.returncode == 0


def register_copy_app(copy: ExampleCopy, namespace: str) -> None:
    """Turn on backup for one listed app in the copy as an owner would: its backups on in
    values/apps.yaml, its credential minted as jarvis secrets does, then render."""
    import yaml

    sops = shutil.which("sops")
    if not sops:
        raise RuntimeError("sops not found on PATH (run make deps)")
    path = copy.env_dir / "values" / "apps.yaml"
    apps = yaml.safe_load(path.read_text(encoding="utf-8"))
    apps[namespace.removeprefix("app-")].pop("backups", None)
    path.write_text(yaml.safe_dump(apps, sort_keys=False), encoding="utf-8")
    mod = importlib.import_module("jarvis.secrets.mint")
    previous_root, previous_repo_root = mod.REPO_ROOT, mod._repo_root
    mod.REPO_ROOT = copy.root
    mod._repo_root = lambda: copy.root
    try:
        mod.bootstrap_app(copy.name, namespace, sops_bin=sops)
    finally:
        mod.REPO_ROOT, mod._repo_root = previous_root, previous_repo_root
    render_switches(copy.env_dir, copy.root)


def kyverno_generated(work: Path, policies: list[dict], triggers: list[dict], cluster: list[dict] | None = None) -> list[dict]:
    """Every object the GeneratingPolicies make for the triggers, with the kyverno CLI (kyverno
    apply -o). cluster holds what resource.get and resource.list read, and each trigger's
    Namespace, whose labels a namespaceSelector reads. kyverno test cannot be used: it passes a
    generatedResource that was never made."""
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
    values = {"apiVersion": "cli.kyverno.io/v1alpha1", "kind": "Value", "metadata": {"name": "v"},
              "namespaceSelector": [{"name": doc["metadata"]["name"], "labels": doc["metadata"].get("labels") or {}}
                                    for doc in cluster or [] if doc.get("kind") == "Namespace"]}  # fmt: skip
    (work / "values.yaml").write_text(yaml.safe_dump(values), encoding="utf-8")
    out = work / "out"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir()
    proc = subprocess.run(
        [kyverno, "apply", str(work / "policies.yaml"), "--resource", str(work / "triggers.yaml"),
         "--context-file", str(work / "context.yaml"), "--values-file", str(work / "values.yaml"), "-o", str(out), "--remove-color"],
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
