# jarvis documentation

## Start

| Path | Topic |
| --- | --- |
| [quickstart.md](quickstart.md) | One node to a verified cluster, command by command |
| [concepts.md](concepts.md) | The repositories, layers, pins, checks, secrets and backups, one section each |

## How-to

| Path | Topic |
| --- | --- |
| [deploy.md](deploy.md) | Every step of a deploy in detail: init, node prep, site, verify, drill, restore |
| [releasing.md](releasing.md) | Cut a core release, and move an env to it |
| [add-an-addon.md](add-an-addon.md) | Write an add-on, and turn it on or off in an env |
| [add-a-hosted-app.md](add-a-hosted-app.md) | Write a hosted app's folder, and turn it on or off in an env |
| [../ci/README.md](../ci/README.md) | What `ci/run-checks` and the test suite do before a push |
| [../README.md](../README.md) | What this repo is, and the hardware limits |
| [../infra/ansible/README.md](../infra/ansible/README.md) | Node bootstrap (`site.yml`) |
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | How to send a change |
| [../SECURITY.md](../SECURITY.md) | How to report a vulnerability |
| [../AGENTS.md](../AGENTS.md) | Where coding agents start |
| [../agents/README.md](../agents/README.md) | The agent kit for instance repositories (`jarvis init --with-agents`) |
| [../agents/coding-agents.md](../agents/coding-agents.md) | Agent workflow on core |

## Reference

| Path | Topic |
| --- | --- |
| [reference.md](reference.md) | Every key of an env's values files: type, default, when it is required, what it means (generated from `schema/`) |
| [CONTRACT.template.md](CONTRACT.template.md) | Shared contract section layout |
| [code-delivery/CONTRACT.md](code-delivery/CONTRACT.md) | Code delivery provider (`github`) |
| [../schema/CONTRACT.md](../schema/CONTRACT.md) | Env schema |
| [../core/feature/app-dns/CONTRACT.md](../core/feature/app-dns/CONTRACT.md) | App DNS |
| [../core/feature/app-hosting/CONTRACT.md](../core/feature/app-hosting/CONTRACT.md) | Hosted apps: the app file, admission and network baseline |
| [../core/platform/backups/CONTRACT.md](../core/platform/backups/CONTRACT.md) | PVC backups and etcd snapshot copy |
| [../core/platform/cert-manager/CONTRACT.md](../core/platform/cert-manager/CONTRACT.md) | Internal CA and wildcard |
| [../core/platform/configs/CONTRACT.md](../core/platform/configs/CONTRACT.md) | platform-configs list |
| [../core/platform/flux-operator/CONTRACT.md](../core/platform/flux-operator/CONTRACT.md) | Flux Operator |
| [../core/platform/kyverno/CONTRACT.md](../core/platform/kyverno/CONTRACT.md) | Kyverno admission |
| [../core/feature/CONTRACT.md](../core/feature/CONTRACT.md) | Optional core pieces and how an env enables them, observability included |
| [../core/platform/testapp/CONTRACT.md](../core/platform/testapp/CONTRACT.md) | Platform canary |
| [../core/platform/traefik/CONTRACT.md](../core/platform/traefik/CONTRACT.md) | Ingress exposure |
| [../addons/CONTRACT.md](../addons/CONTRACT.md) | Add-ons: what every add-on meets |
| [../ci/tests/fixtures/addons/hello/CONTRACT.md](../ci/tests/fixtures/addons/hello/CONTRACT.md) | Sample add-on (a test fixture) |

## Env docs

| Path | Topic |
| --- | --- |
| [secrets.md](secrets.md) | How an env's secrets are made, kept, rotated and recovered; the two age keys |

Real envs live in private instance repositories that `jarvis init` writes ([deploy.md](deploy.md#new-env)).
