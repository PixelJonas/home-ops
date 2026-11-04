# Taxbuddy owner profile one-off initialization

Initialize the canonical Markdown owner profile from confirmed structured
`tax.tax_profile` rows before enabling the new doc-pipeline and agent-runtime
profile writers. The migration reads no Honcho data and prints no profile facts.

The Job manifest is a runbook artifact only. It is deliberately absent from all
ArgoCD kustomizations, so a sync cannot rerun it. Do not add `--replace`: the
initializer refuses a nonempty destination rather than overwriting curated
facts.

## Run before the final rollout

Confirm that the additive owner-profile storage from `main` is already synced
and bound. Run these commands from the root of the `honcho-removal` checkout,
before merging the final image-migration PR:

```bash
oc get pvc -n taxbuddy taxbuddy-app-owner-profile
oc apply -f docs/runbooks/taxbuddy-owner-profile-initializer-job.yaml
oc wait -n taxbuddy --for=condition=Complete \
  --timeout=10m job/taxbuddy-owner-profile-initializer
oc logs -n taxbuddy job/taxbuddy-owner-profile-initializer
```

Success logs report only the number of confirmed Steuerjahr profiles exported.
If the wait fails, inspect the same Job logs and resolve the error; do not retry
with `--replace` or delete the PVC.

Verify that the file exists and is nonempty through the existing agent-runtime
pod. This prints only its byte size, never its contents:

```bash
oc exec -n taxbuddy deploy/taxbuddy-app-agent-runtime -- python -c \
  'from pathlib import Path; p = Path("/data/owner-profile/owner.md"); size = p.stat().st_size; assert size > 0, "owner profile is empty"; print(f"owner.md present ({size} bytes)")'
```

After successful verification, delete only the one-off Job. This does not delete
the owner-profile PVC, database secret, or any application:

```bash
oc delete -n taxbuddy --wait=true job/taxbuddy-owner-profile-initializer
```

Proceed with the final PR rollout only after initialization and cleanup succeed.
