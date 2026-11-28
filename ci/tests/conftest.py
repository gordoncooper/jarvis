"""Test harness. pytest.ini puts the repo root on the path, so tests import the jarvis package."""

from __future__ import annotations

import os
import shutil

import pytest

# Background git maintenance holds locks inside a test's repo while the test deletes it, and a
# commit a test makes (support.git) needs an identity: a fixture one.
_count = int(os.environ.get("GIT_CONFIG_COUNT", "0"))
for _key, _value in (("maintenance.auto", "false"), ("gc.auto", "0"), ("user.email", "fixture@example.com"), ("user.name", "fixture")):
    os.environ[f"GIT_CONFIG_KEY_{_count}"] = _key
    os.environ[f"GIT_CONFIG_VALUE_{_count}"] = _value
    _count += 1
os.environ["GIT_CONFIG_COUNT"] = str(_count)

# Every test reads env trees against the test core: core's trees plus the fixture add-on
# (sample.use_test_core). A test with its own core trees swaps it with support.core_at.
import sample  # noqa: E402

sample.use_test_core()
# The default APPS_REPO: the fixture apps repository, tagged at the pins the sample env lists.
if not os.environ.get("INSTANCE") and "APPS_REPO" not in os.environ:
    os.environ["APPS_REPO"] = str(sample.fixture_apps_checkout())


# The test files that prove the Ansible roles by running ansible-playbook. They are over half the
# suite's time, so CI runs them (-m roles) beside the rest (-m "not roles"), each on its own runner.
ROLE_PROOFS = frozenset(
    {
        "test_ansible_operator_tags.py",
        "test_base_os.py",
        "test_env_shapes.py",
        "test_flux_operator.py",
        "test_inventory_roles.py",
        "test_k3s.py",
        "test_kubeconfig.py",
        "test_node_disk.py",
        "test_pci_gpu.py",
        "test_preflight.py",
    }
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if item.path.name in ROLE_PROOFS:
            item.add_marker(pytest.mark.roles)


@pytest.fixture(scope="session")
def example_copy(tmp_path_factory: pytest.TempPathFactory):
    """One bootstrapped copy of the sample env with every feature and the fixture add-on, for the
    structural env rules and for test_flux_entry.py, the flux build select-tests runs for every core change."""
    from support import bootstrap_example_copy

    return bootstrap_example_copy(tmp_path_factory.mktemp("example-copy"), features=sample.ALL_FEATURES, addons=True)


def ci_mode() -> bool:
    return os.environ.get("CI", "").lower() in {"1", "true", "yes"}


def require_cli(name: str) -> str:
    path = shutil.which(name)
    if path:
        return path
    message = f"{name} not found on PATH (run make deps)"
    if ci_mode():
        pytest.fail(f"{message} — required in CI")
    pytest.skip(message)
