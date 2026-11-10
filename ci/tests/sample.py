"""The sample instance core's tests and checks run on: built the way jarvis init builds one.

Core ships no env of its own. Tests and the env-level rule checks run on an instance written by
the same code jarvis init runs (jarvis/env/scaffold.py), then edited as an owner would: more
nodes, the sample hosted apps from the apps fixture, the fixture add-on, and placeholder sealed
files for what an owner seals by hand. Its secrets are sealed to a throwaway age key that stays
in the instance's own .age/ folder.

  python ci/tests/sample.py <dest>    writes a sample instance at <dest> and prints its path
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jarvis.env import scaffold  # noqa: E402

FIXTURE_APPS_REPO = REPO_ROOT / "ci" / "tests" / "fixtures" / "app-hosting" / "jarvis-apps"
FIXTURE_ADDONS = REPO_ROOT / "ci" / "tests" / "fixtures" / "addons"
NAME = "env-00"
HOSTS = """# Host names only; each must resolve from the controller and every node.
all:
  hosts:
    cp-1:
      node_roles: [control_plane]
      data_disk: true
    wk-1:
      node_roles: [workers, edge]
      data_disk: true
    wk-2:
      node_roles: [workers, gpu]
      data_disk: true
    wk-3:
      node_roles: [workers, gpu]
      data_disk: true
    wk-4:
      node_roles: [workers]
    wk-5:
      node_roles: [workers]
      data_disk: true
    bk-1:
      node_roles: [backup]
      data_disk: true
"""
# The sample apps' pins, grant and backup selection, as an owner writes values/apps.yaml.
APPS_TEXT = """versions:
  hello-web: v0.1.0
  notes: v0.1.0
  lan-demo: v0.1.0
exceptions:
  - lan-demo: lan-port 8080/TCP
backups:
  mode: selected
  selected: []
"""
APPS = {
    "versions": {"hello-web": "v0.1.0", "notes": "v0.1.0", "lan-demo": "v0.1.0"},
    "exceptions": [{"lan-demo": "lan-port 8080/TCP"}],
    "backups": {"mode": "selected", "selected": []},
}


def spec(name: str = NAME, *, features: list[str] | None = None) -> scaffold.EnvSpec:
    """The sample env's starting decisions, as jarvis init takes them."""
    return scaffold.EnvSpec(
        name=name,
        domain=f"{name}.example",
        repo_url="https://github.com/example-owner/example-cluster",
        core_url="https://github.com/example-owner/example-core",
        core_tag="v0.2.0",
        nodes=[scaffold.Node.parse("node1", only=True)],
        ssh_user="example",
        features=["app-dns", "app-hosting", "offsite-backup-copy"] if features is None else features,
        apps_repo_url="https://github.com/example-owner/example-apps",
    )


def link_core(root: Path) -> None:
    """root/core links to core, and root/addons holds core's add-ons and the fixture ones (hello):
    the product ships no sample add-on, so the add-on rules run on the fixture."""
    if not (root / "core").exists():
        (root / "core").symlink_to(REPO_ROOT / "core")
    addons = root / "addons"
    addons.mkdir(exist_ok=True)
    for source in (REPO_ROOT / "addons", FIXTURE_ADDONS):
        for entry in source.iterdir():
            if entry.name != "__pycache__" and not (addons / entry.name).exists():
                (addons / entry.name).symlink_to(entry)


def throwaway_key(root: Path) -> str:
    """A new age key in root/.age/keys.txt; returns its public half."""
    folder = root / ".age"
    folder.mkdir(mode=0o700, exist_ok=True)
    proc = subprocess.run(["age-keygen", "-o", str(folder / "keys.txt")], check=True, capture_output=True, text=True)
    found = re.search(r"age1[0-9a-z]+", proc.stderr)
    if found is None:
        raise RuntimeError("age-keygen did not produce a public key")
    return found.group(0)


def _seal(root: Path, env: str, name: str, namespace: str, data: dict[str, str], *, folder: str | None = None) -> None:
    from jarvis.secrets.sops import encrypt_secret_to_file

    document = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": namespace, "annotations": {"kustomize.toolkit.fluxcd.io/prune": "disabled"}},
        "type": "Opaque",
        "stringData": data,
    }
    encrypt_secret_to_file(
        repo_root=root, env=env, name=name, namespace=folder, document=document, sops_bin=shutil.which("sops") or "sops"
    )


def _mint(root: Path, env: str) -> None:
    import importlib

    mint = importlib.import_module("jarvis.secrets.mint")
    previous = mint.REPO_ROOT, mint._repo_root
    mint.REPO_ROOT, mint._repo_root = root, lambda: root
    try:
        mint.bootstrap(env)
    finally:
        mint.REPO_ROOT, mint._repo_root = previous


def add_env(
    root: Path,
    name: str = NAME,
    *,
    hosts_text: str | None = HOSTS,
    with_apps: bool = True,
    with_addons: bool = True,
    features: list[str] | None = None,
    public_keys: list[str],
) -> Path:
    """One sample env in root: scaffolded, edited as an owner would, sealed and rendered."""
    return add_env_from_spec(
        root, spec(name, features=features), public_keys=public_keys,
        hosts_text=hosts_text, with_apps=with_apps, with_addons=with_addons,
    )  # fmt: skip


def add_env_from_spec(
    root: Path,
    sample: scaffold.EnvSpec,
    *,
    public_keys: list[str],
    hosts_text: str | None = HOSTS,
    with_apps: bool = True,
    with_addons: bool = True,
) -> Path:
    """The env sample describes, in root: written by jarvis init's code, then the sample apps and
    add-on and the owner's hand-sealed files, minted, sealed and rendered."""
    from jarvis.render.env import render_env

    name = sample.name
    env_dir = scaffold.write_env(root, sample)
    scaffold.write_sops_rule(root, name, public_keys)
    if hosts_text is not None:
        (env_dir / "inventory" / "hosts.yml").write_text(hosts_text, encoding="utf-8")
    values = env_dir / "values" / "cluster-vars.yaml"
    values.write_text(values.read_text(encoding="utf-8") + "lan_cidrs:\n  - 10.200.0.0/24\n", encoding="utf-8")
    switches = env_dir / "values" / "switches.yaml"
    text = switches.read_text(encoding="utf-8")
    if with_apps and "app-hosting" in sample.features:
        text = text.replace("apps: []\n", "apps:\n" + "".join(f"  - {app}\n" for app in APPS["versions"]))
        (env_dir / "values" / "apps.yaml").write_text(APPS_TEXT, encoding="utf-8")
    if with_addons:
        names = sorted(entry.name for entry in FIXTURE_ADDONS.iterdir() if entry.is_dir())
        text = text.replace("addons: []\n", "addons:\n" + "".join(f"  - {name}\n" for name in names))
    switches.write_text(text, encoding="utf-8")
    _mint(root, name)
    if "offsite-backup-copy" in sample.features:
        # The owner seals the target's login by hand; a placeholder stands in.
        _seal(root, name, "backups-offsite", "platform-backups",
              {"RESTIC_REPOSITORY": "rest:https://offsite.example.invalid/", "RESTIC_PASSWORD": "placeholder"})  # fmt: skip
    previous = os.environ.get("APPS_REPO")
    os.environ.setdefault("APPS_REPO", str(FIXTURE_APPS_REPO))
    try:
        render_env(env_dir, root)
    finally:
        if previous is None:
            os.environ.pop("APPS_REPO", None)
    from jarvis.schema.env import addon_documents, addon_secret_namespaces, enabled_addons

    for addon in enabled_addons(env_dir):
        for namespace in addon_secret_namespaces(addon_documents(root, addon)):
            _seal(root, name, f"{namespace}-fixture", namespace, {"greeting": "placeholder"}, folder=namespace)
    return env_dir


def build(root: Path, name: str = NAME, **options) -> Path:
    """A sample instance at root: core linked in, one sample env, sealed to a throwaway key."""
    root.mkdir(parents=True, exist_ok=True)
    (root / ".gitignore").write_text(".age/\n", encoding="utf-8")
    link_core(root)
    public = throwaway_key(root)
    add_env(root, name, public_keys=[public], **options)
    return root


if __name__ == "__main__":
    dest = Path(sys.argv[1]).resolve()
    subprocess.run(["git", "init", "-q", str(dest)], check=True)
    build(dest)
    for args in (["add", "-A"], ["-c", "user.email=sample@example.invalid", "-c", "user.name=sample", "commit", "-q", "-m", "sample"]):
        subprocess.run(["git", "-C", str(dest), *args], check=True)
    print(dest)
