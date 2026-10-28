"""Renovate config: flux distribution pin is tracked and not auto-merged."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULTS = REPO_ROOT / "infra/ansible/roles/flux-operator/defaults/main.yml"


def _compile_file_pattern(pattern: str) -> re.Pattern[str]:
    body = pattern[1:-1] if pattern.startswith("/") and pattern.endswith("/") else pattern
    return re.compile(body)


def _compile_match(pattern: str) -> re.Pattern[str]:
    # Renovate named groups are (?<name>...). Python's re uses (?P<name>...).
    return re.compile(pattern.replace("(?<", "(?P<"))


def test_renovate_tracks_flux_distribution_and_does_not_automerge() -> None:
    config = json.loads((REPO_ROOT / "renovate.json").read_text(encoding="utf-8"))
    assert config["automerge"] is False
    rule = next(
        item
        for item in config["packageRules"]
        if item.get("matchDepNames") == ["fluxcd/flux2"]
    )
    assert rule.get("automerge") is False
    manager = next(
        item
        for item in config["customManagers"]
        if any("flux_distribution_version" in pattern for pattern in item["matchStrings"])
    )
    file_patterns = [_compile_file_pattern(item) for item in manager["managerFilePatterns"]]
    matchers = [_compile_match(item) for item in manager["matchStrings"]]
    pinned = yaml.safe_load(_DEFAULTS.read_text(encoding="utf-8"))["flux_distribution_version"]
    assert isinstance(pinned, str) and pinned
    paths = [_DEFAULTS, *sorted(REPO_ROOT.glob("envs/*/flux/flux-instance.yaml"))]
    assert len(paths) >= 2
    for path in paths:
        rel = path.relative_to(REPO_ROOT).as_posix()
        assert any(pattern.search(rel) for pattern in file_patterns), rel
        found = [
            match.group("currentValue")
            for matcher in matchers
            for match in matcher.finditer(path.read_text(encoding="utf-8"))
        ]
        assert found == [pinned], rel
