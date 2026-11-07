import copy
import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from support import REPO_ROOT as ROOT
from support import ExampleCopy
from support import renovate_python_pattern as _js_to_python


def test_each_env_flux_instance_version_is_a_renovate_pin(example_copy: ExampleCopy) -> None:
    """R33: Renovate reads each env FluxInstance's Flux version, its only pin."""
    renovate = json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))
    paths = sorted(ROOT.glob("envs/*/flux/flux-instance.yaml"))
    tracked = set(paths)
    paths.append(example_copy.env_dir / "flux" / "flux-instance.yaml")
    assert paths
    for path in paths:
        # The copy may sit in a git-ignored directory inside the repo; it is still a copy.
        if path in tracked:
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


def _nested_extends(node: object, top: bool = True) -> int:
    count = 0
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "extends" and not top:
                count += 1
            count += _nested_extends(value, top=False)
    elif isinstance(node, list):
        for item in node:
            count += _nested_extends(item, top=False)
    return count


# The host binaries whose bumps wait for approval in the dependency dashboard.
_APPROVAL_PINS = frozenset({"k3s-io/k3s", "helm/helm"})


def _rule_pins(rule: dict) -> set[str]:
    return set(rule.get("matchPackageNames") or []) | set(rule.get("matchDepNames") or [])


def _without_approved_pins(config: dict) -> dict:
    """The config with dependencyDashboardApproval removed from the two host-binary rules."""
    stripped = copy.deepcopy(config)
    for rule in stripped["packageRules"]:
        if _rule_pins(rule) <= _APPROVAL_PINS and _rule_pins(rule):
            rule.pop("dependencyDashboardApproval", None)
    return stripped


def _assert_no_hold_or_automerge(config: dict) -> None:
    # A change to extends must re-check the resolved config for holds and automerge.
    assert config["extends"] == ["config:recommended"]
    assert _nested_extends(config) == 0
    assert _delay_keys(_without_approved_pins(config)) == []
    assert config["automerge"] is False
    assert True not in _automerge_values(config)


def test_updates_open_with_no_release_hold_and_no_automerge() -> None:
    """Renovate opens every update with no release-age wait and never auto-merges."""
    _assert_no_hold_or_automerge(_renovate_config())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda config: config["packageRules"].append({"minimumReleaseAge": "3 days"}),
        lambda config: config["packageRules"].append({"dependencyDashboardApproval": True}),
        lambda config: config["packageRules"].append({"automerge": True}),
        lambda config: config.update(automerge=True),
        lambda config: config["extends"].append("schedule:weekly"),
        lambda config: config["packageRules"].append({"extends": [":automergeMinor"]}),
        lambda config: config.update(lockFileMaintenance={"automerge": True}),
        lambda config: config.update(vulnerabilityAlerts={"automerge": True}),
        lambda config: config.update(minor={"automerge": True}),
        lambda config: config.update(lockFileMaintenance={"extends": [":automergeAll"]}),
    ],
    ids=[
        "minimumReleaseAge",
        "approval-on-another-rule",
        "rule-automerge",
        "top-level-automerge",
        "extends-preset",
        "rule-extends",
        "lockfile-automerge",
        "vulnerability-automerge",
        "minor-automerge",
        "nested-extends",
    ],
)
def test_seeded_hold_or_automerge_fails(mutate: Callable[[dict], None]) -> None:
    """A hold, automerge, or extra preset fails the same check as the live config."""
    config = _renovate_config()
    mutate(config)
    with pytest.raises(AssertionError):
        _assert_no_hold_or_automerge(config)


def test_k3s_and_helm_bumps_wait_for_dashboard_approval() -> None:
    """The host binaries are not disabled: a bump is offered, and a PR opens only once approved."""
    rules = _renovate_config()["packageRules"]
    for pin in sorted(_APPROVAL_PINS):
        rule = next(item for item in rules if pin in _rule_pins(item))
        assert rule["dependencyDashboardApproval"] is True, pin
        assert "enabled" not in rule, pin


def _manager_matches(rel: str, text: str) -> list[re.Match[str]]:
    """Matches of every custom manager whose file pattern selects rel."""
    return [
        match
        for manager in _renovate_config()["customManagers"]
        if any(re.search(pattern.strip("/"), rel) for pattern in manager["managerFilePatterns"])
        for pattern in manager["matchStrings"]
        for match in re.finditer(_js_to_python(pattern), text)
    ]


def test_every_tool_version_in_versions_env_is_a_renovate_pin() -> None:
    """A *_version line without a renovate comment, or a comment Renovate can't read, fails."""
    text = (ROOT / "tools/versions.env").read_text(encoding="utf-8")
    declared = dict(re.findall(r"^(\w+_version)=(\S+)$", text, flags=re.MULTILINE))
    assert declared
    found = {
        match.group(0).splitlines()[-1].split("=", 1)[0]: match.group("currentValue")
        for match in _manager_matches("tools/versions.env", text)
    }
    assert found == declared
    unreadable = text.replace("depName=", "dep=")
    assert not _manager_matches("tools/versions.env", unreadable)


def test_each_env_k3s_version_is_a_renovate_pin() -> None:
    paths = sorted(ROOT.glob("envs/*/values/cluster-vars.yaml"))
    assert paths
    for path in paths:
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        found = _manager_matches(rel, text)
        assert [match.group("currentValue") for match in found] == [
            yaml.safe_load(text)["k3s_version"]
        ], rel


def test_ansible_core_stays_on_the_2_21_line() -> None:
    """ansible 14.x requires ansible-core 2.21.x. The cap is raised with an ansible major bump."""
    renovate = json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))
    rule = next(
        item
        for item in renovate["packageRules"]
        if item.get("matchPackageNames") == ["ansible-core"]
    )
    assert rule["matchManagers"] == ["pip_requirements"]
    assert rule["matchFileNames"] == ["requirements-dev.txt"]
    assert rule["allowedVersions"] == "<2.22"
    assert "2.21" in rule["description"]


def _next_minor_cap(version: str) -> str:
    major, minor, _patch = version.split(".")
    return f"<{major}.{int(minor) + 1}"


def _same_minor(left: str, right: str) -> bool:
    return tuple(left.split(".")[:2]) == tuple(right.split(".")[:2])


def test_kubectl_stays_on_the_k3s_minor() -> None:
    """Renovate offers no kubectl minor ahead of k3s, and the pin and the cap match each env's k3s."""
    rule = next(
        item
        for item in _renovate_config()["packageRules"]
        if item.get("matchDepNames") == ["kubernetes/kubernetes"]
    )
    pinned = re.search(
        r"^kubectl_version=(\S+)$", (ROOT / "tools/versions.env").read_text(encoding="utf-8"), re.M
    )
    assert pinned
    paths = sorted(ROOT.glob("envs/*/values/cluster-vars.yaml"))
    assert paths
    for path in paths:
        k3s = yaml.safe_load(path.read_text(encoding="utf-8"))["k3s_version"]
        k3s_version = k3s.removeprefix("v").split("+")[0]
        assert _same_minor(pinned.group(1), k3s_version), path
        assert rule["allowedVersions"] == _next_minor_cap(k3s_version), path


def test_only_kustomize_prefixed_tags_are_kustomize_releases() -> None:
    """The kustomize repo also tags api/, cmd/config/ and old unprefixed releases."""
    rule = next(
        item
        for item in _renovate_config()["packageRules"]
        if item.get("matchDepNames") == ["kubernetes-sigs/kustomize"]
    )
    pattern = re.compile(_js_to_python(rule["extractVersion"]))
    match = pattern.match("kustomize/v5.6.0")
    assert match and match.group("version") == "5.6.0"
    for tag in ("api/v0.19.0", "cmd/config/v0.19.0", "v4.5.7"):
        assert pattern.match(tag) is None, tag


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


@pytest.mark.parametrize("rel", ["platform/testapp/testapp.yaml", "services/hello/app/hello.yaml"])
def test_testapp_image_tag_and_digest_are_a_custom_manager(rel: str) -> None:
    """R33: the canary image pins, and the sample add-on's copy, are one file-scoped regex manager."""
    source = (ROOT / rel).read_text(encoding="utf-8")
    renovate = json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))
    managers = [
        manager
        for manager in renovate["customManagers"]
        if any(re.search(pattern.strip("/"), rel) for pattern in manager["managerFilePatterns"])
    ]
    assert len(managers) == 1
    manager = managers[0]
    assert manager["datasourceTemplate"] == "docker"
    found = list(re.finditer(_js_to_python(manager["matchStrings"][0]), source))
    assert len(found) == 1
    image = next(line.strip() for line in source.splitlines() if line.strip().startswith("image:"))
    name, digest = image.removeprefix("image: ").split("@", 1)
    assert found[0].group("depName") == "nginxinc/nginx-unprivileged"
    assert found[0].group("currentValue") == name.rsplit(":", 1)[1]
    assert found[0].group("currentDigest") == digest


def test_grant_revoke_python_image_is_a_renovate_pin() -> None:
    """The grant reconciler's image is read by a manager, so its tag and digest get bump PRs."""
    rel = "platform/optional/app-hosting/grant-revoke.yaml"
    source = (ROOT / rel).read_text(encoding="utf-8")
    found = _manager_matches(rel, source)
    assert len(found) == 1
    image = next(line.strip() for line in source.splitlines() if line.strip().startswith("image:"))
    name, digest = image.removeprefix("image: ").split("@", 1)
    assert found[0].group("depName") == "python"
    assert found[0].group("currentValue") == name.rsplit(":", 1)[1]
    assert found[0].group("currentDigest") == digest


@pytest.mark.parametrize(
    ("group", "chart"), [("cert-manager-image", "cert-manager"), ("kyverno-image", "kyverno")]
)
def test_chart_and_its_images_update_in_one_pr(group: str, chart: str) -> None:
    """Merged alone, a chart bump or an image bump would run a chart with images of another release."""
    rule = next(item for item in _renovate_config()["packageRules"] if item.get("groupName") == group)
    assert chart in rule["matchDepNames"]


def _unmanaged_renovate_comments(rel: str, text: str) -> list[str]:
    """Renovate comments where no custom manager's match covers the comment or the pin below it."""
    spans = [match.span() for match in _manager_matches(rel, text)]
    return [
        comment.group(0).splitlines()[0].strip()
        for comment in re.finditer(r"# renovate:[^\n]*\n[^\n]*", text)
        if not any(start < comment.end() and comment.start() < end for start, end in spans)
    ]


def test_every_renovate_comment_has_a_manager() -> None:
    """A renovate comment no manager reads gets no bump PRs, and nothing else reports it."""
    tracked = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True, text=True
    ).stdout.split("\0")
    commented = {
        rel: text
        for rel in tracked
        if rel and rel != "renovate.json" and not rel.startswith("ci/") and (ROOT / rel).is_file()
        and "# renovate:" in (text := (ROOT / rel).read_text(encoding="utf-8", errors="ignore"))
    }
    assert commented
    missed = {rel: _unmanaged_renovate_comments(rel, text) for rel, text in commented.items()}
    assert {rel: found for rel, found in missed.items() if found} == {}


def test_seeded_renovate_comment_without_a_manager_fails() -> None:
    """The same pin in a file no manager selects is reported."""
    source = (ROOT / "platform/optional/app-hosting/grant-revoke.yaml").read_text(encoding="utf-8")
    assert _unmanaged_renovate_comments("platform/optional/app-hosting/unmanaged.yaml", source)
