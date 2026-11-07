# CI

`ci/run-checks` runs every check listed in `ci/rule-checks.txt` and reports each one that fails. Any failure makes the run fail. `ci/tests/` holds the pytest suite. CI runs both twice, once on the full tree and once with every env except the `env-00` sample removed.

Run `bash ci/run-checks` and the tests near your change before pushing. CI at the head SHA is the test record.

`ci/run-env-checks` runs the env-level checks (the ones that read `envs/<env>` through `INSTANCE`) in one go: `INSTANCE=<path> bash ci/run-env-checks`. An instance repo's CI calls it, so a check added to its list reaches every instance.

`ci/checkout-apps-for-instance.sh` runs first in instance CI and on an operator's checkout before `make verify`. It fetches the tip of the instance's `apps_git_repo_url`, at the branch `platform/optional/app-hosting/app/flux.yaml` deploys, logs the checked SHA, and prints the directory to export as `APPS_REPO`. It prints nothing for a core-only instance, which needs no apps checkout, and refuses envs that read different apps repositories. Product CI never runs it; product tests use fixtures.

Tests that build a throwaway env copy refuse a tracked path in the repo. Where the temp dir must sit inside a worktree, point pytest at a git-ignored one: `pytest --basetemp=.scratch/tmp`.

## Coding-agent deny lists

`ci/checks/cli-deny` proves `.cursor/cli.json` and `.claude/settings.json` against one corpus, `ci/checks/fixtures/guard-corpus.tsv`. To add a category, add `block` rows that name it (the bare command and the wrapper forms both clients see), add `allow` rows for the everyday commands nearest to it, then add entries to both files until the check passes. Leave a form neither client can block out of the corpus and describe it by category only. A failure names the client, category and corpus line, never the command.

Write each entry so it names a command position, not a word anywhere in the line: a guarded word also appears in commit messages, search patterns, test selectors and file paths, and an allow rule cannot carve an exception out of a deny rule in either client. A deny entry that fires on a wrapper names both the wrapper and the program it wraps, so an unrelated command that merely mentions the program stays allowed.

## Known weak spots

These are gaps a maintainer should know about when a change looks green.

- **Manifest schemas need the network.** `test_flux_entry.py` validates rendered manifests with kubeconform, which downloads the Kubernetes schemas, a public CRD catalog and the VolSync CRDs for the pinned chart version. A failed download fails the test. The rendered tree holds no ReplicationSource or ReplicationDestination, since the backup policy generates them, so only the seeded test exercises the VolSync schemas.
- **The manifest checks read raw files.** `digest-pin`, `loadbalancer`, `manifest-lint` and `pvc-backup` scan YAML as written. A value that only appears after `kustomize build` (a patch, a generator) or inside a rendered HelmRelease is not seen.
- **`require_roots` is off by default in the library functions.** The four check scripts turn it on, and `test_manifest_roots.py` runs the scripts to prove it. A new caller of `load_documents` has to opt in.
- **The k3s role's `main.yml` is never run end to end.** It downloads a binary, writes under `/etc` and starts a service, which needs root and a network. Tests cover its parts: the templates, the join URL expression, the refuse guard, the mount drop-in, and the modes of the config files. A change to the order of tasks in `main.yml` is not caught.
- **The node token read is not run.** The `slurp` of the control plane's node token in `token.yml` needs a real k3s server. Only the name resolution and the URL are tested.
- **Host tools are faked.** Role tests put a fake `systemctl` or `findmnt` first on `PATH`. They prove the role reacts to the answers it is given, not that the real tools answer that way.
- **Renovate config is checked by regex only.** The tests run each manager's pattern against the files it targets. They do not run Renovate. Run `renovate-config-validator` after any edit to `renovate.json`.
- **`cli-deny` proves a model of each client, not the client.** The model follows each client's documented matching; where Claude Code's docs are silent it takes the stricter reading for each verdict. A client release that matches differently is not caught.
- **`core-names` matches whole tokens.** A value written in an encoded or split form is not matched. Binary files and files that are not UTF-8 are skipped.
- **Tests that need `sops`, `age-keygen`, `kustomize` or `ansible-playbook` skip when the tool is missing, except in CI, where a missing tool fails.** A green local run on a machine without them proves less than CI does.
- **`test_app_admission_apiserver.py` skips without its API server, except in CI.** It alone runs the hosted-app policies on a real `kube-apiserver`, which validates them as the kyverno CLI does not. Run `ci/fetch-test-apiserver` first to run it locally: it downloads the pinned binaries into `.scratch/apiserver/`.
