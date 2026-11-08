"""Render every generated file of an env from its values, or check the committed ones.

    tools/render --env <env> [--check]

From values/cluster-vars.yaml, values/switches.yaml and values/apps.yaml it writes flux/sync.yaml,
flux/kustomization.yaml, flux/kustomizations/services.yaml, flux/platform-configs/kustomization.yaml
and its backups/ registrations, and flux/generated/ (the cluster-vars ConfigMap, the app wiring and
the backup selection). --check writes nothing: it renders into a scratch copy of the env and lists
each generated file that differs from the committed one (exit 1). INSTANCE=<path> points it at an
instance checkout.

Why a render and not a Flux ResourceSet: the app wiring's values are computed, not copied. Each
exception line becomes a grant annotation, a LAN grant is checked against lan_cidrs, a pin becomes a
tag ref, and an app that asks for something the env does not grant is refused. That logic is Python
(jarvis.lib.apps) and runs before anything reaches the cluster, where a bad value fails a PR instead
of a reconcile. A ResourceSet would still need those values handed to it from a generated file, so it
would remove no file and move checks from CI to the cluster. One render with a --check keeps the
logic testable and makes a stale file a CI failure.
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
import tempfile
from pathlib import Path

from jarvis.lib import render_switches
from jarvis.lib.env import instance_root
from jarvis.lib.render_cluster_vars_configmap import render_document

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIGMAP_REL = Path("flux") / "generated" / "cluster-vars-configmap.yaml"
# What the render owns under the env: these files, and every file under these folders.
_GENERATED_FILES = (
    Path("flux") / "sync.yaml",
    Path("flux") / "kustomization.yaml",
    Path("flux") / "kustomizations" / "services.yaml",
    Path("flux") / "platform-configs" / "kustomization.yaml",
)
_GENERATED_DIRS = (Path("flux") / "generated", Path("flux") / "platform-configs" / "backups")


def render_env(env_dir: Path, repo_root: Path) -> None:
    """Write every generated file of env_dir. SystemExit names what the values get wrong."""
    render_switches.render_env(env_dir, repo_root)
    configmap = env_dir / CONFIGMAP_REL
    configmap.parent.mkdir(parents=True, exist_ok=True)
    configmap.write_text(render_document(env_dir / "values" / "cluster-vars.yaml"), encoding="utf-8")


def generated_files(env_dir: Path) -> set[Path]:
    """The render's files that exist under env_dir, relative to it."""
    found = {rel for rel in _GENERATED_FILES if (env_dir / rel).is_file()}
    for rel in _GENERATED_DIRS:
        if (env_dir / rel).is_dir():
            found |= {path.relative_to(env_dir) for path in (env_dir / rel).rglob("*") if path.is_file()}
    return found


def stale(env_dir: Path, repo_root: Path) -> list[str]:
    """Each generated file that is missing, extra or different, as committed against a fresh render."""
    with tempfile.TemporaryDirectory() as work:
        copy = Path(work) / env_dir.name
        shutil.copytree(env_dir, copy)
        # Only a fresh render's files: what the committed tree holds beyond them is reported.
        for rel in generated_files(copy):
            (copy / rel).unlink()
        render_env(copy, repo_root)
        want, have = generated_files(copy), generated_files(env_dir)
        problems = [f"{rel}: missing" for rel in sorted(want - have)]
        problems += [f"{rel}: not rendered from the values; delete it or render again" for rel in sorted(have - want)]
        problems += [
            f"{rel}: differs from the render"
            for rel in sorted(want & have)
            if not filecmp.cmp(copy / rel, env_dir / rel, shallow=False)
        ]
        return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", required=True, help="Env name under envs/")
    parser.add_argument("--check", action="store_true", help="Write nothing; list each stale generated file")
    args = parser.parse_args(argv)
    repo_root = instance_root(REPO_ROOT)
    env_dir = repo_root / "envs" / args.env
    if not (env_dir / "values" / "cluster-vars.yaml").is_file():
        raise SystemExit(f"missing {env_dir / 'values' / 'cluster-vars.yaml'}")
    if args.check:
        problems = stale(env_dir, repo_root)
        for problem in problems:
            print(f"envs/{args.env}/{problem}", file=sys.stderr)
        if problems:
            print(f"render: re-render with tools/render --env {args.env}", file=sys.stderr)
        return 1 if problems else 0
    render_env(env_dir, repo_root)
    return 0
