#!/usr/bin/env python3
"""Idempotent upsert of ingestbuddy's pipeline:* tags and pipeline intake
Workflow into a live Paperless-ngx instance (ingestbuddy#7, infra-ops#61 +
infra-ops#56). Run as a regular-sync Job (wave ~150, Replace=true,Force=true)
in components-apps/paperless/ -- deliberately NOT an ArgoCD PostSync hook,
which ignores sync waves and would deadlock the first sync before the
paperless app itself is up.

Behaviour: drift-correcting upsert by name, never deletes anything not
in the two committed JSON files (pipeline-tags.json,
pipeline-intake.workflow.json) this reads. It never touches any other
tag or workflow -- in particular it never touches the UI-managed
workflows id 2 ("Tax Agent Ingest") or id 3 ("Vehicle cost pipeline
webhook"), whose current config is only snapshotted read-only under
./snapshots/ for drift visibility.

Uses only the Python stdlib (urllib) -- quay.io/openshift/origin-cli
ships python3 but not `requests`, matching the pattern already used by
components-apps/gitea/bootstrap-job.yaml's inline python3 one-liner.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

TAGS_FILE = "/config/pipeline-tags.json"
WORKFLOW_FILE = "/config/pipeline-intake.workflow.json"
WEBHOOK_SECRET_PLACEHOLDER = "__PAPERLESS_WEBHOOK_SECRET__"


def log(msg: str) -> None:
    print(f"==> {msg}", flush=True)


def api_request(base_url: str, token: str, method: str, path: str, body: dict | None = None):
    url = f"{base_url}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"Token {token}")
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"{method} {path} failed: HTTP {exc.code} {exc.reason}: {detail}"
        ) from exc


def list_all(base_url: str, token: str, path: str) -> list[dict]:
    """Follows Paperless's `next` pagination link until exhausted -- the
    Workflow list endpoint has no server-side name filter (unlike Tag's
    name__iexact), so a name lookup has to walk every page and match
    client-side."""
    results: list[dict] = []
    next_path: str | None = path
    while next_path:
        page = api_request(base_url, token, "GET", next_path)
        results.extend(page["results"])
        next_url = page.get("next")
        if not next_url:
            break
        parsed = urllib.parse.urlparse(next_url)
        next_path = f"{parsed.path}?{parsed.query}" if parsed.query else parsed.path
    return results


def upsert_tag(base_url: str, token: str, desired: dict) -> int:
    name = desired["name"]
    query = urllib.parse.urlencode({"name__iexact": name})
    existing = api_request(base_url, token, "GET", f"/api/tags/?{query}")["results"]
    if existing:
        tag = existing[0]
        patch = {}
        if "color" in desired and tag.get("color") != desired["color"]:
            patch["color"] = desired["color"]
        if "is_inbox_tag" in desired and tag.get("is_inbox_tag") != desired["is_inbox_tag"]:
            patch["is_inbox_tag"] = desired["is_inbox_tag"]
        if patch:
            api_request(base_url, token, "PATCH", f"/api/tags/{tag['id']}/", patch)
            log(f"tag '{name}' (id={tag['id']}): drift-corrected {patch}")
        else:
            log(f"tag '{name}' (id={tag['id']}): already up to date")
        return tag["id"]
    created = api_request(base_url, token, "POST", "/api/tags/", desired)
    log(f"tag '{name}': created (id={created['id']})")
    return created["id"]


def resolve_workflow_payload(template: dict, tag_ids: dict[str, int], webhook_secret: str) -> dict:
    payload = {k: v for k, v in template.items() if not k.startswith("_")}

    for trigger in payload["triggers"]:
        all_names = trigger.pop("filter_has_all_tag_names", [])
        not_names = trigger.pop("filter_has_not_tag_names", [])
        try:
            trigger["filter_has_all_tags"] = [tag_ids[n] for n in all_names]
            trigger["filter_has_not_tags"] = [tag_ids[n] for n in not_names]
        except KeyError as exc:
            raise RuntimeError(
                f"workflow template references unknown tag name {exc} -- add it to "
                "pipeline-tags.json first"
            ) from exc

    for action in payload["actions"]:
        webhook = action.get("webhook")
        if not webhook:
            continue
        if isinstance(webhook.get("headers"), dict):
            webhook["headers"] = {
                key: (webhook_secret if value == WEBHOOK_SECRET_PLACEHOLDER else value)
                for key, value in webhook["headers"].items()
            }
        if isinstance(webhook.get("body"), str):
            webhook["body"] = webhook["body"].replace(WEBHOOK_SECRET_PLACEHOLDER, webhook_secret)

    return payload


def upsert_workflow(base_url: str, token: str, payload: dict) -> None:
    name = payload["name"]
    existing = [w for w in list_all(base_url, token, "/api/workflows/") if w["name"] == name]
    if existing:
        workflow_id = existing[0]["id"]
        api_request(base_url, token, "PUT", f"/api/workflows/{workflow_id}/", payload)
        log(f"workflow '{name}' (id={workflow_id}): drift-corrected")
    else:
        created = api_request(base_url, token, "POST", "/api/workflows/", payload)
        log(f"workflow '{name}': created (id={created['id']})")


def main() -> int:
    base_url = os.environ["PAPERLESS_URL"].rstrip("/")
    token = os.environ["PAPERLESS_API_TOKEN"]
    webhook_secret = os.environ["PAPERLESS_WEBHOOK_SECRET"]

    with open(TAGS_FILE, encoding="utf-8") as f:
        tags = json.load(f)
    with open(WORKFLOW_FILE, encoding="utf-8") as f:
        workflow_template = json.load(f)

    log(f"upserting {len(tags)} pipeline tag(s) against {base_url}...")
    tag_ids = {tag["name"]: upsert_tag(base_url, token, tag) for tag in tags}

    log("upserting pipeline intake workflow...")
    payload = resolve_workflow_payload(workflow_template, tag_ids, webhook_secret)
    upsert_workflow(base_url, token, payload)

    log("done.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - top-level: fail the Job loudly with context
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
