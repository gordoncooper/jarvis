# Wrap-up audit (Grok 4.7)

SHA `a7550522bf63924c3a1eb5dd1a3a9d17b49d06b1`. Read-only audit. No product code changed. No secrets decrypted. No live cluster, SSH, or age key.

The paper walk used a temporary copy of `envs/` with `env-01` removed, plus the docs the deploy path links. Hardware was not touched. Single-node and mixed-GPU shapes were checked against the schema and the fixtures the suite already runs.

## Big picture

The platform half is a real product slice: one control plane, Flux from Git, SOPS, an internal CA, LAN-only ingress and DNS, digest pins, and a backup policy that enrolls PVCs instead of hand-writing them. `env-00` is a usable template. Core stays free of the owner's name. The checks that guard that shape are unusually thorough, and they pass on this SHA aside from one failure that belongs to this machine (see Concerns).

What is fragile is everything an adopter has to get right by hand before those checks run, and everything that has to survive a dead disk. Three copies of "where Flux syncs" and "what the cluster is called" (values, `flux-instance.yaml`, the generated ConfigMap) are reconciled by pytest, not by the schema command the new-env docs actually drive. Recovery is a same-node etcd reset plus a scratch-volume drill. The k3s server token and the k3s data directory stay on the root disk, outside `backup_path`, and the restic copy of etcd has no owner-run restore. A green `make drill` does not mean a dead disk is recoverable.

The same split shows up in the next layers. Hermes, Grafana, the apps repo URL, and the code-delivery provider are already names in the schema, in bootstrap secrets, and in the owner's env README. None of them is on a Flux path that decrypts secrets. The doc index then points a new reader at that owner's env and at the agent rulebook, which the product is not supposed to need.

## Ranked findings

| ID | Severity | Class | Finding | Questions |
| --- | --- | --- | --- | --- |
| F1 | critical | bug | Dead disk is not recoverable: token and k3s state sit outside the backup set, and the etcd restic copy cannot be restored | 3, 4, 6, 13 |
| F2 | high | bug | The written restic rotation issues a new password and leaves existing repositories unreadable | 15, 16 |
| F3 | high | bug | The documented `prep-node` order fails the schema after the host has already been changed | 9, 10 |
| F4 | high | bug | A clone cannot add an env whose GitHub owner differs from `env-01` | 5, 8, 9 |
| F5 | high | bug | `flux-instance.yaml` sync URL and ref are not checked against `cluster-vars` | 4, 9, 10 |
| F6 | high | bug | An age-key mismatch prompt deletes every encrypted secret for the env | 15, 17 |
| F7 | high | bug | kubectl Secret reads are denied for two case spellings only | 17 |
| F8 | high | bug | Deploy tells the adopter to read a secrets README the template does not have | 9, 12, 15 |
| F9 | medium | brittle | A stale cluster-vars ConfigMap passes the env schema | 4, 10 |
| F10 | medium | brittle | The address-change `cluster-reset` is easy to run instead of a snapshot restore | 13, 16 |
| F11 | medium | brittle | Kyverno enrollment ignores admission failures, so a PVC can exist with no backup | 2, 6, 17 |
| F12 | medium | brittle | `make verify` treats a fresh sync timestamp as a healthy backup | 13, 16, 18 |
| F13 | medium | brittle | k3s, Flux, and an upstream pull have no adopter upgrade procedure | 12, 14 |
| F14 | medium | brittle | Single-node and mixed GPUs are fixtures, not a deploy path, and there is no GPU runtime | 11, 22, 23 |
| F15 | medium | brittle | New services have no decrypting Flux path, and the apps repo URL deploys nothing | 21, 23 |
| F16 | medium | strict | Kyverno's backup role can delete every Secret | 17 |
| F17 | medium | strict | Opt-out and future-product keys are tighter than the trees that exist | 5 |
| F18 | medium | clarity | Most contracts omit Operations or Limits, so runbooks stay scattered | 16 |
| F19 | medium | cruft | Bootstrap always writes Grafana and Hermes secrets Flux does not apply | 7, 21, 24 |
| F20 | low | cruft | Plan codes sit in product YAML the plan-code check does not scan; the doc index leads with the owner env | 8, 12 |
| F21 | low | brittle | `make deps` installs apt packages without updating package lists | 9, 10 |

## Findings

### F1. Dead disk is not recoverable

**What breaks and for whom.** An operator who loses the control-plane root disk, or the one disk that holds both volume data and restic, cannot rebuild the cluster from the copies the repo knows how to make. `make drill` still passes on a healthy cluster. This is the gap between the written limit ("not disk loss") and a product someone else will run.

**Location.** `infra/ansible/group_vars/all.yml:6`, `infra/ansible/roles/k3s/tasks/token.yml:23`, `infra/ansible/roles/k3s/templates/config.yaml.j2:4`, `infra/ansible/roles/node-disk/tasks/main.yml:76`, `platform/backups/CONTRACT.md:183`, `platform/backups/CONTRACT.md:187`, `platform/backups/CONTRACT.md:192`, `platform/backups/rest-server.yaml:27`, `platform/backups/etcd-snapshots.yaml:23`.

**Question.** 3, 4, 6, 13.

**Evidence.** k3s state stays on the root disk. The server token is read from that tree and is not part of `backup_path`:

```6:6:infra/ansible/group_vars/all.yml
k3s_data_dir: /var/lib/rancher/k3s
```

```21:23:infra/ansible/roles/k3s/tasks/token.yml
- name: Read the server node token
  ansible.builtin.slurp:
    src: /var/lib/rancher/k3s/server/node-token
```

Snapshots are configured onto `backup_path`, and secrets encryption is on:

```4:4:infra/ansible/roles/k3s/templates/config.yaml.j2
secrets-encryption: true
```

```10:12:infra/ansible/roles/k3s/templates/config.yaml.j2
etcd-snapshot-schedule-cron: "{{ k3s_etcd_snapshot_schedule_cron }}"
etcd-snapshot-retention: {{ k3s_etcd_snapshot_retention }}
etcd-snapshot-dir: {{ backup_path }}/etcd-snapshots
```

With `data_disk: true`, that backup path is forced onto the same filesystem as the volume data on every such host:

```76:82:infra/ansible/roles/node-disk/tasks/main.yml
    - name: Refuse local_path_data or backup_path outside the data mount
      ansible.builtin.assert:
        that:
          - local_path_data is match('^' ~ (node_disk_mount | regex_escape) ~ '/.+')
          - >-
            backup_path == node_disk_mount
            or backup_path is match('^' ~ (node_disk_mount | regex_escape) ~ '/')
```

Restic itself is a hostPath on the single `backup` node (`platform/backups/rest-server.yaml:27` and `:65`). The etcd restic copy is taken on the control plane (`platform/backups/etcd-snapshots.yaml:23`). The contract then says the reset does not use that copy, and a rebuilt node needs the original token:

```183:183:platform/backups/CONTRACT.md
k3s secrets-encryption is on, so restoring onto a rebuilt node also needs the original k3s server token. Same-node restore is the scope. The drill only checks that the newest restic copy matches the k3s file's sha256.
```

```187:188:platform/backups/CONTRACT.md
- Backups live on the same host as the data unless `backup_path` is on another disk. They cover mistakes and rollback, not disk loss.
- While the rest-server's data sits on the same disk as the k3s snapshot files, the etcd restic copy adds retention and dedup, not disk-failure protection.
```

```192:192:platform/backups/CONTRACT.md
- An older snapshot that exists only in the restic copy has no owner-run path yet.
```

`env-00` does put the `backup` role on its own host (`envs/env-00/inventory/hosts.yml:21`), so a worker disk is not the only copy of PVC bytes. The token and the live datastore are still only on the control-plane root disk. The "other disk" sentence cannot be combined with `data_disk: true`, because that flag refuses a `backup_path` off `/var/lib/jarvis`.

**Severity.** critical.

**Size guess.** L.

**Suggested fix.** Back up the server credential directory with the etcd snapshots, and give the restic copy an owner-run restore onto a reinstalled node. Allow `backup_path` on a second disk even when volume data uses `data_disk`. What we lose: the current rule that a data mount contains only Jarvis directories, and the "same-node reset is the whole etcd story" simplicity.

**Confidence.** high. The token path and the "no owner-run path" sentence are in the repo. A live disk pull was not run.

### F2. Restic rotation orphans existing repositories

**What breaks and for whom.** An operator who follows the only rotation sentence gets a new restic password and a new htpasswd. Repositories already on disk stay encrypted to the old password. Rollback copies become unreadable. The live volumes are untouched until the next backup cannot append.

**Location.** `envs/env-01/secrets/README.md:25`, `tools/bootstrap-secrets:261`, `platform/backups/CONTRACT.md` (Operations has no rotation section).

**Question.** 15, 16.

**Evidence.**

```25:25:envs/env-01/secrets/README.md
Bootstrap files: internal CA (`cert-manager-ca`), VolSync restic credentials (`backups-restic`: `RESTIC_PASSWORD`, `RESTIC_REPOSITORY` as the `rest:` server prefix, `RESTIC_REST_USERNAME`, `RESTIC_REST_PASSWORD`, and bcrypt `htpasswd`; a volume's repository URL appends `ns-<namespace>/pvc-<pvc>`), Grafana admin password (`grafana-admin`), and per-deploy Hermes API keys and dashboard credentials (`hermes-ops-*`, `hermes-code-factory-*`). Later secrets (Telegram, Google OAuth, and so on) are added with `make seal` or other make targets. `bootstrap-secrets` does not rewrite an existing file. To rotate `backups-restic`, delete that file and run the command again. Encryption uses the public key in `.sops.yaml`.
```

```261:268:tools/bootstrap-secrets
def _restic_string_data() -> dict[str, str]:
    rest_password = _random_token(32)
    return {
        "RESTIC_PASSWORD": _random_token(32),
        "RESTIC_REPOSITORY": RESTIC_REPOSITORY,
        "RESTIC_REST_USERNAME": RESTIC_REST_USERNAME,
        "RESTIC_REST_PASSWORD": rest_password,
        "htpasswd": _htpasswd_line(RESTIC_REST_USERNAME, rest_password),
    }
```

`bootstrap` skips a file that already exists (`tools/bootstrap-secrets:351`) and otherwise writes a new random password. `platform/backups/CONTRACT.md` Operations covers PVC restore, the drill, and etcd reset. It does not mention this rotation. The template env has no `secrets/README.md` at all (F8), so the sentence exists only on the owner's env.

**Severity.** high.

**Size guess.** M.

**Suggested fix.** Move rotation into the backups contract and make it a re-key of existing repositories, or say plainly that deleting the file starts empty repositories and abandons the old ones. What we lose: a one-line rotate that looks like the CA rotate.

**Confidence.** high.

### F3. Documented prep-node order fails the schema

**What breaks and for whom.** A new adopter who follows `docs/deploy.md` step for step. `prep-node` changes the host, then exits 1 because the age creation rule is step 3.

**Location.** `docs/deploy.md:36`, `tools/prep-node:894`, `tools/prep-node:983`, `schema/env.py:185`.

**Question.** 9, 10.

**Evidence.** Deploy says to run prep-node, then add the rule, and says the script will not add a missing rule:

```36:37:docs/deploy.md
2. Run `tools/prep-node` for the first host ([Prepare a node](#prepare-a-node)). It can create the age private key, or read one that is already there, and it prints the public key. It rewrites an age recipient that already exists. It does not add a missing creation rule.
3. Add one age creation rule in the repo root `.sops.yaml` for `envs/<env>/secrets/`, using that public key.
```

prep-node validates the whole tree after the host work:

```983:992:tools/prep-node
    sync_age_key(REPO_ROOT, env)
    schema_error: PrepError | None = None
    try:
        run_env_schema(REPO_ROOT)
    except PrepError as exc:
        schema_error = exc
    print("git diff:")
    show_git_diff(REPO_ROOT, env)
    if schema_error is not None:
        raise schema_error
```

When the recipient is absent it prints and returns; it does not write a rule (`tools/prep-node:894`).

Command, on a temporary tree with `env-01` removed, `env-00` copied to `lab`, paths and `jarvis_env` rewritten, no creation rule:

```text
A_renamed_no_sops_rule: SCHEMA_FAIL
  .sops.yaml: expected exactly one creation rule for envs/lab/secrets/, found 0
```

That is `schema/env.py:185`. The host edits have already happened by the time prep-node raises.

**Severity.** high.

**Size guess.** S.

**Suggested fix.** Validate before SSH, or write the creation rule in the same step that prints the public key. What we lose: the current split where a human edits `.sops.yaml` by hand.

**Confidence.** high.

### F4. A second env must share env-01's GitHub owner

**What breaks and for whom.** Someone who clones the repo and copies `env-00` to their own name, and points `flux_git_repo_url` at their own GitHub account, while `env-01` is still in the tree. The schema rejects the tree. The docs say to copy `env-00` and never say to delete `env-01`. Replacing the owner env is possible, and the docs do not say it is required.

**Location.** `schema/env.py:535`, `envs/env-01/values/cluster-vars.yaml:34`, `.sops.yaml:2`, `docs/deploy.md:33`.

**Question.** 5, 8, 9.

**Evidence.**

```535:539:schema/env.py
def _cross_env_owner_errors(owners_by_env: dict[str, str]) -> list[str]:
    if len(set(owners_by_env.values())) <= 1:
        return []
    detail = ", ".join(f"{name}={owners_by_env[name]}" for name in sorted(owners_by_env))
    return [f"repo owner handle must be the same across non-example envs: {detail}"]
```

```34:35:envs/env-01/values/cluster-vars.yaml
flux_git_repo_url: https://github.com/gordoncooper/jarvis
apps_git_repo_url: https://github.com/gordoncooper/jarvis-apps
```

Command: copy the real `envs/` tree, rename `env-00` to `lab` (paths and `jarvis_env` rewritten, example owner left in place), add a `lab` creation rule beside the existing `env-01` rule.

```text
E_clone_plus_lab_example_owner: SCHEMA_FAIL
  repo owner handle must be the same across non-example envs: env-01=gordoncooper, lab=example-owner
```

A tree with `env-01` removed and two new envs under different owners fails the same way (`D_two_owners`). Deleting `envs/env-01` and its `.sops.yaml` rule is enough for a single new owner. `.mailmap` is independent. Nothing in `platform/` or `infra/` hard-codes the handle (core-names is what keeps it that way).

**Severity.** high.

**Size guess.** S.

**Suggested fix.** Say, at the top of the new-env steps, that a published non-example env has to go before a different owner is added, or stop shipping that env in the adopter branch. What we lose if the rule is dropped: one owner handle for the identity scan. The rule can stay if the docs make the deletion explicit.

**Confidence.** high.

### F5. Flux sync URL is a second copy, unchecked by the schema

**What breaks and for whom.** An adopter who updates `values/cluster-vars.yaml` and the `envs/env-00/` path strings, which is what step 1 names, and leaves the sync URL in `flux-instance.yaml`. `site.yml` applies that file once. Flux then syncs `example-owner` (or the previous URL) forever. env-schema exits 0.

**Location.** `envs/env-00/flux/flux-instance.yaml:24`, `infra/ansible/tasks/preflight.yml:47`, `ci/tests/test_flux_entry.py:183`, `docs/deploy.md:35`.

**Question.** 4, 9, 10.

**Evidence.** The instance carries its own URL, ref, and path:

```24:27:envs/env-00/flux/flux-instance.yaml
    url: "https://github.com/example-owner/example-cluster"
    ref: "refs/heads/example"
    path: "envs/env-00/flux"
```

Preflight only checks that the file exists:

```47:52:infra/ansible/tasks/preflight.yml
- name: Require the env FluxInstance in Git
  ansible.builtin.assert:
    that: flux_instance_file is file
    fail_msg: >-
      envs/{{ jarvis_env }}/flux/flux-instance.yaml is missing. flux-operator
      applies it on a new cluster; commit it before running site.yml.
```

The comparison lives in pytest:

```183:191:ci/tests/test_flux_entry.py
    sync = doc.get("spec", {}).get("sync") or {}
    expected = {
        "url": cluster["flux_git_repo_url"],
        "ref": cluster["flux_git_ref"],
        "path": f"envs/{env}/flux",
    }
    for key, want in expected.items():
        if sync.get(key) != want:
            errors.append(f"spec.sync.{key} is {sync.get(key)!r}, expected {want!r}")
```

Command, `env-01` removed, `lab` created, values URL and ref changed, instance URL left as shipped, creation rule added:

```text
B_values_urls_changed_instance_not: SCHEMA_PASS
  flux-instance url: https://github.com/example-owner/example-cluster
  flux-instance ref: refs/heads/example
  flux-instance path: envs/lab/flux
  cluster-vars url: https://github.com/lab-owner/lab-cluster
  cluster-vars ref: refs/heads/main
```

Deploy step 1 says to replace values and every `envs/env-00/` path. The parenthetical that the instance URL "is" `flux_git_repo_url` is later in the same section (`docs/deploy.md:42`), not a check.

**Severity.** high.

**Size guess.** S.

**Suggested fix.** Make `env-schema` compare `spec.sync` to the cluster-vars keys, or generate the instance from those keys. What we lose: a hand-edited FluxInstance as the only pin file. The version pin can stay in that file.

**Confidence.** high.

### F6. Age-key mismatch offers to delete the env's secrets

**What breaks and for whom.** An operator who points `SOPS_AGE_KEY_FILE` at the wrong key, or runs prep-node on a machine whose default key is a different env's key. Confirming the prompt deletes `envs/<env>/secrets/*.sops.yaml`. If the cluster copy of `sops-age` is also gone, the old values are gone. The warning text says the key may belong to another env, then still offers the delete.

**Location.** `tools/prep-node:897`, `infra/ansible/README.md:76`.

**Question.** 15, 17.

**Evidence.**

```897:907:tools/prep-node
    if key_existed:
        print(
            f"Warning: {path} public key does not match the .sops.yaml recipient"
            f" for envs/{env}/secrets/ ({current})."
        )
        print("This key may belong to another env. Set SOPS_AGE_KEY_FILE to a per-env key file.")
    print(f"Age recipient in .sops.yaml: {current}")
    if not confirm("Set the recipient to this public key and delete this env's encrypted secrets?"):
        return
    sops_path.write_text(set_sops_recipient(sops_text, env, public), encoding="utf-8")
    delete_env_secrets(repo, env)
```

The recovery write-up tells the operator to accept that prompt (`infra/ansible/README.md:76`). That is the right path when the key is actually lost. It is the wrong path when the file is simply the other env's key. `flux-operator` correctly refuses to overwrite a different `sops-age` secret (`infra/ansible/roles/flux-operator/tasks/sops_age.yml`).

**Severity.** high.

**Size guess.** S.

**Suggested fix.** Delete secrets only when the operator types the env name, and only after the cluster secret has been copied aside. What we lose: a single yes/no that both rotates the recipient and clears ciphertext.

**Confidence.** high.

### F7. Secret reads: mixed-case kind is not denied

**What breaks and for whom.** The agents building this repo. A kubectl read of a Secret whose kind spelling is neither all lower nor all upper is allowed by the checked-in matcher. The cluster's RBAC is unchanged. This is the command-guard bug, reported by category only.

**Location.** `.cursor/cli.json:26`, `ci/checks/lib/_cli_permissions.py:12`.

**Question.** 17.

**Evidence.** The deny entries cover two case spellings:

```26:29:.cursor/cli.json
      "Shell(*kubectl*get*ecret*)",
      "Shell(*kubectl*get*ECRET*)",
      "Shell(*kubectl*describe*ecret*)",
      "Shell(*kubectl*describe*ECRET*)",
```

The matcher is case-sensitive:

```12:12:ci/checks/lib/_cli_permissions.py
- only * is a wildcard (? is literal), matching is case-sensitive and
```

The in-repo `command_allowed` helper, loaded with `.cursor/cli.json`, returns deny for an all-lower Secret kind and for an all-upper Secret kind, and allow for a mixed-case spelling of that kind, on both `get` and `describe`. The corpus in `corpus_read_cases` / the must-block list does not include that spelling. No bypass string is recorded here.

**Severity.** high.

**Size guess.** S.

**Suggested fix.** Match the resource kind case-insensitively for the Secret read category. What we lose: nothing operational. Reads of ordinary objects stay allowed.

**Confidence.** high for the matcher in this repo. The module says it mirrors cursor-agent `2026.10.22` and does not prove the client (`ci/checks/lib/_cli_permissions.py:3`).

### F8. The secrets README is not in the template

**What breaks and for whom.** An adopter at bootstrap. Deploy sends them to "that env's `secrets/README.md`". `envs/env-00` has no `secrets/` directory. The only such file is `envs/env-01/secrets/README.md`, and it is written for one age-key path and one `SOPS_AGE_KEY_FILE` prefix. The doc index lists that file and does not list `env-00`.

**Location.** `docs/deploy.md:77`, `docs/README.md:33`, `envs/env-01/secrets/README.md:15`.

**Question.** 9, 12, 15.

**Evidence.**

```77:77:docs/deploy.md
Commit all of `envs/<env>/`: `flux/`, values, inventory, prep-node's edits, secrets, and `ca.crt`. Commit `.sops.yaml` too. Do not commit the age private key. Push to the branch `flux_git_ref` names. Flux syncs from that remote, so push before `site.yml`. Produces those files on the remote. What each secret file is, and how a second `make bootstrap-secrets` behaves, is in that env's `secrets/README.md`.
```

```33:34:docs/README.md
| [../envs/env-01/README.md](../envs/env-01/README.md) | env-01 |
| [../envs/env-01/secrets/README.md](../envs/env-01/secrets/README.md) | env-01 bootstrap secrets |
```

`find envs -name README.md` returns `envs/env-00/README.md`, `envs/env-01/README.md`, and `envs/env-01/secrets/README.md` only. The age-key path an operator must export is stated here:

```15:15:envs/env-01/secrets/README.md
Ensure `~/.local/bin` is on `PATH` on that machine. The age **public** key must already be in the repo root `.sops.yaml`. Encryption does not need the private key. Keep the private key offline except on the controller during bootstrap. The env-01 age key is `~/.config/sops/age/env-01-bench.txt`. `tools/bootstrap-secrets` does not read `age_key_file`. `SOPS_AGE_KEY_FILE` must be set to that path on the same command line as `make bootstrap-secrets ENV=env-01`. `tools/prep-node` can create a missing key; then run that command, commit, and run `site.yml`. If that file is lost, see Recovery in `infra/ansible/README.md` before deleting anything.
```

A new env that leaves the default `~/.config/sops/age/keys.txt` never sees that per-env path. Offline age-key backup is a sentence in `docs/deploy.md:9` and a paragraph in `infra/ansible/README.md:76`, not a step in the new-env list.

**Severity.** high.

**Size guess.** S.

**Suggested fix.** Put a secrets README in `env-00` that describes the file set, the second-run behavior, and the offline key backup, and point the doc index at `env-00`. What we lose: a single owner-specific key path as the worked example. That example can stay under `env-01`.

**Confidence.** high.

### F9. Stale cluster-vars ConfigMap passes the schema

**What breaks and for whom.** An adopter who edits `values/cluster-vars.yaml` after the generated file exists and does not re-render. Ansible reads the values file. Flux postBuild reads the committed ConfigMap. DNS, backup schedule, and `jarvis_env` can diverge. `make verify` then talks to a cluster whose marker does not match the file the operator thinks is current, or it matches the old marker and checks the wrong zone.

**Location.** `schema/env.py` (no reference to `generated/`), `tools/lib/render_cluster_vars_configmap.py:26`, `docs/deploy.md:39`.

**Question.** 4, 10.

**Evidence.** `rg cluster-vars-configmap schema/env.py` returns no hits. The renderer is the source of the committed file. On this SHA both committed ConfigMaps match it (`env-00` and `env-01`, compared with `render_document`).

Command: change `dns_suffix` in the values file of a renamed copy and do not re-render.

```text
C_dns_changed_configmap_not_rerendered: SCHEMA_PASS
  generated contains lab.example: False
  renderer contains lab.example: True
  generated equals renderer: False
```

Deploy step 5 tells the operator to render, and prep-node rewrites the file when it already exists (`tools/prep-node:849`). A later hand edit is not checked until pytest (`ci/tests/test_flux_entry.py`, `test_cluster_vars_configmap_matches_render_script`). `make verify` is not that test.

**Severity.** medium.

**Size guess.** S.

**Suggested fix.** Have `env-schema` fail when the generated file exists and differs from `render_document`. What we lose: the ability to commit a hand-edited ConfigMap. The header already says not to edit it.

**Confidence.** high.

### F10. cluster-reset without a snapshot path

**What breaks and for whom.** An operator whose node address changed, or who is trying to restore etcd and finds the short command first. `k3s server --cluster-reset` with no restore path rebuilds membership and drops the datastore. The snapshot restore is a different command, in a different document.

**Location.** `infra/ansible/README.md:72`, `platform/backups/CONTRACT.md:177`.

**Question.** 13, 16.

**Evidence.**

```72:72:infra/ansible/README.md
The node address changed and k3s fails with an etcd member URL mismatch. Stop k3s (`systemctl stop k3s`), run `/usr/local/bin/k3s server --cluster-reset` once, then start it (`systemctl start k3s`). A later `site.yml` run does not clear this.
```

```177:177:platform/backups/CONTRACT.md
2. Stop k3s (`systemctl stop k3s`). Run `/usr/local/bin/k3s server --cluster-reset --cluster-reset-restore-path=<backup_path>/etcd-snapshots/<file>` once. Start k3s (`systemctl start k3s`).
```

**Severity.** medium.

**Size guess.** S.

**Suggested fix.** Name the address-change command as a datastore wipe in the heading, and point at the snapshot command for restore. What we lose: one short recovery paragraph.

**Confidence.** high.

### F11. Enrollment webhook fails open

**What breaks and for whom.** A future service whose PVC is created while Kyverno admission is down or still starting. The PVC binds and is never given a ReplicationSource. `make verify` only notices later, and only if it is run after a schedule interval with no source (F12). The contract states the choice.

**Location.** `platform/backups/enrollment.yaml:11`.

**Question.** 2, 6, 17.

**Evidence.**

```11:12:platform/backups/enrollment.yaml
  webhookConfiguration:
    failurePolicy: Ignore
```

`platform/backups/CONTRACT.md:52` says this is only `spec.webhookConfiguration.failurePolicy`. A native Kyverno generate is the right tool; `Ignore` is the patch that keeps the API up when the webhook is down, and it also skips the backup.

**Severity.** medium.

**Size guess.** S.

**Suggested fix.** Fail closed once the platform-controllers health gate has passed, and keep Ignore only during bootstrap if that is still required. What we lose: creates succeeding during a Kyverno outage.

**Confidence.** high.

### F12. Verify checks timestamps, not restorable bytes

**What breaks and for whom.** An operator who treats `make verify` as proof the backup can be restored. A ReplicationSource with a recent `lastSyncTime` passes even when the restic repository would not restore. The drill is the integrity check, and it is a separate command that does not reset etcd (F1).

**Location.** `tools/verify/run:348`, `tools/verify/run:373`.

**Question.** 13, 16, 18.

**Evidence.**

```348:351:tools/verify/run
        if not isinstance(schedule, str) or not isinstance(status.get("lastSyncTime"), str):
            raise CheckError(f"{namespace}/{name}: no completed scheduled sync")
        if not _recent(status["lastSyncTime"], schedule, zone_name, now):
            raise CheckError(f"{namespace}/{name}: last successful sync is stale")
```

```373:376:tools/verify/run
    if not _recent(
        status["lastSuccessfulTime"], spec["schedule"], cron_zone, now
    ):
        raise CheckError("etcd snapshot copy is stale")
```

There is no `restic check` and no restore in this function. The drill's etcd half compares sha256 of the newest file to a restic restore of that file (`tools/drill`, described at `platform/backups/CONTRACT.md:163`). That is a real check of the copy, on a live cluster, and it is not a datastore restore.

**Severity.** medium.

**Size guess.** M.

**Suggested fix.** Keep the timestamp check, and add a repository `restic check` on the backup node, or say in the verify output that freshness is not a restore. What we lose: a verify that stays a handful of API reads.

**Confidence.** high.

### F13. No adopter upgrade procedure

**What breaks and for whom.** Someone who pulls a newer Jarvis into their fork, or bumps k3s. Renovate will not open the k3s bump. The coupling to the Traefik contract is a renovate comment. The only "re-run site.yml after infra merges" note is in the owner env README.

**Location.** `renovate.json:7`, `docs/deploy.md:112`, `envs/env-01/README.md:15`, `platform/traefik/CONTRACT.md:34`, `schema/CONTRACT.md:51`.

**Question.** 12, 14.

**Evidence.**

```7:10:renovate.json
      "description": "k3s host binary is pinned in cluster-vars.yaml and changes only by PR. A bump has to re-check platform/traefik and update the bundled chart and k3s versions in platform/traefik/CONTRACT.md.",
      "matchDatasources": ["github-releases"],
      "matchPackageNames": ["k3s-io/k3s"],
      "enabled": false
```

```112:114:docs/deploy.md
## Dependency updates

Renovate opens update PRs right away and never merges them; the owner chooses when to merge, and may wait a few days after a fresh release first.
```

```15:16:envs/env-01/README.md
- Infra Ansible (`infra/`): the admin re-runs `site.yml` on the controller node after merge (`infra/ansible/README.md`).
```

```34:34:platform/traefik/CONTRACT.md
- Checked: bundled Traefik chart v40 on k3s v1.36.5+k3s1.
```

Flux chart and distribution pins are renovate groups (`renovate.json` groups `flux-operator-chart` and `flux-distribution`). The host binary is not. `k3s_sha256` is fetched by hand (`docs/deploy.md:13`). Nothing describes merging upstream into a fork that already has its own `envs/<name>/`.

**Severity.** medium.

**Size guess.** M.

**Suggested fix.** One upgrade section in `docs/deploy.md`: which pins move together, when to re-render the ConfigMap, when to re-run `site.yml`, and how a fork pulls upstream without taking `env-01`. What we lose: keeping that procedure implicit in renovate comments and one env README.

**Confidence.** high.

### F14. Single-node and mixed GPUs are not a documented setup

**What breaks and for whom.** An adopter with one machine, or with one 16 GB GPU and two 8 GB GPUs. The schema and the tests accept a single host and two `gpu` workers. Deploy's template is seven hosts. Nothing records GPU memory, and nothing installs a device plugin, so a pod cannot ask for a GPU.

**Location.** `envs/env-00/inventory/hosts.yml:3`, `ci/tests/fixtures/env-shapes/single-node/hosts.yml:4`, `ci/tests/fixtures/env-shapes/gpu-mix/hosts.yml:5`, `infra/ansible/tasks/assert-gpu.yml:23`, `README.md:11`.

**Question.** 11, 22, 23.

**Evidence.** The template declares every role across seven hosts (`envs/env-00/inventory/hosts.yml:3`). The single-node fixture is one host:

```3:5:ci/tests/fixtures/env-shapes/single-node/hosts.yml
    node-a:
      # One host. backup is required: the schema allows exactly one backup host.
      node_roles: [control_plane, workers, edge, backup]
```

The gpu-mix fixture is two workers with the `gpu` role and no size (`ci/tests/fixtures/env-shapes/gpu-mix/hosts.yml`). Those shapes pass `test_env_shape_copy_bootstraps_builds_and_lists_hosts` (both suite runs below). The runtime check is NVIDIA's PCI vendor and nothing else:

```23:30:infra/ansible/tasks/assert-gpu.yml
- name: A declared gpu host has an NVIDIA device
  ansible.builtin.assert:
    that:
      - >-
        node_pci_vendor_ids.results | default([])
        | map(attribute='content') | map('b64decode') | map('trim') | map('lower')
        | select('equalto', '0x10de') | list | length > 0
    fail_msg: "gpu host {{ inventory_hostname }} has no NVIDIA device."
```

`rg` for `nvidia.com/gpu`, `device-plugin`, and `RuntimeClass` across the repo returns no matches. Host keys the schema allows are `node_roles` and `data_disk` (`schema/CONTRACT.md:87`). Ansible README says a single-node env has no agent hosts (`infra/ansible/README.md:3`). Deploy does not show that inventory.

**Severity.** medium.

**Size guess.** M for a device plugin later; S to document the one-host inventory.

**Suggested fix.** Put the single-node and two-gpu-worker inventories in the deploy doc, and treat a GPU runtime as a later platform component that schedules on `role.<dns_suffix>/gpu`. What we lose if memory stays out of the schema: the foundation cannot place a model on the 16 GB card by itself. That can wait until a workload exists. The missing sentence in the docs should not wait.

**Confidence.** high.

### F15. No place for a new service to land its secrets

**What breaks and for whom.** The next app, the assistant, and the code factory. Platform ingress, the wildcard cert, DNS, and PVC enrollment are real and documented. A Secret for a new namespace is not: `services` and `apps` substitute cluster-vars and do not decrypt. `apps_git_repo_url` is validated and never read by a manifest. `make verify` curls only `testapp`.

**Location.** `envs/env-00/flux/kustomizations/services.yaml:9`, `envs/env-00/flux/kustomizations/apps.yaml:9`, `envs/env-00/flux/kustomizations/platform-configs.yaml:16`, `schema/CONTRACT.md:58`, `tools/verify/run:390`, `platform/traefik/CONTRACT.md:13`.

**Question.** 21, 23.

**Evidence.** Decryption appears only on the two platform-configs Flux files (`rg decryption: -g '*.yaml'`). The services path:

```9:19:envs/env-00/flux/kustomizations/services.yaml
  path: ./services
  prune: true
  sourceRef:
    kind: GitRepository
    name: flux-system
  dependsOn:
    - name: platform-configs
  postBuild:
    substituteFrom:
      - kind: ConfigMap
        name: cluster-vars
```

`apps_git_repo_url` is referenced from schema, tests, and env values, not from any Flux manifest (`rg` under `*.yaml` outside `envs/*/values`). Verify hard-codes the canary:

```390:390:tools/verify/run
    host = f"testapp.{dns_suffix}"
```

Exposure itself is one Ingress (`platform/traefik/CONTRACT.md:3`). Gatus is explicitly later (`platform/traefik/CONTRACT.md:13`). testapp shows the Ingress annotations (`platform/testapp/testapp.yaml`). There is no one page that says: put the workload in `services/` or `apps/`, add the Ingress, let `pvc-backup` enroll the claim, and add `decryption` before the Secret is applied.

**Severity.** medium.

**Size guess.** M.

**Suggested fix.** Document that recipe, and add `decryption` to the services and apps Kustomizations when the first Secret lands there. Wire `apps_git_repo_url` only when the apps repo is actually a Flux source. What we lose: a Flux path that cannot see SOPS, which is safe today only because those paths hold placeholders.

**Confidence.** high.

### F16. Backup ClusterRole can delete every Secret

**What breaks and for whom.** The whole cluster, if the Kyverno controller is compromised or a generate rule is wrong. The role is cluster-scoped and includes `delete` on all Secrets, not on the restic secrets it creates.

**Location.** `platform/kyverno/rbac.yaml:12`.

**Question.** 17.

**Evidence.**

```12:15:platform/kyverno/rbac.yaml
rules:
  - apiGroups: [""]
    resources: ["secrets"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
```

ConfigMap access is already narrowed with `resourceNames: ["backup-rebuild-hold"]` on the next rule. Secret access is not. rest-server runs as root on a hostPath (`platform/backups/rest-server.yaml:54`); the manifest says hostPath ignores `fsGroup`. That root user is local to the backup node. The ClusterRole is the broader grant.

**Severity.** medium.

**Size guess.** M.

**Suggested fix.** Drop `delete` if cleanup does not need it, or scope the rule to the generated secret names. What we lose: Kyverno deleting generated restic secrets when the policy goes away.

**Confidence.** medium. The verbs are intentional for generate; whether `delete` is required was not proven against a live Kyverno.

### F17. Schema is tighter than the product that exists

**What breaks and for whom.** An adopter adding a second env (also F4), or a future cache volume that should not be backed up. `backup: none` is legal only under VictoriaMetrics and VictoriaLogs paths that are not in the tree. A hermes values file and an apps values file are required before either system exists. `code_delivery` and `build_agent` reject the named future providers. Omitting those two keys is valid, so a Gitea HTTPS remote already works as `flux_git_repo_url` if the key is left unset.

**Location.** `ci/checks/lib/_pvc_backup.py:17`, `schema/CONTRACT.md:44`, `schema/CONTRACT.md:70`.

**Question.** 5.

**Evidence.**

```17:21:ci/checks/lib/_pvc_backup.py
BACKUP_NONE_ALLOWED_PREFIXES: tuple[str, ...] = (
    "services/observability/victoria-metrics/",
    "services/observability/victoria-logs/",
)
```

`rg services/observability` finds that tuple and the contract-root list, not a directory. `build_agent` allows `cursor` only; `grok-build` and `opencode-local` are rejected (`schema/CONTRACT.md:44`). `docs/code-delivery/CONTRACT.md:5` names Forgejo as planned and unimplemented. No manifest reads `code_delivery` (`docs/code-delivery/CONTRACT.md:54`).

amd64-only and exactly one control plane (`README.md:11`, `infra/ansible/tasks/preflight.yml:3`) are stated limits and should stay, written as Limits (F18). Exactly one `backup` host is the restic SPOF in F1; loosening it is an L, not a schema nit.

**Severity.** medium.

**Size guess.** S for the opt-out paths; L to allow two backup hosts.

**Suggested fix.** Allow `backup: none` on an explicit annotation list the contract names, until those observability paths exist. Keep rejecting unknown providers. What we lose: a guarantee that nobody opts a PVC out early.

**Confidence.** high.

### F18. Operations and Limits are missing on most contracts

**What breaks and for whom.** An operator who starts from the contract index when something breaks. Backup and the CA rotate are written. Age recovery, restic rotation, Flux rollback, edge DNS order, and the upgrade re-run are somewhere else.

**Location.** `docs/CONTRACT.template.md:23`. Each contract below.

**Question.** 16.

**Evidence.** The template:

```23:29:docs/CONTRACT.template.md
## Operations

Optional. Steps to run, rotate, or restore this component. Omit this heading when there are no steps.

## Limits

Optional. What this component deliberately does not do. Omit this heading when there is nothing to say.
```

| Contract | Has Operations | Has Limits | Add |
| --- | --- | --- | --- |
| `platform/backups/CONTRACT.md` | yes | yes | Restic rotation (F2) and the token/rebuild gap (F1) belong in the existing sections |
| `platform/cert-manager/CONTRACT.md` | yes (CA rotate) | no | Limits: wildcard only, no public ACME, leaf does not reissue until `wildcard-tls` is deleted |
| `platform/flux-operator/CONTRACT.md` | no | no | Operations: sops-age bootstrap, do not delete a differing `sops-age`, Flux rollback from `infra/ansible/README.md:74`. Limits: Ansible applies the instance once; Git reconciles after |
| `platform/kyverno/CONTRACT.md` | no | no | Operations: chart bump via Renovate. Limits: backup policy is the only policy; webhook `failurePolicy: Ignore` (F11) |
| `platform/configs/CONTRACT.md` | no | no | Operations: moving an object between Flux paths takes two commits (already one sentence at line 20). Limits: this list is core configs, not env secrets |
| `platform/app-dns/CONTRACT.md` | no | no | Operations: router forwards `dns_suffix` to an edge node; run `--tags resolved` before the chart (`infra/ansible/README.md:60`). Limits: Ingress hosts only; not a recursive resolver |
| `platform/traefik/CONTRACT.md` | no | no | Operations: k3s and chart v40 move together (F13). Limits: no Gateway API; ports 80 and 443 on edge nodes only |
| `platform/testapp/CONTRACT.md` | no | no | Operations: the `make verify` curl. Limits: no PVC, not a health check for other apps |
| `schema/CONTRACT.md` | no | no | Omit Operations. Limits: validation only; it does not render Flux or talk to a host |
| `docs/code-delivery/CONTRACT.md` | no | no | Operations: fork pull and required check `ci` (F13). Limits: planned rows are not selectable in the schema |

Buried outside those files: age key path and restic rotation in `envs/env-01/secrets/README.md`; promotion and `site.yml` re-run in `envs/env-01/README.md:15`; age loss and the bare `cluster-reset` in `infra/ansible/README.md:72` and `:76`.

**Severity.** medium.

**Size guess.** M.

**Suggested fix.** Add the rows above. Do not copy the owner env's hostnames into core contracts. What we lose: contracts that stay short because the steps live in READMEs an adopter may never open.

**Confidence.** high.

### F19. Bootstrap writes secrets nothing applies

**What breaks and for whom.** Every new env. `make bootstrap-secrets` creates seven files. Flux applies two. The other five are Grafana and Hermes credentials for namespaces that do not exist. They are real secrets in Git, with no consumer.

**Location.** `tools/bootstrap-secrets:297`, `envs/env-00/flux/platform-configs/kustomization.yaml:3`.

**Question.** 7, 21, 24.

**Evidence.**

```3:6:envs/env-00/flux/platform-configs/kustomization.yaml
resources:
  - ../../../../platform/configs
  - ../../secrets/backups-restic.sops.yaml
  - ../../secrets/cert-manager-ca.sops.yaml
```

The spec list also builds `grafana-admin`, `hermes-ops-api`, `hermes-ops-dashboard`, `hermes-code-factory-api`, and `hermes-code-factory-dashboard` (`tools/bootstrap-secrets:297`). `env-01` has those files on disk. Metadata check: each is a Secret, `sops.age` present, MAC present, every value `ENC[`. Namespaces in the metadata are `observability`, `hermes-ops`, and `hermes-code-factory`. No Flux path lists them (`rg` of the filenames under `flux/`).

**Severity.** medium.

**Size guess.** S.

**Suggested fix.** Keep the generators behind a flag or a comment in the template secrets README until the namespace is on a decrypting path (F15). What we lose if they are deleted outright: credentials already waiting for Hermes and Grafana (see the deferred list). Do not delete the code as cruft without that decision.

**Confidence.** high.

### F20. Plan codes and the owner env in the front door

**What breaks and for whom.** A reader judging whether this is a clean product. Core markdown is clean (plan-codes check passes). The two placeholders an empty Flux path still ships carry phase tokens. The doc index opens with agent docs and `env-01`.

**Location.** `apps/placeholder.yaml:1`, `services/placeholder.yaml:1`, `ci/checks/lib/_plan_codes.py:3`, `docs/README.md:10`.

**Question.** 8, 12.

**Evidence.**

```1:1:apps/placeholder.yaml
# D9: placeholder until app hosting (jarvis-apps) is wired in P4a.
```

```1:1:services/placeholder.yaml
# D9: placeholder until observability and Hermes land in later phases.
```

```3:7:ci/checks/lib/_plan_codes.py
Scanned paths: every Markdown file, every README* (any case, any extension),
and everything under docs/ (including docs/CONTRACT.template.md). That picks
up CONTRACT.md, RUNBOOK.md, readme.md, and README.txt, including under envs/.

Not scanned: rulebooks/, AGENTS.md, .github/, and code. No path exemptions.
```

```10:11:docs/README.md
| [../AGENTS.md](../AGENTS.md) | Where coding agents start |
| [../rulebooks/coding-agents.md](../rulebooks/coding-agents.md) | Agent workflow |
```

`AGENTS.md`, `rulebooks/`, and `.cursor/` are allowed to exist for today's build. The deploy index does not have to list them. Tailscale does not appear anywhere (`rg -i tailscale` is empty). Router forwarding of `dns_suffix` is one line at `docs/deploy.md:21`. Both are adopter prerequisites and stay outside the cluster.

`envs/env-01/README.md:9` says observability, Hermes, and hosted apps deploy there. Those paths are the placeholders above. That file is the owner's env; the index is what makes it the front door (F8).

**Severity.** low.

**Size guess.** S.

**Suggested fix.** Rewrite the two comments without phase tokens, and point the doc index at `env-00` and `docs/deploy.md`. What we lose: a reminder of which phase the placeholder was waiting on. The deferred list in this file is a better place for that.

**Confidence.** high.

### F21. apt install without an update

**What breaks and for whom.** A machine whose apt lists are older than the package index, including this audit VM on the first `make deps`. `python3-venv` and `python3-bcrypt` were "not available" until `apt-get update`. After the update, the same install succeeded and the pinned CLIs installed.

**Location.** `tools/install-deps:97`.

**Question.** 9, 10.

**Evidence.**

```97:102:tools/install-deps
def _try_apt_install(packages: list[str]) -> None:
    subprocess.run(
        ["sudo", "apt-get", "install", "-y", *packages],
        check=True,
    )
```

There is no `apt-get update` in that file. First run on this VM: `E: Package 'python3-venv' has no installation candidate` and `Unable to locate package python3-bcrypt`. The script then exits with the same `apt-get install` line, which fails the same way until lists are updated. This may be specific to images with empty lists. It is still the first command in the deploy doc.

**Severity.** low.

**Size guess.** S.

**Suggested fix.** Run `apt-get update` before the install, or print that command in the failure text. What we lose: a deps run that does not hit the network for package lists when the cache is already fresh.

**Confidence.** medium. Reproduced here; not claimed for every Ubuntu image.

## Answers by question

1. **Lean.** The platform slice is about as small as the job: k3s, Flux, Kyverno generate, cert-manager, one DNS chart, one rest server. The extra weight is three copies of the same env facts (F5, F9) and secrets for systems that are not installed (F19). Restore, drill, and prep-node are custom because nothing native does those operator steps.

2. **Glue.** Kyverno generating the ReplicationSource should stay; it replaces hand-written backup objects. Ansible installing k3s and the one-shot Flux operator should stay. The glue to replace is the unchecked copy of the Flux sync URL and the unchecked ConfigMap (F5, F9). `failurePolicy: Ignore` is a patch on top of that generate rule (F11).

3. **Too complex.** The hermes and apps value files, and five unused secrets, are early (F17, F19). Trim the unused secret generation until a namespace exists. Keep digest pins, SOPS, enrollment, and the drill. The DR story is the opposite problem: too small for the failure it claims to bound (F1).

4. **Brittle.** One control plane, one backup host, k3s data on the root disk, `data_disk` gluing backups to the volume disk (F1), hand-copied Flux URL (F5), hand-rendered ConfigMap (F9), k3s pin tied to a sentence in the Traefik contract (F13), GPU as a label only (F14). `app_dns_cluster_ip` is strict and checked; that one is fine.

5. **Too strict.** One GitHub owner across every non-example env, with no doc telling you to delete `env-01` (F4). `backup: none` only under trees that do not exist (F17). Future provider names are rejected, which is right while nothing reads the key. amd64 and one control plane should stay and be labeled Limits (F18).

6. **Real gaps.** Disk loss (F1). A new-env path that exits 1 and then points at a missing README (F3, F8). Flux sync not validated before `site.yml` (F5). No upgrade section (F13). No decrypting path for the next service (F15). GPU runtime is a gap only once a GPU workload exists; the missing deploy example is a gap now (F14).

7. **Cruft.** Unapplied Grafana and Hermes secrets (F19). Phase tokens in the two placeholders (F20). The placeholders themselves are what keep the `apps` and `services` Flux paths non-empty; they stay until those paths have objects. `EXPLICIT_COMPONENT_ROOTS` names directories that are not on disk; that list is a deferred index, not a dead file.

8. **Clean product.** `platform/`, `infra/`, `tools/`, and `schema/` do not contain the owner handle or the owner's host. `.mailmap` and `envs/env-01/` are the deliberate exceptions, and F4 is why "replace them" is not obvious. The doc index and the placeholder comments are the parts that still read as this build rather than the product (F20). No decision log or changelog is in the tree.

9. **Docs alone, env-01 ignored.** Not yet. The walk opened README, `docs/deploy.md`, `docs/README.md`, `envs/env-00/README.md`, three values files, `inventory/hosts.yml`, `flux-instance.yaml`, the platform-configs kustomization, the generated ConfigMap, `.sops.yaml`, `infra/ansible/README.md`, `schema/CONTRACT.md`, the app-dns contract, and the backups contract (15 files), then `envs/env-01/secrets/README.md` because deploy names a file the copy does not have. Stuck at F3 (prep-node exits 1), F4 (if the clone's env-01 is still there), F5 (URL left behind), F8 (no secrets README), and F14 (seven-host template, one machine). `make verify` and `make drill` are specified clearly once a cluster exists. `known_hosts` is required and is only in the ansible README (`infra/ansible/README.md:5`).

10. **Errors before a host is touched.** `ci/checks/env-schema` is clear for unknown keys, roles, CIDRs, and a missing age rule, and it can be run with no `OWNER_NAME`. prep-node runs that check after SSH (F3). It does not catch a wrong Flux URL (F5) or a stale ConfigMap (F9). `site.yml` preflight checks a short list of defined variables and that the instance file exists, on the hosts. `make deps` can fail before any of that on stale apt lists (F21).

11. **Single-node and mixed GPUs.** Inventory yes, GPUs as schedulable devices no. `test_env_shapes` passed for `single-node`, `gpu-mix`, and `secondary-disk` in both suite runs. Mixed memory is not a field (F14). A single node puts `backup` on the same machine as the data, which is the F1 worst case.

12. **Unwritten.** Tailscale, or any remote-access method, is absent; router DNS is one line (`docs/deploy.md:21`). Both stay outside the cluster. Per-host `prep-node`, `known_hosts`, and the per-env age file are easy to miss (F8). How a fork pulls upstream is unwritten (F13). Hermes behavior is unwritten because Hermes is not in the tree; the schema still requires the values file.

13. **Real restore.** Same-node etcd reset from `backup_path/etcd-snapshots` is written and is not the drill. PVC `make restore` is written and the drill actually runs it on a scratch volume. A dead root disk takes the server token with it, and the restic etcd copy has no restore command (F1). `cluster-reset` with no path is a different, destructive command (F10). `make verify` would still have been green before the disk died (F12).

14. **Upgrades.** Renovate opens grouped PRs for charts and images and does not merge them. k3s is disabled there and coupled to the Traefik contract and a hand-copied sha256 (F13). Pulling a new Jarvis into a fork is not documented. `site.yml` again after infra changes is only in `envs/env-01/README.md`.

15. **Rotation and the age key.** CA rotation in `platform/cert-manager/CONTRACT.md` is specific and matches cert-manager's refusal to reissue. Age-key backup is "keep it offline" plus a recovery paragraph that deletes ciphertext (F6, F8). Restic rotation as written is unsafe (F2). An offline copy of the age key is not a numbered step with a restore check.

16. **When it breaks.** `make verify` prints a `PASS` or `FAIL` per check (Flux, the restic secret, node roles, backup freshness, testapp HTTPS) and exits 1. It does not say which node holds restic, and freshness is not integrity (F12). Contract coverage is in F18. The steps that are easy to miss live in `envs/env-01/secrets/README.md` and `infra/ansible/README.md`, not in the component contract.

17. **Trust.** No default passwords in the platform manifests. Bootstrap tokens are random. SOPS files are ciphertext (metadata check below). `ca.crt` is the public root, by design. LoadBalancers are Traefik and App DNS. rest-server is ClusterIP. The broad grant is the Kyverno Secret role (F16). The agent-guard bug is mixed-case Secret reads (F7). The age-key delete prompt is F6.

18. **Tests worth adding.** Schema assertions for F5 and F9. A fixture that a `backup: none` outside the Victoria prefixes fails, already exists in spirit; a test that those prefixes are still the intended future paths would lock F17. A live disk-loss test is not worth automating here.

19. **Tests to cut.** None of the long suites (`test_flux_operator`, `test_restore`, `test_env_schema`, `test_k3s`) are empty weight: they render roles and fail closed. `test_renovate` is repetitive and is what locks F13's pin groups. Leave it.

20. **Behavior, and env-00.** Most tests call the checker or render a role against a fixture. Some compare a rendered unit drop-in to an expected string (`ci/tests/test_k3s.py`), which is the rendered behavior of that template. `ci/run-checks` passed on the full tree and on a detached worktree with `envs/env-01` removed the way CI does. pytest: 482 passed, 1 failed, same test both ways (`test_chrony_template_notifies_restart_only_when_changed`). That failure is this machine (Concerns), not `env-01`.

21. **A place to land.** Partial. `platform/`, `services/`, `apps/`, and `envs/<env>/flux` are the four Flux layers, chained. Certs, DNS, and ingress are one Ingress plus the wildcard (testapp is the example). PVCs are enrolled by policy. Secrets decrypt only on platform-configs (F15). Health for anything except testapp is the deferred Gatus line.

22. **Constraints on what comes next.** Nothing in the foundation forbids an assistant or a code factory. `build_agent` and `code_delivery` are optional and unused by manifests, so a non-GitHub remote already works. The awkward parts are: GPU nodes are labels (F14), apps do not come from `apps_git_repo_url` (F15), one control plane and one backup host (F1), and `.cursor/cli.json` is the build-agent guard, not a runtime sandbox. The sandbox is still absent, which is the right deferral until the code factory exists.

23. **Needed by all of them.** A decrypting Flux path and a one-page recipe (F15). A backup that survives the disk the token is on (F1). A health check that is not hard-coded to testapp (the Gatus deferral). A GPU runtime only for the GPU case (F14). An identity separate from the GitOps admin for the code factory, not designed in this foundation and not blocked by it.

24. **Do not lose.** Gatus per endpoint (`platform/traefik/CONTRACT.md:13`). Forgejo, the issue queue, `admin-gate`, draft code-factory PRs, and `build.yml` (`docs/code-delivery/CONTRACT.md`). `grok-build` and `opencode-local` as named non-values (`schema/CONTRACT.md:44`). Hermes schema path `services/hermes/core/schema/CONTRACT.md` (`schema/CONTRACT.md:3`). Contract roots in `ci/checks/lib/_contract_md.py:8` for hermes, observability, and `docs/build-agent`. Model names staying out of core until the later core-names step (`rulebooks/coding-agents.md:17`). Backup limits, including no on-demand backup and no restic-only etcd restore (`platform/backups/CONTRACT.md:185`). Rebuild hold. Two-commit moves (`platform/configs/CONTRACT.md:20`). Later secrets via `make seal` (`envs/env-01/secrets/README.md:25`). The placeholders (F20). F19's generators are part of this list until someone decides the secrets move with the first consumer.

## What's good and should stay

- Git is the source of truth, Flux applies it, and `StrictPostBuildSubstitutions` fails a missing key at reconcile. The published ConfigMap allow-list is short on purpose.
- SOPS age, no plaintext in the seven secret files, gitleaks clean, public CA root separated from the key.
- Digest pins, the LoadBalancer allow-list, and core-names. Identity for the real env stays under `envs/env-01/`.
- PVC enrollment by policy, the rebuild hold, and a drill that restores a scratch volume and checks checksums.
- env schema errors name the file and the key. `env-00` carries every key and every role so the template cannot drift quietly.
- testapp is a complete, small example of Ingress, probes, non-root, and the wildcard, with no PVC.
- CA rotation is written as steps that match the chart's behavior.
- The `env00-only` CI job already deletes every env but `env-00` before the suite. That is the right adopter check.
- Renovate does not automerge, and image bumps that must move together are grouped.

## Delete list

Nothing on disk is safe to delete outright.

- `apps/placeholder.yaml` and `services/placeholder.yaml` are the only resources in those kustomizations. Deleting them breaks the Flux paths. The phase tokens in the comments can go (F20).
- The five Grafana and Hermes `SecretSpec`s are unused by Flux and are the deferred credentials (F19, question 24). Deleting them loses that head start. Stop calling them for a new env, or keep them and document them. Do not drop the files from `env-01` as an audit cleanup.
- `EXPLICIT_COMPONENT_ROOTS` entries for missing directories are not files.
- `.claude/settings.json` is a second deny list. CI reads `.cursor/cli.json` only. Deleting the Claude file is a workflow choice, not a product fix. It was not fully reviewed; F7 is the one command-guard finding.

## Coverage

Tags: `finding` (cited above), `no finding` (read, nothing to change), `excluded: …` (vendored, generated, or ciphertext).

| File | Tag |
| --- | --- |
| .claude/settings.json | no finding |
| .cursor/cli.json | finding |
| .github/pull_request_template.md | no finding |
| .github/workflows/ci.yml | no finding |
| .gitignore | no finding |
| .mailmap | no finding |
| .sops.yaml | finding |
| AGENTS.md | no finding |
| CLAUDE.md | no finding |
| Makefile | no finding |
| README.md | finding |
| apps/kustomization.yaml | no finding |
| apps/placeholder.yaml | finding |
| ci/checks/cli-deny | no finding |
| ci/checks/contract-md | no finding |
| ci/checks/core-names | no finding |
| ci/checks/digest-pin | no finding |
| ci/checks/env-schema | no finding |
| ci/checks/fixtures/must-allow-main.txt | no finding |
| ci/checks/fixtures/must-block-main.tsv | no finding |
| ci/checks/flux-substitute | no finding |
| ci/checks/lib/_cli_permissions.py | finding |
| ci/checks/lib/_contract_md.py | no finding |
| ci/checks/lib/_core_names.py | no finding |
| ci/checks/lib/_digest_pin.py | no finding |
| ci/checks/lib/_flux_substitute.py | no finding |
| ci/checks/lib/_loadbalancer.py | no finding |
| ci/checks/lib/_manifest_lint.py | no finding |
| ci/checks/lib/_manifests.py | no finding |
| ci/checks/lib/_plan_codes.py | finding |
| ci/checks/lib/_product_ci.py | no finding |
| ci/checks/lib/_pvc_backup.py | finding |
| ci/checks/lib/_python_pins.py | no finding |
| ci/checks/loadbalancer | no finding |
| ci/checks/manifest-lint | no finding |
| ci/checks/plan-codes | no finding |
| ci/checks/product-ci | no finding |
| ci/checks/pvc-backup | no finding |
| ci/checks/python-pins | no finding |
| ci/checks/secret-scan | no finding |
| ci/ci-local-verify | no finding |
| ci/rule-checks.txt | no finding |
| ci/run-checks | no finding |
| ci/tests/ansible_role_proof.py | no finding |
| ci/tests/conftest.py | no finding |
| ci/tests/fixtures/ansible-list-tasks/hosts.yml | no finding |
| ci/tests/fixtures/digest-pin/tag-without-digest.yaml | no finding |
| ci/tests/fixtures/env-shapes/gpu-mix/hosts.yml | finding |
| ci/tests/fixtures/env-shapes/secondary-disk/hosts.yml | no finding |
| ci/tests/fixtures/env-shapes/single-node/hosts.yml | finding |
| ci/tests/fixtures/gpu-assert/hosts.yml | no finding |
| ci/tests/fixtures/gpu-assert/play.yml | no finding |
| ci/tests/fixtures/loadbalancer/extra-service.yaml | no finding |
| ci/tests/fixtures/manifest-lint/missing-memory-limit.yaml | no finding |
| ci/tests/fixtures/schemas/apiextensions.k8s.io/customresourcedefinition_v1.json | excluded: kubeconform schema fixture, not a product manifest |
| ci/tests/fixtures/schemas/helm.cattle.io/HelmChartConfig_v1.json | excluded: kubeconform schema fixture, not a product manifest |
| ci/tests/fixtures/schemas/volsync.backube/replicationdestination_v1alpha1.json | excluded: local kubeconform override for VolSync 0.17 moverVolumes; not a shipped CRD |
| ci/tests/fixtures/schemas/volsync.backube/replicationsource_v1alpha1.json | excluded: local kubeconform override for VolSync 0.17 moverVolumes; not a shipped CRD |
| ci/tests/support.py | no finding |
| ci/tests/test_ansible_operator_tags.py | no finding |
| ci/tests/test_app_dns.py | no finding |
| ci/tests/test_backup_enrollment.py | no finding |
| ci/tests/test_base_os.py | no finding |
| ci/tests/test_bootstrap_secrets.py | no finding |
| ci/tests/test_cert_manager.py | no finding |
| ci/tests/test_cli_permissions.py | no finding |
| ci/tests/test_contract_md.py | no finding |
| ci/tests/test_core_names.py | no finding |
| ci/tests/test_digest_pin.py | no finding |
| ci/tests/test_drill.py | no finding |
| ci/tests/test_env_schema.py | no finding |
| ci/tests/test_env_shapes.py | no finding |
| ci/tests/test_flux_entry.py | finding |
| ci/tests/test_flux_operator.py | no finding |
| ci/tests/test_flux_operator_install.py | no finding |
| ci/tests/test_flux_substitute.py | no finding |
| ci/tests/test_install_deps.py | no finding |
| ci/tests/test_inventory_roles.py | no finding |
| ci/tests/test_k3s.py | no finding |
| ci/tests/test_kubeconfig.py | no finding |
| ci/tests/test_loadbalancer.py | no finding |
| ci/tests/test_manifest_lint.py | no finding |
| ci/tests/test_node_disk.py | no finding |
| ci/tests/test_pci_gpu.py | no finding |
| ci/tests/test_plan_codes.py | no finding |
| ci/tests/test_preflight.py | no finding |
| ci/tests/test_prep_node.py | no finding |
| ci/tests/test_product_ci.py | no finding |
| ci/tests/test_prune_guards.py | no finding |
| ci/tests/test_pvc_backup.py | no finding |
| ci/tests/test_python_pins.py | no finding |
| ci/tests/test_renovate.py | no finding |
| ci/tests/test_restic_rest_server.py | no finding |
| ci/tests/test_restore.py | no finding |
| ci/tests/test_seal.py | no finding |
| ci/tests/test_testapp.py | no finding |
| ci/tests/test_traefik.py | no finding |
| ci/tests/test_verify.py | no finding |
| docs/CONTRACT.template.md | finding |
| docs/README.md | finding |
| docs/code-delivery/CONTRACT.md | finding |
| docs/deploy.md | finding |
| envs/env-00/README.md | no finding |
| envs/env-00/flux/flux-instance.yaml | finding |
| envs/env-00/flux/generated/cluster-vars-configmap.yaml | excluded: generated; byte-matches tools/lib/render_cluster_vars_configmap.py |
| envs/env-00/flux/kustomization.yaml | no finding |
| envs/env-00/flux/kustomizations/apps.yaml | finding |
| envs/env-00/flux/kustomizations/platform-configs.yaml | finding |
| envs/env-00/flux/kustomizations/platform-controllers.yaml | no finding |
| envs/env-00/flux/kustomizations/services.yaml | finding |
| envs/env-00/flux/platform-configs/kustomization.yaml | finding |
| envs/env-00/inventory/hosts.yml | finding |
| envs/env-00/values/apps.yaml | no finding |
| envs/env-00/values/cluster-vars.yaml | no finding |
| envs/env-00/values/hermes.yaml | no finding |
| envs/env-01/README.md | finding |
| envs/env-01/ca.crt | no finding |
| envs/env-01/flux/flux-instance.yaml | no finding |
| envs/env-01/flux/generated/cluster-vars-configmap.yaml | excluded: generated; byte-matches tools/lib/render_cluster_vars_configmap.py |
| envs/env-01/flux/kustomization.yaml | no finding |
| envs/env-01/flux/kustomizations/apps.yaml | no finding |
| envs/env-01/flux/kustomizations/platform-configs.yaml | no finding |
| envs/env-01/flux/kustomizations/platform-controllers.yaml | no finding |
| envs/env-01/flux/kustomizations/services.yaml | no finding |
| envs/env-01/flux/platform-configs/kustomization.yaml | no finding |
| envs/env-01/inventory/hosts.yml | no finding |
| envs/env-01/secrets/README.md | finding |
| envs/env-01/secrets/backups-restic.sops.yaml | excluded: SOPS; metadata only (Secret platform-backups/backups-restic, age, MAC, values ENC, keys RESTIC_PASSWORD, RESTIC_REPOSITORY, RESTIC_REST_USERNAME, RESTIC_REST_PASSWORD, htpasswd) |
| envs/env-01/secrets/cert-manager-ca.sops.yaml | excluded: SOPS; metadata only (Secret platform-cert-manager/cert-manager-ca, age, MAC, values ENC, keys tls.crt, tls.key) |
| envs/env-01/secrets/grafana-admin.sops.yaml | excluded: SOPS; metadata only (Secret observability/grafana-admin, age, MAC, values ENC, key admin_password) |
| envs/env-01/secrets/hermes-code-factory-api.sops.yaml | excluded: SOPS; metadata only (Secret hermes-code-factory/hermes-code-factory-api, age, MAC, values ENC, key api_server_key) |
| envs/env-01/secrets/hermes-code-factory-dashboard.sops.yaml | excluded: SOPS; metadata only (Secret hermes-code-factory/hermes-code-factory-dashboard, age, MAC, values ENC, keys admin_password, password_hash, signing_secret) |
| envs/env-01/secrets/hermes-ops-api.sops.yaml | excluded: SOPS; metadata only (Secret hermes-ops/hermes-ops-api, age, MAC, values ENC, key api_server_key) |
| envs/env-01/secrets/hermes-ops-dashboard.sops.yaml | excluded: SOPS; metadata only (Secret hermes-ops/hermes-ops-dashboard, age, MAC, values ENC, keys admin_password, password_hash, signing_secret) |
| envs/env-01/values/apps.yaml | no finding |
| envs/env-01/values/cluster-vars.yaml | finding |
| envs/env-01/values/hermes.yaml | no finding |
| infra/ansible/README.md | finding |
| infra/ansible/ansible.cfg | no finding |
| infra/ansible/group_vars/all.yml | finding |
| infra/ansible/roles/base-os/defaults/main.yml | no finding |
| infra/ansible/roles/base-os/handlers/main.yml | no finding |
| infra/ansible/roles/base-os/tasks/chrony.yml | no finding |
| infra/ansible/roles/base-os/tasks/main.yml | no finding |
| infra/ansible/roles/base-os/templates/20auto-upgrades.j2 | no finding |
| infra/ansible/roles/base-os/templates/50unattended-upgrades.j2 | no finding |
| infra/ansible/roles/base-os/templates/chrony.conf.j2 | no finding |
| infra/ansible/roles/base-os/templates/journald-jarvis.conf.j2 | no finding |
| infra/ansible/roles/base-os/templates/resolved-stub.conf.j2 | no finding |
| infra/ansible/roles/flux-operator/defaults/main.yml | no finding |
| infra/ansible/roles/flux-operator/tasks/chart_pin.yml | no finding |
| infra/ansible/roles/flux-operator/tasks/helm_cli.yml | no finding |
| infra/ansible/roles/flux-operator/tasks/instance.yml | no finding |
| infra/ansible/roles/flux-operator/tasks/main.yml | no finding |
| infra/ansible/roles/flux-operator/tasks/operator.yml | no finding |
| infra/ansible/roles/flux-operator/tasks/rebuild_hold.yml | no finding |
| infra/ansible/roles/flux-operator/tasks/sops_age.yml | no finding |
| infra/ansible/roles/flux-operator/tasks/sops_age_key_mode.yml | no finding |
| infra/ansible/roles/flux-operator/templates/backup-rebuild-hold.yaml.j2 | no finding |
| infra/ansible/roles/flux-operator/templates/helm-upgrade.cmd.j2 | no finding |
| infra/ansible/roles/flux-operator/templates/sops-age-secret.yaml.j2 | no finding |
| infra/ansible/roles/k3s/defaults/main.yml | no finding |
| infra/ansible/roles/k3s/handlers/main.yml | no finding |
| infra/ansible/roles/k3s/tasks/data-dirs.yml | no finding |
| infra/ansible/roles/k3s/tasks/guard.yml | no finding |
| infra/ansible/roles/k3s/tasks/labels.yml | no finding |
| infra/ansible/roles/k3s/tasks/main.yml | no finding |
| infra/ansible/roles/k3s/tasks/refuse.yml | no finding |
| infra/ansible/roles/k3s/tasks/service-unit.yml | no finding |
| infra/ansible/roles/k3s/tasks/token.yml | finding |
| infra/ansible/roles/k3s/templates/agent-config.yaml.j2 | no finding |
| infra/ansible/roles/k3s/templates/config.yaml.j2 | finding |
| infra/ansible/roles/k3s/templates/jarvis-mount.conf.j2 | no finding |
| infra/ansible/roles/k3s/templates/k3s.service.j2 | no finding |
| infra/ansible/roles/k3s/templates/label-patches.json.j2 | no finding |
| infra/ansible/roles/kubeconfig/tasks/main.yml | no finding |
| infra/ansible/roles/node-disk/tasks/main.yml | finding |
| infra/ansible/roles/node-disk/vars/main.yml | no finding |
| infra/ansible/site.yml | no finding |
| infra/ansible/tasks/assert-gpu.yml | finding |
| infra/ansible/tasks/preflight.yml | finding |
| infra/inventory/roles.yml/constructed.yml | no finding |
| infra/inventory/roles.yml/groups.yml | no finding |
| platform/app-dns/CONTRACT.md | finding |
| platform/app-dns/coredns-custom.yaml | no finding |
| platform/app-dns/helmrelease.yaml | no finding |
| platform/app-dns/helmrepository.yaml | no finding |
| platform/app-dns/kustomization.yaml | no finding |
| platform/app-dns/namespace.yaml | no finding |
| platform/backups/CONTRACT.md | finding |
| platform/backups/controller/helmrelease.yaml | no finding |
| platform/backups/controller/helmrepository.yaml | no finding |
| platform/backups/controller/kustomization.yaml | no finding |
| platform/backups/controller/namespace.yaml | no finding |
| platform/backups/controller/volumesnapshot-crd.yaml | excluded: vendored VolumeSnapshot CRD; header cites external-snapshotter v8.4.0; body not byte-compared to upstream |
| platform/backups/enrollment.yaml | finding |
| platform/backups/etcd-snapshots.yaml | finding |
| platform/backups/kustomization.yaml | no finding |
| platform/backups/namespace.yaml | no finding |
| platform/backups/rest-server.yaml | finding |
| platform/cert-manager/CONTRACT.md | finding |
| platform/cert-manager/config/internal-ca.yaml | no finding |
| platform/cert-manager/config/kustomization.yaml | no finding |
| platform/cert-manager/controller/helmrelease.yaml | no finding |
| platform/cert-manager/controller/helmrepository.yaml | no finding |
| platform/cert-manager/controller/kustomization.yaml | no finding |
| platform/cert-manager/controller/namespace.yaml | no finding |
| platform/configs/CONTRACT.md | finding |
| platform/configs/kustomization.yaml | no finding |
| platform/flux-operator/CONTRACT.md | finding |
| platform/flux-operator/helmrelease.yaml | no finding |
| platform/flux-operator/kustomization.yaml | no finding |
| platform/flux-operator/oci-repository.yaml | no finding |
| platform/kustomization.yaml | no finding |
| platform/kyverno/CONTRACT.md | finding |
| platform/kyverno/helmrelease.yaml | no finding |
| platform/kyverno/helmrepository.yaml | no finding |
| platform/kyverno/kustomization.yaml | no finding |
| platform/kyverno/namespace.yaml | no finding |
| platform/kyverno/rbac.yaml | finding |
| platform/testapp/CONTRACT.md | finding |
| platform/testapp/kustomization.yaml | no finding |
| platform/testapp/namespace.yaml | no finding |
| platform/testapp/testapp.yaml | no finding |
| platform/traefik/CONTRACT.md | finding |
| platform/traefik/helmchartconfig.yaml | no finding |
| platform/traefik/kustomization.yaml | no finding |
| pytest.ini | no finding |
| renovate.json | finding |
| requirements-dev.txt | no finding |
| rulebooks/coding-agents.md | no finding |
| schema/CONTRACT.md | finding |
| schema/__init__.py | no finding |
| schema/env.py | finding |
| services/kustomization.yaml | no finding |
| services/placeholder.yaml | finding |
| tools/bootstrap-secrets | finding |
| tools/drill | no finding |
| tools/install-deps | finding |
| tools/lib/_env.py | no finding |
| tools/lib/_etcd_image.py | no finding |
| tools/lib/_sops.py | no finding |
| tools/lib/render_cluster_vars_configmap.py | finding |
| tools/prep-node | finding |
| tools/restore | no finding |
| tools/seal | no finding |
| tools/verify/run | finding |
| tools/versions.env | no finding |
| audit/findings-wrapup-grok-4-7.md | excluded: this audit report |

`tools/drill` is cited in F1 and F12 via the backups contract's description of what the drill does. The script itself was read; the finding is the contract's limit and verify's timestamp check, so the drill script is tagged `no finding`. The behavior tests around it passed.

## Concerns and questions

- The brief's branch is `audit/wrapup-grok-4-7`. That name does not match the cloud agent's usual `cursor/…-b6df` prefix. The brief won, because it names the branch and says to follow it exactly.
- `OWNER_NAME` was set to the personal name the brief gives, for `ci/run-checks` and pytest. `ci/run-checks` exited 0 on the full tree (gitleaks: no leaks, 185 commits). pytest collected 483 and reported 482 passed, 1 failed: `ci/tests/test_base_os.py::test_chrony_template_notifies_restart_only_when_changed`. The handler uses `ansible.builtin.service`. This VM's systemd is `offline` and `chrony` is not installed, so the play fails with `Could not find the requested service chrony: host` before the test's PATH `systemctl` stub matters. The same single failure happened in a detached worktree with `envs/env-01` removed (CI's env-00-only steps). The brief says this SHA passes. This failure is the environment, not a product finding.
- No SSH, no live kubectl, no age key, no SOPS decrypt. F1's "rebuilt node" outcome is the contract plus the token path, not a disk that was pulled.
- Tailscale is not in the repo. It is listed as an unwritten adopter prerequisite. It should not move into the cluster.
- The VolumeSnapshot CRD body was not diffed against `external-snapshotter` v8.4.0. The header cites that file. Generated cluster-vars ConfigMaps were diffed against the renderer and matched.
- `.cursor/cli.json` comments already say two forms are not closed: a remote-copy invoked through a shell wrapper, and a kubectl certificate subcommand with a flag between the words (`ci/checks/lib/_cli_permissions.py:15` and `:708`). Those were treated as known residuals. F7 is the gap the corpus does not mention.
- Cross-checking the other two auditors is a later pass. This file is the first-pass review only.
- `make deps` was run, then the checks and pytest entrypoint `ci/ci-local-verify` with `OWNER_NAME` set. The Makefile `ci-verify` target depends on `deps` and then that script. The first `deps` attempt failed on stale apt lists (F21); the second, after `apt-get update`, installed the pinned CLIs and the dev venv.
