# vehicle-pipeline (deploy manifests)

Application source lives in its own repo: https://git.janz.digital/jonas/vehicle-pipeline
(Gitea Actions builds and pushes `ghcr.io/pixeljonas/vehicle-pipeline:{sha-<7>,latest}`).

This directory only holds the ArgoCD-deployed manifests. Changes to the app's
env-var contract need a paired PR in both repos.
