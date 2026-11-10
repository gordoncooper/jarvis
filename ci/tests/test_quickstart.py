"""The quickstart workflow runs docs/quickstart.md's commands as written (ci/quickstart/script)."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import re

import pytest

from support import REPO_ROOT

_loader = importlib.machinery.SourceFileLoader("quickstart_script", str(REPO_ROOT / "ci" / "quickstart" / "script"))
_spec = importlib.util.spec_from_loader(_loader.name, _loader)
assert _spec is not None
script = importlib.util.module_from_spec(_spec)
_loader.exec_module(script)

DOC = (REPO_ROOT / "docs" / "quickstart.md").read_text(encoding="utf-8")
WORKFLOW = (REPO_ROOT / ".github" / "workflows" / "quickstart.yml").read_text(encoding="utf-8")


def _choices() -> list[str]:
    return sorted(script.names(script.blocks(DOC)[0]))


def test_every_job_sets_exactly_the_doc_choices() -> None:
    """Rule: CI replaces the doc's choices with its own; a choice the doc adds that a job does not
    set would leave the job running on an unset variable."""
    heredocs = re.findall(r"cat > choices\.sh <<EOF\n(.*?)\n\s*EOF", WORKFLOW, re.S)
    assert len(heredocs) == 2
    for body in heredocs:
        assert sorted(script.names("\n".join(line.strip() for line in body.splitlines()))) == _choices()


def test_the_script_is_the_docs_commands_in_order() -> None:
    choices = "".join(f"{name}=x\n" for name in _choices())
    out = script.script(DOC, choices)
    commands = [line for block in script.blocks(DOC)[1:] for line in block.splitlines()]
    body = out.splitlines()[1 + len(_choices()) :]
    assert [line.removesuffix(' < "${QUICKSTART_ANSWERS}"') for line in body] == commands
    assert any(line.startswith("jarvis node prep ") and line.endswith('< "${QUICKSTART_ANSWERS}"') for line in body)
    assert body[-2:] == ["jarvis up", "jarvis verify --smoke"]


def test_the_controller_half_stops_before_the_node() -> None:
    choices = "".join(f"{name}=x\n" for name in _choices())
    out = script.script(DOC, choices, until="jarvis node prep")
    assert "jarvis init " in out and "node prep" not in out and "jarvis up" not in out


def test_choices_that_differ_from_the_docs_fail() -> None:
    with pytest.raises(script.QuickstartError, match="the doc's choices are"):
        script.script(DOC, "JARVIS_VERSION=x\n")
