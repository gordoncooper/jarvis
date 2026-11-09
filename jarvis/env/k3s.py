"""The k3s release an env runs: core's (infra/ansible/group_vars/all.yml) unless its cluster-vars sets
k3s_version and k3s_sha256, which then override both."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from jarvis.paths import PRODUCT_ROOT

DEFAULTS_FILE = PRODUCT_ROOT / "infra" / "ansible" / "group_vars" / "all.yml"


def core_release() -> tuple[str, str]:
    """Core's k3s version and the binary's sha256."""
    defaults = yaml.safe_load(DEFAULTS_FILE.read_text(encoding="utf-8")) or {}
    return str(defaults["k3s_version"]), str(defaults["k3s_sha256"])


def release(cluster_vars: dict[str, Any]) -> tuple[str, str]:
    """The env's k3s version and sha256: its own pair when set, else core's."""
    if "k3s_version" in cluster_vars and "k3s_sha256" in cluster_vars:
        return str(cluster_vars["k3s_version"]), str(cluster_vars["k3s_sha256"])
    return core_release()


def main(argv: list[str] | None = None) -> None:
    """Print the k3s version of the env whose cluster-vars.yaml is the one argument."""
    import sys

    args = sys.argv[1:] if argv is None else argv
    cluster_vars = yaml.safe_load(Path(args[0]).read_text(encoding="utf-8")) or {}
    print(release(cluster_vars)[0])


if __name__ == "__main__":
    main()
