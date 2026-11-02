import json
import re
from pathlib import Path

import yaml

from support import REPO_ROOT as ROOT
from support import ExampleCopy
from support import renovate_python_pattern as _js_to_python


def test_each_env_flux_instance_version_is_a_renovate_pin(example_copy: ExampleCopy) -> None:
    """R33: Renovate reads each env FluxInstance's Flux version, its only pin."""
    renovate = json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))
    paths = sorted(ROOT.glob("envs/*/flux/flux-instance.yaml"))
    paths.append(example_copy.env_dir / "flux" / "flux-instance.yaml")
    assert paths
    for path in paths:
        if path.is_relative_to(ROOT):
            rel = path.relative_to(ROOT).as_posix()
        else:
            rel = f"envs/{example_copy.name}/flux/flux-instance.yaml"
        managers = [
            manager
            for manager in renovate["customManagers"]
            if any(re.search(p.strip("/"), rel) for p in manager["managerFilePatterns"])
        ]
        text = path.read_text(encoding="utf-8")
        found = [
            match
            for manager in managers
            for pattern in manager["matchStrings"]
            for match in re.finditer(_js_to_python(pattern), text)
        ]
        assert len(found) == 1, rel
        version = yaml.safe_load(text)["spec"]["distribution"]["version"]
        assert found[0].group("depName") == "fluxcd/flux2", rel
        assert found[0].group("currentValue") == version, rel


# Release-age and pull-request delay settings. Absent keys keep Renovate's no-wait defaults.
_RELEASE_AGE_OR_PR_DELAY = frozenset(
    {
        "automergeSchedule",
        "dependencyDashboardApproval",
        "masterIssueApproval",
        "minimumReleaseAge",
        "minimumReleaseAgeBehaviour",
        "minimumReleaseAgeBuffer",
        "prCreation",
        "prNotPendingHours",
        "schedule",
        "stabilityDays",
    }
)


def _renovate_config() -> dict:
    return json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))


def _delay_keys(node: object) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _RELEASE_AGE_OR_PR_DELAY:
                found.append(key)
            found.extend(_delay_keys(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_delay_keys(item))
    return found


def _automerge_values(node: object) -> list[object]:
    found: list[object] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "automerge":
                found.append(value)
            else:
                found.extend(_automerge_values(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_automerge_values(item))
    return found


def test_updates_open_with_no_release_hold_and_no_automerge() -> None:
    """Renovate opens every update with no release-age wait and never auto-merges."""
    renovate = _renovate_config()
    assert _delay_keys(renovate) == []
    assert renovate["automerge"] is False
    assert True not in _automerge_values(renovate)


def test_seeded_release_age_hold_fails() -> None:
    """A package rule that sets a release-age hold fails."""
    renovate = _renovate_config()
    renovate["packageRules"].append(
        {
            "matchUpdateTypes": ["minor", "patch", "digest"],
            "minimumReleaseAge": "3 days",
        }
    )
    assert _delay_keys(renovate) == ["minimumReleaseAge"]


def test_seeded_package_rule_automerge_fails() -> None:
    """A package rule that sets automerge true fails."""
    renovate = _renovate_config()
    renovate["packageRules"].append({"matchUpdateTypes": ["patch"], "automerge": True})
    assert True in _automerge_values(renovate)


def test_ansible_core_stays_on_the_2_20_line() -> None:
    """ansible 13.x requires ansible-core 2.20.x. The cap is raised with an ansible major bump."""
    renovate = json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))
    rule = next(
        item
        for item in renovate["packageRules"]
        if item.get("matchPackageNames") == ["ansible-core"]
    )
    assert rule["matchManagers"] == ["pip_requirements"]
    assert rule["matchFileNames"] == ["requirements-dev.txt"]
    assert rule["allowedVersions"] == "<2.21"
    assert "2.20" in rule["description"]


def _next_minor_cap(version: str) -> str:
    major, minor, _patch = version.split(".")
    return f"<{major}.{int(minor) + 1}"


def _same_minor(left: str, right: str) -> bool:
    return tuple(left.split(".")[:2]) == tuple(right.split(".")[:2])


def test_managed_image_without_a_version_tag_fails() -> None:
    """Every custom manager that captures a digest also captures a version. A digest-only line does not match."""
    renovate = json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))
    managers = [
        manager
        for manager in renovate["customManagers"]
        if any("currentDigest" in pattern for pattern in manager.get("matchStrings", []))
    ]
    assert managers
    chart_version = yaml.safe_load(
        (ROOT / "platform/backups/controller/helmrelease.yaml").read_text(encoding="utf-8")
    )["spec"]["chart"]["spec"]["version"]
    saw_volsync = False
    for manager in managers:
        patterns = [pattern for pattern in manager["matchStrings"] if "currentDigest" in pattern]
        for pattern_src in patterns:
            assert "currentValue" in pattern_src, manager.get("description")
            pattern = _js_to_python(pattern_src)
            for path in _manager_files(manager):
                text = path.read_text(encoding="utf-8")
                assert _unmatched_image_lines(text, pattern_src) == [], path
            found = [
                match
                for path in _manager_files(manager)
                for match in re.finditer(pattern, path.read_text(encoding="utf-8"))
            ]
            assert found, manager.get("description")
            for match in found:
                version = match.group("currentValue")
                digest = match.group("currentDigest")
                assert version
                assert digest.startswith("sha256:")
                bare = match.group(0).replace(version, "", 1)
                assert bare != match.group(0)
                assert re.search(pattern, bare) is None
                if "quay.io/backube/volsync" in match.group(0):
                    saw_volsync = True
                    assert _same_minor(version, chart_version)
    assert saw_volsync
    rule = next(
        item
        for item in renovate["packageRules"]
        if item.get("matchDepNames") == ["quay.io/backube/volsync"]
    )
    assert rule["allowedVersions"] == _next_minor_cap(chart_version)
    assert rule["groupName"] == "volsync-image"
    volsync = next(
        manager
        for manager in managers
        if any("quay.io/backube/volsync" in pattern for pattern in manager["matchStrings"])
    )
    pattern_src = next(pattern for pattern in volsync["matchStrings"] if "currentDigest" in pattern)
    source = (ROOT / "platform/backups/controller/helmrelease.yaml").read_text(encoding="utf-8")
    assert _unmatched_image_lines(source, pattern_src) == []
    versionless = source + "\nimage: quay.io/backube/volsync@sha256:" + ("ab" * 32) + "\n"
    assert _unmatched_image_lines(versionless, pattern_src)


def _unmatched_image_lines(text: str, pattern_src: str) -> list[str]:
    """Image lines that name this manager's dep and are not inside a match."""
    dep = re.search(r"\(\?<depName>([^)]+)\)", pattern_src)
    assert dep is not None
    pattern = _js_to_python(pattern_src)
    spans = [(match.start(), match.end()) for match in re.finditer(pattern, text)]
    missed: list[str] = []
    for line in re.finditer(rf"image:[^\n]*{dep.group(1)}[^\n]*", text):
        if any(start < line.end() and line.start() < end for start, end in spans):
            continue
        missed.append(line.group(0).strip())
    return missed


def _manager_files(manager: dict) -> list[Path]:
    files: list[Path] = []
    patterns = manager.get("managerFilePatterns") or []
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in {".git", ".venv"} for part in path.parts):
            continue
        rel = path.relative_to(ROOT).as_posix()
        if any(re.search(pattern.strip("/"), rel) for pattern in patterns):
            files.append(path)
    return files


def test_testapp_image_tag_and_digest_are_a_custom_manager() -> None:
    """R33: platform/testapp image pins are a file-scoped regex manager."""
    source = (ROOT / "platform/testapp/testapp.yaml").read_text(encoding="utf-8")
    renovate = json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))
    managers = [
        manager
        for manager in renovate["customManagers"]
        if any("platform\\/testapp\\/" in pattern for pattern in manager["managerFilePatterns"])
    ]
    assert len(managers) == 1
    manager = managers[0]
    assert manager["datasourceTemplate"] == "docker"
    assert manager["managerFilePatterns"] == ["/^platform\\/testapp\\/.+\\.yaml$/"]
    found = list(re.finditer(_js_to_python(manager["matchStrings"][0]), source))
    assert len(found) == 1
    image = next(line.strip() for line in source.splitlines() if line.strip().startswith("image:"))
    name, digest = image.removeprefix("image: ").split("@", 1)
    assert found[0].group("depName") == "nginxinc/nginx-unprivileged"
    assert found[0].group("currentValue") == name.rsplit(":", 1)[1]
    assert found[0].group("currentDigest") == digest
