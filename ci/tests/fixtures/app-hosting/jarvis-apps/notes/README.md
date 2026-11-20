# notes

A small file server (`sigoden/dufs`, pinned by digest) for plain-text notes, stored on one volume. Upload, edit and delete work through the browser.

**Demonstrates:**

- **The per-app backup path.** `app.yaml` declares one volume, `data`, with `backup: true`. `deploy/` ships a PVC with that name and no `backup: none` annotation, no `volumeName` and no `dataSource`. The backup runs with the app's **own** credential, which the env mints while it keeps the app's backups on (the default; `backups: false` in its `values/apps.yaml` turns them off).
- **State survives a restart.** Delete the pod; the notes (and `welcome.txt`) are still there.
- **An init container and a ConfigMap.** `seed` copies `welcome.txt` from the ConfigMap onto the volume once (marker `/data/.seeded`), never overwriting later edits.
- **A caller.** `network.callers: [hello-web]`: hello-web may call notes (hello-web lists notes under `egress.apps`); no other app may.
- **Restricted pods.** Non-root, all capabilities dropped, `RuntimeDefault` seccomp, read-only root filesystem, `priorityClassName: hosted-app`, startup, readiness and liveness probes on `/__dufs__/health`, memory request equal to limit (notes 128Mi, init 16Mi).
- **A web entry.** `network.web: [{service: notes, port: 80}]`: core makes the HTTPRoute to that Service and port.

- `app.yaml`: one web entry, `callers: [hello-web]`, `volumes: [{name: data, backup: true}]`.
- Address: `https://notes.apps.<dns suffix>`.

**Env grants needed:** none. Its backup credential is not a grant.

**Not for real data as is:** dufs runs with `--allow-all` and no login, so anyone who can reach the address can read, change or delete every note. Before keeping anything private in it, add dufs's `--auth` with a credential from a Secret.

To keep a volume out of backups instead, declare it `backup: false` and annotate its PVC `backup: none`. The cluster refuses one without the other.

**Releases:** a change here bumps `VERSION`; the merge tags `notes/v<version>`, and each env moves its pin by a Renovate PR.
