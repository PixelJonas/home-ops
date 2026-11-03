# LiteLLM bge-m3 Cutover

The `bge-m3` vLLM backend is owned by `components-apps/litellm` and serves
ingestbuddy indexing/search and the Taxbuddy lookup MCP through LiteLLM. The
public model name remains `bge-m3`; consumers do not change endpoints or model
names.

LiteLLM runs with `STORE_MODEL_IN_DB=true`. Its live model registry is in
Postgres, so editing or adding a Git `model_list` does not perform this cutover.
Use the Model Management API while the old Honcho backend is still running.

## Zero-downtime procedure

1. **Reconcile only the LiteLLM workload addition.** Do not reconcile the
   Honcho application deletion yet. Wait for the new backend and its persistent
   model cache:

   ```bash
   oc rollout status -n litellm deployment/litellm-bge-m3-app --timeout=15m
   oc get pvc -n litellm litellm-bge-m3-app
   ```

   The first start downloads the model and can take several minutes. The PVC
   must be `Bound` and the deployment fully ready before continuing.

2. **Test vLLM directly, bypassing LiteLLM.** This creates no embedding traffic
   through the old backend:

   ```bash
   oc port-forward -n litellm svc/litellm-bge-m3-app 18000:8000 \
     >/tmp/bge-m3-port-forward.log 2>&1 &
   PF_PID=$!
   trap 'kill "$PF_PID" 2>/dev/null || true' EXIT
   curl --retry 10 --retry-connrefused --retry-delay 2 -fsS \
     http://127.0.0.1:18000/v1/embeddings \
     -H 'Content-Type: application/json' \
     -d '{"model":"bge-m3","input":["cutover test"]}' \
     | jq -e '.data[0].embedding | length == 1024' >/dev/null
   kill "$PF_PID"
   trap - EXIT
   ```

3. **Set the admin endpoint and credential without printing the credential.**
   `LITELLM_MASTER_KEY` must already be loaded securely in the shell environment:

   ```bash
   export LITELLM_BASE_URL="https://litellm.apps.altus.janz.digital"
   : "${LITELLM_MASTER_KEY:?set LITELLM_MASTER_KEY without echoing it}"
   ```

   Identify exactly one existing database-backed `bge-m3` deployment and retain
   its old backend URL for rollback. These commands emit only the non-secret
   model ID and backend URL into shell variables; they do not print the model
   registry or any API keys:

   ```bash
   MODEL_ID="$(
     curl -fsS "${LITELLM_BASE_URL}/model/info" \
       -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
       | jq -er '[.data[] | select(.model_name == "bge-m3") | .model_info.id]
           | if length == 1 then .[0]
             else error("expected exactly one bge-m3 deployment") end'
   )"
   OLD_API_BASE="$(
     curl -fsS "${LITELLM_BASE_URL}/model/info" \
       -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
       | jq -er --arg id "$MODEL_ID" \
           '.data[] | select(.model_info.id == $id) | .litellm_params.api_base'
   )"
   ```

   If the first command finds zero or multiple deployments, stop and resolve
   the live registry state rather than creating another `bge-m3` deployment.

4. **Atomically point the existing model at the new service.** `PATCH` merges
   these parameters into the existing deployment, preserving its model name,
   provider settings, credentials, access metadata, and model ID. No LiteLLM
   restart is required:

   ```bash
   curl -fsS -X PATCH "${LITELLM_BASE_URL}/model/${MODEL_ID}/update" \
     -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
     -H 'Content-Type: application/json' \
     -d '{
       "model_name": "bge-m3",
       "litellm_params": {
         "api_base": "http://litellm-bge-m3-app.litellm.svc:8000/v1"
       }
     }' >/dev/null
   ```

   Verify only the non-secret routing fields:

   ```bash
   curl -fsS "${LITELLM_BASE_URL}/model/info" \
     -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
     | jq -e --arg id "$MODEL_ID" '
       .data[] | select(.model_info.id == $id)
       | {model_name, api_base: .litellm_params.api_base}
       | select(
           .model_name == "bge-m3"
           and .api_base == "http://litellm-bge-m3-app.litellm.svc:8000/v1"
         )'
   ```

   If validation fails while the old backend is still available, roll back the
   same model ID immediately:

   ```bash
   jq -n --arg api_base "$OLD_API_BASE" \
     '{model_name: "bge-m3", litellm_params: {api_base: $api_base}}' \
     | curl -fsS -X PATCH "${LITELLM_BASE_URL}/model/${MODEL_ID}/update" \
         -H "Authorization: Bearer ${LITELLM_MASTER_KEY}" \
         -H 'Content-Type: application/json' \
         --data @- >/dev/null
   ```

5. **Test through LiteLLM with an existing consumer key.** Load a bge-m3-scoped
   key into `LITELLM_CONSUMER_KEY` without printing it, then run:

   ```bash
   : "${LITELLM_CONSUMER_KEY:?set LITELLM_CONSUMER_KEY without echoing it}"
   curl -fsS "${LITELLM_BASE_URL}/v1/embeddings" \
     -H "Authorization: Bearer ${LITELLM_CONSUMER_KEY}" \
     -H 'Content-Type: application/json' \
     -d '{"model":"bge-m3","input":["cutover test"]}' \
     | jq -e '.data[0].embedding | length == 1024' >/dev/null
   unset LITELLM_MASTER_KEY LITELLM_CONSUMER_KEY MODEL_ID OLD_API_BASE
   ```

   Complete one normal ingestbuddy indexing/search operation and one Taxbuddy
   lookup MCP query. Only after both succeed should ArgoCD reconcile the Honcho
   application deletion. There are no consumer restarts or Git consumer endpoint
   changes in this cutover.
