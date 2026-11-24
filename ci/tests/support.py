"""Shared test helpers.

One repo root, one script loader, and the fixtures several modules used to
copy from each other. Ansible role renders stay in ansible_role_proof.
"""

from __future__ import annotations

import contextlib
import copy
import functools
import importlib
import json
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def instance_apps_repo_for_checks(instance_root: Path) -> Path:
    """The fixture apps repository beside a tmp instance, tagged at the sample's pins."""
    import sample

    dest = instance_root / "example-apps"
    if not dest.is_dir():
        sample.apps_repo(dest)
    return dest


def _kustomize_build(rel_path: str, *, root: Path | None = None) -> str:
    """Chart defaults the values don't set pass unchecked."""
    repo = REPO_ROOT if root is None else root
    kustomize = shutil.which("kustomize")
    if not kustomize:
        pytest.fail("kustomize not found on PATH (run jarvis doctor)")
    proc = subprocess.run(
        [
            kustomize,
            "build",
            "--load-restrictor",
            "LoadRestrictionsNone",
            str(repo / rel_path),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=repo,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return proc.stdout


def _default_hosts() -> str:
    return (
        textwrap.dedent(
            """
            all:
              hosts:
                node.example:
                  node_roles: [control_plane, backup, edge]
            """
        ).strip()
        + "\n"
    )


# The recipient _write_env's rule names, and a sealed file's shape for the schema: SOPS metadata
# naming it.
FIXTURE_RECIPIENT = "age1" + "q" * 58
SEALED_PLACEHOLDER = f"sops:\n  age:\n    - recipient: {FIXTURE_RECIPIENT}\n"


def _ensure_sops_creation_rule(root: Path, env_name: str) -> None:
    from jarvis.secrets import age

    path = root / ".sops.yaml"
    if not age.recipients(path, env_name):
        age.write_creation_rules(path, [*age.creation_rules(path), age.creation_rule(env_name, [FIXTURE_RECIPIENT])])


def _valid_cluster_vars() -> dict:
    """The sample env's cluster-vars, as jarvis init writes them plus the sample's LAN range."""
    import sample
    import yaml

    from jarvis.env import scaffold

    reference = yaml.safe_load(scaffold.cluster_vars(sample.spec()))
    reference["lan_cidrs"] = ["10.200.0.0/24"]
    return dict(reference)


def _write_env(
    root: Path,
    env_name: str = "env-test",
    *,
    cluster_vars: dict | None = None,
    omit_ssh_user: bool = False,
    hosts_yml: str | None = None,
) -> Path:
    import yaml

    env = root / "envs" / env_name
    (env / "values").mkdir(parents=True)
    (env / "inventory").mkdir(parents=True)

    cv = cluster_vars if cluster_vars is not None else _valid_cluster_vars()
    if omit_ssh_user:
        cv = {k: v for k, v in cv.items() if k != "ssh_user"}
    if cv.get("jarvis_env") in (None, "env-00"):
        cv["jarvis_env"] = env_name

    (env / "values" / "cluster-vars.yaml").write_text(
        yaml.safe_dump(cv, sort_keys=False), encoding="utf-8"
    )
    (env / "inventory" / "hosts.yml").write_text(
        hosts_yml if hosts_yml is not None else _default_hosts(),
        encoding="utf-8",
    )
    (env / "values" / "switches.yaml").write_text("features: []\naddons: []\n", encoding="utf-8")
    # The secrets jarvis init seals, as placeholders that name the rule's recipient.
    (env / "secrets").mkdir()
    for name in ("k3s-token", "cert-manager-ca", "backups-restic"):
        (env / "secrets" / f"{name}.sops.yaml").write_text(SEALED_PLACEHOLDER, encoding="utf-8")
    _ensure_sops_creation_rule(root, env_name)
    return env


@dataclass(frozen=True)
class ExampleCopy:
    """A throwaway sample env, built as jarvis init builds one. The age private key stays in tmp."""

    root: Path
    name: str

    @property
    def env_dir(self) -> Path:
        return self.root / "envs" / self.name


def real_and_copy_envs(copy: ExampleCopy) -> list[tuple[Path, str]]:
    """The envs a whole-env rule runs on: core ships none, so the sample copy."""
    return [(copy.root, copy.name)]


def bootstrap_example_copy(
    tmp_path: Path, *, features: Iterable[str] = (), addons: bool = False, hosts_text: str | None = None
) -> ExampleCopy:
    """A sample instance in tmp_path/repo holding one env, env-copy (ci/tests/sample.py): written by
    jarvis init's code with features on (none: core only), edited as an owner would, sealed to a
    throwaway key, and rendered. addons lists the fixture add-on; hosts_text replaces its inventory."""
    import sample

    root = (tmp_path / "repo").resolve()
    repo = REPO_ROOT.resolve()
    if (root == repo or repo in root.parents) and not _git_ignored(repo, root):
        raise RuntimeError("refusing to write the example copy at a tracked path in the repo")
    sample.build(root, "env-copy", features=features, with_addons=addons, hosts_text=hosts_text or sample.HOSTS)
    return ExampleCopy(root, "env-copy")


def sample_root(*features: str) -> Path:
    """A pristine sample instance holding env-00 with features on (none: core only) and no add-on,
    built once per feature set per test process. Read it; to change it, copy it (copy_sample) first."""
    return _sample_root(tuple(sorted(set(features))))


@functools.cache
def _sample_root(features: tuple[str, ...]) -> Path:
    import atexit

    import sample

    base = Path(tempfile.mkdtemp(prefix="jarvis-sample-"))
    atexit.register(shutil.rmtree, base, True)
    # No add-on, so a copy of one env builds in any tree; example_copy lists the fixture add-on.
    return sample.build(base / "instance", features=features)


def copy_sample(dest: Path, *features: str) -> Path:
    """A copy of sample_root(*features) at dest (which may exist), its core links kept."""
    shutil.copytree(sample_root(*features), dest, symlinks=True, dirs_exist_ok=True)
    return dest


@contextlib.contextmanager
def core_at(core: Path) -> Iterator[Path]:
    """Read env trees against core (a folder holding core/ and/or addons/) instead of the test
    core, for a test that ships its own core trees."""
    from jarvis import paths

    previous = paths.core_root
    paths.core_root = lambda: core
    try:
        yield core
    finally:
        paths.core_root = previous


# A jarvis module run in a child process against the test core: [*RUN_MODULE, "jarvis.env.validate", ...].
RUN_MODULE = (sys.executable, str(Path(__file__).resolve().parent / "run_module.py"))


def checks_without_charts() -> list[str]:
    """Every env check but the two that render charts (kubeconform, policies). Those run on the
    sample instance (ci/checks/sample-instance) and on jarvis init's own env (test_init.py); a
    test of another env shape runs the rest."""
    from jarvis.env.validate import CHECKS

    return [name for name in CHECKS if name not in ("kubeconform", "policies")]


def flux_built(root: Path, env: str, name: str = "platform-configs") -> list[dict]:
    """The objects the env's Flux Kustomization name applies, as Flux builds them (substituted)."""
    from jarvis.checks.flux_build import env_tree

    tree = env_tree(root, f"envs/{env}/flux")
    assert tree.error is None, tree.error
    path = next((item for item in tree.paths if (item.kustomization.get("metadata") or {}).get("name") == name), None)
    assert path is not None, f"envs/{env}/flux applies no Kustomization {name}"
    assert path.error is None, path.error
    return path.docs


def flux_envsubst(docs: list[dict], values: dict[str, str]) -> list[dict]:
    """docs after Flux's own postBuild substitution of values (flux envsubst --strict): every
    variable they use must be among values, as on the cluster."""
    import os

    import yaml

    flux = shutil.which("flux")
    assert flux, "flux not found on PATH (run jarvis doctor)"
    proc = subprocess.run(
        [flux, "envsubst", "--strict"], input=yaml.safe_dump_all(docs, sort_keys=False), capture_output=True,
        text=True, check=False, env={**os.environ, **{key: str(value) for key, value in values.items()}},
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    return [doc for doc in yaml.safe_load_all(proc.stdout) if isinstance(doc, dict)]


def write_flux_root(env_dir: Path) -> Path:
    """The env's flux/sync.yaml as jarvis render writes it (the root and core sources, and the root
    Kustomization applying the env's flux/), for a test env written by hand."""
    from jarvis.env import switches

    cluster_vars = {
        "flux_git_repo_url": "https://github.com/example-owner/example-cluster",
        "flux_git_ref": "refs/heads/main",
        "core_git_repo_url": "https://github.com/example-owner/example-core",
        "core_git_ref": "refs/tags/v0.0.0",
    }
    path = env_dir / "flux" / switches.SYNC_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(switches.sync(cluster_vars, env_dir.name), encoding="utf-8")
    return path


def set_switches(env_dir: Path, **sections: list[str]) -> None:
    """Replace these sections of the env's values/switches.yaml, as an owner edits the file."""
    import yaml

    path = env_dir / "values" / "switches.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    data.update(sections)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def render_switches(env_dir: Path, root: Path) -> None:
    """Render the env's Flux wiring from its switches, as an owner would."""
    from jarvis.render.switches import render_env

    render_env(env_dir, root)


def _git_ignored(repo: Path, path: Path) -> bool:
    """True when git ignores path, so a copy there cannot touch tracked files.

    An agent bound to its worktree runs the suite with --basetemp=.scratch/tmp.
    """
    proc = subprocess.run(
        ["git", "check-ignore", "-q", str(path)], cwd=repo, check=False, capture_output=True
    )
    return proc.returncode == 0


def register_copy_app(copy: ExampleCopy, namespace: str) -> None:
    """Turn on backup for one listed app in the copy as an owner would: its backups on in
    values/apps.yaml, its credential minted as jarvis secrets does, then render."""
    import yaml

    sops = shutil.which("sops")
    if not sops:
        raise RuntimeError("sops not found on PATH (run make deps)")
    path = copy.env_dir / "values" / "apps.yaml"
    apps = yaml.safe_load(path.read_text(encoding="utf-8"))
    apps[namespace.removeprefix("app-")].pop("backups", None)
    path.write_text(yaml.safe_dump(apps, sort_keys=False), encoding="utf-8")
    mod = importlib.import_module("jarvis.secrets.mint")
    previous_root, previous_repo_root = mod.REPO_ROOT, mod._repo_root
    mod.REPO_ROOT = copy.root
    mod._repo_root = lambda: copy.root
    try:
        mod.bootstrap_app(copy.name, namespace, sops_bin=sops)
    finally:
        mod.REPO_ROOT, mod._repo_root = previous_root, previous_repo_root
    render_switches(copy.env_dir, copy.root)


def kyverno_generated(work: Path, policies: list[dict], triggers: list[dict], cluster: list[dict] | None = None) -> list[dict]:
    """Every object the GeneratingPolicies make for the triggers, with the kyverno CLI (kyverno
    apply -o). cluster holds what resource.get and resource.list read, and each trigger's
    Namespace, whose labels a namespaceSelector reads. kyverno test cannot be used: it passes a
    generatedResource that was never made."""
    kyverno = shutil.which("kyverno")
    if kyverno is None:
        pytest.fail("kyverno CLI not found on PATH (run make deps)")
    import yaml

    work.mkdir(parents=True, exist_ok=True)
    (work / "policies.yaml").write_text(yaml.safe_dump_all(policies), encoding="utf-8")
    (work / "triggers.yaml").write_text(yaml.safe_dump_all(triggers), encoding="utf-8")
    context = {"apiVersion": "cli.kyverno.io/v1alpha1", "kind": "Context", "metadata": {"name": "c"},
               "spec": {"resources": cluster or []}}
    (work / "context.yaml").write_text(yaml.safe_dump(context), encoding="utf-8")
    values = {"apiVersion": "cli.kyverno.io/v1alpha1", "kind": "Value", "metadata": {"name": "v"},
              "namespaceSelector": [{"name": doc["metadata"]["name"], "labels": doc["metadata"].get("labels") or {}}
                                    for doc in cluster or [] if doc.get("kind") == "Namespace"]}  # fmt: skip
    (work / "values.yaml").write_text(yaml.safe_dump(values), encoding="utf-8")
    out = work / "out"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir()
    proc = subprocess.run(
        [kyverno, "apply", str(work / "policies.yaml"), "--resource", str(work / "triggers.yaml"),
         "--context-file", str(work / "context.yaml"), "--values-file", str(work / "values.yaml"), "-o", str(out), "--remove-color"],
        capture_output=True, text=True, check=False,
    )
    report = proc.stdout + proc.stderr
    assert proc.returncode == 0 and ", error: 0," in report, report
    assert "Invalid Policy" not in report and "failed to" not in report, report
    return [
        doc
        for path in sorted(out.iterdir())
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8"))
        if isinstance(doc, dict)
    ]


def yaml_docs(source: Path | str) -> list[dict]:
    """The mappings among the YAML documents in source, a file or YAML text."""
    import yaml

    text = source.read_text(encoding="utf-8") if isinstance(source, Path) else source
    return [doc for doc in yaml.safe_load_all(text) if isinstance(doc, dict)]


def git(repo: Path, *args: str) -> str:
    """git -C repo args, which must succeed (conftest gives commits a fixture identity); its
    stdout, less the last newline."""
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.rstrip("\n")


def completed(
    args: Sequence[str], value: object = "", *, returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    """A finished run of args, as a fake tool returns it: value is its stdout, as JSON unless it is text."""
    stdout = value if isinstance(value, str) else json.dumps(value)
    return subprocess.CompletedProcess(list(args), returncode, stdout, stderr)


def write_env_marker(root: Path, env: str = "env-test", marker: str | None = None, **values: object) -> Path:
    """envs/<env>/values/cluster-vars.yaml under root, holding the repository marker a command
    checks, jarvis_env (marker, default env), and values (a None one is left out). Returns the env's folder."""
    import yaml

    env_dir = root / "envs" / env
    (env_dir / "values").mkdir(parents=True, exist_ok=True)
    data = {"jarvis_env": env if marker is None else marker, **{key: value for key, value in values.items() if value is not None}}
    (env_dir / "values" / "cluster-vars.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return env_dir


def free_port() -> int:
    """A TCP port on 127.0.0.1 that nothing listens on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_listening(port: int, proc: subprocess.Popen, seconds: float = 10) -> None:
    """Return once 127.0.0.1:port accepts a connection. Fails if proc exits first or seconds pass."""
    deadline = time.monotonic() + seconds
    while True:
        assert proc.poll() is None, f"{proc.args} exited with {proc.returncode} before listening on port {port}"
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            return
        except OSError:
            if time.monotonic() > deadline:
                pytest.fail(f"{proc.args} did not listen on 127.0.0.1:{port} within {seconds} s")
            time.sleep(0.05)


def sealing_root(root: Path, monkeypatch: pytest.MonkeyPatch, env: str = "env-test") -> Path:
    """root as the instance (INSTANCE), its .sops.yaml holding jarvis's creation rule for env,
    which encrypts to a throwaway age key. Returns the key file (root/.age/keys.txt)."""
    from jarvis.secrets import age

    key, rules = root / ".age" / "keys.txt", root / ".sops.yaml"
    age.write_creation_rules(rules, [*age.creation_rules(rules), age.creation_rule(env, [age.create_key(key)])])
    monkeypatch.setenv("INSTANCE", str(root))
    return key


def marker_sops(tmp_path: Path) -> tuple[Path, Path]:
    """(bin_dir, marker): bin_dir/sops is a fake sops that creates marker and exits 99, to prove a
    refusal comes before sops runs. Pass bin_dir / "sops" as the sops to run, or put bin_dir first on PATH."""
    bin_dir, marker = tmp_path / "marker-sops", tmp_path / "sops-ran"
    bin_dir.mkdir()
    sops = bin_dir / "sops"
    sops.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nexit 99\n", encoding="utf-8")
    sops.chmod(0o755)
    return bin_dir, marker


# The hosted-app admission kit: core/feature/app-hosting/policies on seeded objects, offline, with
# the pinned kyverno CLI. apps is a folder of app folders (ci/tests/fixtures/app-hosting/apps); an
# env is {app: (its App, the env's grants for it)}. The CLI resolves neither the app-file binding's
# selector paramRef nor a namespaceObject's annotations, so run_admission stands in for both;
# test_app_admission_apiserver.py proves the real ones.
POLICIES = REPO_ROOT / "core" / "feature" / "app-hosting" / "policies"
SUFFIX = "env-00.example"
PASS = {"app-file": "pass", "app-grants": "pass"}
FILE_FAILS = {"app-file": "fail"}
GRANT_FAILS = {"app-grants": "fail"}
BOTH_FAIL = {"app-file": "fail", "app-grants": "fail"}
_TENANT = "system:serviceaccount:flux-system:app-{app}"
_APP_CLUSTER_VARS = {"pod_cidr": "10.80.0.0/16", "service_cidr": "10.81.0.0/16", "lan_cidrs": ["10.200.0.0/24"],
                     "apps_git_repo_url": "https://example.invalid/apps"}  # fmt: skip
_RESTRICTED = {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}


def app_doc(apps: Path, name: str, **spec: object) -> dict:
    """App name from apps, or a new one, as the cluster holds it (in its namespace), spec's keys set."""
    path = apps / name / "app.yaml"
    app = yaml_docs(path)[0] if path.is_file() else {
        "apiVersion": "hosting.jarvis.io/v1alpha1", "kind": "App", "metadata": {"name": name},
        "spec": {"network": {"web": [{"service": "web", "port": 80}]}, "volumes": []},
    }  # fmt: skip
    app["spec"].update(spec)
    app["metadata"]["namespace"] = f"app-{name}"
    return app


def app_wiring(app: str, grants: list[str]) -> list[dict]:
    """What the app's wiring applies, rendered with the env's grants for it."""
    from jarvis.apps import values

    data = {app: {"version": "v0.1.0", "grants": grants}}
    substitutes = {**values.substitutes(data, app, _APP_CLUSTER_VARS), "dns_suffix": SUFFIX}
    return flux_envsubst(yaml_docs(REPO_ROOT / "core/feature/app-hosting/app/namespace.yaml"), substitutes)


def app_deploy(apps: Path, app: str) -> list[dict]:
    """App app's deploy/ objects in apps, as its Kustomization applies them."""
    docs = [doc for path in sorted((apps / app / "deploy").glob("*.yaml")) for doc in yaml_docs(path)]
    docs = flux_envsubst(docs, {"app_host": f"{app}.apps.{SUFFIX}"})
    for doc in docs:
        doc["metadata"]["namespace"] = f"app-{app}"
    return docs


def app_mutated(env: dict[str, tuple[dict, list[str]]], docs: list[dict], user: str | None = None) -> list[dict]:
    """docs as both MutatingPolicies leave them at admission in env, written by user (default: the
    tenant of the first doc's namespace). resource.get reads each app's Namespace, as its wiring renders it."""
    from jarvis.apps.app_file import mutated

    user = user or _TENANT.format(app=docs[0]["metadata"]["namespace"].removeprefix("app-"))
    namespaces = tuple(doc for app, (_doc, grants) in env.items() for doc in app_wiring(app, grants) if doc["kind"] == "Namespace")
    found, error = mutated(docs, user, ("app-priority", "app-lan-name"), namespaces)
    assert not error, error
    return found


def app_pod(name: str, app: str, *, owner_job: bool = False, **pod: object) -> dict:
    """A Pod Security restricted pod in app's namespace at the hosted-app class, as app-priority
    leaves it; pod's keys replace the spec's, and a None one drops it."""
    spec = {
        "priorityClassName": "hosted-app",
        "securityContext": {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [{"name": "c", "image": "registry.example/x@sha256:" + "0" * 64,
                        "securityContext": copy.deepcopy(_RESTRICTED)}],
        **pod,
    }  # fmt: skip
    metadata: dict = {"name": name, "namespace": f"app-{app}"}
    if owner_job:
        metadata["ownerReferences"] = [
            {"apiVersion": "batch/v1", "kind": "Job", "name": "volsync-src-data", "uid": "1", "controller": True}
        ]
    return {"apiVersion": "v1", "kind": "Pod", "metadata": metadata,
            "spec": {key: value for key, value in spec.items() if value is not None}}  # fmt: skip


def app_job(name: str, app: str, **pod: object) -> dict:
    """A VolSync-labelled Job in app's namespace, its pod app_pod's."""
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": name, "namespace": f"app-{app}", "labels": {"app.kubernetes.io/created-by": "volsync"}},
        "spec": {"template": {"spec": app_pod("t", app, **pod)["spec"]}},
    }  # fmt: skip


def app_route(name: str, app: str, *hostnames: str) -> dict:
    """An HTTPRoute in app's namespace on core's apps listener, naming hostnames if any."""
    spec = {"parentRefs": [{"name": "jarvis", "namespace": "platform-gateway", "sectionName": "apps"}],
            "rules": [{"backendRefs": [{"name": "web", "port": 80}]}], **({"hostnames": list(hostnames)} if hostnames else {})}
    return {"apiVersion": "gateway.networking.k8s.io/v1", "kind": "HTTPRoute",
            "metadata": {"name": name, "namespace": f"app-{app}"}, "spec": spec}  # fmt: skip


def run_admission(
    tmp_path: Path, env: dict[str, tuple[dict, list[str]]], label: str, user: str | None,
    cases: list[tuple[dict, dict[str, str]]],
) -> None:
    """kyverno test on cases, (object, {policy: pass|fail|skip}), in env, as user (None: the CLI's
    own). Fails with the CLI's table."""
    import yaml

    from jarvis.apps.app_file import app_file_binding, userinfo

    kyverno = shutil.which("kyverno") or pytest.fail("kyverno CLI not found on PATH (run make deps)")
    work = tmp_path / label
    work.mkdir()
    namespaces = [doc for app, (_doc, grants) in env.items() for doc in app_wiring(app, grants)]
    policies = [doc for doc in yaml_docs(POLICIES / "admission.yaml")
                if (doc["kind"], doc["metadata"]["name"]) != ("ValidatingAdmissionPolicyBinding", "app-file")]  # fmt: skip
    annotations = {doc["metadata"]["name"]: doc["metadata"]["annotations"] for doc in namespaces}
    for policy in policies:
        if (policy["kind"], policy["metadata"]["name"]) == ("ValidatingAdmissionPolicy", "app-grants"):
            grants = next(item for item in policy["spec"]["variables"] if item["name"] == "grants")
            grants["expression"] = f"{json.dumps(annotations)}[namespaceObject.metadata.name]"
    (work / "policies.yaml").write_text(
        yaml.safe_dump_all([*policies, *(_own_namespace(app_file_binding(app), app) for app in env)]), encoding="utf-8"
    )
    (work / "params.yaml").write_text(yaml.safe_dump_all([app for app, _ in env.values()]), encoding="utf-8")
    (work / "resources.yaml").write_text(yaml.safe_dump_all([doc for doc, _ in cases]), encoding="utf-8")
    selector = [
        {"name": doc["metadata"]["name"], "labels": {**doc["metadata"]["labels"], "kubernetes.io/metadata.name": doc["metadata"]["name"]}}
        for doc in namespaces
    ] + [{"name": name, "labels": {"kubernetes.io/metadata.name": name}} for name in ("flux-system", "default")]
    (work / "values.yaml").write_text(
        yaml.safe_dump({"apiVersion": "cli.kyverno.io/v1alpha1", "kind": "Value", "metadata": {"name": "v"}, "namespaceSelector": selector}),
        encoding="utf-8",
    )
    results = [
        {"isValidatingAdmissionPolicy": True, "kind": doc["kind"], "policy": policy, "resources": [doc["metadata"]["name"]], "result": result}
        for doc, expected in cases
        for policy, result in expected.items()
    ]
    test = {
        "apiVersion": "cli.kyverno.io/v1alpha1", "kind": "Test", "metadata": {"name": label},
        "policies": ["policies.yaml"], "resources": ["resources.yaml"], "paramResources": ["params.yaml"],
        "variables": "values.yaml", "results": results,
    }  # fmt: skip
    if user is not None:
        (work / "userinfo.yaml").write_text(yaml.safe_dump(userinfo(user)), encoding="utf-8")
        test["userinfo"] = "userinfo.yaml"
    (work / "kyverno-test.yaml").write_text(yaml.safe_dump(test), encoding="utf-8")
    proc = subprocess.run([kyverno, "test", str(work), "--remove-color", "--detailed-results"],
                          capture_output=True, text=True, check=False)  # fmt: skip
    wrong = [line for line in proc.stdout.splitlines() if "Want " in line]
    assert proc.returncode == 0, "\n".join(wrong) or proc.stdout + proc.stderr


def _own_namespace(binding: dict, app: str) -> dict:
    """One app-file binding per app in one run, each held to its own namespace, as the API server reads one App per request."""
    spec = {**binding["spec"], "matchResources": {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": f"app-{app}"}}}}
    return {**binding, "metadata": {"name": f"app-file-{app}"}, "spec": spec}
