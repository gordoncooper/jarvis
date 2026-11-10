"""Run a jarvis module as `python -m` does, against the test core (sample.use_test_core), for
tests that run a command in a child process:

  python ci/tests/run_module.py jarvis.env.validate --only env-schema
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(1, str(Path(__file__).resolve().parents[2]))

import sample  # noqa: E402

sample.use_test_core()
module, sys.argv = sys.argv[1], sys.argv[1:]
runpy.run_module(module, run_name="__main__", alter_sys=True)
