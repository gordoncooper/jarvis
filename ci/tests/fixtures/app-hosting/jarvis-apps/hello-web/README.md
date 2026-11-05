# hello-web

A static page served by an unprivileged nginx (`nginxinc/nginx-unprivileged`, pinned by digest).

**Proves:** an app on plain defaults runs with nothing granted. It has no volumes, no internet and no risky item, and needs only the defaults of 500m CPU and 512Mi memory. The env lists `hello-web` and adds no exception line.

- `app.yaml`: `webPort: 8080`, `volumes: []`, nothing else.
- `deploy/`: a ConfigMap with the page, a Deployment (non-root, no privilege escalation, every capability dropped, readiness and liveness probes on `/`), a Service and an Ingress on `${app_host}`.
- Address: `https://hello-web.apps.<dns suffix>`.
