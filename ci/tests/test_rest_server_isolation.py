"""An app login cannot open a core repository or another app's; it can use its own.

Runs the pinned rest-server and restic as the manifests configure them: core's server with
core's one user and no --private-repos, and the apps server from its own manifest script
(htpasswd composed from registered credentials, --private-repos), both on one data directory.
Throwaway passwords; nothing is printed.
"""

from __future__ import annotations

import os
import re
import secrets
import subprocess
from pathlib import Path

import pytest
import yaml
from conftest import require_cli

from jarvis.secrets.mint import _htpasswd_line
from support import REPO_ROOT, free_port, wait_listening

APPS_MANIFEST = REPO_ROOT / "core/feature/app-hosting/backups/rest-server.yaml"


def _require_pinned(name: str, version_args: list[str], manifest: Path, image: str) -> str:
    """name on PATH, which must be the release of image that manifest pins."""
    pinned = re.search(rf"image: {re.escape(image)}:(\d+\.\d+\.\d+)@", manifest.read_text(encoding="utf-8"))
    assert pinned, manifest
    binary = require_cli(name)
    out = subprocess.run([binary, *version_args], capture_output=True, text=True, check=False)
    found = re.search(r"(\d+\.\d+\.\d+)", out.stdout + out.stderr)
    assert found and found.group(1) == pinned.group(1), f"{name} on PATH is not the pinned {pinned.group(1)}"
    return binary


@pytest.fixture
def servers(tmp_path: Path):
    """Both servers on one data directory, each binary the release the manifests pin, and three logins."""
    rest_server = _require_pinned("rest-server", ["--version"], APPS_MANIFEST, "restic/rest-server")
    restic = _require_pinned("restic", ["version"], REPO_ROOT / "core/platform/backups/restic-check.yaml", "restic/restic")
    # One binary stands in for both servers: they must run one image.
    image = re.compile(r"image: restic/rest-server:\S+")
    core_manifest = REPO_ROOT / "core/platform/backups/rest-server.yaml"
    assert image.findall(APPS_MANIFEST.read_text(encoding="utf-8")) == image.findall(core_manifest.read_text(encoding="utf-8"))
    data, creds, auth = tmp_path / "data", tmp_path / "credentials", tmp_path / "auth"
    for folder in (data, creds, auth):
        folder.mkdir()
    logins = {user: secrets.token_urlsafe(24) for user in ("restic", "ns-app-shop", "ns-app-blog")}
    (tmp_path / "core.htpasswd").write_text(_htpasswd_line("restic", logins["restic"]), encoding="utf-8")
    for app in ("app-shop", "app-blog"):
        (creds / app).mkdir()
        # The blog line names core's user; the apps server must still call it ns-app-blog.
        named = "restic" if app == "app-blog" else f"ns-{app}"
        (creds / app / "htpasswd").write_text(_htpasswd_line(named, logins[f"ns-{app}"]), encoding="utf-8")
    # A folder that is not a namespace name never becomes a user.
    (creds / "app-Bad").mkdir()
    (creds / "app-Bad" / "htpasswd").write_text(_htpasswd_line("x", logins["ns-app-shop"]), encoding="utf-8")
    core_port, apps_port = free_port(), free_port()
    container = yaml.safe_load(APPS_MANIFEST.read_text(encoding="utf-8").split("\n---\n")[0])["spec"]["template"]["spec"]["containers"][0]
    script = (
        container["command"][-1]
        .replace("/credentials", str(creds))
        .replace("/run/auth", str(auth))
        .replace("--path /data", f"--path {data}")
        .replace("--listen :8000", f"--listen 127.0.0.1:{apps_port}")
    )
    env = {**os.environ, "PATH": f"{Path(rest_server).parent}:{os.environ['PATH']}"}
    procs = [
        subprocess.Popen(
            [rest_server, "--path", str(data), "--htpasswd-file", str(tmp_path / "core.htpasswd"), "--listen", f"127.0.0.1:{core_port}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ),
        subprocess.Popen(["sh", "-c", script], env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True),
    ]
    try:
        wait_listening(core_port, procs[0])
        wait_listening(apps_port, procs[1])
        yield {"core": core_port, "apps": apps_port, "logins": logins, "tmp": tmp_path, "auth": auth, "restic": restic}
    finally:
        for proc in procs:
            proc.terminate()
            proc.wait(timeout=10)


def _restic(servers: dict, port: str, user: str, login: str, path: str, password: str, *args: str) -> int:
    env = {
        **os.environ,
        "RESTIC_REPOSITORY": f"rest:http://127.0.0.1:{servers[port]}/{path}",
        "RESTIC_REST_USERNAME": user,
        "RESTIC_REST_PASSWORD": login,
        "RESTIC_PASSWORD": password,
    }
    return subprocess.run(
        [servers["restic"], "--no-cache", "--retry-lock", "0s", *args], env=env, capture_output=True, text=True, check=False, timeout=60
    ).returncode


def test_app_login_is_confined_to_its_own_repositories(servers: dict) -> None:
    """Rule: an app's login inits, backs up and prunes its own repositories on the apps server, and opens, prunes or
    creates no other repository on either server, even with the right password; its user is its folder's name."""
    logins = servers["logins"]
    core_pw, shop_pw, blog_pw = (secrets.token_urlsafe(24) for _ in range(3))
    payload = servers["tmp"] / "payload"
    payload.write_text("fixture", encoding="utf-8")

    def core(path: str, *args: str) -> int:
        return _restic(servers, "core", "restic", logins["restic"], path, core_pw, *args)

    def shop(path: str, *args: str, password: str = shop_pw, port: str = "apps") -> int:
        return _restic(servers, port, "ns-app-shop", logins["ns-app-shop"], path, password, *args)

    # Core repositories and another app's, written by their own logins.
    for path in ("host-etcd-snapshots", "ns-platform-x/pvc-data"):
        assert core(path, "init") == 0
        assert core(path, "backup", str(payload)) == 0
    assert _restic(servers, "apps", "ns-app-blog", logins["ns-app-blog"], "ns-app-blog/pvc-db", blog_pw, "init") == 0

    # Its own repository: init, backup, forget and prune.
    own = "ns-app-shop/pvc-db"
    assert shop(own, "init") == 0
    assert shop(own, "backup", str(payload)) == 0
    assert shop(own, "forget", "--keep-last", "1", "--prune") == 0
    assert shop(own, "snapshots") == 0

    # Never a core repository or another app's, even holding the right restic password.
    # Exit codes are compared outside the assert, so a failure never shows a password.
    opened = []
    for path, password in (
        ("host-etcd-snapshots", core_pw),
        ("ns-platform-x/pvc-data", core_pw),
        ("ns-app-blog/pvc-db", blog_pw),
    ):
        if shop(path, "cat", "config", password=password) == 0:
            opened.append(f"apps server opened {path}")
        if shop(path, "forget", "--keep-last", "1", "--prune", password=password) == 0:
            opened.append(f"apps server pruned {path}")
        if shop(path, "cat", "config", password=password, port="core") == 0:
            opened.append(f"core server opened {path}")
    if shop("ns-platform-x/pvc-new", "init") == 0:
        opened.append("apps server created a core repository")
    # A registered line that names core's user still serves only ns-app-blog.
    if _restic(servers, "apps", "restic", logins["ns-app-blog"], "ns-app-blog/pvc-db", blog_pw, "cat", "config") == 0:
        opened.append("apps server took the user name from the sealed line")
    assert opened == []
    assert core("ns-platform-x/pvc-data", "snapshots") == 0
    users = [line.split(":", 1)[0] for line in (servers["auth"] / "htpasswd").read_text(encoding="utf-8").splitlines()]
    assert sorted(users) == ["ns-app-blog", "ns-app-shop"]
