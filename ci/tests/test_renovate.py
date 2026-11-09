import copy
import json
import re
import subprocess
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from support import REPO_ROOT as ROOT
from support import renovate_python_pattern as _js_to_python


def test_core_flux_instance_version_is_a_renovate_pin() -> None:
    """Renovate reads core's FluxInstance's Flux version, its only pin; every env runs it at its core tag."""
    renovate = json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))
    for path in [ROOT / "core" / "flux" / "flux-instance.yaml"]:
        rel = path.relative_to(ROOT).as_posix()
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


def _mise_tools() -> dict[str, str]:
    """Each tool mise.toml pins and its version (a table's version key, or the plain value)."""
    tools = tomllib.loads((ROOT / "mise.toml").read_text(encoding="utf-8"))["tools"]
    return {name: str(value["version"] if isinstance(value, dict) else value) for name, value in tools.items()}


def test_mise_pins_every_tool_exactly() -> None:
    """Rule: every CLI is one exact release, so mise.lock holds one checksum per platform for it
    and Renovate moves it by PR. A range or "latest" would install whatever is newest."""
    tools = _mise_tools()
    assert {"uv", "sops", "age", "kubectl", "kustomize", "kubeconform", "kyverno", "gitleaks", "restic"} <= set(tools)
    for name, version in tools.items():
        assert re.fullmatch(r"\d+\.\d+\.\d+", version), (name, version)
    settings = tomllib.loads((ROOT / "mise.toml").read_text(encoding="utf-8"))["settings"]
    assert settings["lockfile"] is True


def test_cli_pins_match_the_cluster_images() -> None:
    """Rule: the restic, rest-server and kyverno CLIs the tests run are the releases the cluster
    runs, and Renovate moves each CLI in its image's group."""
    tools = _mise_tools()
    images = {
        "restic": ("core/platform/backups/restic-check.yaml", r"image: restic/restic:(\d+\.\d+\.\d+)@"),
        "github:restic/rest-server": ("core/platform/backups/rest-server.yaml", r"image: restic/rest-server:(\d+\.\d+\.\d+)@"),
        "kyverno": ("core/platform/kyverno/helmrelease.yaml", r'tag: "v(\d+\.\d+\.\d+)@'),
    }
    for tool, (rel, pattern) in images.items():
        found = set(re.findall(pattern, (ROOT / rel).read_text(encoding="utf-8")))
        assert found == {tools[tool]}, (tool, rel, found)
    groups = {
        name: rule["groupName"]
        for rule in _renovate_config()["packageRules"]
        if rule.get("matchManagers") == ["mise"] and "groupName" in rule
        for name in rule["matchDepNames"]
    }
    assert groups["restic"] == groups["github:restic/rest-server"] == "restic"
    assert groups["kyverno"] == "kyverno"


def test_core_k3s_version_is_a_renovate_pin() -> None:
    """Rule: Renovate reads core's k3s release (group_vars), the default every env runs."""
    for path in [ROOT / "infra/ansible/group_vars/all.yml"]:
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
    assert rule["matchManagers"] == ["pep621"]
    assert rule["matchFileNames"] == ["pyproject.toml"]
    assert rule["allowedVersions"] == "<2.22"
    assert "2.21" in rule["description"]


def _next_minor_cap(version: str) -> str:
    major, minor, _patch = version.split(".")
    return f"<{major}.{int(minor) + 1}"


def _same_minor(left: str, right: str) -> bool:
    return tuple(left.split(".")[:2]) == tuple(right.split(".")[:2])


def test_the_flux_cli_is_the_cluster_flux_version() -> None:
    """Rule: make deps installs the flux CLI at the version core's FluxInstance runs, and Renovate
    moves both in one PR (both read the package fluxcd/flux2)."""
    instance = yaml.safe_load((ROOT / "core/flux/flux-instance.yaml").read_text(encoding="utf-8"))
    assert _mise_tools()["flux2"] == instance["spec"]["distribution"]["version"]
    rule = next(item for item in _renovate_config()["packageRules"] if item.get("groupName") == "flux")
    assert rule["matchPackageNames"] == ["fluxcd/flux2"]


def test_kubectl_stays_on_the_k3s_minor() -> None:
    """Renovate offers no kubectl minor ahead of k3s, kube-apiserver moves with kubectl, and the
    pin and the cap match core's k3s release."""
    rule = next(
        item
        for item in _renovate_config()["packageRules"]
        if item.get("matchDepNames") == ["kubectl", "aqua:kubernetes/kubernetes/kube-apiserver"]
    )
    tools = _mise_tools()
    pinned = tools["kubectl"]
    assert tools["aqua:kubernetes/kubernetes/kube-apiserver"] == pinned
    for path in [ROOT / "infra/ansible/group_vars/all.yml"]:
        k3s = yaml.safe_load(path.read_text(encoding="utf-8"))["k3s_version"]
        k3s_version = k3s.removeprefix("v").split("+")[0]
        assert _same_minor(pinned, k3s_version), path
        assert rule["allowedVersions"] == _next_minor_cap(k3s_version), path


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
        (ROOT / "core/platform/backups/controller/helmrelease.yaml").read_text(encoding="utf-8")
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
    source = (ROOT / "core/platform/backups/controller/helmrelease.yaml").read_text(encoding="utf-8")
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


def test_volsync_chart_prs_name_the_vendored_crd() -> None:
    """Nothing updates the vendored VolumeSnapshot CRD, so a chart bump's PR says to re-check it."""
    renovate = json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))
    rule = next(item for item in renovate["packageRules"] if item.get("matchDepNames") == ["volsync"])
    crd = next(word.strip("`") for word in rule["prBodyNotes"][0].split() if word.endswith("crd.yaml`"))
    assert (ROOT / crd).is_file()


@pytest.mark.parametrize("rel", ["core/platform/testapp/testapp.yaml"])
def test_testapp_image_tag_and_digest_are_a_custom_manager(rel: str) -> None:
    """The canary image pins are one file-scoped regex manager (an add-on that runs the image joins it)."""
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
    rel = "core/feature/app-hosting/grant-revoke.yaml"
    source = (ROOT / rel).read_text(encoding="utf-8")
    found = _manager_matches(rel, source)
    assert len(found) == 1
    image = next(line.strip() for line in source.splitlines() if line.strip().startswith("image:"))
    name, digest = image.removeprefix("image: ").split("@", 1)
    assert found[0].group("depName") == "python"
    assert found[0].group("currentValue") == name.rsplit(":", 1)[1]
    assert found[0].group("currentDigest") == digest


@pytest.mark.parametrize(
    ("group", "dep"),
    [("cert-manager", "cert-manager"), ("kyverno", "kyverno"), ("kyverno", "ghcr.io/kyverno/kyverno")],
)
def test_chart_cli_and_images_update_in_one_pr(group: str, dep: str) -> None:
    """Merged alone, a chart, CLI or image bump would leave its release's other pins behind. The
    CLI's rule (mise) shares the group with the chart's and images' (custom.regex)."""
    rules = [item for item in _renovate_config()["packageRules"] if item.get("groupName") == group]
    assert any(dep in rule["matchDepNames"] for rule in rules)


_RENOVATE_COMMENT = r"# renovate:[^\n]*"
_DIGEST_PIN = r"[^\n]*sha256:[a-f0-9]{64}[^\n]*"


def _unmanaged(rel: str, text: str, line: str) -> list[str]:
    """Lines matching line that no custom manager's match covers."""
    spans = [match.span() for match in _manager_matches(rel, text)]
    return [
        found.group(0).strip()
        for found in re.finditer(line, text)
        if not any(start < found.end() and found.start() < end for start, end in spans)
    ]


def _tracked() -> dict[str, str]:
    """Every tracked file outside ci/ and the Renovate configs (renovate.json, renovate/), by path."""
    listed = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True, text=True
    ).stdout.split("\0")
    return {
        rel: (ROOT / rel).read_text(encoding="utf-8", errors="ignore")
        for rel in listed
        if rel and rel != "renovate.json" and not rel.startswith(("ci/", "renovate/")) and (ROOT / rel).is_file()
    }


def test_every_renovate_comment_is_read_by_a_manager() -> None:
    """A comment no manager reads gets no bump PRs, and a comment a manager ignores can say anything."""
    commented = {rel: text for rel, text in _tracked().items() if "# renovate:" in text}
    assert commented
    missed = {rel: _unmanaged(rel, text, _RENOVATE_COMMENT) for rel, text in commented.items()}
    assert {rel: found for rel, found in missed.items() if found} == {}


def test_every_digest_pin_in_a_manifest_is_read_by_a_manager() -> None:
    """A digest pin needs no comment to pass digest-pin, so one with no manager would never move."""
    manifests = {
        rel: text
        for rel, text in _tracked().items()
        if rel.startswith(("core/platform/", "addons/")) and rel.endswith((".yaml", ".yml"))
    }
    assert any(re.search(_DIGEST_PIN, text) for text in manifests.values())
    missed = {rel: _unmanaged(rel, text, _DIGEST_PIN) for rel, text in manifests.items()}
    assert {rel: found for rel, found in missed.items() if found} == {}


@pytest.mark.parametrize("line", [_RENOVATE_COMMENT, _DIGEST_PIN], ids=["comment", "digest-pin"])
def test_seeded_pin_without_a_manager_fails(line: str) -> None:
    """The same pin in a file no manager selects is reported."""
    source = (ROOT / "core/feature/app-hosting/grant-revoke.yaml").read_text(encoding="utf-8")
    assert _unmanaged("core/feature/app-hosting/unmanaged.yaml", source, line)


def test_seeded_comment_a_manager_ignores_fails() -> None:
    """A comment whose depName no manager reads is reported, though the pin below it is managed."""
    rel = "core/platform/backups/restic-check.yaml"
    source = (ROOT / rel).read_text(encoding="utf-8").replace("depName=restic/restic", "depName=other/image")
    assert _unmanaged(rel, source, _RENOVATE_COMMENT)


def test_etcd_stays_on_the_minor_kubeadm_pairs_with_kubectl() -> None:
    """Rule: the loopback etcd stays on 3.6 while kubectl is on 1.36; raise both together."""
    renovate = json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))
    caps = [rule["allowedVersions"] for rule in renovate["packageRules"] if rule.get("matchDepNames") == ["etcd"]]
    assert caps == ["<3.7"]
    tools = _mise_tools()
    assert tools["etcd"].startswith("3.6.") and tools["kubectl"].startswith("1.36.")


_INSTANCE_PRESET = ROOT / "renovate" / "instance.json"


def _preset_matches(rel: str, text: str) -> list[re.Match[str]]:
    preset = json.loads(_INSTANCE_PRESET.read_text(encoding="utf-8"))
    return [
        match
        for manager in preset["customManagers"]
        if any(re.search(p.strip("/"), rel) for p in manager["managerFilePatterns"])
        for pattern in manager["matchStrings"]
        for match in re.finditer(_js_to_python(pattern), text)
    ]


def test_instance_preset_reads_every_pin_an_env_holds() -> None:
    """Rule: the instance preset opens PRs for an env's core tag (cluster-vars only: sync.yaml is
    rendered from it) and k3s; a core that follows a branch is not a pin it moves."""
    cluster_vars = (ROOT / "envs/env-00/values/cluster-vars.yaml").read_text(encoding="utf-8")
    cluster_vars = re.sub(r"(?m)^core_git_ref:.*$", "core_git_ref: refs/tags/v1.2.3", cluster_vars)
    core = {m.group("depName"): m.group("currentValue")
            for m in _preset_matches("envs/env-x/values/cluster-vars.yaml", cluster_vars) if "depName" in m.groupdict()}
    assert core == {"example-owner/example-core": "v1.2.3"}
    # k3s: an env that overrides core's release holds the pin, and the preset reads it.
    assert not [m for m in _preset_matches("envs/env-x/values/cluster-vars.yaml", cluster_vars) if "depName" not in m.groupdict()]
    overridden = cluster_vars + "k3s_version: v1.36.5+k3s1\nk3s_sha256: " + "0" * 64 + "\n"
    k3s = [m.group("currentValue") for m in _preset_matches("envs/env-x/values/cluster-vars.yaml", overridden)
           if "depName" not in m.groupdict()]
    assert k3s == ["v1.36.5+k3s1"]
    # The render writes the core GitRepository's ref from core_git_ref; the preset reads no render output.
    sync = (ROOT / "envs/env-00/flux/sync.yaml").read_text(encoding="utf-8")
    assert not _preset_matches("envs/env-x/flux/sync.yaml", sync)
    # Flux comes with the core tag: an env holds no FluxInstance, and the preset reads none.
    instance = (ROOT / "core/flux/flux-instance.yaml").read_text(encoding="utf-8")
    assert not _preset_matches("envs/env-x/flux/flux-instance.yaml", instance)
    branch = re.sub(r"(?m)^core_git_ref:.*$", "core_git_ref: refs/heads/main", cluster_vars)
    assert not [m for m in _preset_matches("envs/env-x/values/cluster-vars.yaml", branch) if "depName" in m.groupdict()]


def test_instance_preset_moves_nothing_on_its_own() -> None:
    preset = json.loads(_INSTANCE_PRESET.read_text(encoding="utf-8"))
    assert preset["automerge"] is False
    assert not any(rule.get("automerge") for rule in preset["packageRules"])


def test_instance_preset_reads_each_app_pin() -> None:
    """Rule: the preset opens one PR per app pin under versions, and no exception line reads as one."""
    text = (ROOT / "envs/env-00/values/apps.yaml").read_text(encoding="utf-8")
    pins = {m.group("depName"): m.group("currentValue") for m in _preset_matches("envs/env-x/values/apps.yaml", text)}
    assert pins == yaml.safe_load(text)["versions"]


def test_instance_preset_makes_one_core_pr_and_one_app_pr_per_env() -> None:
    """Rule: a core release is one PR (each env's core pin, the instance workflow and the preset
    share the "jarvis core" group, with no per-env branch prefix); an app release opens one PR per
    env, its branch prefixed with the env's folder name."""
    preset = json.loads(_INSTANCE_PRESET.read_text(encoding="utf-8"))
    rules = preset["packageRules"]
    core = [rule for rule in rules if rule.get("groupName") == "jarvis core"]
    assert any(rule.get("matchDepTypes") == ["jarvis-core"] for rule in core)
    tooling = next(rule for rule in core if "matchManagers" in rule)
    assert set(tooling["matchManagers"]) == {"github-actions", "renovate-config"}
    assert re.fullmatch(tooling["matchDepNames"][0].strip("/"), "gordoncooper/jarvis")
    assert not any("additionalBranchPrefix" in rule for rule in core)
    app = next(rule for rule in rules if rule.get("matchDepTypes") == ["jarvis-app"])
    template = re.fullmatch(r"\{\{\{replace '(.+)' '(.+)' packageFileDir\}\}\}", app["additionalBranchPrefix"])
    assert template
    find, replace = template.groups()
    assert re.sub(find, replace.replace("$1", r"\1"), "envs/env-01/values") == "env-01-"
    assert "jarvis-render@noreply.invalid" in preset["gitIgnoredAuthors"]
