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
