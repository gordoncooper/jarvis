import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
NOTE = "# renovate: datasource=github-releases depName=fluxcd/flux2 versioning=semver"

def test_flux_distribution_pin_matches() -> None:
    role = (ROOT / "infra/ansible/roles/flux-operator/defaults/main.yml").read_text()
    pinned = yaml.safe_load(role)["flux_distribution_version"]
    docs = [role, *(p.read_text() for p in sorted(ROOT.glob("envs/*/flux/flux-instance.yaml")))]
    assert len(docs) > 1
    for text in docs:
        data = yaml.safe_load(text)
        key = "flux_distribution_version:" if "flux_distribution_version" in data else "version:"
        got = data.get("flux_distribution_version") or data["spec"]["distribution"]["version"]
        assert got == pinned and text.split(key, 1)[0].rstrip().endswith(NOTE)


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
    python_pattern = re.sub(
        r"\(\?<([A-Za-z_]\w*)>",
        r"(?P<\1>",
        manager["matchStrings"][0],
    )
    found = list(re.finditer(python_pattern, source))
    assert len(found) == 1
    image = next(line.strip() for line in source.splitlines() if line.strip().startswith("image:"))
    name, digest = image.removeprefix("image: ").split("@", 1)
    assert found[0].group("depName") == "nginxinc/nginx-unprivileged"
    assert found[0].group("currentValue") == name.rsplit(":", 1)[1]
    assert found[0].group("currentDigest") == digest
