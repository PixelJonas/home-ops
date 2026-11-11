# vehicle-pipeline (deploy manifests)

Application source lives in its own repo: https://git.janz.digital/jonas/vehicle-pipeline
(Gitea Actions builds and pushes `git.janz.digital/ci/vehicle-pipeline:{sha-<7>,latest}` to
Gitea's own registry, private org `ci`). The pods need no pull secret: both
clusters pull `ci/` images through their global pull secret (read-only bot
`ci-pull`, written by infra-ops `mise run <cluster>:registry-pull-secret`).

This directory only holds the ArgoCD-deployed manifests. Changes to the app's
env-var contract need a paired PR in both repos.
