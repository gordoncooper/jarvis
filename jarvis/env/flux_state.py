"""Whether Flux has applied what its sources hold, read from Flux's own status.

A Ready condition can be left over from an older spec or an older artifact. kustomize-controller
reconciles at once on a new generation or a new source revision. But the root GitRepository's
revision is the instance commit, and a new core tag reaches it as a new included artifact with the
same revision: its Kustomizations apply that on their next interval, and read Ready until then.
So a Kustomization is current only once it has also reconciled since its source's artifact was
archived (status.history, kept by Flux 2.7 and later).
"""

from __future__ import annotations

from datetime import datetime

SOURCES = "gitrepositories.source.toolkit.fluxcd.io,ocirepositories.source.toolkit.fluxcd.io"


def _time(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def source_index(items: list[dict]) -> dict[tuple[str, str, str], dict]:
    """Sources by (kind, namespace, name), from kubectl's list of gitrepositories and ocirepositories."""
    index = {}
    for item in items:
        metadata = item.get("metadata") or {}
        index[(str(item.get("kind")), str(metadata.get("namespace")), str(metadata.get("name")))] = item
    return index


def lag(item: dict, sources: dict[tuple[str, str, str], dict] | None = None) -> str | None:
    """Why item (any Flux object) is not current, or None when it is. sources, from source_index,
    is needed for a Kustomization: it must be at its source's artifact and have reconciled since."""
    metadata = item.get("metadata") or {}
    status = item.get("status") or {}
    ready = next((c for c in status.get("conditions") or [] if c.get("type") == "Ready"), {})
    if ready.get("status") != "True":
        return str(ready.get("message") or "no Ready condition yet")
    if status.get("observedGeneration") != metadata.get("generation"):
        return "Ready for an older spec; reconciling"
    if item.get("kind") != "Kustomization" or sources is None:
        return None
    ref = (item.get("spec") or {}).get("sourceRef") or {}
    kind, name = str(ref.get("kind")), str(ref.get("name"))
    source = sources.get((kind, str(ref.get("namespace") or metadata.get("namespace")), name))
    if source is None:
        return f"its source {kind}/{name} is not listed"
    artifact = (source.get("status") or {}).get("artifact") or {}
    if status.get("lastAppliedRevision") != artifact.get("revision"):
        return f"has not applied {name}'s revision {artifact.get('revision')}"
    reconciled = [_time(snapshot.get("lastReconciled")) for snapshot in status.get("history") or []]
    last, archived = max((t for t in reconciled if t), default=None), _time(artifact.get("lastUpdateTime"))
    if last is None or archived is None or last < archived:
        interval = (item.get("spec") or {}).get("interval")
        archived_at = artifact.get("lastUpdateTime")
        return f"has not applied {name}'s artifact of {archived_at} yet (it reconciles every {interval})"
    return None
