#!/usr/bin/env python3
"""Emit JSON adoption targets from a flux-operator install.yaml manifest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from _flux_operator_install import InstallResource, parse_install_manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="Path to install.yaml",
    )
    args = parser.parse_args()
    text = args.manifest.read_text(encoding="utf-8")
    targets = [
        {
            "kind": item.kind,
            "kubectl_resource": item.kubectl_resource,
            "name": item.name,
            "namespace": item.namespace,
        }
        for item in parse_install_manifest(text)
    ]
    json.dump(targets, sys.stdout)


if __name__ == "__main__":
    main()
