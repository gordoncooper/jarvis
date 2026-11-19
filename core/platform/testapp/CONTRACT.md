# CONTRACT: test app

The platform canary at `https://testapp.<dns_suffix>/`. It depends on no service or app, and nothing depends on it.

## Offers

| What | Detail |
| --- | --- |
| Namespace `platform-testapp` | Layer `platform`, Pod Security `restricted`. Deployment, ClusterIP Service on 8080 and HTTPRoute `testapp` |
| `GET /` | ConfigMap `testapp` key `index.html`: `testapp` and a newline, sha256 `60eea60c805ba09c287eead2e6f0c7f04b5b8e1f66634f941216527e41c8bcc7` |
| Image | `nginxinc/nginx-unprivileged` (uid 101), digest-pinned, read-only root, `/tmp` emptyDir |
| HTTPRoute | Hostname `testapp.${dns_suffix}` on the core door (`parentRefs` Gateway `platform-gateway/jarvis`, `sectionName: core`), no certificate of its own: the pattern every core service and add-on follows; the core door's first user |

## Expects

| What | From |
| --- | --- |
| `dns_suffix` substituted by `platform-configs` | `values/cluster-vars.yaml` |
| The core door and its wildcard | [traefik](../traefik/CONTRACT.md) |

## Health

| Check | Pass |
| --- | --- |
| Deployment `testapp` | Ready (HTTP `GET /` on 8080) |
| `jarvis verify` | the body above over HTTPS, chaining to the internal CA; by hand, `curl -fsS --cacert envs/<env>/ca.crt https://testapp.<dns_suffix>/` |
