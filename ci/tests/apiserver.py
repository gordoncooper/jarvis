"""A throwaway kube-apiserver and etcd on loopback, for proofs that need the real API.

The binaries are mise.dev.toml's pins (Linux). The client is a token the caller makes and a loopback
URL; a kubeconfig, when one is written, names only that, so a proof cannot reach another cluster.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml
from conftest import ci_mode

from support import REPO_ROOT, free_port

WORK = REPO_ROOT / ".scratch" / "apiserver"


def binaries() -> dict[str, str]:
    """kube-apiserver and etcd at their mise.dev.toml pins, which make deps installs on Linux."""
    found = {name: shutil.which(name) for name in ("kube-apiserver", "etcd")}
    missing = sorted(name for name, path in found.items() if path is None)
    if missing:
        message = f"no {' or '.join(missing)} on PATH (make deps installs them on Linux)"
        if ci_mode():
            pytest.fail(f"{message} — required in CI")
        pytest.skip(message)
    return {name: str(path) for name, path in found.items()}


def eventually(check, what: str, seconds: int = 60):
    """check()'s first truthy answer. Fails naming what, once seconds pass."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        answer = check()
        if answer:
            return answer
        time.sleep(0.2)
    pytest.fail(f"timed out waiting for {what}")


class Api:
    def __init__(self, port: int, token: str) -> None:
        self.port, self.url, self.token = port, f"https://127.0.0.1:{port}", token
        # Its own self-signed certificate, on loopback. No proxy, whatever the environment says.
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))

    def request(self, method: str, path: str, body: dict | None = None, *, as_user: str | None = None) -> tuple[int, bytes]:
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        if as_user is not None:
            headers["Impersonate-User"] = as_user
            headers["Impersonate-Group"] = "system:authenticated"
        request = urllib.request.Request(self.url + path, method=method, data=json.dumps(body).encode() if body else None,
                                         headers=headers)
        try:
            with self.opener.open(request, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def kubeconfig(self, path: Path) -> Path:
        """A kubeconfig for this server alone, at path (mode 0600)."""
        config = {
            "apiVersion": "v1", "kind": "Config", "current-context": "proof",
            "clusters": [{"name": "proof", "cluster": {"server": self.url, "insecure-skip-tls-verify": True}}],
            "users": [{"name": "proof", "user": {"token": self.token}}],
            "contexts": [{"name": "proof", "context": {"cluster": "proof", "user": "proof"}}],
        }
        path.write_text(yaml.safe_dump(config), encoding="utf-8")
        path.chmod(0o600)
        return path


@contextmanager
def serve(tokens: dict[str, str]) -> Iterator[tuple[int, Path]]:
    """etcd and kube-apiserver until the block ends: (secure port, work directory). tokens maps
    each bearer token to its tokens.csv rest (user, uid, groups)."""
    found = binaries()
    WORK.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="run-", dir=WORK))
    (work / "tokens.csv").write_text("".join(f"{token},{rest}\n" for token, rest in tokens.items()), encoding="utf-8")
    subprocess.run(["openssl", "genrsa", "-out", str(work / "sa.key"), "2048"], check=True, capture_output=True)
    client, peer, secure = free_port(), free_port(), free_port()
    log = (work / "server.log").open("wb")
    env = {key: os.environ[key] for key in ("PATH",) if key in os.environ}
    servers = [
        subprocess.Popen([found["etcd"], "--data-dir", str(work / "etcd"), "--unsafe-no-fsync",
                          "--listen-client-urls", f"http://127.0.0.1:{client}", "--advertise-client-urls", f"http://127.0.0.1:{client}",
                          "--listen-peer-urls", f"http://127.0.0.1:{peer}", "--initial-advertise-peer-urls", f"http://127.0.0.1:{peer}",
                          "--initial-cluster", f"default=http://127.0.0.1:{peer}"], stdout=log, stderr=subprocess.STDOUT, env=env),
        subprocess.Popen([found["kube-apiserver"], "--etcd-servers", f"http://127.0.0.1:{client}",
                          "--bind-address", "127.0.0.1", "--advertise-address", "127.0.0.1", "--secure-port", str(secure),
                          "--cert-dir", str(work / "certs"), "--token-auth-file", str(work / "tokens.csv"),
                          "--authorization-mode", "RBAC", "--anonymous-auth=false", "--allow-privileged=true",
                          "--service-cluster-ip-range", "10.0.0.0/24", "--disable-admission-plugins", "ServiceAccount",
                          "--service-account-issuer", f"https://127.0.0.1:{secure}", "--service-account-key-file", str(work / "sa.key"),
                          "--service-account-signing-key-file", str(work / "sa.key")], stdout=log, stderr=subprocess.STDOUT, env=env),
    ]
    try:
        probe = Api(secure, next(iter(tokens)))

        def ready() -> bool:
            assert all(proc.poll() is None for proc in servers), (work / "server.log").read_text(encoding="utf-8")[-4000:]
            try:
                return probe.request("GET", "/readyz")[0] == 200
            except OSError:
                return False

        eventually(ready, "the API server to be ready", seconds=120)
        yield secure, work
    finally:
        for proc in reversed(servers):
            proc.terminate()
            proc.wait(timeout=30)
        log.close()
        shutil.rmtree(work)


def token() -> str:
    return secrets.token_urlsafe(24)
