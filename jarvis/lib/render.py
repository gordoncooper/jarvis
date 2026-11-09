"""Render every generated file of an env from its values, or check the committed ones.

    tools/render --env <env> [--check]

From values/cluster-vars.yaml, values/switches.yaml and values/apps.yaml it writes the env's
whole flux/: sync.yaml, kustomization.yaml, kustomizations/services.yaml,
platform-configs/kustomization.yaml and its backups/ registrations, and generated/ (the
cluster-vars ConfigMap, the app wiring and the backup selection). flux/ is render output only: a
render replaces it whole, so a file core moved or retired leaves on the next render, and a file
nobody renders is reported stale. The render runs on a copy and swaps flux/ in only when it
succeeds, so a refused render leaves the env as it was. --check writes nothing: it lists each file
under flux/ that is missing, extra or different from a fresh render (exit 1). INSTANCE=<path>
points it at an instance checkout.

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
FLUX_DIR = "flux"


def _render_into(env_dir: Path, repo_root: Path) -> None:
    """Write every generated file into env_dir, which holds no flux/ yet."""
    render_switches.render_env(env_dir, repo_root)
    configmap = env_dir / CONFIGMAP_REL
    configmap.parent.mkdir(parents=True, exist_ok=True)
    configmap.write_text(render_document(env_dir / "values" / "cluster-vars.yaml"), encoding="utf-8")


def _fresh(env_dir: Path, work: Path, repo_root: Path) -> Path:
    """A copy of env_dir under work, its flux/ rendered from scratch from the values."""
    copy = work / env_dir.name
    shutil.copytree(env_dir, copy, ignore=lambda folder, _names: [FLUX_DIR] if Path(folder) == env_dir else [])
    _render_into(copy, repo_root)
    return copy


def render_env(env_dir: Path, repo_root: Path) -> None:
    """Replace env_dir's flux/ with a fresh render. SystemExit names what the values get wrong,
    and leaves flux/ as it was."""
    with tempfile.TemporaryDirectory() as work:
        fresh = _fresh(env_dir, Path(work), repo_root)
        shutil.rmtree(env_dir / FLUX_DIR, ignore_errors=True)
        shutil.copytree(fresh / FLUX_DIR, env_dir / FLUX_DIR)


def generated_files(env_dir: Path) -> set[Path]:
    """Every file under env_dir's flux/, relative to env_dir."""
    flux = env_dir / FLUX_DIR
    return {path.relative_to(env_dir) for path in flux.rglob("*") if path.is_file()} if flux.is_dir() else set()


def stale(env_dir: Path, repo_root: Path) -> list[str]:
    """Each file under flux/ that is missing, extra or different, as committed against a fresh render."""
    with tempfile.TemporaryDirectory() as work:
        fresh = _fresh(env_dir, Path(work), repo_root)
        want, have = generated_files(fresh), generated_files(env_dir)
        problems = [f"{rel}: missing" for rel in sorted(want - have)]
        problems += [f"{rel}: not rendered from the values; the next render removes it" for rel in sorted(have - want)]
        problems += [
            f"{rel}: differs from the render"
            for rel in sorted(want & have)
            if not filecmp.cmp(fresh / rel, env_dir / rel, shallow=False)
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
