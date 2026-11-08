#!/usr/bin/env python3
"""Assertions on the rendered Hogwarts restore-drill objects.

    check_hogwarts_drill.py <stage> [--require-restore-target] < objects.jsonl
    check_hogwarts_drill.py rest-server-policy < objects.jsonl
    check_hogwarts_drill.py --self-test

<stage> is a directory under components-infra/hogwarts/drill (restore,
fix-owner, boot, revert-snapshot, revert-copy). stdin holds the rendered
objects as JSON, one object per line (`kustomize build <dir> | yq -o=json
-I=0`). Standard library only, so CI and knowhere run it without extra
packages.

Used twice: the CI job `hogwarts-drill` (scripts/test_hogwarts_drill.sh) on
every PR, and infra-ops `hogwarts-restore-drill.sh` on the exact render it is
about to apply (with --require-restore-target for the restore stage: the
sentinel restore time must have been replaced). Exit 0 = all assertions hold;
otherwise one line per violation and exit 1.

What is asserted (components-infra/hogwarts/drill/README.md):
  - exactly the expected objects per stage, all in hogwarts-restore;
  - nothing that names the live namespace or the live home disk, no Secret,
    no ReplicationSource, no Telegram or Tailscale reference, no host
    namespaces (hostNetwork/hostPID/hostIPC) and no privileged container;
  - ExternalSecrets: exactly secretStoreRef + target + data (no dataFrom, so
    never a whole Doppler config), the store and target pinned, exactly the
    expected keys;
  - the VM is hogwarts-restore, Halted, home disk = PVC home-test, pod network
    with exactly one masquerade interface;
  - egress = exactly the DNS pods (5353) and the LiteLLM pods (4000), whole
    peers compared (namespaceSelector + podSelector + ports); ingress =
    exactly the knowhere VM's launcher pod on TCP 22;
  - cloud-init keeps partition: auto and sets hostname hogwarts-restore;
  - the helper Jobs run as drill-helper with anyuid pinned, one container
    with the pinned command, image digest and runAsUser 0, and touch only the
    scratch claims.
rest-server-policy (components-infra/restic-rest-server): hogwarts-restore
appears at most in the VolSync-mover peer (namespace list + pod label
app.kubernetes.io/created-by: volsync), nowhere else in the policy.
"""

import copy
import json
import sys

NS = "hogwarts-restore"
SENTINEL_TIME = "1970-01-01T00:00:00Z"
SENTINEL_TOKEN = "drill-sentinel-unset"
FORBIDDEN_NAMESPACES = {"hogwarts", "nextcloud", "paperless", "qdrant", "gitea"}
FORBIDDEN_TEXT = ("telegram", "tailscale", "hogwarts-home\"")

EXPECTED = {
    "restore": {
        ("Namespace", NS),
        ("ExternalSecret", "home-external-restic-config"),
        ("PersistentVolumeClaim", "home-test"),
        ("ReplicationDestination", "home-restore"),
        ("ServiceAccount", "drill-helper"),
        ("RoleBinding", "drill-helper-anyuid"),
    },
    "fix-owner": {("Job", "fix-owner")},
    "boot": {
        ("VirtualMachine", "hogwarts-restore"),
        ("DataVolume", "hogwarts-restore-rootdisk"),
        ("ExternalSecret", "hogwarts-restore-cloudinit-template"),
        ("NetworkPolicy", "hogwarts-restore-egress"),
        ("NetworkPolicy", "hogwarts-restore-ingress"),
        ("Service", "hogwarts-restore-ssh"),
    },
    "revert-snapshot": {("VolumeSnapshot", "home-test-snap")},
    "revert-copy": {
        ("PersistentVolumeClaim", "home-test-revert"),
        ("Job", "revert-copy"),
    },
}


SECRET_STORE = {"kind": "ClusterSecretStore", "name": "doppler-cluster"}
# Every ExternalSecret of the drill: target name, template keys (None = no
# template) and the exact secretKey -> Doppler key mapping.
EXTERNAL_SECRETS = {
    "home-external-restic-config": (
        "restic-config-home",
        None,
        {"RESTIC_REPOSITORY": "HOGWARTS_HOME_REST_REPO_LOCAL", "RESTIC_PASSWORD": "RESTIC_PASSWORD"},
    ),
    "hogwarts-restore-cloudinit-template": (
        "hogwarts-restore-cloudinit-template",
        {"engineVersion", "data"},
        {"sshUser": "HOGWARTS_SSH_USER", "primaryUser": "HOGWARTS_PRIMARY_USER",
         "sshPublicKey": "HOGWARTS_SSH_PUBLIC_KEY"},
    ),
}
DNS_PEER = {
    "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "openshift-dns"}},
    "podSelector": {"matchLabels": {"dns.operator.openshift.io/daemonset-dns": "default"}},
}
LITELLM_PEER = {
    "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "litellm"}},
    "podSelector": {"matchLabels": {"app.kubernetes.io/controller": "litellm",
                                    "app.kubernetes.io/name": "litellm-app"}},
}
KNOWHERE_PEER = {
    "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "knowhere"}},
    "podSelector": {"matchLabels": {"kubevirt.io": "virt-launcher", "vm.kubevirt.io/name": "knowhere"}},
}
EGRESS_RULES = [
    {"to": [DNS_PEER], "ports": [{"protocol": "UDP", "port": 5353}, {"protocol": "TCP", "port": 5353}]},
    {"to": [LITELLM_PEER], "ports": [{"protocol": "TCP", "port": 4000}]},
]
JOB_IMAGE_PREFIX = "registry.redhat.io/openshift4/ose-cli-rhel9:"
# The helper Jobs' exact scripts: a change here is a change of what runs as
# root on the restored disk, so it needs a change of this checker too.
JOB_SCRIPTS = {
    "fix-owner": (
        "set -euo pipefail\n"
        "f=/restore/disk.img\n"
        "test -f \"$f\"\n"
        "chown 107:107 \"$f\"\n"
        "echo \"RESULT $(stat -c '%s %u:%g' \"$f\")\"\n"
    ),
    "revert-copy": (
        "set -euo pipefail\n"
        "test -f /src/disk.img\n"
        "cp --sparse=always /src/disk.img /dst/disk.img\n"
        "chown 107:107 /dst/disk.img\n"
        "echo \"RESULT $(stat -c '%s %u:%g' /dst/disk.img)\"\n"
    ),
}
JOB_MOUNTS = {
    "fix-owner": [{"name": "restore", "mountPath": "/restore"}],
    "revert-copy": [{"name": "src", "mountPath": "/src", "readOnly": True},
                    {"name": "dst", "mountPath": "/dst"}],
}
MOVER_LABELS = {"app.kubernetes.io/created-by": "volsync"}


def walk(value):
    """Every dict in a nested structure."""
    if isinstance(value, dict):
        yield value
        for v in value.values():
            yield from walk(v)
    elif isinstance(value, list):
        for v in value:
            yield from walk(v)


def get(obj, *path, default=None):
    for key in path:
        if isinstance(obj, dict):
            obj = obj.get(key)
        elif isinstance(obj, list) and isinstance(key, int) and 0 <= key < len(obj):
            obj = obj[key]
        else:
            return default
        if obj is None:
            return default
    return obj


def by_kind(objs, kind, name=None):
    return [o for o in objs if o.get("kind") == kind and (name is None or get(o, "metadata", "name") == name)]


def check_common(stage, objs, errors):
    found = {(o.get("kind"), get(o, "metadata", "name")) for o in objs}
    if len(found) != len(objs):
        errors.append("duplicate objects in the render")
    want = EXPECTED[stage]
    for missing in sorted(want - found):
        errors.append(f"missing {missing[0]}/{missing[1]}")
    for extra in sorted(found - want):
        errors.append(f"unexpected {extra[0]}/{extra[1]} (not part of the drill stage {stage})")
    for o in objs:
        kind, name = o.get("kind"), get(o, "metadata", "name")
        ns = get(o, "metadata", "namespace")
        if kind == "Namespace":
            if name != NS:
                errors.append(f"Namespace {name} is not {NS}")
        elif ns != NS:
            errors.append(f"{kind}/{name} is in namespace {ns!r}, not {NS}")
        if kind in ("Secret", "ReplicationSource"):
            errors.append(f"{kind}/{name}: the drill never ships a {kind}")
        text = json.dumps(o).lower()
        for needle in FORBIDDEN_TEXT:
            if needle in text:
                errors.append(f"{kind}/{name} mentions {needle.strip(chr(34))!r}")
        for d in walk(o):
            for key in ("namespace",):
                if d.get(key) in FORBIDDEN_NAMESPACES:
                    errors.append(f"{kind}/{name} references namespace {d[key]}")
            for v in (get(d, "matchLabels", "kubernetes.io/metadata.name"),):
                if v in FORBIDDEN_NAMESPACES:
                    errors.append(f"{kind}/{name} selects namespace {v}")
            if d.get("claimName") == "hogwarts-home" or d.get("persistentVolumeClaimName") == "hogwarts-home":
                errors.append(f"{kind}/{name} uses the live claim hogwarts-home")
            for key in ("hostNetwork", "hostPID", "hostIPC", "privileged", "allowPrivilegeEscalation"):
                if key in d and d[key] is not False:
                    errors.append(f"{kind}/{name} sets {key}")
        if kind == "ExternalSecret":
            check_external_secret(o, errors)


def check_external_secret(es, errors):
    name = get(es, "metadata", "name")
    spec = get(es, "spec", default={})
    if name not in EXTERNAL_SECRETS:
        errors.append(f"ExternalSecret/{name} is not one of the drill's ExternalSecrets")
        return
    target_name, template_keys, mapping = EXTERNAL_SECRETS[name]
    if not isinstance(spec, dict) or set(spec) != {"secretStoreRef", "target", "data"}:
        keys = sorted(spec) if isinstance(spec, dict) else spec
        errors.append(f"ExternalSecret/{name} spec must be exactly secretStoreRef, target, data "
                      f"(no dataFrom: never a whole Doppler config), got {keys}")
    if spec.get("secretStoreRef") != SECRET_STORE:
        errors.append(f"ExternalSecret/{name} must read the ClusterSecretStore doppler-cluster")
    target = spec.get("target") or {}
    want_target = {"name"} | ({"template"} if template_keys else set())
    if not isinstance(target, dict) or set(target) != want_target or target.get("name") != target_name:
        errors.append(f"ExternalSecret/{name} target must be exactly {sorted(want_target)} "
                      f"with name {target_name}")
    elif template_keys and set(target.get("template") or {}) != template_keys:
        errors.append(f"ExternalSecret/{name} template keys must be {sorted(template_keys)}")
    data = spec.get("data")
    got = {}
    if isinstance(data, list):
        for item in data:
            if (not isinstance(item, dict) or set(item) != {"secretKey", "remoteRef"}
                    or not isinstance(item.get("remoteRef"), dict) or set(item["remoteRef"]) != {"key"}):
                errors.append(f"ExternalSecret/{name} data entries must be secretKey + remoteRef.key only")
                continue
            got[item["secretKey"]] = item["remoteRef"]["key"]
    if got != mapping or not isinstance(data, list) or len(data) != len(mapping):
        errors.append(f"ExternalSecret/{name} reads {sorted(got.values())}, expected {sorted(mapping.values())}")


def check_restore(objs, errors, require_target):
    for rd in by_kind(objs, "ReplicationDestination", "home-restore"):
        restic = get(rd, "spec", "restic", default={})
        if restic.get("destinationPVC") != "home-test":
            errors.append("RD home-restore must restore into home-test")
        if restic.get("copyMethod") != "Direct":
            errors.append("RD home-restore must use copyMethod Direct")
        if restic.get("repository") != "restic-config-home":
            errors.append("RD home-restore must read restic-config-home")
        trigger = get(rd, "spec", "trigger", default={})
        if set(trigger) != {"manual"} or not trigger.get("manual"):
            errors.append("RD home-restore needs exactly a manual trigger (never {} or a schedule)")
        when, token = restic.get("restoreAsOf"), trigger.get("manual")
        if require_target:
            if when in (None, SENTINEL_TIME) or token in (None, SENTINEL_TOKEN):
                errors.append("RD home-restore still carries the sentinel restoreAsOf/trigger")
        elif when != SENTINEL_TIME or token != SENTINEL_TOKEN:
            errors.append("RD home-restore in git must carry the sentinels (the drill script sets them)")
    # The ExternalSecret is checked in check_common (check_external_secret).
    for rb in by_kind(objs, "RoleBinding"):
        if get(rb, "roleRef", "name") != "system:openshift:scc:anyuid":
            errors.append("RoleBinding must grant only the anyuid SCC")
        subjects = get(rb, "subjects", default=[])
        if subjects != [{"kind": "ServiceAccount", "name": "drill-helper", "namespace": NS}]:
            errors.append("RoleBinding must bind only the drill-helper ServiceAccount")
    for pvc in by_kind(objs, "PersistentVolumeClaim", "home-test"):
        if get(pvc, "spec", "dataSource") or get(pvc, "spec", "dataSourceRef"):
            errors.append("home-test must be empty before the restore (no dataSource)")


def check_job(objs, errors, name, claims):
    for job in by_kind(objs, "Job", name):
        pod = get(job, "spec", "template", default={})
        if get(pod, "spec", "serviceAccountName") != "drill-helper":
            errors.append(f"Job {name} must run as drill-helper")
        if get(pod, "metadata", "annotations", "openshift.io/required-scc") != "anyuid":
            errors.append(f"Job {name} must pin openshift.io/required-scc: anyuid")
        used = {(get(v, "persistentVolumeClaim", "claimName"), bool(get(v, "persistentVolumeClaim", "readOnly")))
                for v in get(pod, "spec", "volumes", default=[])}
        if used != claims:
            errors.append(f"Job {name} mounts {sorted(used)}, expected {sorted(claims)}")
        pspec = get(pod, "spec", default={})
        if "initContainers" in pspec or "ephemeralContainers" in pspec:
            errors.append(f"Job {name} must have no init or ephemeral containers")
        containers = pspec.get("containers") or []
        if len(containers) != 1:
            errors.append(f"Job {name} must have exactly one container")
            continue
        c = containers[0]
        if c.get("command") != ["/bin/bash", "-c", JOB_SCRIPTS[name]] or "args" in c:
            errors.append(f"Job {name} runs another command than the pinned script")
        if c.get("securityContext") != {"runAsUser": 0}:
            errors.append(f"Job {name} securityContext must be exactly runAsUser 0")
        image = str(c.get("image", ""))
        if not image.startswith(JOB_IMAGE_PREFIX) or "@sha256:" not in image:
            errors.append(f"Job {name} must use the digest-pinned ose-cli image")
        if c.get("volumeMounts") != JOB_MOUNTS[name]:
            errors.append(f"Job {name} volumeMounts differ from the pinned mounts")
        if "env" in c or "envFrom" in c:
            errors.append(f"Job {name} must not get env (no secrets in the helper)")


def check_boot(objs, errors):
    for vm in by_kind(objs, "VirtualMachine", "hogwarts-restore"):
        spec = get(vm, "spec", default={})
        if spec.get("runStrategy") != "Halted" or "running" in spec:
            errors.append("VM hogwarts-restore must be runStrategy Halted (the drill script starts it)")
        tspec = get(spec, "template", "spec", default={})
        vols = {v.get("name"): v for v in tspec.get("volumes", [])}
        if set(vols) != {"rootdisk", "homedisk", "cloudinitdisk"}:
            errors.append(f"VM volumes are {sorted(vols)}")
        if get(vols, "rootdisk", "dataVolume", "name") != "hogwarts-restore-rootdisk":
            errors.append("VM root disk must be hogwarts-restore-rootdisk")
        home = vols.get("homedisk", {})
        if get(home, "persistentVolumeClaim", "claimName") != "home-test" or "dataVolume" in home:
            errors.append("VM home disk must be the PVC home-test")
        if get(vols, "cloudinitdisk", "cloudInitNoCloud", "secretRef", "name") != "hogwarts-restore-cloudinit":
            errors.append("VM cloud-init must be the Secret hogwarts-restore-cloudinit")
        nets = tspec.get("networks", [])
        if nets != [{"name": "default", "pod": {}}]:
            errors.append("VM must have exactly the pod network")
        if get(tspec, "domain", "devices", "interfaces") != [{"name": "default", "masquerade": {}}]:
            errors.append("VM must have exactly one masquerade interface on the pod network")
        if get(spec, "template", "metadata", "labels", "kubevirt.io/domain") != "hogwarts-restore":
            errors.append("VM domain label must be hogwarts-restore")
    for dv in by_kind(objs, "DataVolume"):
        if not get(dv, "spec", "source", "http", "url"):
            errors.append("root disk must import the pinned image (http source)")
    for es in by_kind(objs, "ExternalSecret", "hogwarts-restore-cloudinit-template"):
        user_data = str(get(es, "spec", "target", "template", "data", "userData", default=""))
        hosts = [line.strip() for line in user_data.splitlines() if line.strip().startswith("hostname:")]
        if hosts != ["hostname: hogwarts-restore"]:
            errors.append(f"cloud-init hostname lines are {hosts}, expected exactly hostname: hogwarts-restore")
        if "partition: auto" not in user_data or "partition: none" in user_data:
            errors.append("cloud-init fs_setup must keep partition: auto (reuse the restored ext4)")
        if get(es, "spec", "target", "name") != "hogwarts-restore-cloudinit-template":
            errors.append("cloud-init template must render into hogwarts-restore-cloudinit-template")
    for np in by_kind(objs, "NetworkPolicy", "hogwarts-restore-egress"):
        spec = get(np, "spec", default={})
        if spec.get("policyTypes") != ["Egress"]:
            errors.append("egress policy must be policyTypes [Egress]")
        if get(spec, "podSelector", "matchLabels") != {"vm.kubevirt.io/name": "hogwarts-restore"}:
            errors.append("egress policy must select the hogwarts-restore VM")
        if set(spec) != {"podSelector", "policyTypes", "egress"}:
            errors.append(f"egress policy spec keys are {sorted(spec)}")
        rules = spec.get("egress", [])
        if any("ipBlock" in peer for rule in rules for peer in rule.get("to", []) or []):
            errors.append("egress policy has an ipBlock rule (internet or LAN)")
        # Whole rules: namespaceSelector AND podSelector of each peer, and the
        # ports. A missing podSelector would open every pod of the namespace,
        # a missing `to` every destination on that port.
        got = sorted((normalise_rule(r, "to") for r in rules), key=json.dumps)
        want = sorted((normalise_rule(r, "to") for r in EGRESS_RULES), key=json.dumps)
        if got != want:
            errors.append("egress must be exactly the DNS pods on 5353 and the LiteLLM pods on TCP 4000 "
                          f"(whole peers compared), got {got}")
    for np in by_kind(objs, "NetworkPolicy", "hogwarts-restore-ingress"):
        spec = get(np, "spec", default={})
        ok = (set(spec) == {"podSelector", "policyTypes", "ingress"}
              and spec.get("policyTypes") == ["Ingress"]
              and get(spec, "podSelector") == {"matchLabels": {"vm.kubevirt.io/name": "hogwarts-restore"}}
              and spec.get("ingress") == [{"from": [KNOWHERE_PEER], "ports": [{"protocol": "TCP", "port": 22}]}])
        if not ok:
            errors.append("ingress policy must admit exactly the knowhere VM launcher (namespace knowhere) "
                          "on TCP 22 to hogwarts-restore")
    for svc in by_kind(objs, "Service", "hogwarts-restore-ssh"):
        if get(svc, "spec", "selector") != {"vm.kubevirt.io/name": "hogwarts-restore"}:
            errors.append("SSH Service must select only hogwarts-restore")
        if get(svc, "spec", "type", default="ClusterIP") != "ClusterIP":
            errors.append("SSH Service must be ClusterIP")


def normalise_rule(rule, peers_key):
    """A NetworkPolicy rule as comparable data: peers as given, ports as sorted tuples."""
    if not isinstance(rule, dict):
        return {"invalid": repr(rule)}
    ports = rule.get("ports")
    norm_ports = None if ports is None else sorted(
        (p.get("protocol"), p.get("port")) if isinstance(p, dict) and set(p) <= {"protocol", "port"}
        else ("invalid", json.dumps(p, sort_keys=True))
        for p in ports
    )
    return {"keys": sorted(rule), peers_key: rule.get(peers_key), "ports": norm_ports}


def check_rest_server_policy(objs):
    """hogwarts-restore may appear only in the VolSync-mover peer of the rest-server policy."""
    errors = []
    pols = by_kind(objs, "NetworkPolicy", "restic-rest-server-ingress")
    if len(pols) != 1:
        return ["expected exactly one NetworkPolicy restic-rest-server-ingress in the render"]
    for o in objs:
        if o is not pols[0] and NS in json.dumps(o):
            errors.append(f"{o.get('kind')}/{get(o, 'metadata', 'name')} mentions {NS}")
    for i, rule in enumerate(get(pols[0], "spec", "ingress", default=[])):
        for j, peer in enumerate(rule.get("from", []) or []):
            if NS not in json.dumps(peer):
                continue
            exprs = get(peer, "namespaceSelector", "matchExpressions", default=[])
            mover = (set(peer) == {"namespaceSelector", "podSelector"}
                     and set(peer["namespaceSelector"]) == {"matchExpressions"}
                     and len(exprs) == 1
                     and exprs[0].get("key") == "kubernetes.io/metadata.name"
                     and exprs[0].get("operator") == "In"
                     and NS in (exprs[0].get("values") or [])
                     and peer.get("podSelector") == {"matchLabels": MOVER_LABELS})
            if not mover:
                errors.append(f"ingress[{i}].from[{j}] names {NS} outside the VolSync-mover peer "
                              f"(namespace list + podSelector {MOVER_LABELS})")
    return errors


def check_revert_snapshot(objs, errors):
    for vs in by_kind(objs, "VolumeSnapshot", "home-test-snap"):
        if get(vs, "spec", "source", "persistentVolumeClaimName") != "home-test":
            errors.append("VolumeSnapshot must snapshot home-test")


def check_revert_copy(objs, errors):
    for pvc in by_kind(objs, "PersistentVolumeClaim", "home-test-revert"):
        if get(pvc, "spec", "dataSource", "name") != "home-test-snap":
            errors.append("home-test-revert must come from home-test-snap")
    check_job(objs, errors, "revert-copy", {("home-test-revert", True), ("home-test", False)})


def check(stage, objs, require_target=False):
    errors = []
    if stage == "rest-server-policy":
        return check_rest_server_policy(objs)
    if stage not in EXPECTED:
        return [f"unknown stage {stage!r}"]
    check_common(stage, objs, errors)
    if stage == "restore":
        check_restore(objs, errors, require_target)
    elif stage == "fix-owner":
        check_job(objs, errors, "fix-owner", {("home-test", False)})
    elif stage == "boot":
        check_boot(objs, errors)
    elif stage == "revert-snapshot":
        check_revert_snapshot(objs, errors)
    elif stage == "revert-copy":
        check_revert_copy(objs, errors)
    return errors


def read_objects(stream):
    objs = []
    for line in stream:
        line = line.strip()
        if not line or line == "null":
            continue
        obj = json.loads(line)
        if isinstance(obj, dict):
            objs.append(obj)
    return objs


def self_test(renders):
    """Mutations of the good renders that must each be refused."""
    def mutate(stage, fn):
        objs = copy.deepcopy(renders[stage])
        fn(objs)
        return check(stage, objs)

    def find(objs, kind, name=None):
        return by_kind(objs, kind, name)[0]

    def egress(objs):
        return find(objs, "NetworkPolicy", "hogwarts-restore-egress")["spec"]["egress"]

    def ingress_peer(objs):
        return find(objs, "NetworkPolicy", "hogwarts-restore-ingress")["spec"]["ingress"][0]["from"][0]

    def job_container(objs):
        return find(objs, "Job")["spec"]["template"]["spec"]["containers"][0]

    def rest_rule(objs):
        return find(objs, "NetworkPolicy", "restic-rest-server-ingress")["spec"]["ingress"][0]

    def rest_peer(objs, namespace):
        return next(p for p in rest_rule(objs)["from"]
                    if get(p, "namespaceSelector", "matchLabels", "kubernetes.io/metadata.name") == namespace)

    def rest_mover(objs):
        return next(p for p in rest_rule(objs)["from"] if p.get("podSelector") == {"matchLabels": MOVER_LABELS})

    cases = {
        "internet egress": ("boot", lambda o: find(o, "NetworkPolicy", "hogwarts-restore-egress")["spec"]["egress"].append(
            {"to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}]})),
        "nextcloud egress": ("boot", lambda o: find(o, "NetworkPolicy", "hogwarts-restore-egress")["spec"]["egress"].append(
            {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "nextcloud"}}}],
             "ports": [{"protocol": "TCP", "port": 80}]})),
        "VM Always": ("boot", lambda o: find(o, "VirtualMachine")["spec"].update(runStrategy="Always")),
        "live home disk": ("boot", lambda o: find(o, "VirtualMachine")["spec"]["template"]["spec"]["volumes"][1].update(
            persistentVolumeClaim={"claimName": "hogwarts-home"})),
        "live namespace": ("boot", lambda o: find(o, "Service")["metadata"].update(namespace="hogwarts")),
        "VM renamed": ("boot", lambda o: find(o, "VirtualMachine")["metadata"].update(name="hogwarts")),
        "hostname": ("boot", lambda o: find(o, "ExternalSecret")["spec"]["target"]["template"]["data"].update(
            userData="#cloud-config\nhostname: hogwarts\nfs_setup:\n  - partition: auto\n")),
        "partition none": ("boot", lambda o: find(o, "ExternalSecret")["spec"]["target"]["template"]["data"].update(
            userData="#cloud-config\nhostname: hogwarts-restore\nfs_setup:\n  - partition: none\n")),
        "telegram key": ("boot", lambda o: find(o, "ExternalSecret")["spec"]["data"].append(
            {"secretKey": "t", "remoteRef": {"key": "HOGWARTS_HERMIONE_TELEGRAM_BOT_TOKEN"}})),
        "extra object": ("boot", lambda o: o.append(
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "x", "namespace": NS}})),
        "secret": ("restore", lambda o: o.append(
            {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "x", "namespace": NS}})),
        "continuous trigger": ("restore", lambda o: find(o, "ReplicationDestination")["spec"].update(trigger={})),
        "restore into live": ("restore", lambda o: find(o, "ReplicationDestination")["spec"]["restic"].update(
            destinationPVC="hogwarts-home")),
        "wide rolebinding": ("restore", lambda o: find(o, "RoleBinding")["subjects"].append(
            {"kind": "Group", "name": "system:authenticated"})),
        "job SCC": ("fix-owner", lambda o: find(o, "Job")["spec"]["template"]["metadata"].update(annotations={})),
        "revert source writable": ("revert-copy", lambda o: find(o, "Job")["spec"]["template"]["spec"]["volumes"][0][
            "persistentVolumeClaim"].update(readOnly=False)),
        # NetworkPolicy peers compared whole (namespaceSelector + podSelector + ports).
        "DNS peer without podSelector": ("boot", lambda o: egress(o)[0]["to"][0].pop("podSelector")),
        "LiteLLM peer without podSelector": ("boot", lambda o: egress(o)[1]["to"][0].pop("podSelector")),
        "LiteLLM podSelector widened": ("boot", lambda o: egress(o)[1]["to"][0].update(podSelector={})),
        "LiteLLM peer without namespaceSelector": ("boot", lambda o: egress(o)[1]["to"][0].pop(
            "namespaceSelector")),
        "DNS rule without to": ("boot", lambda o: egress(o)[0].pop("to")),
        "LiteLLM extra port": ("boot", lambda o: egress(o)[1]["ports"].append({"protocol": "TCP", "port": 22})),
        "DNS rule without ports": ("boot", lambda o: egress(o)[0].pop("ports")),
        "ingress without namespaceSelector": ("boot", lambda o: ingress_peer(o).pop("namespaceSelector")),
        "ingress namespaceSelector widened": ("boot", lambda o: ingress_peer(o).update(namespaceSelector={})),
        "ingress launcher label dropped": ("boot", lambda o: ingress_peer(o)["podSelector"]["matchLabels"].pop(
            "kubevirt.io")),
        "ingress extra peer": ("boot", lambda o: find(o, "NetworkPolicy", "hogwarts-restore-ingress")["spec"][
            "ingress"][0]["from"].append({"podSelector": {}})),
        # ExternalSecrets: never a whole Doppler config, pinned store and target.
        "restic dataFrom extract": ("restore", lambda o: find(o, "ExternalSecret")["spec"].update(
            dataFrom=[{"extract": {"key": "homelab"}}])),
        "restic template": ("restore", lambda o: find(o, "ExternalSecret")["spec"]["target"].update(
            template={"data": {"x": "{{ .RESTIC_PASSWORD }}"}})),
        "restic other store": ("restore", lambda o: find(o, "ExternalSecret")["spec"]["secretStoreRef"].update(
            name="other-store")),
        "restic other target": ("restore", lambda o: find(o, "ExternalSecret")["spec"]["target"].update(
            name="restic-config-other")),
        "restic extra key": ("restore", lambda o: find(o, "ExternalSecret")["spec"]["data"].append(
            {"secretKey": "X", "remoteRef": {"key": "LITELLM_MASTER_KEY"}})),
        "restic remoteRef property": ("restore", lambda o: find(o, "ExternalSecret")["spec"]["data"][0][
            "remoteRef"].update(property="x")),
        "cloud-init dataFrom": ("boot", lambda o: find(o, "ExternalSecret")["spec"].update(
            dataFrom=[{"find": {"name": {"regexp": ".*"}}}])),
        "cloud-init extra key": ("boot", lambda o: find(o, "ExternalSecret")["spec"]["data"].append(
            {"secretKey": "k", "remoteRef": {"key": "HOGWARTS_HOME_REST_REPO_LOCAL"}})),
        # VM network and helper Jobs pinned.
        "VM bridge interface": ("boot", lambda o: find(o, "VirtualMachine")["spec"]["template"]["spec"]["domain"][
            "devices"].update(interfaces=[{"name": "default", "bridge": {}}])),
        "VM second interface": ("boot", lambda o: find(o, "VirtualMachine")["spec"]["template"]["spec"]["domain"][
            "devices"]["interfaces"].append({"name": "x", "masquerade": {}})),
        "job hostNetwork": ("fix-owner", lambda o: find(o, "Job")["spec"]["template"]["spec"].update(
            hostNetwork=True)),
        "job privileged": ("revert-copy", lambda o: job_container(o)["securityContext"].update(privileged=True)),
        "job other command": ("fix-owner", lambda o: job_container(o)["command"].__setitem__(
            2, "chown -R 107:107 /restore")),
        "job args": ("fix-owner", lambda o: job_container(o).update(args=["x"])),
        "job not root": ("revert-copy", lambda o: job_container(o).update(securityContext={"runAsUser": 1000})),
        "job image tag only": ("fix-owner", lambda o: job_container(o).update(image=JOB_IMAGE_PREFIX + "latest")),
        "job init container": ("fix-owner", lambda o: find(o, "Job")["spec"]["template"]["spec"].update(
            initContainers=[{"name": "x", "image": "busybox"}])),
        "job env": ("revert-copy", lambda o: job_container(o).update(
            envFrom=[{"secretRef": {"name": "restic-config-home"}}])),
        "job mount writable": ("revert-copy", lambda o: job_container(o)["volumeMounts"][0].pop("readOnly")),
        # rest-server policy: hogwarts-restore only in the mover peer.
        "rest-server: in the knowhere peer": ("rest-server-policy", lambda o: rest_peer(o, "knowhere")[
            "namespaceSelector"].update(matchExpressions=[{"key": "kubernetes.io/metadata.name",
                                                            "operator": "In", "values": [NS]}])),
        "rest-server: own peer without podSelector": ("rest-server-policy", lambda o: rest_rule(o)["from"].append(
            {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": NS}}})),
        "rest-server: mover peer without podSelector": ("rest-server-policy", lambda o: rest_mover(o).pop(
            "podSelector")),
        "rest-server: mover podSelector widened": ("rest-server-policy", lambda o: rest_mover(o).update(
            podSelector={})),
        "rest-server: second policy rule": ("rest-server-policy", lambda o: find(
            o, "NetworkPolicy", "restic-rest-server-ingress")["spec"]["ingress"].append(
            {"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": NS}},
                       "podSelector": {}}]})),
    }
    failed = 0
    for label, (stage, fn) in cases.items():
        errors = mutate(stage, fn)
        if errors:
            print(f"ok    refused: {label}")
        else:
            print(f"FAIL  accepted: {label}")
            failed += 1
    # The sentinel check, both ways.
    objs = copy.deepcopy(renders["restore"])
    if not check("restore", objs, require_target=True):
        print("FAIL  accepted: sentinel restoreAsOf with --require-restore-target")
        failed += 1
    else:
        print("ok    refused: sentinel restoreAsOf with --require-restore-target")
    rd = find(objs, "ReplicationDestination")
    rd["spec"]["restic"]["restoreAsOf"] = "2026-11-08T20:00:00Z"
    rd["spec"]["trigger"]["manual"] = "restore-20261108T200000Z"
    if check("restore", objs, require_target=True):
        print("FAIL  refused: a set restoreAsOf with --require-restore-target")
        failed += 1
    else:
        print("ok    accepted: a set restoreAsOf with --require-restore-target")
    return failed


def main(argv):
    if argv[1:2] == ["--self-test"]:
        # stdin: "<stage>\t<json object>" lines of the good renders.
        renders = {s: [] for s in (*EXPECTED, "rest-server-policy")}
        for line in sys.stdin:
            if not line.strip():
                continue
            stage, _, payload = line.partition("\t")
            renders[stage].extend(read_objects([payload]))
        for stage, objs in renders.items():
            errors = check(stage, objs)
            if errors:
                print(f"FAIL  the good render of {stage} is refused: {errors}")
                return 1
        return 1 if self_test(renders) else 0
    if (len(argv) not in (2, 3) or (len(argv) == 3 and argv[2] != "--require-restore-target")
            or (len(argv) == 3 and argv[1] != "restore")):
        print((__doc__ or "").strip().splitlines()[2], file=sys.stderr)
        return 2
    errors = check(argv[1], read_objects(sys.stdin), require_target=len(argv) == 3)
    for e in errors:
        print(f"FAIL  {argv[1]}: {e}")
    if not errors:
        print(f"PASS  {argv[1]}: drill objects within the guard rails")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
