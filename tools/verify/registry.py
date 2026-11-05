"""Named verify components. Core's always run; an add-on's run only when the env enables it."""

from __future__ import annotations

import importlib.util
from collections.abc import Callable, Sequence
from pathlib import Path

from schema.env import ADDONS_DIR, addon_names

from verify.common import CheckError, Context

Builder = Callable[[Context], list[tuple[int, str, Callable[[], object]]]]

DEFAULT_ENABLED = ("platform", "backups")
COMPONENTS: dict[str, Builder] = {}


def register(name: str) -> Callable[[Builder], Builder]:
    def wrap(fn: Builder) -> Builder:
        COMPONENTS[name] = fn
        return fn

    return wrap


def load_addons(core: Path, addons: Sequence[str]) -> tuple[str, ...]:
    """Load services/<name>/verify.py for each enabled add-on that has one. Each registers a
    component under its own name. An add-on that is not enabled is never loaded."""
    loaded: list[str] = []
    known = set(addon_names(core))
    for name in addons:
        if not isinstance(name, str) or name not in known:
            raise CheckError(f"the env enables {name!r}, which is not an add-on in core/{ADDONS_DIR}")
        path = core / ADDONS_DIR / name / "verify.py"
        if not path.is_file():
            continue
        if name not in COMPONENTS:
            spec = importlib.util.spec_from_file_location(f"addon_verify_{name.replace('-', '_')}", path)
            if spec is None or spec.loader is None:
                raise CheckError(f"cannot load {path}")
            spec.loader.exec_module(importlib.util.module_from_spec(spec))
        if name not in COMPONENTS:
            raise CheckError(f"{path} registers no component named {name}")
        loaded.append(name)
    return tuple(loaded)


def enabled_components(enabled: Sequence[str] | None) -> tuple[str, ...]:
    names: list[str] = list(DEFAULT_ENABLED)
    if enabled is None:
        return tuple(names)
    for name in enabled:
        if name not in names:
            names.append(name)
    return tuple(names)


def collect(
    names: Sequence[str], ctx: Context
) -> list[tuple[str, Callable[[], object]]]:
    checks: list[tuple[int, str, Callable[[], object]]] = []
    for name in names:
        builder = COMPONENTS.get(name)
        if builder is None:
            raise CheckError(f"unknown verify component {name}")
        checks.extend(builder(ctx))
    checks.sort(key=lambda item: item[0])
    return [(label, fn) for _order, label, fn in checks]
