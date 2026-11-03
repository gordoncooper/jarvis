# CI

`ci/run-checks` runs every check listed in `ci/rule-checks.txt` and reports each one that fails. Any failure makes the run fail. `ci/tests/` holds the pytest suite. CI runs both twice, once on the full tree and once with every env except the `env-00` sample removed.

Run `bash ci/run-checks` and the tests near your change before pushing. CI at the head SHA is the test record.

## Known weak spots

These are gaps a maintainer should know about when a change looks green.

- **Manifest schemas need the network.** `test_flux_entry.py` validates rendered manifests with kubeconform, which downloads the Kubernetes schemas and a public CRD catalog. The local VolSync schemas accept any `spec`, so a typo in a ReplicationSource spec is not caught. Vendoring the real schemas is the fix.
- **The manifest checks read raw files.** `digest-pin`, `loadbalancer`, `manifest-lint` and `pvc-backup` scan YAML as written. A value that only appears after `kustomize build` (a patch, a generator) or inside a rendered HelmRelease is not seen.
- **`require_roots` is off by default in the library functions.** The four check scripts turn it on, and `test_manifest_roots.py` runs the scripts to prove it. A new caller of `load_documents` has to opt in.
- **The k3s role's `main.yml` is never run end to end.** It downloads a binary, writes under `/etc` and starts a service, which needs root and a network. Tests cover its parts: the templates, the join URL expression, the refuse guard, the mount drop-in, and the modes of the config files. A change to the order of tasks in `main.yml` is not caught.
- **The node token read is not run.** The `slurp` of the control plane's node token in `token.yml` needs a real k3s server. Only the name resolution and the URL are tested.
- **Host tools are faked.** Role tests put a fake `systemctl` or `findmnt` first on `PATH`. They prove the role reacts to the answers it is given, not that the real tools answer that way.
- **Renovate config is checked by regex only.** The tests run each manager's pattern against the files it targets. They do not run Renovate. Run `renovate-config-validator` after any edit to `renovate.json`.
- **`core-names` matches whole tokens.** A value written in an encoded or split form is not matched. Binary files and files that are not UTF-8 are skipped.
- **Tests that need `sops`, `age-keygen`, `kustomize` or `ansible-playbook` skip when the tool is missing, except in CI, where a missing tool fails.** A green local run on a machine without them proves less than CI does.
