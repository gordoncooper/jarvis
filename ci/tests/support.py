"""Shared test helpers.

One repo root, one script loader, and the fixtures several modules used to
copy from each other. Ansible role renders stay in ansible_role_proof.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import re
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

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


def load_tool(module_name: str, path: Path) -> ModuleType:
    """Load a repo script that is not an importable package."""
    loader = importlib.machinery.SourceFileLoader(module_name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


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
        pytest.fail("kustomize not found on PATH (run make deps)")
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
    import yaml
    from schema.env import EXAMPLE_ENV

    cluster_vars_path = REPO_ROOT / "envs" / EXAMPLE_ENV / "values" / "cluster-vars.yaml"
    reference = yaml.safe_load(cluster_vars_path.read_text(encoding="utf-8"))
    assert isinstance(reference, dict)
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
    from schema.env import EXAMPLE_ENV, is_example_env

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
    (env / "inventory" / "hosts.yml").write_text(
        hosts_yml if hosts_yml is not None else _default_hosts(),
        encoding="utf-8",
    )
    if not is_example_env(env_name):
        _ensure_sops_creation_rule(root, env_name)
    return env


@dataclass(frozen=True)
class ExampleCopy:
    """A throwaway env built from env-00. The age private key stays in tmp."""

    root: Path
    name: str

    @property
    def env_dir(self) -> Path:
        return self.root / "envs" / self.name


def real_and_copy_envs(copy: ExampleCopy) -> list[tuple[Path, str]]:
    """Non-example envs in this tree, then the bootstrapped copy."""
    from ansible_role_proof import non_example_env_names

    return [(REPO_ROOT, name) for name in non_example_env_names(REPO_ROOT)] + [
        (copy.root, copy.name)
    ]


def bootstrap_example_copy(
    tmp_path: Path,
    *,
    name: str = "env-copy",
    hosts_text: str | None = None,
    core_only: bool = False,
) -> ExampleCopy:
    """Copy env-00 into a tmp repo, rename it, bootstrap secrets, and render.

    Steps, and nothing is written into the real tree:
    1. Link platform, services, and apps so kustomize can build the Flux paths.
    2. Copy envs/env-00 to envs/<name>.
    3. Set jarvis_env to <name> and rewrite every envs/env-00/ path.
    4. Replace inventory/hosts.yml when hosts_text is set. With core_only, drop the add-on list.
    5. Write a .sops.yaml age rule from a throwaway age-keygen key.
    6. Run bootstrap-secrets' bootstrap() against the tmp repo, then seal a placeholder
       backups-offsite and one placeholder Secret per namespace an enabled add-on decrypts.
    7. Render the cluster-vars ConfigMap over the copied file.
    """
    from schema.env import CORE_SOURCE_DIR, EXAMPLE_ENV

    root = (tmp_path / "repo").resolve()
    repo = REPO_ROOT.resolve()
    if (root == repo or repo in root.parents) and not _git_ignored(repo, root):
        raise RuntimeError("refusing to write the example copy at a tracked path in the repo")
    root.mkdir()
    for dirname in ("platform", "services", "apps"):
        (root / dirname).symlink_to(repo / dirname)
    # The root source includes core here, as an instance checkout does.
    (root / CORE_SOURCE_DIR).symlink_to(repo)
    dest = root / "envs" / name
    shutil.copytree(repo / "envs" / EXAMPLE_ENV, dest)
    _rename_copied_env(dest, EXAMPLE_ENV, name)
    if hosts_text is not None:
        (dest / "inventory" / "hosts.yml").write_text(hosts_text, encoding="utf-8")
    if core_only:
        _drop_addons(dest)
    _write_copy_sops_rule(root, name, _throwaway_age_public(tmp_path))
    _bootstrap_copy(root, name)
    _seal_copy_offsite(root, name)
    _seal_copy_addons(root, name)
    _render_copy(dest)
    return ExampleCopy(root, name)


def _drop_addons(env_dir: Path) -> None:
    import yaml
    from schema.env import SERVICES_KUSTOMIZATION

    path = env_dir / "flux" / SERVICES_KUSTOMIZATION
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["spec"].pop("components", None)
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def _seal_copy_addons(root: Path, name: str) -> None:
    """One placeholder Secret, sealed to the copy's throwaway key, per namespace an enabled add-on decrypts."""
    from _sops import encrypt_secret_to_file
    from schema.env import addon_documents, addon_secret_namespaces, enabled_addons

    for addon in enabled_addons(root / "envs" / name):
        for namespace in addon_secret_namespaces(addon_documents(REPO_ROOT, addon)):
            secret = f"{namespace}-fixture"
            document = {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": secret,
                    "namespace": namespace,
                    "annotations": {"kustomize.toolkit.fluxcd.io/prune": "disabled"},
                },
                "type": "Opaque",
                "stringData": {"greeting": "placeholder"},
            }
            encrypt_secret_to_file(
                repo_root=root,
                env=name,
                name=secret,
                namespace=namespace,
                document=document,
                sops_bin=shutil.which("sops"),
            )


def _git_ignored(repo: Path, path: Path) -> bool:
    """True when git ignores path, so a copy there cannot touch tracked files.

    An agent bound to its worktree runs the suite with --basetemp=.scratch/tmp.
    """
    proc = subprocess.run(
        ["git", "check-ignore", "-q", str(path)], cwd=repo, check=False, capture_output=True
    )
    return proc.returncode == 0


def _seal_copy_offsite(root: Path, name: str) -> None:
    """The owner seals backups-offsite with make seal; the copy gets a placeholder to the same key."""
    from _sops import encrypt_secret_to_file

    document = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": "backups-offsite",
            "namespace": "platform-backups",
            "annotations": {"kustomize.toolkit.fluxcd.io/prune": "disabled"},
        },
        "type": "Opaque",
        "stringData": {
            "RESTIC_REPOSITORY": "rest:https://offsite.example.invalid/",
            "RESTIC_PASSWORD": "placeholder",
        },
    }
    encrypt_secret_to_file(
        repo_root=root, env=name, name="backups-offsite", document=document, sops_bin=shutil.which("sops")
    )


def _rename_copied_env(env_dir: Path, old: str, new: str) -> None:
    for path in sorted(item for item in env_dir.rglob("*") if item.is_file()):
        text = path.read_text(encoding="utf-8")
        updated = text.replace(f"envs/{old}/", f"envs/{new}/").replace(
            f"jarvis_env: {old}", f"jarvis_env: {new}"
        )
        if updated != text:
            path.write_text(updated, encoding="utf-8")


def _throwaway_age_public(tmp_path: Path) -> str:
    """Public half of a new age key. The private key is not returned or printed."""
    age_keygen = shutil.which("age-keygen")
    if not age_keygen:
        raise RuntimeError("age-keygen not found on PATH (run make deps)")
    proc = subprocess.run(
        [age_keygen, "-o", str(tmp_path / "age-key.txt")],
        check=True,
        capture_output=True,
        text=True,
    )
    # Public key is on stderr. The private key stays in the file and is not read.
    match = re.search(r"age1[0-9a-z]+", proc.stderr)
    if match is None:
        raise RuntimeError("age-keygen did not produce a public key")
    return match.group(0)


def _write_copy_sops_rule(root: Path, name: str, public: str) -> None:
    text = textwrap.dedent(
        f"""
        creation_rules:
          - path_regex: envs/{name}/secrets/.*\\.sops\\.yaml$
            encrypted_regex: ^(data|stringData)$
            age: {public}
        """
    ).strip()
    (root / ".sops.yaml").write_text(text + "\n", encoding="utf-8")


def _bootstrap_copy(root: Path, name: str) -> None:
    sops = shutil.which("sops")
    if not sops:
        raise RuntimeError("sops not found on PATH (run make deps)")
    mod = load_tool("bootstrap_secrets_example_copy", REPO_ROOT / "tools" / "bootstrap-secrets")
    previous_root = mod.REPO_ROOT
    previous_repo_root = mod._repo_root
    mod.REPO_ROOT = root
    mod._repo_root = lambda: root
    try:
        mod.bootstrap(name, sops_bin=sops)
    finally:
        mod.REPO_ROOT = previous_root
        mod._repo_root = previous_repo_root


def register_copy_app(copy: ExampleCopy, namespace: str) -> None:
    """Turn on backup for one app in the copy: selection, render, bootstrap-secrets, as an owner would."""
    import yaml

    app = namespace.removeprefix("app-")
    apps_path = copy.env_dir / "values" / "apps.yaml"
    data = yaml.safe_load(apps_path.read_text(encoding="utf-8"))
    names = data.setdefault("apps", [])
    if app not in names:
        names.append(app)
    data["backups"] = {"mode": "selected", "selected": [app]}
    apps_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    lib = str(REPO_ROOT / "tools" / "lib")
    if lib not in sys.path:
        sys.path.insert(0, lib)
    from render_apps import render_env as render_apps_env
    from render_backup_selection import render_env

    rendered = render_apps_env(copy.env_dir)
    if rendered is not None:
        (copy.env_dir / "flux" / "generated" / "apps.yaml").write_text(rendered, encoding="utf-8")
    render_env(copy.env_dir, copy.root)
    sops = shutil.which("sops")
    if not sops:
        raise RuntimeError("sops not found on PATH (run make deps)")
    mod = load_tool("bootstrap_secrets_app_copy", REPO_ROOT / "tools" / "bootstrap-secrets")
    mod.REPO_ROOT = copy.root
    mod._repo_root = lambda: copy.root
    mod.bootstrap_app(copy.name, namespace, sops_bin=sops)


def _render_copy(env_dir: Path) -> None:
    lib = str(REPO_ROOT / "tools" / "lib")
    if lib not in sys.path:
        sys.path.insert(0, lib)
    from render_cluster_vars_configmap import render_document

    source = env_dir / "values" / "cluster-vars.yaml"
    output = env_dir / "flux" / "generated" / "cluster-vars-configmap.yaml"
    output.write_text(render_document(source), encoding="utf-8")
