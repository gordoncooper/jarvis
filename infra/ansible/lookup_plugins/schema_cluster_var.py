"""Resolve one cluster-vars key through the env schema (D34)."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml
from ansible.errors import AnsibleError
from ansible.plugins.lookup import LookupBase

DOCUMENTATION = """
    name: schema_cluster_var
    short_description: Read a cluster-vars key after env-schema defaults
    description:
      - Loads the file in jarvis_cluster_vars and applies schema defaults.
      - ssh_user's default lives only in the schema.
    options:
      _terms:
        description: Cluster-vars key to return.
        required: True
"""

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from schema.env import resolve_cluster_vars  # noqa: E402


def _cluster_vars_path(variables: dict) -> Path:
    raw = variables.get("jarvis_cluster_vars")
    if not isinstance(raw, str) or not raw.strip():
        raise AnsibleError("jarvis_cluster_vars is not set")
    path = Path(raw)
    if path.is_absolute():
        return path
    playbook_dir = variables.get("playbook_dir")
    if isinstance(playbook_dir, str):
        candidate = Path(playbook_dir) / path
        if candidate.is_file():
            return candidate
    return Path.cwd() / path


def _resolved(variables: dict) -> dict:
    path = _cluster_vars_path(variables)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AnsibleError(f"cannot read cluster-vars {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise AnsibleError(f"invalid cluster-vars YAML {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise AnsibleError(f"cluster-vars {path} is not a mapping")
    return resolve_cluster_vars(data)


class LookupModule(LookupBase):
    def run(self, terms, variables=None, **kwargs):
        self.set_options(var_options=variables, direct=kwargs)
        if not terms:
            raise AnsibleError("schema_cluster_var needs a key")
        resolved = _resolved(variables or {})
        values = []
        for term in terms:
            key = str(term)
            if key not in resolved:
                raise AnsibleError(f"schema_cluster_var: {key} is not set")
            values.append(resolved[key])
        return values
