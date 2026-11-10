# Jarvis

A cluster foundation you clone and run on day 1: k3s and Flux with ingress, an internal CA, admission policies, backups with restore, and sealed secrets. Features, add-ons and hosted apps bolt on top. Each cluster lives in a git repository you own, pins one core release, and moves when you merge the pull request that bumps it.

- **Start here:** [Quickstart](docs/quickstart.md), one node to a verified cluster.
- **How it fits together:** [Concepts](docs/concepts.md).
- **Everything else:** [docs/README.md](docs/README.md).

## Requirements

- Nodes: Ubuntu Server, amd64 only, with exactly one control-plane host.
- GPUs: NVIDIA only, and only on a host with the `gpu` role.
- A controller: macOS or Linux, with `curl` and `git`.
- GitHub for the repository Flux reads.

`site.yml` checks the node requirements in its opening play, before it changes a node ([infra/ansible/README.md](infra/ansible/README.md)).

## License

[Apache License 2.0](LICENSE). Contributing: [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: [SECURITY.md](SECURITY.md).
