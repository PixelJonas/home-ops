# vehicle-pipeline

Vehicle cost ledger (Paperless/ingestbuddy -> `vehicle_pipeline.costs`), the
trip detector (`python -m vehicle_pipeline.trips.detector`, schema `trips`)
and the Vollkostenrechnung views. Deployed by
`components-apps/vehicle-pipeline/`.

## Importing a VW app trip export

The VW app can export the car's trip memory ("Export trips"): a zip with
`Short-term data.csv` (one row per trip), `From refuelling.csv` (one row per
refuel segment) and `Long-term data.csv` (cumulative counters). The export
contains the VIN; it is mapped to the vehicle via `vehicle_pipeline.vehicles`
and never stored. Unknown VIN -> the import aborts.

Stream the zip into the detector pod (it has no copy of the file):

```bash
# check first: parses, looks up the vehicle, counts new vs. known rows
oc exec -i -n vehicle-pipeline deploy/vehicle-pipeline-app-detector -- \
  python -m vehicle_pipeline.trips.import_vw - --dry-run < "ExportTrips-<date>.zip"

oc exec -i -n vehicle-pipeline deploy/vehicle-pipeline-app-detector -- \
  python -m vehicle_pipeline.trips.import_vw - < "ExportTrips-<date>.zip"
```

It prints counts only. A local `--dry-run` without `DATABASE_URL` just parses
the file (`python -m vehicle_pipeline.trips.import_vw <zip|dir> --dry-run`).

**Re-importing is idempotent.** Rows are keyed by a hash of their content, so
importing the same file again, or a newer export that repeats older trips,
inserts only rows not seen before (and links the known ones to the new
export). Just import every new export as it comes; older exports never need
to be removed.

What happens next (detector cycle, <= 60 s; see `trips/imports.py`):

* Short-term rows become trips with `source = 'vw_export'`, key
  `vw:<vehicle>:<trip end, UTC>`. Start = end - driving time (an estimate:
  VW merges trips separated by stops under 2 h; `evidence.start_estimated`).
* Odometers are chained backwards from the export's odometer (the anchor),
  cross-checked against the first Data Act reading after the last exported
  trip; a mismatch, or a trip that was still open at export time, leaves
  the trips `odometer_pending`. Whole-km rounding can drift the derived
  odometer by a few km over many trips.
* Detector trips (`ha_phone`, `odometer_gap`) of that vehicle fully inside
  an export's time range are superseded (left out of the projection, so the
  Vollkosten views count each km once); their business/private annotations
  carry over to the covering imported trip. Detector trips straddling the
  range's edge stay (counted as `import_edge_overlap` in the cycle log).
* Imported trips are never notified.
* Refuel segments and long-term rows are stored raw. Odometer at each
  refuel, chained over the refuel segments and compared with the trips:
  `python -m vehicle_pipeline.trips.import_vw --refuel-check multivan`.

Tables: `trips.import_exports`, `trips.imported_trips` (raw, append-only),
`trips.imported_trip_exports` (which export contained which row).
