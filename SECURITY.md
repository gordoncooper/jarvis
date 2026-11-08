# Security

## Reporting a vulnerability

Report it privately through GitHub: the repository's **Security** tab, **Report a vulnerability**. Do not open a public issue or pull request for it.

Include what is affected (a path, a component, a commit), how to reproduce it, and what an attacker gains. You get an answer on the report itself.

## Scope

In scope: everything in this repository, including the platform, add-ons, the hosted-app boundary (admission, network allows, backup isolation), the node bootstrap and the tools. A hosted app getting past its namespace, reaching core's credentials, or reaching the LAN or another app without the env's grant is a vulnerability.

Out of scope: an env repository's own values and secrets, the hosted apps themselves, and upstream projects (k3s, Flux, Kyverno, Traefik, cert-manager, VolSync, restic). Report those upstream.

## Supported versions

Only the latest tag and `main` get fixes.
