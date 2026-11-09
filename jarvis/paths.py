"""Where the core checkout this package runs from keeps its trees."""

from __future__ import annotations

import os
from pathlib import Path

# The core checkout: the package lives at <core>/jarvis/.
PRODUCT_ROOT = Path(__file__).resolve().parents[1]


def cache_dir() -> Path:
    """Where the CLI keeps its checkouts: $XDG_CACHE_HOME/jarvis, default ~/.cache/jarvis."""
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "jarvis"
