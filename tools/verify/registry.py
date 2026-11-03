"""Named verify components. Platform-layer names always run today."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from verify.common import CheckError, Context

Builder = Callable[[Context], list[tuple[int, str, Callable[[], object]]]]

DEFAULT_ENABLED = ("platform", "backups")
COMPONENTS: dict[str, Builder] = {}


def register(name: str) -> Callable[[Builder], Builder]:
    def wrap(fn: Builder) -> Builder:
        COMPONENTS[name] = fn
        return fn

    return wrap


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
