# knx-ets backup (VolSync restic, local-only)

Prepared but NOT wired into `../kustomization.yaml` yet. Requires Doppler
keys `KNX_ETS_VOLUME_REST_REPO_LOCAL` and `KNX_ETS_PERSISTENT_STATE_REST_REPO_LOCAL`
in homelab/home (rest repo URLs on the in-cluster restic-rest-server;
local-only, no B2 by decision 2026-10-11). Enable by adding `./backup`
(lists both `volume` and `persistent-state`) to the base kustomization after
the first successful boot of the restored VM.
