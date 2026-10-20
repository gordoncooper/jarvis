"""P0a phase verify (make verify PHASE=p0a). Evidence-driven; cluster reads via kubectl."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml

from _ci_rule_checks import expected_ci_rule_checks
from _core_names import sole_env_directory_name

DECRYPT_SECRET_NAME = "backups-restic"
DECRYPT_SECRET_NAMESPACE = "platform-backups"
DECRYPT_KUSTOMIZATION = "platform-configs"
ROLLBACK_DRILL_NEW_VALUE = os.environ.get("JARVIS_ROLLBACK_DRILL_NEW_VALUE", "drill-v2")
ROLLBACK_DRILL_BASELINE_VALUE = os.environ.get(
    "JARVIS_ROLLBACK_DRILL_BASELINE_VALUE", "baseline"
)

EVIDENCE_CI_SEEDS = "ci-seed-pr-links.txt"
EVIDENCE_RENOVATE = "renovate-pr-link.txt"
EVIDENCE_ROLLBACK_LOG = "rollback-drill.log"
EVIDENCE_SITE_YML = "site-yml-second-run.txt"
EVIDENCE_BOOTSTRAP_SECRETS = "bootstrap-secrets-second-run.txt"

DEFAULT_EVIDENCE_DIR = ".jarvis-verify-evidence"

FLUX_API_GROUP_SUFFIX = ".toolkit.fluxcd.io"
FLUX_CONTROLPLANE_GROUP = "fluxcd.controlplane.io"

REQUIRED_FLUX_API_NAMES: frozenset[str] = frozenset(
    {
        "kustomizations.kustomize.toolkit.fluxcd.io",
        "gitrepositories.source.toolkit.fluxcd.io",
        "helmreleases.helm.toolkit.fluxcd.io",
        "fluxinstances.fluxcd.controlplane.io",
    }
)

# Config-only Flux API resources (no Ready); skipped by full api-resources name.
FLUX_SKIP_API_NAMES = frozenset(
    {
        "alerts.notification.toolkit.fluxcd.io",
        "providers.notification.toolkit.fluxcd.io",
        "receivers.notification.toolkit.fluxcd.io",
    }
)

BOOTSTRAP_NO_CHANGE_LINE = "bootstrap-secrets: all files present; nothing changed"

_RECAP_FIELD = re.compile(r"(changed|failed|unreachable)\s*=\s*(\d+)", re.IGNORECASE)
_REACHED_LINE = re.compile(
    r"reached\s+value=(\S+)\s+flux_kustomization_revision=(\S+)",
)


@dataclass(frozen=True)
class KubectlResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def error_text(self) -> str:
        return (self.stderr or self.stdout or "kubectl failed").strip()


@dataclass(frozen=True)
class VerifyResult:
    lines: tuple[str, ...]
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


def default_evidence_dir(repo_root: Path) -> Path:
    env = os.environ.get("JARVIS_VERIFY_EVIDENCE")
    if env:
        return Path(env).expanduser()
    return repo_root / DEFAULT_EVIDENCE_DIR


def github_slug_from_env_or_git(
    repo_root: Path,
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> str | None:
    explicit = os.environ.get("JARVIS_CODE_DELIVERY_REPO", "").strip()
    if explicit:
        return explicit.lstrip("/")
    runner = run or subprocess.run
    proc = runner(
        ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    url = proc.stdout.strip()
    if url.endswith(".git"):
        url = url[:-4]
    if url.startswith("git@github.com:"):
        return url.split(":", 1)[1]
    if "github.com/" in url:
        return url.split("github.com/", 1)[1]
    return None


def github_pull_request_url_ok(slug: str, url: str) -> bool:
    pattern = re.compile(rf"^https://github\.com/{re.escape(slug)}/pull/\d+$")
    return bool(pattern.match(url.strip()))


def inventory_hostnames(repo_root: Path) -> tuple[str, ...]:
    env = os.environ.get("JARVIS_ENV") or sole_env_directory_name(repo_root)
    path = repo_root / "envs" / env / "inventory" / "hosts.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return ()
    all_group = data.get("all")
    if not isinstance(all_group, dict):
        return ()
    hosts = all_group.get("hosts")
    if not isinstance(hosts, dict):
        return ()
    return tuple(sorted(str(name) for name in hosts))


def site_yml_paste_ok(text: str, required_hosts: tuple[str, ...]) -> bool:
    if "PLAY RECAP" not in text or not required_hosts:
        return False
    recap_lines: list[str] = []
    in_recap = False
    for line in text.splitlines():
        if "PLAY RECAP" in line:
            in_recap = True
            continue
        if not in_recap:
            continue
        if not line.strip():
            continue
        if re.search(r"changed\s*=", line, re.IGNORECASE):
            recap_lines.append(line)
    if not recap_lines:
        return False
    seen_hosts: set[str] = set()
    for line in recap_lines:
        host = line.split(":", 1)[0].strip()
        if not host:
            return False
        seen_hosts.add(host)
        fields = {m.group(1).lower(): int(m.group(2)) for m in _RECAP_FIELD.finditer(line)}
        for key in ("changed", "failed", "unreachable"):
            if fields.get(key) != 0:
                return False
    return seen_hosts == set(required_hosts)


def bootstrap_secrets_paste_ok(text: str) -> bool:
    if BOOTSTRAP_NO_CHANGE_LINE not in text:
        return False
    if re.search(r"created\s+", text, re.IGNORECASE):
        return False
    return True


def flux_groups_from_api_resource_list(doc: dict[str, Any]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    if "resources" not in doc:
        if "items" in doc:
            errors.append("flux: api-resources JSON missing resources list (got items)")
        else:
            errors.append("flux: api-resources JSON missing resources list")
        return [], errors
    raw = doc.get("resources")
    if not isinstance(raw, list):
        errors.append("flux: api-resources JSON missing resources list")
        return [], errors
    groups: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        group = entry.get("group")
        if not isinstance(group, str) or not group:
            continue
        if group == FLUX_CONTROLPLANE_GROUP or group.endswith(FLUX_API_GROUP_SUFFIX):
            groups.add(group)
    return sorted(groups), errors


def parse_api_resources_name_output(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def discover_flux_api_resource_names(
    kubectl_run: Callable[[list[str]], KubectlResult],
) -> tuple[set[str], list[str], list[str]]:
    """Return (api resource names, log lines, errors)."""
    lines: list[str] = []
    errors: list[str] = []
    proc = kubectl_run(["api-resources", "-o", "json"])
    if proc.returncode != 0:
        errors.append(f"flux: kubectl api-resources failed: {proc.error_text}")
        return set(), lines, errors
    try:
        doc = json.loads(proc.stdout)
    except json.JSONDecodeError:
        errors.append("flux: api-resources returned invalid JSON")
        return set(), lines, errors
    groups, group_errors = flux_groups_from_api_resource_list(doc)
    errors.extend(group_errors)
    if group_errors:
        return set(), lines, errors

    discovered: set[str] = set()
    for group in groups:
        group_proc = kubectl_run(["api-resources", f"--api-group={group}", "-o", "name"])
        if group_proc.returncode != 0:
            errors.append(
                f"flux: kubectl api-resources for group {group} failed: {group_proc.error_text}"
            )
            continue
        for name in parse_api_resources_name_output(group_proc.stdout):
            if name in FLUX_SKIP_API_NAMES:
                lines.append(f"  (skip {name}: config-only)")
                continue
            discovered.add(name)

    for required in sorted(REQUIRED_FLUX_API_NAMES):
        if required not in discovered:
            errors.append(f"flux: required CRD missing: {required}")

    return discovered, lines, errors


def _crd_missing(message: str) -> bool:
    lower = message.lower()
    return (
        "not found" in lower
        or "the server could not find the requested resource" in lower
        or "no matches for kind" in lower
        or "couldn't find" in lower
    )


def collect_flux_objects(
    api_resource_names: set[str],
    kubectl_run: Callable[[list[str]], KubectlResult],
) -> tuple[list[tuple[str, dict[str, Any]]], list[str], list[str]]:
    objects: list[tuple[str, dict[str, Any]]] = []
    notes: list[str] = []
    errors: list[str] = []
    for api_name in sorted(api_resource_names):
        args = ["get", api_name, "-A", "-o", "json"]
        proc = kubectl_run(args)
        if proc.returncode != 0:
            msg = proc.error_text
            if api_name in REQUIRED_FLUX_API_NAMES:
                errors.append(f"flux: required get {api_name} failed: {msg}")
                continue
            if _crd_missing(msg):
                notes.append(f"  (skip {api_name}: CRD not installed)")
                continue
            errors.append(f"flux: get {api_name} failed: {msg}")
            continue
        try:
            doc = json.loads(proc.stdout)
        except json.JSONDecodeError:
            errors.append(f"flux: get {api_name} returned invalid JSON")
            continue
        items = doc.get("items")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    objects.append((api_name, item))
    return objects, notes, errors


def _ready_condition(doc: dict[str, Any]) -> str | None:
    status = doc.get("status")
    if not isinstance(status, dict):
        return None
    conditions = status.get("conditions")
    if not isinstance(conditions, list):
        return None
    for cond in conditions:
        if not isinstance(cond, dict):
            continue
        if cond.get("type") == "Ready":
            val = cond.get("status")
            return val if isinstance(val, str) else None
    return None


def _flux_sort_key(entry: tuple[str, dict[str, Any]]) -> tuple[str, str, str]:
    api_name, doc = entry
    metadata = doc.get("metadata") or {}
    name = str(metadata.get("name", ""))
    namespace = str(metadata.get("namespace", ""))
    return (api_name, namespace, name)


def _ready_reason(doc: dict[str, Any]) -> str:
    status = doc.get("status")
    if not isinstance(status, dict):
        return "no status"
    conditions = status.get("conditions")
    if not isinstance(conditions, list):
        return "no conditions"
    for cond in conditions:
        if isinstance(cond, dict) and cond.get("type") == "Ready":
            msg = cond.get("message")
            if isinstance(msg, str) and msg:
                return msg
            reason = cond.get("reason")
            if isinstance(reason, str) and reason:
                return reason
    return "no Ready condition"


def flux_ready_lines(
    objects: list[tuple[str, dict[str, Any]]],
    notes: list[str],
    discovery_notes: list[str],
) -> tuple[list[str], list[str]]:
    lines: list[str] = ["Flux Ready:"]
    errors: list[str] = []
    for note in discovery_notes + notes:
        lines.append(note)
    if not objects:
        lines.append("  (no Flux objects found)")
        errors.append("flux: no Flux objects found")
        return (lines, errors)

    ready_kustomizations = 0
    ready_fluxinstances = 0
    for api_name, doc in sorted(objects, key=_flux_sort_key):
        metadata = doc.get("metadata") or {}
        name = metadata.get("name", "?")
        namespace = metadata.get("namespace") or ""
        kind = doc.get("kind", "?")
        ready = _ready_condition(doc)
        label = (
            f"  {api_name}/{namespace}/{name}"
            if namespace
            else f"  {api_name}/{name}"
        )
        if ready == "True":
            lines.append(f"{label}: Ready=True")
            if kind == "Kustomization":
                ready_kustomizations += 1
            if kind == "FluxInstance":
                ready_fluxinstances += 1
        else:
            reason = _ready_reason(doc)
            lines.append(f"{label}: Ready={ready or 'Unknown'} ({reason})")
            errors.append(f"flux not ready: {api_name} {namespace}/{name}")

    if ready_kustomizations < 1:
        errors.append("flux: no Ready Kustomization found")
    if ready_fluxinstances < 1:
        errors.append("flux: no Ready FluxInstance found")
    return (lines, errors)


def run_flux_ready_check(
    kubectl_run: Callable[[list[str]], KubectlResult],
) -> tuple[list[str], list[str]]:
    discovered, discovery_notes, discovery_errors = discover_flux_api_resource_names(
        kubectl_run
    )
    if discovery_errors:
        lines = ["Flux Ready:", *discovery_notes]
        return lines, discovery_errors
    objects, notes, collect_errors = collect_flux_objects(discovered, kubectl_run)
    lines, ready_errors = flux_ready_lines(objects, notes, discovery_notes)
    return lines, discovery_errors + collect_errors + ready_errors


def decrypt_proof_lines(secret_json: dict[str, Any]) -> tuple[list[str], list[str]]:
    lines: list[str] = []
    errors: list[str] = []
    lines.append("SOPS decrypt proof:")
    items = secret_json.get("items")
    if not isinstance(items, list) or not items:
        lines.append(
            f"  Secret/{DECRYPT_SECRET_NAMESPACE}/{DECRYPT_SECRET_NAME}: missing"
        )
        errors.append("decrypt proof: secret missing")
        return (lines, errors)

    doc = items[0]
    if not isinstance(doc, dict):
        errors.append("decrypt proof: invalid secret document")
        return (lines, errors)

    metadata = doc.get("metadata") or {}
    labels = metadata.get("labels") or {}
    flux_name = labels.get("kustomize.toolkit.fluxcd.io/name") or labels.get(
        "flux.toolkit.fluxcd.io/name"
    )
    data = doc.get("data") or {}
    string_data = doc.get("stringData") or {}
    key_count = 0
    if isinstance(data, dict):
        key_count += sum(1 for v in data.values() if v)
    if isinstance(string_data, dict):
        key_count += sum(1 for v in string_data.values() if v)

    lines.append(f"  Secret/{DECRYPT_SECRET_NAMESPACE}/{DECRYPT_SECRET_NAME}: exists")
    if flux_name == DECRYPT_KUSTOMIZATION:
        lines.append(f"  applied_by: Kustomization/{DECRYPT_KUSTOMIZATION}")
    else:
        lines.append(f"  applied_by: {flux_name or 'unknown'} (expected {DECRYPT_KUSTOMIZATION})")
        errors.append("decrypt proof: not applied by platform-configs kustomization")

    if key_count > 0:
        lines.append(f"  data_keys_nonempty: {key_count}")
    else:
        lines.append("  data_keys_nonempty: 0")
        errors.append("decrypt proof: secret has no non-empty data")
    return (lines, errors)


def _read_nonempty_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def parse_ci_seed_lines(
    lines: list[str],
    required_checks: tuple[str, ...],
) -> tuple[dict[str, str], list[str]]:
    errors: list[str] = []
    found: dict[str, str] = {}
    for raw in lines:
        parts = raw.split(None, 1)
        if len(parts) != 2:
            errors.append(f"evidence: invalid ci seed line (need '<check> <url>'): {raw}")
            continue
        check_name, url = parts[0].strip(), parts[1].strip()
        if check_name in found:
            errors.append(f"evidence: duplicate ci seed check name: {check_name}")
        found[check_name] = url
    for name in required_checks:
        if name not in found:
            errors.append(f"evidence: missing ci seed for check: {name}")
    for name in found:
        if name not in required_checks:
            errors.append(f"evidence: unknown ci seed check name: {name}")
    return found, errors


def evidence_pr_lines(
    evidence_dir: Path,
    slug: str | None,
    required_checks: tuple[str, ...],
) -> tuple[list[str], list[str]]:
    lines: list[str] = []
    errors: list[str] = []
    seeds_path = evidence_dir / EVIDENCE_CI_SEEDS
    renovate_path = evidence_dir / EVIDENCE_RENOVATE

    lines.append("CI seed PR links:")
    seed_lines = _read_nonempty_lines(seeds_path)
    if not seed_lines:
        lines.append(f"  (missing file: {EVIDENCE_CI_SEEDS})")
        errors.append(f"evidence: missing {EVIDENCE_CI_SEEDS}")
    else:
        mapping, parse_errors = parse_ci_seed_lines(seed_lines, required_checks)
        errors.extend(parse_errors)
        for check_name in required_checks:
            url = mapping.get(check_name)
            if url:
                lines.append(f"  {check_name} {url}")
                if slug and not github_pull_request_url_ok(slug, url):
                    errors.append(
                        f"evidence: ci seed for {check_name} is not "
                        f"https://github.com/{slug}/pull/<digits>"
                    )

    lines.append("Renovate PR link:")
    renovate_lines = _read_nonempty_lines(renovate_path)
    if not renovate_lines:
        lines.append(f"  (missing file: {EVIDENCE_RENOVATE})")
        errors.append(f"evidence: missing {EVIDENCE_RENOVATE}")
    else:
        lines.append(f"  {renovate_lines[0]}")
        for extra in renovate_lines[1:]:
            lines.append(f"  {extra}")
        if slug and not github_pull_request_url_ok(slug, renovate_lines[0]):
            errors.append(
                f"evidence: renovate link is not https://github.com/{slug}/pull/<digits>"
            )
    return (lines, errors)


def validate_rollback_log(
    text: str,
    *,
    new_value: str = ROLLBACK_DRILL_NEW_VALUE,
    baseline: str = ROLLBACK_DRILL_BASELINE_VALUE,
) -> list[str]:
    errors: list[str] = []
    reached: list[tuple[str, str]] = []
    for line in text.splitlines():
        match = _REACHED_LINE.search(line)
        if match:
            reached.append((match.group(1), match.group(2)))

    new_idx = None
    base_idx = None
    for idx, (val, _rev) in enumerate(reached):
        if val == new_value and new_idx is None:
            new_idx = idx
        if val == baseline and new_idx is not None and idx > new_idx:
            base_idx = idx
            break

    if new_idx is None:
        errors.append(f"rollback log: no reached line for new value {new_value}")
        return errors
    if base_idx is None:
        errors.append(f"rollback log: no reached line for baseline after {new_value}")
        return errors

    new_rev = reached[new_idx][1]
    base_rev = reached[base_idx][1]
    if new_rev == base_rev:
        errors.append("rollback log: flux revision did not change between drill and revert")
    return errors


def rollback_log_lines(evidence_dir: Path) -> tuple[list[str], list[str]]:
    path = evidence_dir / EVIDENCE_ROLLBACK_LOG
    lines: list[str] = ["Rollback drill log:"]
    errors: list[str] = []
    if not path.is_file():
        lines.append(f"  (missing file: {EVIDENCE_ROLLBACK_LOG})")
        errors.append(f"evidence: missing {EVIDENCE_ROLLBACK_LOG}")
        return (lines, errors)
    body = path.read_text(encoding="utf-8")
    for ln in body.splitlines():
        if ln.strip():
            lines.append(f"  {ln.rstrip()}")
    if not body.strip():
        lines.append("  (empty log)")
        errors.append(f"evidence: empty {EVIDENCE_ROLLBACK_LOG}")
        return (lines, errors)
    errors.extend(validate_rollback_log(body))
    return (lines, errors)


def gordon_site_yml_lines(
    evidence_dir: Path,
    repo_root: Path,
) -> tuple[list[str], list[str]]:
    hosts = inventory_hostnames(repo_root)

    def validator(text: str) -> bool:
        return site_yml_paste_ok(text, hosts)

    return _gordon_paste_lines(
        evidence_dir,
        EVIDENCE_SITE_YML,
        "Second site.yml run (idempotence):",
        validator,
        "site.yml PLAY RECAP must list every inventory host with changed=0 failed=0 unreachable=0",
    )


def gordon_bootstrap_lines(evidence_dir: Path) -> tuple[list[str], list[str]]:
    return _gordon_paste_lines(
        evidence_dir,
        EVIDENCE_BOOTSTRAP_SECRETS,
        "Second bootstrap-secrets run (idempotence):",
        bootstrap_secrets_paste_ok,
        "bootstrap-secrets must show the no-change line and no created lines",
    )


def _gordon_paste_lines(
    evidence_dir: Path,
    filename: str,
    heading: str,
    validator: Callable[[str], bool],
    fail_hint: str,
) -> tuple[list[str], list[str]]:
    path = evidence_dir / filename
    lines: list[str] = [heading, "NEEDS GORDON"]
    errors: list[str] = []
    if not path.is_file():
        lines.append(f"  (missing paste: {filename})")
        errors.append(f"gordon paste missing: {filename}")
        return (lines, errors)
    body = path.read_text(encoding="utf-8")
    if not validator(body):
        lines.append(f"  (paste failed validation: {fail_hint})")
        errors.append(f"gordon paste invalid: {filename}")
    else:
        for ln in body.splitlines():
            lines.append(f"  {ln.rstrip()}")
    return (lines, errors)


def run_verify(
    repo_root: Path,
    *,
    kubectl_run: Callable[[list[str]], KubectlResult] | None = None,
    evidence_dir: Path | None = None,
    slug: str | None = None,
    required_checks: tuple[str, ...] | None = None,
) -> VerifyResult:
    evidence = evidence_dir or default_evidence_dir(repo_root)
    slug = slug if slug is not None else github_slug_from_env_or_git(repo_root)
    checks = required_checks or expected_ci_rule_checks(repo_root)

    out: list[str] = []
    errors: list[str] = []

    runner = kubectl_run or _kubectl_run

    flux_lines, flux_errs = run_flux_ready_check(runner)
    out.extend(flux_lines)
    errors.extend(flux_errs)

    secret_args = [
        "get",
        "secret",
        DECRYPT_SECRET_NAME,
        "-n",
        DECRYPT_SECRET_NAMESPACE,
        "-o",
        "json",
    ]
    secret_proc = runner(secret_args)
    if secret_proc.returncode != 0:
        secret_lines = [
            "SOPS decrypt proof:",
            f"  Secret/{DECRYPT_SECRET_NAMESPACE}/{DECRYPT_SECRET_NAME}: kubectl error ({secret_proc.error_text})",
        ]
        secret_errs = ["decrypt proof: kubectl failed"]
    else:
        try:
            secret_raw = json.loads(secret_proc.stdout)
            secret_doc = _normalize_secret_list(secret_raw)
            secret_lines, secret_errs = decrypt_proof_lines(secret_doc)
        except json.JSONDecodeError:
            secret_lines = ["SOPS decrypt proof:", "  invalid JSON from kubectl"]
            secret_errs = ["decrypt proof: kubectl returned invalid JSON"]
    out.extend(secret_lines)
    errors.extend(secret_errs)

    pr_lines, pr_errs = evidence_pr_lines(evidence, slug, checks)
    out.extend(pr_lines)
    errors.extend(pr_errs)

    rb_lines, rb_errs = rollback_log_lines(evidence)
    out.extend(rb_lines)
    errors.extend(rb_errs)

    site_lines, site_errs = gordon_site_yml_lines(evidence, repo_root)
    out.extend(site_lines)
    errors.extend(site_errs)

    boot_lines, boot_errs = gordon_bootstrap_lines(evidence)
    out.extend(boot_lines)
    errors.extend(boot_errs)

    return VerifyResult(lines=tuple(out), errors=tuple(errors))


def _normalize_secret_list(doc: dict[str, Any]) -> dict[str, Any]:
    if doc.get("kind") == "List":
        return doc
    if doc.get("kind") == "Secret":
        return {"items": [doc]}
    return {"items": []}


def _kubectl_run(args: list[str]) -> KubectlResult:
    proc = subprocess.run(
        ["kubectl", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return KubectlResult(
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    result = run_verify(repo_root)
    for line in result.lines:
        print(line)
    if result.errors:
        print("verify-p0a: failed:", file=os.sys.stderr)
        for err in result.errors:
            print(f"  - {err}", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
