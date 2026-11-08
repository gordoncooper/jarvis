# jarvis

Infra, platform, and services for Jarvis (GitOps on k3s).

From a fresh copy to a running cluster: [docs/deploy.md](docs/deploy.md).

Doc index: [docs/README.md](docs/README.md).

## Requirements

- amd64 hosts only.
- Exactly one control-plane host.
- NVIDIA GPUs only, and only on a host with the `gpu` role.

`site.yml` checks these in its opening play, before it changes a node ([infra/ansible/README.md](infra/ansible/README.md)).

## License

[Apache License 2.0](LICENSE). Contributing: [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: [SECURITY.md](SECURITY.md).
