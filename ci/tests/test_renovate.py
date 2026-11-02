import json
import re

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


def test_digest_updates_use_the_minor_patch_hold() -> None:
    """Digest-only updates wait the same 3-day hold as minor and patch."""
    renovate = json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))
    rule = next(item for item in renovate["packageRules"] if "minimumReleaseAge" in item)
    assert set(rule["matchUpdateTypes"]) >= {"minor", "patch", "digest"}


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


def test_managed_image_without_a_version_tag_fails() -> None:
    """R33: an image line Renovate manages must name a version. A digest-only line does not match."""
    renovate = json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))
    cases = (
        ("platform/backups/controller/helmrelease.yaml", "quay.io/backube/volsync", "0.17"),
        ("platform/backups/etcd-snapshots.yaml", "restic/restic", None),
    )
    chart = yaml.safe_load(
        (ROOT / "platform/backups/controller/helmrelease.yaml").read_text(encoding="utf-8")
    )
    chart_version = chart["spec"]["chart"]["spec"]["version"]
    for rel, dep, minor in cases:
        text = (ROOT / rel).read_text(encoding="utf-8")
        managers = [
            manager
            for manager in renovate["customManagers"]
            if any(dep in pattern for pattern in manager["matchStrings"])
        ]
        assert len(managers) == 1, rel
        pattern = _js_to_python(managers[0]["matchStrings"][0])
        found = list(re.finditer(pattern, text))
        assert len(found) == 1, rel
        assert found[0].group("currentValue")
        assert found[0].group("currentDigest").startswith("sha256:")
        bare = f"image: {dep}@{found[0].group('currentDigest')}\n"
        assert re.search(pattern, bare) is None
        if minor is not None:
            assert found[0].group("currentValue").startswith(minor + ".")
            assert chart_version.startswith(minor + ".")
    rule = next(
        item
        for item in renovate["packageRules"]
        if item.get("matchDepNames") == ["quay.io/backube/volsync"]
    )
    assert rule["allowedVersions"] == "<0.18"
    assert rule["groupName"] == "volsync-image"


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
