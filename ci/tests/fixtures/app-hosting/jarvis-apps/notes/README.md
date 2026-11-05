# notes

A small file server (`sigoden/dufs`, pinned by digest) for plain-text notes, stored on one volume. Upload, edit and delete work through the browser.

**Proves:** the per-app backup path. `app.yaml` declares one volume, `data`, with `backup: true`. `deploy/` ships a PVC with that name and no `backup: none` annotation, so the cluster backs it up. The backup runs with the app's **own** credential: the env registers it for `app-notes` (see the backups contract in Jarvis, "App credentials"). Only once that credential exists does the app get its egress to the apps backup server. Until it does, the volume is not backed up and the env's verify step fails it. No other app's credential, and no core credential, is involved.

- `app.yaml`: `webPort: 5000`, `volumes: [{name: data, backup: true}]`, defaults otherwise.
- `deploy/`: the PVC `data`, a Deployment (non-root, read-only root filesystem, probes on `/__dufs__/health`), a Service and an Ingress on `${app_host}`.
- Address: `https://notes.apps.<dns suffix>`.

**Not for real data as is:** dufs runs with `--allow-all` and no login, so anyone who can reach the address can read, change or delete every note. Before keeping anything private in it, add dufs's `--auth` with a credential from a Secret.

To keep a volume out of backups instead, declare it `backup: false` and annotate its PVC `backup: none`. The cluster refuses one without the other.
