#!/usr/bin/env python3
"""
Headless tests for manifest_gen (headless).

  python3 tests/test_generator.py              # parse + base64 + validation checks
  python3 tests/test_generator.py --kubectl    # also `kubectl apply --dry-run=client` each file
  python3 tests/test_generator.py --kubectl --server   # + --dry-run=server (needs a cluster; creates ns demo)

Generated manifests are written to tests/output/.
"""
import base64
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import yaml  # noqa: E402

import manifest_gen as gen  # noqa: E402

OUT = os.path.join(HERE, "output")

TLS_CRT = "-----BEGIN CERTIFICATE-----\nMIIBszCCAVmgAwIBAgIUQ\n-----END CERTIFICATE-----\n"
TLS_KEY = "-----BEGIN PRIVATE KEY-----\nMIGHAgEAMBMGByqGSM49\n-----END PRIVATE KEY-----\n"

CASES = {
    "Deployment": {
        "name": "web", "namespace": "demo", "labels": "app=web\ntier=frontend",
        "annotations": "owner=john\ndescription=demo web app, with commas",
        "image": "nginx:1.27", "image_pull_policy": "Always", "replicas": "3",
        "ports": "http:80\nmetrics:9113/TCP", "env": "LOG_LEVEL=debug\nPORT=8080\nDB_PASS=@secret:db-secret/password\nCFG=@configmap:web-cfg/mode",
        "env_from": "configmap:web-cfg", "cpu_request": "250m", "memory_request": "256Mi",
        "cpu_limit": "1", "memory_limit": "512Mi",
        "volumes": "data:pvc:web-data:/data\ncfg:configMap:web-cfg:/etc/web:ro\nsec:secret:web-secret:/etc/secret:ro\ntmp:emptyDir::/tmp\ncache:emptyDir:Memory:/cache",
        "command": "nginx", "args": "-g\ndaemon off;", "service_account": "web-sa",
        "node_selector": "kubernetes.io/os=linux", "readiness_path": "/", "liveness_path": "/",
        "strategy": "RollingUpdate", "max_surge": "1", "max_unavailable": "0",
        "image_pull_secrets": "regcred",
    },
    "StatefulSet": {
        "name": "postgres", "namespace": "demo", "replicas": "3", "service_name": "postgres",
        "image": "postgres:16.4", "ports": "pg:5432",
        "env": "POSTGRES_PASSWORD=@secret:pg-secret/password\nPGDATA=/var/lib/postgresql/data/pgdata",
        "strategy": "RollingUpdate", "partition": "0", "pod_management": "Parallel",
        "vct_name": "data", "vct_size": "20Gi", "vct_storage_class": "gp3", "vct_access_mode": "ReadWriteOnce",
    },
    "DaemonSet": {
        "name": "log-agent", "namespace": "kube-system", "image": "fluent/fluent-bit:3.1",
        "strategy": "RollingUpdate", "max_unavailable": "1", "host_network": True, "tolerate_all": True,
        "volumes": "varlog:hostPath:/var/log:/var/log:ro", "ports": "metrics:2020",
        "cpu_limit": "200m", "memory_limit": "128Mi", "cpu_request": "50m", "memory_request": "64Mi",
    },
    "Job": {
        "name": "db-migrate", "namespace": "demo", "image": "busybox:1.36",
        "command": "sh -c 'echo migrating && sleep 5'", "restart_policy": "Never",
        "backoff_limit": "4", "completions": "3", "parallelism": "2",
        "active_deadline": "600", "ttl_after_finished": "3600",
    },
    "CronJob": {
        "name": "nightly-backup", "namespace": "demo", "image": "busybox:1.36",
        "schedule": "30 2 * * *", "time_zone": "America/Los_Angeles", "concurrency_policy": "Forbid",
        "restart_policy": "OnFailure", "command": "sh -c 'date; echo backup'",
        "successful_history": "5", "failed_history": "2", "backoff_limit": "2", "starting_deadline": "300",
        "volumes": "backup:pvc:backup-pvc:/backup",
    },
    "Service": {
        "name": "web", "namespace": "demo", "service_type": "NodePort", "selector": "app=web",
        "port_name": "http", "port": "80", "target_port": "http", "node_port": "30080", "protocol": "TCP",
        "extra_ports": "metrics:9113:9113:30913", "external_traffic_policy": "Local",
    },
    "Ingress": {
        "name": "web", "namespace": "demo", "ingress_class": "nginx", "host": "web.example.com",
        "path": "/", "path_type": "Prefix", "backend_service": "web", "backend_port": "80",
        "extra_rules": "- /api Prefix api 8080\nadmin.example.com / Exact admin http",
        "tls_secret": "web-tls", "annotations": "nginx.ingress.kubernetes.io/ssl-redirect=true",
    },
    "ConfigMap": {
        "name": "web-cfg", "namespace": "demo", "data": "mode=production\nTIMEOUT=30\nGREETING=hello\\nworld",
        "file_key": "nginx.conf", "file_content": "server {\n  listen 80;\n  location / { return 200 'ok'; }\n}\n",
    },
    "Secret": {
        "name": "db-secret", "namespace": "demo", "secret_type": "Opaque",
        "data": "username=admin\npassword=S3cr3t!=with=equals\nurl=postgres://admin@db:5432/app",
    },
    "PersistentVolumeClaim": {
        "name": "web-data", "namespace": "demo", "size": "5Gi", "access_mode": "ReadWriteMany",
        "storage_class": "local-path", "volume_mode": "Filesystem",
    },
    "Namespace": {"name": "demo", "labels": "team=platform\nenv=dev", "pss_enforce": "baseline"},
    "NetworkPolicy": {
        "name": "web-policy", "namespace": "demo", "pod_selector": "app=web", "policy_ingress": True,
        "policy_egress": True, "np_allow_dns": True,
        "ingress_rules": [
            {"peer": "pods (podSelector)", "pod_labels": "app=frontend", "ports": "80/TCP, 443", "rule": "1"},
            {"peer": "namespaces (namespaceSelector)", "ns_labels": "kubernetes.io/metadata.name=monitoring",
             "ports": "metrics", "rule": "2"},
            {"peer": "ipBlock (CIDR)", "cidr": "10.0.0.0/8", "except_cidrs": "10.1.0.0/16", "ports": "8000-8080/TCP"}],
        "egress_rules": [{"peer": "pods in namespaces", "pod_labels": "app=postgres", "ns_labels": "team=data",
                          "ports": "5432/TCP"}],
    },
    "StorageClass": {
        "name": "fast", "provisioner": "ebs.csi.aws.com", "parameters": "type=gp3\niops=6000\nencrypted=true",
        "reclaim_policy": "Retain", "binding_mode": "WaitForFirstConsumer", "allow_expansion": True,
        "mount_options": "noatime", "default_class": False,
        "allowed_topologies": [{"key": "topology.kubernetes.io/zone", "values": "us-east-1a, us-east-1b"}],
    },
    "VolumeSnapshotClass": {
        "name": "ebs-snap", "driver": "ebs.csi.aws.com", "deletion_policy": "Retain", "default_snapshot_class": True,
        "parameters": "tagSpecification_1=team=data",
    },
}

# NetworkPolicy quick presets: (overrides, check on the manifest, description)
NP_PRESETS = [
    ({"pod_selector": "", "np_default_deny": "all", "policy_ingress": False},
     lambda m: m["spec"] == {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}, "default-deny-all"),
    ({"pod_selector": "", "np_default_deny": "ingress", "policy_ingress": False},
     lambda m: m["spec"] == {"podSelector": {}, "policyTypes": ["Ingress"]}, "default-deny-ingress"),
    ({"np_allow_same_ns": True}, lambda m: {"from": [{"podSelector": {}}]} in m["spec"]["ingress"],
     "allow-same-namespace"),
    ({"np_allow_dns": True, "policy_egress": True},
     lambda m: any(p.get("port") == 53 and p["protocol"] == "UDP" for r in m["spec"]["egress"]
                   for p in r.get("ports", [])) and "Egress" in m["spec"]["policyTypes"], "allow-DNS-egress"),
    ({"np_allow_namespaces": "monitoring, ingress-nginx"},
     lambda m: m["spec"]["ingress"][0]["from"][0]["namespaceSelector"]["matchExpressions"][0]["values"]
     == ["monitoring", "ingress-nginx"], "allow-namespaces"),
]
NEW_KIND_NEGATIVE = [
    ("NetworkPolicy", {"ingress_rules": [{"peer": "ipBlock (CIDR)", "cidr": "10.0.0.1/8"}]}, "host bits"),
    ("NetworkPolicy", {"ingress_rules": [{"peer": "pods (podSelector)", "ports": "99999"}]}, "1-65535"),
    ("NetworkPolicy", {"np_allow_namespaces": "Bad_NS"}, "DNS-1123"),
    ("StorageClass", {"provisioner": ""}, "provisioner"),
    ("StorageClass", {"parameters": "novalue"}, "key=value"),
    ("VolumeSnapshotClass", {"driver": ""}, "driver"),
]

EXTRA_SECRETS = {
    "tls": {"name": "web-tls", "namespace": "demo", "secret_type": "kubernetes.io/tls", "data": "",
            "file_key": "tls.crt", "file_content": TLS_CRT, "file_key2": "tls.key", "file_content2": TLS_KEY},
    "docker": {"name": "regcred", "namespace": "demo", "secret_type": "kubernetes.io/dockerconfigjson", "data": "",
               "registry_server": "ghcr.io", "registry_user": "john", "registry_password": "tok3n",
               "registry_email": "john@example.com"},
    "basic": {"name": "basic", "namespace": "demo", "secret_type": "kubernetes.io/basic-auth",
              "data": "username=john\npassword=pw"},
    "ssh": {"name": "git-ssh", "namespace": "demo", "secret_type": "kubernetes.io/ssh-auth", "data": "",
            "file_key": "ssh-privatekey", "file_content": "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----\n"},
    "satoken": {"name": "build-robot-token", "namespace": "demo",
                "secret_type": "kubernetes.io/service-account-token", "data": "", "sa_token_account": "build-robot"},
}

# (kind, overrides, substring expected in at least one error)
NEGATIVE = [
    ("Deployment", {"name": "My_App"}, "DNS-1123"),
    ("Deployment", {"replicas": "three"}, "whole number"),
    ("Deployment", {"replicas": "-1"}, ">= 0"),
    ("Deployment", {"image": ""}, "Image is required"),
    ("Deployment", {"cpu_request": "2", "cpu_limit": "500m"}, "must not exceed"),
    ("Deployment", {"memory_limit": "lots"}, "quantity"),
    ("Deployment", {"ports": "99999"}, "between 1 and 65535"),
    ("Deployment", {"env": "1BAD=x"}, "Env var name"),
    ("Deployment", {"volumes": "data:nfs:x:/d"}, "unknown type"),
    ("Deployment", {"restart_policy": "Never"}, "not allowed"),
    ("Job", {"restart_policy": "Always"}, "not allowed"),
    ("CronJob", {"restart_policy": "Always"}, "not allowed"),
    ("CronJob", {"schedule": "every 5 minutes"}, "5 fields"),
    ("CronJob", {"name": "x" * 53}, "too long"),
    ("Service", {"name": "1web"}, "DNS-1035"),
    ("Service", {"service_type": "ClusterIP", "node_port": "30080"}, "nodePort is only valid"),
    ("Service", {"service_type": "ExternalName", "external_name": ""}, "externalName is required"),
    ("Service", {"extra_ports": ":81:81"}, "every port must have a name"),
    ("Ingress", {"host": "https://x.com"}, "DNS name"),
    ("Ingress", {"path": "api"}, "must start with '/'"),
    ("Secret", {"secret_type": "kubernetes.io/tls", "data": ""}, "requires key"),
    ("Secret", {"already_base64": True, "data": "a=not base64!"}, "not valid base64"),
    ("PersistentVolumeClaim", {"size": ""}, "size is required"),
    ("PersistentVolumeClaim", {"size": "10GB"}, "quantity"),
    ("Namespace", {"name": "a.b"}, "DNS-1123 label"),
    ("ConfigMap", {"data": "bad key=1"}, "invalid"),
    ("StatefulSet", {"service_name": ""}, "serviceName is required"),
]

SC = "sidecar (regular container)"
NATIVE = "sidecar (native)"
ADVANCED = {
    "deploy-multi": ("Deployment", {
        "name": "web", "namespace": "demo", "labels": "app=web", "image": "nginx:1.27", "ports": "http:80",
        "volumes": "shared:emptyDir::/shared\nlogs:emptyDir::",
        "node_selector": "kubernetes.io/os=linux",
        "extra_containers": [
            {"role": NATIVE, "name": "mesh-proxy", "image": "envoyproxy/envoy:v1.31.0", "ports": "proxy:15001",
             "args": "--config-path\n/etc/envoy/envoy.yaml", "volume_mounts": "shared:/etc/envoy:ro"},
            {"role": "init", "name": "init-db", "image": "busybox:1.36",
             "command": "sh -c 'until nslookup db; do sleep 2; done'"},
            {"role": SC, "name": "log-shipper", "image": "fluent/fluent-bit:3.1",
             "volume_mounts": "logs:/var/log/app:ro", "env": "FLB_LOG_LEVEL=info"},
            {"role": "main", "name": "metrics", "image": "prom/statsd-exporter:v0.27.1", "ports": "metrics:9102",
             "cpu_request": "10m", "memory_request": "16Mi", "memory_limit": "32Mi"},
        ],
        "tolerations": [
            {"key": "dedicated", "operator": "Equal", "value": "web", "effect": "NoSchedule"},
            {"key": "node.kubernetes.io/unreachable", "operator": "Exists", "effect": "NoExecute",
             "toleration_seconds": "60"},
            {"key": "spot", "operator": "Exists", "effect": ""},
        ],
        "node_affinity": [
            {"type": "required", "key": "kubernetes.io/arch", "operator": "In", "values": "amd64,arm64", "term": "1"},
            {"type": "required", "key": "example.com/cpu-gen", "operator": "Gt", "values": "3", "term": "1"},
            {"type": "required", "key": "node.kubernetes.io/instance-type", "operator": "In",
             "values": "m5.large, m5.xlarge", "term": "2"},
            {"type": "preferred", "key": "topology.kubernetes.io/zone", "operator": "In",
             "values": "us-west-2a,us-west-2b", "weight": "30"},
        ],
        "pod_affinity": [{"type": "required", "key": "app", "operator": "In", "values": "cache",
                          "topology_key": "topology.kubernetes.io/zone", "namespaces": "demo,cache"}],
        "pod_anti_affinity": [
            {"type": "preferred", "key": "app", "operator": "In", "values": "web",
             "topology_key": "kubernetes.io/hostname", "weight": "100"},
            {"type": "required", "key": "tier", "operator": "Exists", "values": "",
             "topology_key": "kubernetes.io/hostname"},
        ],
        "topology_spread": [
            {"max_skew": "1", "topology_key": "topology.kubernetes.io/zone", "when_unsatisfiable": "ScheduleAnyway"},
            {"max_skew": "2", "topology_key": "kubernetes.io/hostname", "when_unsatisfiable": "DoNotSchedule",
             "match_labels": "app=web, tier=frontend"},
        ],
    }),
    "sts-sidecar-vct": ("StatefulSet", {
        "name": "postgres", "namespace": "demo", "service_name": "postgres", "image": "postgres:16.4",
        "vct_name": "data", "vct_size": "5Gi",
        "extra_containers": [{"role": NATIVE, "name": "backup-agent", "image": "busybox:1.36",
                              "command": "sh -c 'while true; do sleep 3600; done'",
                              "volume_mounts": "data:/var/lib/postgresql/data:ro"}],
        "pod_anti_affinity": [{"type": "required", "key": "app", "operator": "In", "values": "postgres",
                               "topology_key": "kubernetes.io/hostname"}],
    }),
    "daemonset-tolerations": ("DaemonSet", {
        "name": "node-exporter", "namespace": "demo", "image": "prom/node-exporter:v1.8.2", "tolerate_all": True,
        "tolerations": [{"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"}],
        "node_affinity": [{"type": "required", "key": "kubernetes.io/os", "operator": "In", "values": "linux"}],
    }),
    "job-native-sidecar": ("Job", {
        "name": "etl", "namespace": "demo", "image": "busybox:1.36", "restart_policy": "Never",
        "command": "sh -c 'echo working; sleep 3'", "volumes": "work:emptyDir::/work",
        "extra_containers": [{"role": NATIVE, "name": "log-forwarder", "image": "busybox:1.36",
                              "command": "sh -c 'tail -F /work/out.log'", "volume_mounts": "work:/work"}],
    }),
    "cronjob-init-tolerations": ("CronJob", {
        "name": "report", "namespace": "demo", "image": "busybox:1.36", "schedule": "0 6 * * 1",
        "volumes": "in:emptyDir::/in",
        "extra_containers": [{"role": "init", "name": "fetch", "image": "curlimages/curl:8.10.1",
                              "command": "curl -so /in/data.csv https://example.com/data.csv",
                              "volume_mounts": "in:/in"}],
        "tolerations": [{"key": "batch", "operator": "Equal", "value": "true", "effect": "PreferNoSchedule"}],
        "pod_affinity": [{"type": "preferred", "key": "app", "operator": "In", "values": "db",
                          "topology_key": "kubernetes.io/hostname", "weight": "50"}],
    }),
}

ADV_NEGATIVE = [
    ("Deployment", {"name": "web", "extra_containers": [{"name": "web", "image": "busybox:1.36"}]}, "must be unique"),
    ("Deployment", {"extra_containers": [{"name": "a", "image": "x:1"}, {"role": "init", "name": "a", "image": "x:1"}]},
     "must be unique"),
    ("Deployment", {"extra_containers": [{"name": "a", "image": "x:1", "volume_mounts": "missing:/m"}]},
     "not defined in Volumes"),
    ("Deployment", {"volumes": "v:emptyDir::", "extra_containers": [{"name": "a", "image": "x:1", "volume_mounts": "v:rel"}]},
     "must be absolute"),
    ("Deployment", {"extra_containers": [{"name": "a", "image": ""}]}, "Image is required"),
    ("Deployment", {"extra_containers": [{"name": "Bad_Name", "image": "x:1"}]}, "DNS-1123 label"),
    ("Deployment", {"extra_containers": [{"name": "a", "image": "x:1", "role": "helper"}]}, "role must be one of"),
    ("Deployment", {"extra_containers": [{"name": "a", "image": "x:1", "cpu_request": "2", "cpu_limit": "1"}]},
     "must not exceed"),
    ("Job", {"tolerations": [{"key": "a", "operator": "Exists", "value": "v"}]}, "value must be empty"),
    ("Job", {"tolerations": [{"key": "", "operator": "Equal"}]}, "requires operator Exists"),
    ("Job", {"tolerations": [{"key": "a", "operator": "Exists", "effect": "NoSchedule", "toleration_seconds": "5"}]},
     "only allowed with effect NoExecute"),
    ("Job", {"tolerations": [{"key": "a", "operator": "Exists", "effect": "NoExecute", "toleration_seconds": "x"}]},
     "whole number"),
    ("CronJob", {"node_affinity": [{"key": "a", "operator": "In", "values": ""}]}, "needs at least one value"),
    ("CronJob", {"node_affinity": [{"key": "a", "operator": "Exists", "values": "x"}]}, "must not have values"),
    ("CronJob", {"node_affinity": [{"key": "a", "operator": "Lt", "values": "abc"}]}, "exactly one integer"),
    ("CronJob", {"node_affinity": [{"key": "", "operator": "Exists"}]}, "key is required"),
    ("StatefulSet", {"node_affinity": [{"type": "preferred", "key": "a", "operator": "Exists", "weight": "0"}]}, ">= 1"),
    ("StatefulSet", {"pod_affinity": [{"type": "preferred", "key": "app", "operator": "In", "values": "x",
                                       "topology_key": "kubernetes.io/hostname", "weight": "101"}]}, "<= 100"),
    ("StatefulSet", {"pod_anti_affinity": [{"key": "app", "operator": "In", "values": "x", "topology_key": ""}]},
     "topologyKey is required"),
    ("DaemonSet", {"pod_affinity": [{"key": "app", "operator": "Gt", "values": "1",
                                     "topology_key": "kubernetes.io/hostname"}]}, "operator must be one of"),
    ("DaemonSet", {"topology_spread": [{"max_skew": "0", "topology_key": "kubernetes.io/hostname"}]}, ">= 1"),
    ("Job", {"labels": "", "topology_spread": [{"max_skew": "1", "topology_key": "kubernetes.io/hostname"}]},
     "match labels are required"),
]

ROW_CHECKS = [
    ("extra_containers", {"name": "", "image": "x:1"}, "name is required"),
    ("tolerations", {"key": "a", "operator": "Exists", "value": "b"}, "value must be empty"),
    ("node_affinity", {"key": "a", "operator": "Gt", "values": "1,2"}, "exactly one integer"),
    ("pod_anti_affinity", {"key": "app", "operator": "In", "values": "", "topology_key": "kubernetes.io/hostname"},
     "at least one value"),
    ("topology_spread", {"max_skew": "x", "topology_key": "kubernetes.io/hostname"}, "whole number"),
]

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL: {msg}")


def is_b64(s):
    try:
        base64.b64decode(s, validate=True)
        return True
    except Exception:
        return False


def write(manifest, fname=None):
    fname = fname or gen.filename_for(manifest)
    path = os.path.join(OUT, fname)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(gen.to_yaml(manifest))
    return path


def main():
    use_kubectl = "--kubectl" in sys.argv
    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(OUT)
    files = []

    print("== Positive cases (one per kind) ==")
    for kind in gen.KINDS:
        res = gen.generate(kind, CASES[kind])
        check(res.ok, f"{kind} should validate, errors={res.errors}")
        if not res.ok:
            continue
        text = gen.to_yaml(res.manifest)
        doc = yaml.safe_load(text)
        check(doc == res.manifest, f"{kind} YAML round-trip mismatch")
        check(doc["kind"] == kind and doc["apiVersion"] == gen.API_VERSIONS[kind], f"{kind} kind/apiVersion")
        check(doc["metadata"]["name"] == CASES[kind]["name"], f"{kind} name")
        files.append(write(res.manifest))
        print(f"  {kind:22s} OK  {gen.filename_for(res.manifest):40s} warnings={res.warnings}")

    print("== Defaults (every kind as shipped) ==")
    for kind in gen.KINDS:
        res = gen.generate(kind)
        check(res.ok, f"default {kind} should validate: {res.errors}")
        yaml.safe_load(gen.to_yaml(res.manifest))
        files.append(write(res.manifest, f"default-{gen.filename_for(res.manifest)}"))
    print(f"  {len(gen.KINDS)} default manifests OK")

    print("== Specific assertions ==")
    d = gen.generate("Deployment", CASES["Deployment"]).manifest
    c = d["spec"]["template"]["spec"]["containers"][0]
    check(d["spec"]["replicas"] == 3, "replicas int")
    check(d["spec"]["selector"]["matchLabels"] == {"app": "web", "tier": "frontend"}, "selector")
    check(c["args"] == ["-g", "daemon off;"], f"args multi-line {c.get('args')}")
    check(c["env"][1] == {"name": "PORT", "value": "8080"}, "env value stays string")
    check(c["env"][2]["valueFrom"]["secretKeyRef"] == {"name": "db-secret", "key": "password"}, "secretKeyRef")
    check(c["ports"][1] == {"name": "metrics", "containerPort": 9113, "protocol": "TCP"}, "ports")
    check(c["readinessProbe"]["httpGet"]["port"] == "http", "probe port defaults to first named port")
    check(d["metadata"]["annotations"]["description"] == "demo web app, with commas", "annotation commas")
    vols = {v["name"]: v for v in d["spec"]["template"]["spec"]["volumes"]}
    check(vols["cache"]["emptyDir"] == {"medium": "Memory"}, "emptyDir Memory")
    check(vols["sec"]["secret"]["secretName"] == "web-secret", "secret volume")
    j = gen.generate("Job", CASES["Job"]).manifest
    check(j["spec"]["template"]["spec"]["restartPolicy"] == "Never", "job restartPolicy")
    check(j["spec"]["template"]["spec"]["containers"][0]["command"] == ["sh", "-c", "echo migrating && sleep 5"], "shlex command")
    cj = gen.generate("CronJob", CASES["CronJob"]).manifest
    check(cj["spec"]["jobTemplate"]["spec"]["template"]["spec"]["restartPolicy"] == "OnFailure", "cron restartPolicy")
    s = gen.generate("Service", CASES["Service"]).manifest
    check(s["spec"]["ports"][0]["targetPort"] == "http" and s["spec"]["ports"][0]["nodePort"] == 30080, "svc ports")
    ing = gen.generate("Ingress", CASES["Ingress"]).manifest
    check(len(ing["spec"]["rules"]) == 2 and len(ing["spec"]["rules"][0]["http"]["paths"]) == 2, "ingress rules")
    check(ing["spec"]["rules"][1]["http"]["paths"][0]["backend"]["service"]["port"] == {"name": "http"}, "named port")
    check(ing["spec"]["tls"][0] == {"hosts": ["web.example.com"], "secretName": "web-tls"}, "tls")
    cm = gen.generate("ConfigMap", CASES["ConfigMap"]).manifest
    check(cm["data"]["GREETING"] == "hello\nworld" and "listen 80" in cm["data"]["nginx.conf"], "configmap data")
    check("nginx.conf: |" in gen.to_yaml(cm), "multi-line literal block style")
    ns = gen.generate("Namespace", CASES["Namespace"]).manifest
    check(ns["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "baseline", "PSS label")

    print("== Secret base64 checks ==")
    sec = gen.generate("Secret", CASES["Secret"]).manifest
    expected = {"username": "admin", "password": "S3cr3t!=with=equals", "url": "postgres://admin@db:5432/app"}
    for k, v in expected.items():
        enc = sec["data"][k]
        check(is_b64(enc), f"{k} is base64")
        check(base64.b64decode(enc).decode() == v, f"{k} decodes to plaintext")
        check(enc != v, f"{k} is not plaintext")
    check("stringData" not in sec, "no stringData")
    print(f"  Opaque secret: {len(expected)} keys base64-encoded and decode back correctly")
    for label, vals in EXTRA_SECRETS.items():
        res = gen.generate("Secret", vals)
        check(res.ok, f"{label} secret should validate: {res.errors}")
        if not res.ok:
            continue
        m = res.manifest
        for k, v in (m.get("data") or {}).items():
            check(is_b64(v), f"{label}.{k} base64")
        if label == "docker":
            cfg = json.loads(base64.b64decode(m["data"][".dockerconfigjson"]))
            check(cfg["auths"]["ghcr.io"]["auth"] == base64.b64encode(b"john:tok3n").decode(), "docker auth")
        if label == "tls":
            check(base64.b64decode(m["data"]["tls.key"]).decode() == TLS_KEY, "tls.key roundtrip")
        if label == "satoken":
            check(m["metadata"]["annotations"]["kubernetes.io/service-account.name"] == "build-robot", "sa ann")
        yaml.safe_load(gen.to_yaml(m))
        files.append(write(m, f"secret-{label}-{m['metadata']['name']}.yaml"))
        print(f"  {m['type']:40s} OK  keys={sorted((m.get('data') or {}).keys())}")
    pre = gen.generate("Secret", {"already_base64": True, "data": "token=" + base64.b64encode(b"x").decode()})
    check(pre.ok and pre.manifest["data"]["token"] == "eA==", "already-base64 passthrough")

    print("== Negative / validation cases ==")
    for kind, ov, needle in NEGATIVE:
        res = gen.generate(kind, ov)
        hit = any(needle.lower() in e.lower() for e in res.errors)
        check(hit, f"{kind} {ov} expected error containing '{needle}', got {res.errors}")
    print(f"  {len(NEGATIVE)} invalid inputs each produced the expected friendly error")
    print("  sample messages:")
    for kind, ov, _ in NEGATIVE[:3]:
        print(f"    - {gen.generate(kind, ov).errors[0]}")

    print("== Multi-container & scheduling ==")
    for label, (kind, vals) in ADVANCED.items():
        res = gen.generate(kind, vals)
        check(res.ok, f"{label} should validate: {res.errors}")
        if not res.ok:
            continue
        m = res.manifest
        check(yaml.safe_load(gen.to_yaml(m)) == m, f"{label} YAML round-trip")
        files.append(write(m, f"adv-{label}.yaml"))
        print(f"  {label:28s} OK  warnings={res.warnings}")
    adv = {k: gen.generate(*v).manifest for k, v in ADVANCED.items()}
    ps = adv["deploy-multi"]["spec"]["template"]["spec"]
    check([c["name"] for c in ps["containers"]] == ["web", "log-shipper", "metrics"], f"containers order {[c['name'] for c in ps['containers']]}")
    check([c["name"] for c in ps["initContainers"]] == ["mesh-proxy", "init-db"], "initContainers keep list order")
    check(ps["initContainers"][0]["restartPolicy"] == "Always", "native sidecar has restartPolicy: Always")
    check("restartPolicy" not in ps["initContainers"][1], "regular init container has no restartPolicy")
    check(all("restartPolicy" not in c for c in ps["containers"]), "regular containers have no restartPolicy")
    check(ps["containers"][1]["volumeMounts"] == [{"name": "logs", "mountPath": "/var/log/app", "readOnly": True}],
          "sidecar volumeMount (ro)")
    check(ps["containers"][0]["volumeMounts"] == [{"name": "shared", "mountPath": "/shared"}],
          "volume with blank mountPath is defined but not mounted in main")
    check({v["name"] for v in ps["volumes"]} == {"shared", "logs"}, "volumes defined")
    check(ps["initContainers"][0]["ports"][0]["containerPort"] == 15001, "native sidecar ports")
    check(ps["containers"][2]["resources"] == {"requests": {"cpu": "10m", "memory": "16Mi"},
                                              "limits": {"memory": "32Mi"}}, "sidecar resources")
    check(ps["initContainers"][1]["command"] == ["sh", "-c", "until nslookup db; do sleep 2; done"], "init command")
    check(list(ps)[:5] == ["serviceAccountName", "restartPolicy", "initContainers", "containers", "volumes"]
          or list(ps)[:4] == ["restartPolicy", "initContainers", "containers", "volumes"], f"pod spec key order {list(ps)[:5]}")
    # scheduling
    check(ps["tolerations"] == [
        {"key": "dedicated", "operator": "Equal", "value": "web", "effect": "NoSchedule"},
        {"key": "node.kubernetes.io/unreachable", "operator": "Exists", "effect": "NoExecute", "tolerationSeconds": 60},
        {"key": "spot", "operator": "Exists"}], f"tolerations {ps['tolerations']}")
    na = ps["affinity"]["nodeAffinity"]
    terms = na["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
    check(len(terms) == 2 and len(terms[0]["matchExpressions"]) == 2, "required node affinity: 2 ORed terms, first has 2 ANDed exprs")
    check(terms[1]["matchExpressions"][0] == {"key": "node.kubernetes.io/instance-type", "operator": "In",
                                              "values": ["m5.large", "m5.xlarge"]}, "term 2 expr")
    check(terms[0]["matchExpressions"][1] == {"key": "example.com/cpu-gen", "operator": "Gt", "values": ["3"]}, "Gt expr")
    pref = na["preferredDuringSchedulingIgnoredDuringExecution"]
    check(pref == [{"weight": 30, "preference": {"matchExpressions": [
        {"key": "topology.kubernetes.io/zone", "operator": "In", "values": ["us-west-2a", "us-west-2b"]}]}}], "preferred node affinity")
    pa = ps["affinity"]["podAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"][0]
    check(pa == {"labelSelector": {"matchExpressions": [{"key": "app", "operator": "In", "values": ["cache"]}]},
                 "namespaces": ["demo", "cache"], "topologyKey": "topology.kubernetes.io/zone"}, "pod affinity required")
    paa = ps["affinity"]["podAntiAffinity"]
    check(paa["preferredDuringSchedulingIgnoredDuringExecution"][0]["weight"] == 100 and
          paa["preferredDuringSchedulingIgnoredDuringExecution"][0]["podAffinityTerm"]["topologyKey"] == "kubernetes.io/hostname",
          "pod anti-affinity preferred")
    check(paa["requiredDuringSchedulingIgnoredDuringExecution"][0]["labelSelector"]["matchExpressions"][0]
          == {"key": "tier", "operator": "Exists"}, "anti-affinity Exists has no values")
    tsc = ps["topologySpreadConstraints"]
    check(tsc[0] == {"maxSkew": 1, "topologyKey": "topology.kubernetes.io/zone", "whenUnsatisfiable": "ScheduleAnyway",
                     "labelSelector": {"matchLabels": {"app": "web"}}}, "spread defaults to pod labels")
    check(tsc[1]["labelSelector"]["matchLabels"] == {"app": "web", "tier": "frontend"} and tsc[1]["maxSkew"] == 2,
          "spread explicit labels")
    check(ps["nodeSelector"] == {"kubernetes.io/os": "linux"}, "nodeSelector still works alongside affinity")
    ds = adv["daemonset-tolerations"]["spec"]["template"]["spec"]
    check(ds["tolerations"][0] == {"operator": "Exists"} and len(ds["tolerations"]) == 2, "DaemonSet tolerate-all + rows")
    sts = adv["sts-sidecar-vct"]["spec"]["template"]["spec"]
    check(sts["initContainers"][0]["volumeMounts"][0]["name"] == "data", "sidecar may mount the StatefulSet VCT volume")
    job = adv["job-native-sidecar"]["spec"]["template"]["spec"]
    check(job["initContainers"][0]["restartPolicy"] == "Always" and job["restartPolicy"] == "Never", "Job native sidecar")
    cj = adv["cronjob-init-tolerations"]["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    check(cj["initContainers"][0]["name"] == "fetch" and cj["tolerations"][0]["effect"] == "PreferNoSchedule",
          "CronJob init container + toleration")
    check(any("1.29" in w for w in gen.generate(*ADVANCED["deploy-multi"]).warnings), "native sidecar 1.29+ warning")
    # value caching contract: defaults are fresh copies
    d1 = gen.defaults("Deployment"); d1["extra_containers"].append({"name": "x"})
    check(gen.defaults("Deployment")["extra_containers"] == [], "defaults() returns fresh list copies")
    check(gen.generate("Deployment", {"tolerations": json.dumps([{"key": "a", "operator": "Exists"}])}).ok,
          "table values may be passed as JSON strings")

    print("== NetworkPolicy presets / StorageClass / VolumeSnapshotClass ==")
    for ov, ok, what in NP_PRESETS:
        res = gen.generate("NetworkPolicy", ov)
        check(res.ok and ok(res.manifest), f"NetworkPolicy preset {what}: {res.errors} {res.manifest}")
        files.append(write(res.manifest, f"np-preset-{what}.yaml"))
    np_m = gen.generate("NetworkPolicy", CASES["NetworkPolicy"]).manifest
    check(len(np_m["spec"]["ingress"]) == 3 and np_m["spec"]["ingress"][2]["ports"][0].get("endPort") == 8080,
          "NetworkPolicy rule grouping + port range")
    sc_m = gen.generate("StorageClass", CASES["StorageClass"]).manifest
    check("namespace" not in sc_m["metadata"] and sc_m["parameters"]["iops"] == "6000"
          and sc_m["allowedTopologies"][0]["matchLabelExpressions"][0]["values"] == ["us-east-1a", "us-east-1b"],
          "StorageClass cluster-scoped, string parameters, allowedTopologies")
    sc_d = gen.generate("StorageClass", {"default_class": True}).manifest
    check(sc_d["metadata"]["annotations"]["storageclass.kubernetes.io/is-default-class"] == "true",
          "default StorageClass annotation")
    for kind, ov, needle in NEW_KIND_NEGATIVE:
        res = gen.generate(kind, ov)
        check(any(needle.lower() in e.lower() for e in res.errors), f"{kind} {ov} expected '{needle}', got {res.errors}")
    print(f"  {len(NP_PRESETS)} NetworkPolicy presets + {len(NEW_KIND_NEGATIVE)} negative cases OK")

    print("== Multi-container & scheduling: invalid input ==")
    for kind, ov, needle in ADV_NEGATIVE:
        res = gen.generate(kind, ov)
        hit = any(needle.lower() in e.lower() for e in res.errors)
        check(hit, f"{kind} {str(ov)[:70]} expected '{needle}', got {res.errors}")
    print(f"  {len(ADV_NEGATIVE)} invalid inputs each produced the expected error, e.g.:")
    for kind, ov, _ in ADV_NEGATIVE[:4]:
        print(f"    - {gen.generate(kind, ov).errors[0]}")
    for key, row, needle in ROW_CHECKS:
        errs = gen.check_row(key, row)
        check(any(needle.lower() in e.lower() for e in errs), f"check_row({key}) expected '{needle}', got {errs}")
    check(gen.check_row("extra_containers", {"name": "ok", "image": "busybox:1.36"}) == [], "check_row ok row -> []")
    print(f"  check_row (dialog validation): {len(ROW_CHECKS) + 1} cases OK")

    print("== Multi-doc ==")
    docs = [gen.generate(k, CASES[k]).manifest for k in gen.KINDS]
    multi = gen.to_multi_yaml(docs)
    loaded = list(yaml.safe_load_all(multi))
    check(len(loaded) == len(gen.KINDS), "multi-doc count")
    bundle = os.path.join(OUT, "all-in-one.yaml")
    with open(bundle, "w") as fh:
        fh.write(multi)
    print(f"  all-in-one.yaml with {len(loaded)} documents parses OK")

    kubectl_rc = 0
    if use_kubectl:
        kubectl_rc = run_kubectl(files + [bundle])

    print(f"\nRESULT: {passed} checks passed, {failed} failed")
    return 1 if failed or kubectl_rc else 0


def _kubectl_bin():
    for cand in (os.environ.get("KUBECTL"), shutil.which("kubectl"),
                 os.path.join(ROOT, "bin", "kubectl"), "/workspace/bin/kubectl"):
        if cand and os.path.exists(cand):
            return cand
    return None


def _run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    lines = [ln for ln in (p.stdout + p.stderr).strip().splitlines() if "memcache.go" not in ln]
    return p.returncode, lines


def run_kubectl(files):
    kubectl = _kubectl_bin()
    if not kubectl:
        print("== kubectl not found; skipping dry-run (set KUBECTL=/path/to/kubectl) ==")
        return 0
    print(f"== kubectl apply --dry-run ({kubectl}) ==")
    _, ver = _run([kubectl, "version", "--client"])
    print("  " + " / ".join(ver))
    rc, info = _run([kubectl, "cluster-info", "--request-timeout=5s"])
    have_cluster = rc == 0
    print(f"  cluster reachable: {have_cluster}" + (f" ({info[0][:100]})" if have_cluster and info else ""))
    modes = [("client", [])]
    if not have_cluster:
        # client dry-run validation needs the server's OpenAPI schema; without a cluster
        # turn validation off (kubectl may still need API discovery to map kinds).
        modes = [("client", ["--validate=false"])]
    elif "--server" in sys.argv:
        modes.append(("server", []))
        for ns in ("demo",):
            _run([kubectl, "create", "namespace", ns])
    if have_cluster and _run([kubectl, "get", "crd", "volumesnapshotclasses.snapshot.storage.k8s.io"])[0]:
        skip = [f for f in files if "volumesnapshotclass" in os.path.basename(f).lower()]
        if skip:
            print(f"  (snapshot CRDs not installed: skipping {', '.join(os.path.basename(f) for f in skip)})")
            files = [f for f in files if f not in skip]
    bad_total = 0
    for mode, extra in modes:
        bad = 0
        print(f"  -- kubectl apply --dry-run={mode} {' '.join(extra)}".rstrip())
        for f in files:
            rc, lines = _run([kubectl, "apply", f"--dry-run={mode}", *extra, "-f", f])
            if rc and not have_cluster and any(s in " ".join(lines) for s in
                                               ("connection refused", "server API group list", "no such host")):
                print("  skipped: kubectl needs a reachable API server even for --dry-run=client "
                      "(use --server with a cluster, or the --kind options of the other tests)")
                bad = 0
                files = []
                break
            if rc:
                bad += 1
            msg = " | ".join(lines) if rc == 0 else " | ".join(lines[-2:])
            print(f"  [{'OK ' if rc == 0 else 'ERR'}] {os.path.basename(f):44s} {msg[:300]}")
        print(f"  => dry-run={mode}: {len(files) - bad}/{len(files)} files OK")
        bad_total += bad
    return 1 if bad_total else 0


if __name__ == "__main__":
    sys.exit(main())
