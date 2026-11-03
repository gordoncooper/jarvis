# Consult round 2: judge

Checked pack2 against the code at `a7550522`, Flux Operator `v0.62.1`, restic `0.19.1` and project-plans `771158d`.

## Round-1 edits

All landed, in the right briefs, unmangled. `CHANGES-consult.md` matches the briefs line for line. The two changes it records (the tools seam moving to G11, and the voice wording) are consistent with INDEX and the later-waves notes. The D9 gate on both sources in `Tag` mode, the G11 `envs/` seam, the env-01 fresh rebuild, G5b-before-G5a and the bastion-target user all read the same in INDEX and in the briefs that mention them. The standing-rules tails are complete for each worker type: the Cursor briefs set `OWNER_NAME` and carry the rulebook bullet; the Claude briefs say it is already set and carry the headless, tests and PR bullets. The DOCS done-check sentence exists as quoted, at `decisions.md:374` (D45), inside the cited range.

## Findings

**G1, BUG: the rotation runbook and its live check fail as written.** I caused this one: my round-1 suggestion to rotate the drill repository's password is wrong.
- Every namespace's `restic-<pvc>` Secret is generated from the single `platform-backups/backups-restic` (`platform/backups/enrollment.yaml:35-50`, an apiCall context with `synchronize: true`). "Per repo: add a key, switch the Secret, remove the old key" switches the Secret for all repositories at once, so any repository that hasn't been given the new key yet is stranded.
- The drill creates its repository and deletes it in the same run (`tools/drill`, cleanup around `:430`), so "rehearse on the drill repo" has nothing to rotate.
- Fix in the brief: the runbook is "add the new key to every repository keyed to the shared password; switch the Secret; confirm every generated `restic-<pvc>` Secret has changed (compare a hash, never print); run one backup, `make drill` and `make verify` green; only then remove the old keys". The live check becomes "Operator rehearses the full shared-password rotation on bench" (test-only).

**G1, NOTE.** Whether Kyverno 1.19 regenerates the downstream Secrets when only the context's source Secret changes is unconfirmed. The runbook's propagation step should say what to do if they don't update (for example, re-trigger the policy), not assume they will.

**G6a, BUG: the new identity can't be applied by the Kustomization that uses it.** `platform/flux-operator/rbac.yaml` sits under `./platform`, which `platform-controllers` applies (`envs/env-00/flux/kustomizations/platform-controllers.yaml:10`). Once `platform-controllers` sets `serviceAccountName: flux-platform`, it has to create the service account and binding it impersonates. That deadlocks on the bench switch (both arrive in one commit) and on every fresh cluster. The `services` and `apps` tenant identities have the same problem.
- Fix in the brief: the service accounts and bindings are applied by the root sync, which keeps kustomize-controller's own identity under the multitenant profile. One way is a file listed in `envs/<env>/flux/kustomization.yaml`; another is the Ansible `flux-operator` role, which is already in scope.
- Add to acceptance: a test that no Kustomization creates the service account it impersonates.
- Add to the INDEX later-waves line for G11: when G11 replaces the root sync, the self-managed root must keep applying these identities.

**G6a, NOTE.** Line 48 says "the first of two Opus runs in wave 1; G8-guard runs after you finish." INDEX, G5c and G8-guard all say three runs, in the order G6a → G5c → G8-guard. Change it to "the first of three Opus runs; G5c runs next."

**G5c, NOTE.** Option (a)'s "a lock-wait budget (`--retry-lock`) on both sides" assumes G5c can set flags on the VolSync movers, which it doesn't own. Reword as "`--retry-lock` on the check, and state how a mover behaves when it meets the check's lock (fails and retries next run, or waits)."

## Verdict

- DOCS: GO
- G7: GO
- G1: FIX (rotation runbook and live check)
- G6a: FIX (identity bootstrap; line 48)
- G4: GO
- G3a-1: GO
- G5c: GO (NOTE optional)
- G8: GO
- G8-guard: GO
- INDEX: GO

**Overall: GO for wave 1 once the G1 and G6a fixes are in.** Neither fix changes order, collisions or models, so the other briefs can launch now.
