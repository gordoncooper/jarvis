def _spec(root: dict, kind: str) -> dict:
    node = root.get("template") if kind == "Deployment" else ((root.get("jobTemplate") or {}).get("spec") or {}).get("template")
    return (node or {}).get("spec") or {}

def placement_blocked(workloads: list) -> list[str]:
    blocked: list[str] = []
    for item in workloads:
        resources, meta = item.get("resources") or [], item.get("item") or {}
        if not resources:
            continue
        spec = _spec(resources[0].get("spec") or {}, str(meta.get("kind")))
        key = f"node-role.jarvis.io/{meta.get('role')}"
        required = ((spec.get("affinity") or {}).get("nodeAffinity") or {}).get("requiredDuringSchedulingIgnoredDuringExecution") or {}
        exprs = [e for t in required.get("nodeSelectorTerms") or [] for e in t.get("matchExpressions") or []]
        term_ok = any(e.get("key") == key and e.get("operator") == "In" and "true" in (e.get("values") or []) for e in exprs)
        tol_ok = any(t.get("key") == "node-role.jarvis.io/dedicated" and t.get("value") == "true" and t.get("effect") == "NoSchedule" for t in spec.get("tolerations") or [])
        if not (term_ok and tol_ok):
            blocked.append(f"{meta.get('kind')} {meta.get('name')}")
    return blocked

class FilterModule:
    def filters(self) -> dict: return {"placement_blocked": placement_blocked}
