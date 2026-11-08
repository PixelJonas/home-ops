#!/usr/bin/env python3
"""Assertions on the rendered Hogwarts restore-drill objects.

    check_hogwarts_drill.py <stage> [--require-restore-target] < objects.jsonl
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
    no ReplicationSource, no Telegram or Tailscale reference;
  - the VM is hogwarts-restore, Halted, home disk = PVC home-test, pod network
    only;
  - egress = cluster DNS + LiteLLM only (no ipBlock, no Nextcloud, Paperless,
    Qdrant, Gitea); ingress = SSH from the knowhere VM only;
  - cloud-init keeps partition: auto and sets hostname hogwarts-restore;
  - the helper Jobs run as drill-helper with anyuid pinned and touch only the
    scratch claims.
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
    for es in by_kind(objs, "ExternalSecret", "home-external-restic-config"):
        keys = sorted(str(get(d, "remoteRef", "key")) for d in get(es, "spec", "data", default=[]))
        if keys != ["HOGWARTS_HOME_REST_REPO_LOCAL", "RESTIC_PASSWORD"]:
            errors.append(f"restic ExternalSecret reads unexpected keys: {keys}")
        if get(es, "spec", "target", "name") != "restic-config-home":
            errors.append("restic ExternalSecret must target restic-config-home")
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
        rules = spec.get("egress", [])
        allowed = []
        for rule in rules:
            for peer in rule.get("to", []):
                if "ipBlock" in peer:
                    errors.append("egress policy has an ipBlock rule (internet or LAN)")
                allowed.append(str(get(peer, "namespaceSelector", "matchLabels", "kubernetes.io/metadata.name")))
        if sorted(allowed) != ["litellm", "openshift-dns"] or len(rules) != 2:
            errors.append(f"egress must allow only openshift-dns and litellm, got {allowed}")
        ports = {get(peer, "namespaceSelector", "matchLabels", "kubernetes.io/metadata.name"):
                 sorted((p.get("protocol"), p.get("port")) for p in rule.get("ports", []))
                 for rule in rules for peer in rule.get("to", [])}
        if ports.get("openshift-dns") not in (None, [("TCP", 5353), ("UDP", 5353)]):
            errors.append("DNS rule must be port 5353 only")
        if ports.get("litellm") not in (None, [("TCP", 4000)]):
            errors.append("LiteLLM rule must be TCP 4000 only")
    for np in by_kind(objs, "NetworkPolicy", "hogwarts-restore-ingress"):
        spec = get(np, "spec", default={})
        ok = (spec.get("policyTypes") == ["Ingress"]
              and get(spec, "podSelector", "matchLabels") == {"vm.kubevirt.io/name": "hogwarts-restore"}
              and len(spec.get("ingress", [])) == 1
              and get(spec, "ingress", 0, "ports") == [{"protocol": "TCP", "port": 22}]
              and get(spec, "ingress", 0, "from", 0, "podSelector", "matchLabels", "vm.kubevirt.io/name") == "knowhere"
              and len(get(spec, "ingress", 0, "from", default=[])) == 1)
        if not ok:
            errors.append("ingress policy must admit only the knowhere VM on TCP 22 to hogwarts-restore")
    for svc in by_kind(objs, "Service", "hogwarts-restore-ssh"):
        if get(svc, "spec", "selector") != {"vm.kubevirt.io/name": "hogwarts-restore"}:
            errors.append("SSH Service must select only hogwarts-restore")
        if get(svc, "spec", "type", default="ClusterIP") != "ClusterIP":
            errors.append("SSH Service must be ClusterIP")


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
        renders = {s: [] for s in EXPECTED}
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
    if len(argv) not in (2, 3) or (len(argv) == 3 and argv[2] != "--require-restore-target"):
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
