import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _js_to_python(pattern: str) -> str:
    return re.sub(r"\(\?<([A-Za-z_]\w*)>", r"(?P<\1>", pattern)


def test_each_env_flux_instance_version_is_a_renovate_pin() -> None:
    """R33: Renovate reads each env FluxInstance's Flux version, its only pin."""
    renovate = json.loads((ROOT / "renovate.json").read_text(encoding="utf-8"))
    paths = sorted(ROOT.glob("envs/*/flux/flux-instance.yaml"))
    assert paths
    for path in paths:
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
