"""One cluster-vars key after env-schema defaults (D34)."""
from __future__ import annotations

import sys
from pathlib import Path

import yaml
from ansible.errors import AnsibleError
from ansible.plugins.lookup import LookupBase

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from schema.env import resolve_cluster_vars  # noqa: E402


class LookupModule(LookupBase):
    def run(self, terms, variables=None, **kwargs):
        raw = (variables or {}).get("jarvis_cluster_vars")
        if not isinstance(raw, str) or not raw.strip():
            raise AnsibleError("jarvis_cluster_vars is not set")
        path = Path(raw) if Path(raw).is_absolute() else Path.cwd() / raw
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise AnsibleError(f"cluster-vars {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise AnsibleError(f"cluster-vars {path} is not a mapping")
        value = resolve_cluster_vars(data).get(str(terms[0]))
        if value is None:
            raise AnsibleError(f"schema_cluster_var: {terms[0]} is not set")
        return [value]
