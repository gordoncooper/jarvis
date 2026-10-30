"""P0b verify trusts envs/<env>/ca.crt and the canary body (R28). Stubbed curl and openssl, no network."""

from __future__ import annotations

import hashlib
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
from subprocess import CompletedProcess

REPO_ROOT = Path(__file__).resolve().parents[2]
_BODY = "testapp\n"
_SHA = "60eea60c805ba09c287eead2e6f0c7f04b5b8e1f66634f941216527e41c8bcc7"
_CHECKSUM = "a" * 64
_OTHER = "b" * 64
_ETCD = "c" * 64

_LOADER = SourceFileLoader("verify_p0b_tls", str(REPO_ROOT / "tools" / "verify" / "p0b"))
_SPEC = importlib.util.spec_from_loader(_LOADER.name, _LOADER)
assert _SPEC is not None
verify_p0b = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(verify_p0b)


def _write_env(root: Path) -> str:
    env = "env-test"
    values = root / "envs" / env / "values"
    values.mkdir(parents=True)
    (values / "cluster-vars.yaml").write_text("dns_suffix: test.example\n", encoding="utf-8")
    return env


def _log(second: str) -> str:
    lines = [
        f"{_CHECKSUM}  /data/payload",
        'persistentvolumeclaim "drill" deleted',
        "restore demo/drill",
        f"{second}  /data/payload",
        f"{_ETCD}  /snapshots/etcd-snapshot-node-1",
        f"{_ETCD}  /restore/snapshots/etcd-snapshot-node-1",
    ]
    return "\n".join(lines) + "\n"


def test_verify_p0b_ca_body_and_drill_log(tmp_path: Path, monkeypatch, capsys) -> None:
    assert hashlib.sha256(_BODY.encode()).hexdigest() == _SHA
    assert verify_p0b._CANARY_SHA256 == _SHA
    contract = (REPO_ROOT / "platform" / "testapp" / "CONTRACT.md").read_text(encoding="utf-8")
    assert _SHA in contract

    env = _write_env(tmp_path)
    ca = tmp_path / "envs" / env / "ca.crt"
    log = tmp_path / "drill.log"
    calls: list[list[str]] = []
    state = {"body": _BODY, "rc": 0, "err": ""}

    def fake_run(argv: list[str], *, input: str | None = None) -> CompletedProcess[str]:
        calls.append(list(argv))
        if argv[0] == "curl":
            assert argv[argv.index("--cacert") + 1] == str(ca)
            return CompletedProcess(list(argv), 0, state["body"], "")
        if argv[:2] == ["openssl", "s_client"]:
            assert argv[argv.index("-CAfile") + 1] == str(ca)
            assert "-verify_return_error" in argv
            return CompletedProcess(list(argv), state["rc"], "CERT\n", state["err"])
        if argv[:2] == ["openssl", "x509"]:
            assert input == "CERT\n"
            return CompletedProcess(list(argv), 0, "issuer=CN = test-ca\n", "")
        raise AssertionError(argv)

    monkeypatch.setattr(verify_p0b, "_run", fake_run)

    def go() -> tuple[int, str]:
        code = verify_p0b.run(tmp_path, env, log, verify_p0b._fetch, verify_p0b._issuer)
        return code, capsys.readouterr().out

    ca.write_text("dummy\n", encoding="utf-8")
    log.write_text(_log(_CHECKSUM), encoding="utf-8")
    code, out = go()
    assert code == 0
    assert "PASS lan curl https://testapp.test.example/" in out
    assert "testapp" in out.splitlines()
    assert "PASS cert issuer issuer=CN = test-ca" in out
    assert f"PASS restore checksum {_CHECKSUM}" in out
    assert out.splitlines()[-1] == "automatic checks passed; 1 owner items remain"
    curl = next(item for item in calls if item[0] == "curl")
    assert f"https://testapp.test.example/" in curl
    client = next(item for item in calls if item[:2] == ["openssl", "s_client"])
    assert "-CAfile" in client and "-verify_return_error" in client

    calls.clear()
    state["body"] = "not-the-canary\n"
    code, out = go()
    assert code != 0
    assert "FAIL lan curl https://testapp.test.example/: body sha256" in out
    assert "automatic checks passed" not in out

    calls.clear()
    state["body"] = _BODY
    state["rc"] = 1
    state["err"] = "verify error:num=20:unable to get local issuer certificate\n"
    code, out = go()
    assert code != 0
    assert "FAIL cert issuer: verify error:num=20:unable to get local issuer certificate" in out
    assert not any(item[:2] == ["openssl", "x509"] for item in calls)
    assert "automatic checks passed" not in out

    calls.clear()
    state["err"] = (
        "Connecting to testapp.test.example:443\n"
        "depth=0 CN = testapp.test.example\n"
        "verify error:num=20:unable to get local issuer certificate\n"
        "Verify return code: 20 (unable to get local issuer certificate)\n"
    )
    code, out = go()
    assert code != 0
    assert "FAIL cert issuer: verify error:num=20:unable to get local issuer certificate" in out
    assert "Connecting to" not in out

    calls.clear()
    state["err"] = "Connecting to testapp.test.example:443\n"
    code, out = go()
    assert code != 0
    assert "FAIL cert issuer: Connecting to testapp.test.example:443" in out

    calls.clear()
    state["rc"] = 0
    state["err"] = ""
    ca.unlink()
    code, out = go()
    assert code != 0
    assert f"FAIL missing envs/{env}/ca.crt" in out
    assert calls == []
    assert "automatic checks passed" not in out

    ca.write_text("dummy\n", encoding="utf-8")
    log.write_text(_log(_OTHER), encoding="utf-8")
    code, out = go()
    assert code != 0
    assert "FAIL drill log:" in out
    assert "PASS lan curl https://testapp.test.example/" in out
    assert "automatic checks passed" not in out
