"""jarvis render: one command renders every generated file of an env, and --check lists stale ones."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pytest

from jarvis.env.switches import FEATURE_COMPONENTS
from jarvis.render import env as render
from support import copy_sample


def _flux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The flux/ of a core-only copy of the sample's env-00 at tmp_path, the instance jarvis render reads."""
    monkeypatch.setenv("INSTANCE", str(copy_sample(tmp_path)))
    return tmp_path / "envs" / "env-00" / "flux"


def _files(flux: Path) -> dict[Path, bytes]:
    return {path: path.read_bytes() for path in flux.rglob("*") if path.is_file()}


def test_check_passes_a_rendered_env_and_names_each_stale_file(tmp_path: Path, monkeypatch, capsys) -> None:
    """Rule: --check writes nothing, passes a rendered env, and names an edited, a missing and an extra file."""
    flux = _flux(tmp_path, monkeypatch)
    assert render.main(["--env", "env-00", "--check"]) == 0
    sync = flux / "sync.yaml"
    sync.write_text(sync.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
    (flux / "generated" / "cluster-vars-configmap.yaml").unlink()
    (flux / "generated" / "stray.yaml").write_text("kind: ConfigMap\n", encoding="utf-8")
    stale = _files(flux)
    # As python -m runs it: the module's __main__ block makes --check's result the exit status.
    monkeypatch.setattr(sys, "argv", [render.__file__, "--env", "env-00", "--check"])
    with pytest.raises(SystemExit) as exited:
        runpy.run_path(render.__file__, run_name="__main__")
    assert (exited.value.code, capsys.readouterr()) == (1, ("", "".join(f"envs/env-00/flux/{line}\n" for line in (
        "generated/cluster-vars-configmap.yaml: missing",
        "generated/stray.yaml: not rendered from the values; the next render removes it",
        "sync.yaml: differs from the render",
    )) + "render: re-render with jarvis render --env env-00\n"))  # fmt: skip
    assert _files(flux) == stale


def test_render_replaces_flux_whole(tmp_path: Path, monkeypatch) -> None:
    """Rule: flux/ is render output: a render removes a file it does not write anywhere under flux/
    (a file core moved or retired leaves with no upgrade step) and restores an edited one."""
    flux = _flux(tmp_path, monkeypatch)
    rendered = _files(flux)
    (flux / "kustomizations" / "platform-controllers.yaml").write_text("kind: Kustomization\n", encoding="utf-8")
    sync = flux / "sync.yaml"
    sync.write_text(sync.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
    assert render.main(["--env", "env-00"]) == 0
    assert _files(flux) == rendered
    assert render.main(["--env", "env-00", "--check"]) == 0


def test_a_refused_render_leaves_flux_as_it_was(tmp_path: Path, monkeypatch) -> None:
    """Rule: the render works on a copy; values it refuses change nothing under flux/."""
    flux = _flux(tmp_path, monkeypatch)
    before = _files(flux)
    (flux.parent / "values" / "switches.yaml").write_text("features: [no-such-feature]\n", encoding="utf-8")
    with pytest.raises(SystemExit) as refused:
        render.main(["--env", "env-00"])
    # The refusal names the render's working copy of values/switches.yaml, not the env's.
    known = ", ".join(FEATURE_COMPONENTS)
    assert str(refused.value.code).endswith(f"/env-00/values/switches.yaml: features: unknown name 'no-such-feature' (known: {known})")
    assert _files(flux) == before
