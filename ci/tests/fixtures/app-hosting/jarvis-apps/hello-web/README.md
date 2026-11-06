# hello-web

A static page served by an unprivileged nginx (`nginxinc/nginx-unprivileged`, pinned by digest) that also calls other things, so a live deploy exercises the network grants.

**Demonstrates:**

- **App-to-app traffic.** `network.egress.apps: [notes]` here and `network.callers: [hello-web]` in [notes](../notes/): `/notes/` on this app's address reaches notes. `/blocked/` calls `lan-demo`, which is not granted, so the default-deny egress blocks it and nginx answers 502.
- **Internet egress.** `network.egress.internet: true`: `/internet/` fetches a public page. Never the LAN or the cluster.
- **ConfigMap-driven config and an init container.** The nginx site is a ConfigMap template; the `render-config` init container writes it, with the cluster DNS address filled in, into a memory-backed emptyDir.
- **Restricted pods.** Non-root, all capabilities dropped, `RuntimeDefault` seccomp, read-only root filesystem (emptyDir for `/tmp`), `priorityClassName: hosted-app`, startup, readiness and liveness probes on `/healthz`, memory request equal to limit (web 64Mi, init 16Mi).
- **Ingress.** `ingressClassName: traefik`, `websecure` entrypoint and TLS annotation, host `${app_host}`.

- `app.yaml`: `network.web.port: 8080`, `egress.apps: [notes]`, `egress.internet: true`, `volumes: []`.
- Address: `https://hello-web.apps.<dns suffix>`.

**Env exception lines needed:** none. List `hello-web` (and `notes`, for `/notes/` to answer; `lan-demo` too if you want the refused call to fail at the network policy rather than on a missing service; both answer 502).
