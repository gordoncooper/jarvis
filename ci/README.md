# CI

`ci/run-checks` runs every check listed in `ci/rule-checks.txt` and reports each one that fails. Any failure makes the run fail. `ci/tests/` holds the pytest suite. CI runs both once; the product holds only the `env-00` sample.

Run `bash ci/run-checks` and the tests near your change before pushing. CI at the head SHA is the test record.

CI picks the tests by the files a pull request changes (`ci/select-tests`): a docs-only PR runs the rule checks and no pytest; a Renovate PR (`renovate/*`) runs `make deps`, which downloads and checksums every pinned CLI, the rule checks, and only the tests that name its changed files, or every test when it cannot place one; any other PR, and every push to `main`, runs the whole suite.

pytest runs on every CPU (`pytest-xdist`, `-n auto` in `pytest.ini`). Each test gets its own `tmp_path`, so tests must not share a fixed path. `-n 0` runs serially, for a debugger or a single slow test.

`ci/run-env-checks` runs the env-level checks (the ones that read `envs/<env>` through `INSTANCE`) in one go: `INSTANCE=<path> bash ci/run-env-checks`. An instance repo's CI calls it, so a check added to its list reaches every instance. `ENVS="<env> ..."` limits the run to those envs (each must exist), so an instance CI can check each env against the core tag that env pins; `.sops.yaml` rules are still checked against every env folder.

`ci/checkout-apps-for-instance.sh` runs first in instance CI and on an operator's checkout before `make verify`. It fetches the instance's `apps_git_repo_url` at its default branch with every tag, since each env deploys and checks an app at its pinned tag (`versions:` in `values/apps.yaml`), logs the checked SHA, and prints the directory to export as `APPS_REPO`. It prints nothing for a core-only instance, which needs no apps checkout, and refuses envs that read different apps repositories. Product CI never runs it; product tests use fixtures.

Tests that build a throwaway env copy refuse a tracked path in the repo. Where the temp dir must sit inside a worktree, point pytest at a git-ignored one: `pytest --basetemp=.scratch/tmp`.

## End to end

`.github/workflows/e2e.yml` brings up a real cluster from the commit under test: k3d (k3s in Docker, at the env's `k3s_version`), the flux-operator role applying the env's FluxInstance and `sync.yaml`, every Kustomization and HelmRelease Ready, `env-00`'s hosted apps running from the apps repository at the tags `env-00` pins (each app's sources at its tag, its Deployments Available, and hello-web answering over HTTPS with notes reachable and lan-demo refused), then `make verify` and `make drill`. The env is a throwaway copy of `env-00` with app hosting and no add-on (`ci/e2e/make-env`), with secrets sealed to an age key that lives only on the runner. Its root source is a temporary branch of this repository holding the commit and that env, and so is its core source, except on a pushed release tag, where core is the tag itself; the job deletes the branch. It runs nightly, by hand, on every pull request and on every `v*` tag. Its `changes` job decides: a pull request that touches no platform, add-on, env, Flux-role, tool or schema code reports `e2e` as skipped, which branch protection counts as passing, so `e2e` can be a required check. A fork's pull request cannot push the branch, so it is skipped there too.

## Coding-agent deny lists

`.claude/settings.json` and `.cursor/cli.json` hold the same short deny list, each in its client's own syntax: no commands that reach real hosts or a cluster (`ssh`, `scp`, `ansible` ad-hoc, `kubectl`, `helm`, `flux`, the host-running `make` targets and tools), no secret decryption (`sops`, `age -d`) or reads of the age key, kubeconfigs and SSH keys, and no force-push or merge. They are a speed bump, not the guard: a coding agent never holds the age key, a kubeconfig or an SSH key. No check proves them; keep the two files in step by hand.

## Known weak spots

These are gaps a maintainer should know about when a change looks green.

- **Manifest schemas need the network.** `test_flux_entry.py` validates rendered manifests with kubeconform, which downloads the Kubernetes schemas, a public CRD catalog and the VolSync CRDs for the pinned chart version. A failed download fails the test. The rendered tree holds no ReplicationSource or ReplicationDestination, since the backup policy generates them, so only the seeded test exercises the VolSync schemas.
- **The manifest checks read raw files.** `digest-pin`, `loadbalancer`, `manifest-lint` and `pvc-backup` scan YAML as written. A value that only appears after `kustomize build` (a patch, a generator) or inside a rendered HelmRelease is not seen.
- **`require_roots` is off by default in the library functions.** The four check scripts turn it on, and `test_manifest_roots.py` runs the scripts to prove it. A new caller of `load_documents` has to opt in.
- **The k3s role's `main.yml` is never run end to end.** It downloads a binary, writes under `/etc` and starts a service, which needs root and a network. Tests cover its parts: the templates, the join URL expression, the refuse guard, the mount drop-in, and the modes of the config files. A change to the order of tasks in `main.yml` is not caught.
- **The node token read is not run.** The `slurp` of the control plane's node token in `token.yml` needs a real k3s server. Only the name resolution and the URL are tested.
- **Host tools are faked.** Role tests put a fake `systemctl` or `findmnt` first on `PATH`. They prove the role reacts to the answers it is given, not that the real tools answer that way.
- **Renovate config is checked by regex only.** The tests run each manager's pattern against the files it targets. They do not run Renovate. Run `renovate-config-validator` after any edit to `renovate.json`.
- **Tests that need `sops`, `age-keygen`, `kustomize` or `ansible-playbook` skip when the tool is missing, except in CI, where a missing tool fails.** A green local run on a machine without them proves less than CI does.
- **`test_app_admission_apiserver.py` skips without its API server, except in CI.** It alone runs the hosted-app policies on a real `kube-apiserver`, which validates them as the kyverno CLI does not. Run `ci/fetch-test-apiserver` first to run it locally: it downloads the pinned binaries into `.scratch/apiserver/`.
