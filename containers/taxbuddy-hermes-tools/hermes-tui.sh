#!/bin/sh
# Run the Hermes TUI directly in this container, sharing the gateway's
# HERMES_HOME over the PVC. Running the TUI as a native pane process (not via
# kubectl exec into the hermes container) is what lets herdr's process-based
# agent detection see it; the pane env natively carries HERDR_* for the
# herdr-agent-state plugin. HOME=/opt/data matches the gateway's passwd home
# (history files live beside HERMES_HOME); uid 10000 == the hermes user.
exec env HOME=/opt/data HERMES_HOME=/opt/data hermes
