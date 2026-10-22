#!/usr/bin/env python3
"""
Tests for app_templates (headless).

  python3 tests/test_app_templates.py                  # static checks of every template (+ variants)
  python3 tests/test_app_templates.py --kubectl        # + kubectl apply --dry-run=client/server against the
                                                       #   current KUBECONFIG cluster (client-only if none)
  python3 tests/test_app_templates.py --kind           # create a temporary kind cluster, dry-run client+server
                                                       #   every template, delete the cluster
  python3 tests/test_app_templates.py --kind --deploy  # + really deploy templates, wait for Ready, run client
                                                       #   checks (psql, mysql, mongosh, redis-cli, wget, ...)
     --deploy=core   only PostgreSQL, MySQL, MariaDB, MongoDB, Redis, Valkey, Memcached, nginx (+ debug pod)
     --deploy="WordPress + MySQL,Gitea"   just these templates
  KIND_SUDO=1 if docker needs sudo; KEEP_KIND=1 leaves the cluster running.

Generated files go to tests/output/templates/.
"""
import base64
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import yaml  # noqa: E402

import app_templates as tpl  # noqa: E402
import manifest_gen as gen  # noqa: E402

OUT = os.path.join(HERE, "output", "templates")
WORKLOADS = ("Deployment", "StatefulSet", "DaemonSet")
passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL {msg}")
    return bool(cond)


def slug(title):
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def pod_spec(m):
    return m["spec"]["template"]["spec"]


def pod_labels(m):
    return m["spec"]["template"]["metadata"]["labels"]


def containers(m):
    ps = pod_spec(m)
    return ps.get("containers", []) + ps.get("initContainers", [])


# --------------------------------------------------------------------------- #
# Static checks
# --------------------------------------------------------------------------- #
def check_set(title, s, params):
    t = tpl.get(title)
    tag = f"[{title} {params.get('namespace', 'default')}]"
    check(s.ok, f"{tag} render errors: {s.errors}")
    ms = s.manifests()
    check(len(ms) == len(s.items), f"{tag} every item generated a manifest")
    # YAML round trip, one file per object + all-in-one
    for m in ms:
        check(yaml.safe_load(gen.to_yaml(m)) == m, f"{tag} {m['kind']}/{m['metadata']['name']} YAML round-trips")
    docs = [d for d in yaml.safe_load_all(s.all_in_one()) if d]
    check(docs == ms, f"{tag} all-in-one.yaml parses to the same {len(ms)} docs")
    order = [tpl.KIND_ORDER.index(m["kind"]) for m in ms]
    check(order == sorted(order), f"{tag} all-in-one ordered Namespace/Secret/ConfigMap/PVC/Service/workload/Ingress")
    names = [gen.filename_for(m) for m in ms]
    check(len(set(names)) == len(names), f"{tag} unique file names {names}")
    by_kind = {}
    for m in ms:
        by_kind.setdefault(m["kind"], []).append(m)
    check(t.primary in by_kind and s.primary_kind == t.primary, f"{tag} primary kind {t.primary} present")
    check(set(s.primary_values()) == set(by_kind), f"{tag} one primary item per kind")
    ns = s.params["namespace"]
    for m in ms:
        if m["kind"] != "Namespace":
            check(m["metadata"].get("namespace") == ns, f"{tag} {m['kind']}/{m['metadata']['name']} in ns {ns}")
        check(m["metadata"]["name"].startswith(s.name) or m["kind"] == "Namespace",
              f"{tag} {m['kind']}/{m['metadata']['name']} prefixed with app name {s.name}")
    if ns != "default":
        check(ms[0]["kind"] == "Namespace" and ms[0]["metadata"]["name"] == ns, f"{tag} Namespace object first")
    else:
        check("Namespace" not in by_kind, f"{tag} no Namespace object for 'default'")

    secrets = {m["metadata"]["name"]: m for m in by_kind.get("Secret", [])}
    cms = {m["metadata"]["name"]: m for m in by_kind.get("ConfigMap", [])}
    pvcs = {m["metadata"]["name"]: m for m in by_kind.get("PersistentVolumeClaim", [])}
    svcs = by_kind.get("Service", [])
    wls = [m for k in WORKLOADS for m in by_kind.get(k, [])]
    check(wls, f"{tag} has a workload")
    check(any(c["image"] == t.image for w in wls for c in containers(w)), f"{tag} main image {t.image} used")

    for sec in secrets.values():
        for k, v in sec.get("data", {}).items():
            try:
                raw = base64.b64decode(v, validate=True).decode()
                ok = base64.b64encode(raw.encode()).decode() == v
            except Exception:
                ok = False
            check(ok, f"{tag} Secret {sec['metadata']['name']}.{k} is valid base64")
        check("stringData" not in sec, f"{tag} Secret uses base64 data:")
    for sname in t.secrets:
        val = s.secrets.get(sname, "")
        if sname == "kafka_cluster_id":
            check(len(val) == 22 and re.fullmatch(r"[A-Za-z0-9_-]{22}", val), f"{tag} KRaft CLUSTER_ID {val!r}")
        else:
            check(len(val) >= 24 and val.isalnum(), f"{tag} random password {sname} is 24+ alnum chars")
            encoded = base64.b64encode(val.encode()).decode()
            check(any(encoded in sec.get("data", {}).values() for sec in secrets.values()),
                  f"{tag} password {sname} stored (base64) in the Secret")

    for w in wls:
        wn = f"{w['kind']}/{w['metadata']['name']}"
        sel = w["spec"]["selector"]["matchLabels"]
        pl = pod_labels(w)
        check(all(pl.get(k) == v for k, v in sel.items()), f"{tag} {wn} selector {sel} matches pod labels")
        check("app.kubernetes.io/version" not in sel, f"{tag} {wn} selector excludes the version label")
        ps = pod_spec(w)
        main = ps["containers"][0]
        img = main["image"]
        check(":" in img.split("/")[-1] and not img.endswith(":latest"), f"{tag} {wn} image {img} pinned")
        for c in ps["containers"]:
            check("readinessProbe" in c and "livenessProbe" in c, f"{tag} {wn} {c['name']} has readiness+liveness probes")
            lim = c.get("resources", {}).get("limits", {})
            check("cpu" in lim and "memory" in lim, f"{tag} {wn} {c['name']} has resource limits")
            pnames = {p.get("name") for p in c.get("ports", [])}
            for pk in ("readinessProbe", "livenessProbe"):
                pr = c.get(pk, {})
                port = (pr.get("httpGet") or pr.get("tcpSocket") or {}).get("port")
                if isinstance(port, str):
                    check(port in pnames, f"{tag} {wn} {pk} port '{port}' is a container port name")
            for e in c.get("env", []):
                ref = e.get("valueFrom", {}).get("secretKeyRef")
                if ref:
                    check(ref["name"] in secrets and ref["key"] in secrets[ref["name"]].get("data", {}),
                          f"{tag} {wn} env {e['name']} -> Secret {ref['name']}/{ref['key']} exists in the set")
            for ef in c.get("envFrom", []):
                if "secretRef" in ef:
                    check(ef["secretRef"]["name"] in secrets, f"{tag} {wn} envFrom Secret {ef['secretRef']} in set")
                if "configMapRef" in ef:
                    check(ef["configMapRef"]["name"] in cms, f"{tag} {wn} envFrom ConfigMap in set")
        for v in ps.get("volumes", []):
            if "configMap" in v:
                check(v["configMap"]["name"] in cms, f"{tag} {wn} volume {v['name']} -> ConfigMap in set")
            if "persistentVolumeClaim" in v:
                cn = v["persistentVolumeClaim"]["claimName"]
                check(cn in pvcs, f"{tag} {wn} volume {v['name']} -> PVC {cn} in set")
                check(w["kind"] != "Deployment" or w["spec"].get("strategy", {}).get("type") == "Recreate"
                      or w["spec"].get("replicas", 1) == 0,
                      f"{tag} {wn} with a RWO PVC uses strategy Recreate")
        if w["kind"] == "StatefulSet":
            sn = w["spec"]["serviceName"]
            hs = [x for x in svcs if x["metadata"]["name"] == sn]
            check(hs and hs[0]["spec"].get("clusterIP") == "None", f"{tag} {wn} serviceName {sn} is a headless Service in the set")
            if hs:
                check(all(pl.get(k) == v for k, v in hs[0]["spec"]["selector"].items()),
                      f"{tag} headless Service {sn} selects the StatefulSet pods")
            vct = w["spec"].get("volumeClaimTemplates", [])
            if t.storage:
                check(vct, f"{tag} {wn} has a volumeClaimTemplate")
                for vc in vct:
                    check(vc["spec"]["resources"]["requests"]["storage"] == s.params["storage_size"],
                          f"{tag} {wn} VCT size = storage param")
                    if s.params.get("storage_class"):
                        check(vc["spec"].get("storageClassName") == s.params["storage_class"], f"{tag} VCT storageClass")
        if "replicas" in [p.key for p in tpl.params_for(title)]:
            check(w["spec"]["replicas"] == s.params["replicas"], f"{tag} {wn} replicas = param")
        elif w["kind"] != "DaemonSet":
            check(w["spec"]["replicas"] == 1, f"{tag} {wn} single instance")

    for svc in svcs:
        sn = svc["metadata"]["name"]
        sel = svc["spec"].get("selector", {})
        targets = [w for w in wls if all(pod_labels(w).get(k) == v for k, v in sel.items())]
        check(sel and targets, f"{tag} Service {sn} selector {sel} selects pods of a workload in the set")
        for p in svc["spec"]["ports"]:
            tp = p.get("targetPort", p["port"])
            cports = [cp for w in targets for c in pod_spec(w)["containers"] for cp in c.get("ports", [])]
            ok = (any(cp.get("name") == tp for cp in cports) if isinstance(tp, str)
                  else any(cp["containerPort"] == tp for cp in cports))
            check(ok, f"{tag} Service {sn} targetPort {tp} exists on the selected pods")
    for pvc in pvcs.values():
        check(pvc["spec"]["resources"]["requests"]["storage"] == s.params["storage_size"], f"{tag} PVC size = param")
        used = any(v.get("persistentVolumeClaim", {}).get("claimName") == pvc["metadata"]["name"]
                   for w in wls for v in pod_spec(w).get("volumes", []))
        check(used, f"{tag} PVC {pvc['metadata']['name']} is mounted by a workload")
    for cm in cms.values():
        used = any(v.get("configMap", {}).get("name") == cm["metadata"]["name"]
                   for w in wls for v in pod_spec(w).get("volumes", []))
        check(used, f"{tag} ConfigMap {cm['metadata']['name']} is mounted by a workload")
    for ing in by_kind.get("Ingress", []):
        check(t.web and params.get("ingress"), f"{tag} Ingress only when requested")
        for rule in ing["spec"]["rules"]:
            check(rule.get("host") == s.params["host"], f"{tag} Ingress host = param")
            for path in rule["http"]["paths"]:
                b = path["backend"]["service"]
                tgt = [x for x in svcs if x["metadata"]["name"] == b["name"]]
                check(tgt and any(p["port"] == b["port"].get("number") or p.get("name") == b["port"].get("name")
                                  for p in tgt[0]["spec"]["ports"]),
                      f"{tag} Ingress backend {b} is a Service port in the set")
        if s.params.get("ingress_class"):
            check(ing["spec"].get("ingressClassName") == s.params["ingress_class"], f"{tag} ingressClassName")
    if t.web and not params.get("ingress"):
        check("Ingress" not in by_kind, f"{tag} no Ingress unless requested")
    return ms


def static_tests():
    print("== registry ==")
    titles = tpl.titles()
    check(len(titles) >= 20, f"at least 20 templates (have {len(titles)})")
    check(set(tpl.grouped()) == set(tpl.GROUPS), "every group has templates")
    for title in titles:
        t = tpl.get(title)
        check(t.description and t.group in tpl.GROUPS, f"{title}: description + group")
        check(gen._DNS1123_LABEL.match(t.default_name), f"{title}: default name is a DNS label")
        check(t.primary in gen.KINDS, f"{title}: primary kind")
        check(re.search(r":[\w][\w.-]*$", t.image) and "latest" not in t.image, f"{title}: pinned image {t.image}")
    for want in ("PostgreSQL", "MySQL", "MongoDB", "Redis", "nginx", "Kafka (KRaft)", "WordPress + MySQL"):
        check(want in titles, f"template {want} registered")
    check(set(tpl.SKIPPED) >= {"Traefik", "MinIO"}, "skipped templates are documented")
    try:
        tpl.template("nginx", "Web / proxy", "nginx:1", "x")(lambda p: [])
        check(False, "duplicate titles are rejected")
    except ValueError:
        check(True, "")

    os.makedirs(OUT, exist_ok=True)
    files = []
    print(f"== {len(titles)} templates x 2 variants ==")
    for title in titles:
        t = tpl.get(title)
        s = tpl.render(title)
        ms = check_set(title, s, tpl.default_params(title))
        d = os.path.join(OUT, slug(title))
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)
        for m in ms:
            with open(os.path.join(d, gen.filename_for(m)), "w") as fh:
                fh.write(gen.to_yaml(m))
        aio = os.path.join(d, "all-in-one.yaml")
        with open(aio, "w") as fh:
            fh.write(s.all_in_one())
        files.append(aio)
        # variant: own namespace, storage class/size, replicas, Ingress on
        vp = {"name": f"x{t.default_name}"[:40], "namespace": "apps", "storage_size": "3Gi",
              "storage_class": "standard", "replicas": "3", "ingress": True, "host": f"{slug(title)}.apps.test",
              "ingress_class": "nginx"}
        sv = tpl.render(title, vp)
        check_set(title, sv, vp)
        vfile = os.path.join(d, "variant-all-in-one.yaml")
        with open(vfile, "w") as fh:
            fh.write(sv.all_in_one())
        files.append(vfile)
        # stable secrets across re-render, fresh ones otherwise
        if t.secrets:
            s2 = tpl.render(title, {"storage_size": "9Gi"} if t.storage else {}, secrets=s.secrets)
            check(s2.secrets == s.secrets,
                  f"[{title}] re-render keeps passwords")
            sec1 = [m for m in s.manifests() if m["kind"] == "Secret"]
            sec2 = [m for m in s2.manifests() if m["kind"] == "Secret"]
            check(sec1 == sec2, f"[{title}] re-rendered Secret identical")
            s3 = tpl.render(title)
            check(s3.secrets != s.secrets, f"[{title}] a fresh render generates new passwords")
        objs = ", ".join("%s/%s" % (m["kind"], m["metadata"]["name"]) for m in ms)
        print(f"  {title:22s} {objs}")

    print("== specific checks ==")
    pg = tpl.render("PostgreSQL").manifests()
    sts = next(m for m in pg if m["kind"] == "StatefulSet")
    c = pod_spec(sts)["containers"][0]
    check(c["ports"][0]["containerPort"] == 5432, "postgres port 5432")
    check({"name": "PGDATA", "value": "/var/lib/postgresql/18/docker"} in c["env"], "postgres PGDATA")
    check("pg_isready" in " ".join(c["readinessProbe"]["exec"]["command"]), "postgres pg_isready readiness")
    check(c["envFrom"] == [{"secretRef": {"name": "postgres-secret"}}], "postgres env from Secret")
    check(sorted(m["metadata"]["name"] for m in pg if m["kind"] == "Service") == ["postgres", "postgres-headless"],
          "postgres ClusterIP + headless Service")
    ng = tpl.render("nginx", {"ingress": True}).manifests()
    check([m["kind"] for m in ng] == ["ConfigMap", "Service", "Deployment", "Ingress"], "nginx set kinds")
    check("default.conf" in ng[0]["data"] and "/healthz" in ng[0]["data"]["default.conf"], "nginx default.conf")
    kf = next(m for m in tpl.render("Kafka (KRaft)").manifests() if m["kind"] == "StatefulSet")
    check(pod_spec(kf).get("enableServiceLinks") is False, "kafka enableServiceLinks: false")
    env = {e["name"]: e.get("value") for e in pod_spec(kf)["containers"][0]["env"]}
    check(env["KAFKA_ADVERTISED_LISTENERS"] == "PLAINTEXT://kafka.default.svc.cluster.local:9092", "kafka advertised listener")
    cs = [m for m in tpl.render("Cassandra").manifests() if m["kind"] == "Service"]
    check(any(m["spec"].get("publishNotReadyAddresses") for m in cs), "cassandra headless publishNotReadyAddresses")
    wp = tpl.render("WordPress + MySQL").manifests()
    check(sorted(m["kind"] for m in wp).count("Service") == 3 and any(m["kind"] == "StatefulSet" for m in wp),
          "wordpress: 3 Services + MySQL StatefulSet")
    wpd = next(m for m in wp if m["kind"] == "Deployment")
    wenv = {e["name"]: e for e in pod_spec(wpd)["containers"][0]["env"]}
    check(wenv["WORDPRESS_DB_HOST"]["value"] == "wordpress-mysql:3306", "wordpress DB host = mysql Service")
    gr = tpl.render("Grafana").manifests()
    check(pod_spec(next(m for m in gr if m["kind"] == "Deployment"))["securityContext"] == {"fsGroup": 472},
          "grafana fsGroup 472")
    pr = next(m for m in tpl.render("Prometheus").manifests() if m["kind"] == "Deployment")
    check(pod_spec(pr)["securityContext"] == {"runAsUser": 65534, "runAsGroup": 65534, "fsGroup": 65534,
                                              "runAsNonRoot": True}, "prometheus securityContext")
    db = tpl.render("busybox debug pod").manifests()
    check([m["kind"] for m in db] == ["Deployment"], "debug pod: just a Deployment")
    # bad params
    bad = tpl.render("PostgreSQL", {"name": "Bad_Name"})
    check(not bad.ok and "App name" in bad.errors[0], "invalid app name rejected")
    check(not tpl.render("PostgreSQL", {"storage_size": "lots"}).ok, "invalid storage size rejected")
    check(not tpl.render("nginx", {"replicas": "two"}).ok, "invalid replicas rejected")
    check(not tpl.render("nginx", {"ingress": True, "host": "bad host"}).ok, "invalid host rejected")
    check(not tpl.render("nginx", {"namespace": "Bad NS"}).ok, "invalid namespace rejected")
    try:
        tpl.get("nope")
        check(False, "unknown template raises")
    except KeyError:
        check(True, "")
    pw = {tpl.random_password() for _ in range(50)}
    check(len(pw) == 50 and all(len(p) == 24 and p.isalnum() for p in pw), "random passwords unique 24-char alnum")

    print("== manifest_gen probe / securityContext extensions ==")
    base = {"name": "p", "image": "busybox:1.37.0", "ports": "http:8080"}
    r = gen.generate("Deployment", {**base, "readiness_type": "tcpSocket", "liveness_type": "exec",
                                    "liveness_command": "sh -c 'test -f /tmp/ok'", "probe_timeout": "4",
                                    "probe_failure_threshold": "5", "readiness_initial_delay": "0",
                                    "fs_group": "2000", "run_as_user": "1000", "run_as_non_root": True,
                                    "enable_service_links": False})
    c = pod_spec(r.manifest)["containers"][0]
    check(r.ok and c["readinessProbe"] == {"tcpSocket": {"port": "http"}, "initialDelaySeconds": 0, "periodSeconds": 10,
                                           "timeoutSeconds": 4, "failureThreshold": 5}, "tcpSocket readiness probe")
    check(c["livenessProbe"]["exec"]["command"] == ["sh", "-c", "test -f /tmp/ok"], "exec liveness probe")
    check(pod_spec(r.manifest)["securityContext"] == {"runAsUser": 1000, "fsGroup": 2000, "runAsNonRoot": True},
          "pod securityContext")
    check(pod_spec(r.manifest)["enableServiceLinks"] is False, "enableServiceLinks false")
    r = gen.generate("Deployment", {**base, "readiness_type": "grpc", "probe_port": "9090"})
    check(r.ok and pod_spec(r.manifest)["containers"][0]["readinessProbe"]["grpc"] == {"port": 9090}, "grpc probe")
    r = gen.generate("Deployment", {**base, "readiness_type": "grpc"})
    check(not r.ok, "grpc probe with a named port rejected")
    r = gen.generate("Deployment", {**base, "liveness_type": "exec"})
    check(not r.ok, "exec probe without command rejected")
    r = gen.generate("Deployment", {**base, "run_as_user": "0", "run_as_non_root": True})
    check(not r.ok, "runAsNonRoot with runAsUser 0 rejected")
    r = gen.generate("Deployment", {**base, "readiness_path": "/ready"})
    pr = pod_spec(r.manifest)["containers"][0]["readinessProbe"]
    check(pr == {"httpGet": {"path": "/ready", "port": "http"}, "initialDelaySeconds": 5, "periodSeconds": 10},
          "legacy readiness path still produces the same httpGet probe")
    check("securityContext" not in pod_spec(r.manifest) and "enableServiceLinks" not in pod_spec(r.manifest),
          "defaults add no securityContext / enableServiceLinks")
    r = gen.generate("Service", {"name": "s", "cluster_ip": "None", "publish_not_ready": True})
    check(r.ok and r.manifest["spec"]["publishNotReadyAddresses"] is True, "Service publishNotReadyAddresses")
    return files


# --------------------------------------------------------------------------- #
# kubectl / kind
# --------------------------------------------------------------------------- #
def _bin(name):
    for cand in (os.environ.get(name.upper()), shutil.which(name), os.path.join(ROOT, "bin", name),
                 f"/workspace/bin/{name}"):
        if cand and os.path.exists(cand):
            return cand
    return None


def _run(cmd, timeout=900, env=None, input=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, input=input)
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s"
    return p.returncode, (p.stdout + p.stderr).strip()


def _sudo():
    return ["sudo"] if os.environ.get("KIND_SUDO") else []


class Kube:
    def __init__(self, kubeconfig=None):
        self.kc = _bin("kubectl")
        self.env = {**os.environ}
        if kubeconfig:
            self.env["KUBECONFIG"] = kubeconfig

    def __call__(self, *args, timeout=900, input=None):
        return _run([self.kc, *args], timeout=timeout, env=self.env, input=input)


def dry_run(k, files, server):
    modes = ["client"] + (["server"] if server else [])
    rc, _ = k("create", "namespace", "apps")
    for mode in modes:
        bad = 0
        for f in files:
            rc, out = k("apply", f"--dry-run={mode}", "-f", f)
            if mode == "server" and "variant" in f:
                out_lines = [ln for ln in out.splitlines() if "namespace/apps" not in ln]
            else:
                out_lines = out.splitlines()
            ok = rc == 0 and all(("created" in ln or "configured" in ln or "unchanged" in ln) for ln in out_lines)
            if not ok:
                bad += 1
            check(ok, f"dry-run={mode} {os.path.relpath(f, OUT)}: {out[-400:]}")
        print(f"  dry-run={mode}: {len(files) - bad}/{len(files)} all-in-one files OK "
              f"({sum(len(list(yaml.safe_load_all(open(f)))) for f in files)} objects)")


def secret_value(k, ns, name, key):
    rc, out = k("-n", ns, "get", "secret", name, "-o", f"jsonpath={{.data.{key}}}")
    return base64.b64decode(out).decode() if rc == 0 else ""


DEBUG_NS = "t-tools"

# title -> list of client checks: (description, exec target or None=debug pod, shell command, expected regex)
CLIENT_CHECKS = {
    "PostgreSQL": [("psql via Service", "sts/postgres",
                    'PGPASSWORD="$POSTGRES_PASSWORD" psql -h postgres -U "$POSTGRES_USER" -d "$POSTGRES_DB" '
                    '-tAc "select 40+2"', r"^42$")],
    "MySQL": [("mysql via Service", "sts/mysql",
               'mysql -h mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE" -N -e "select 40+2" 2>/dev/null',
               r"^42$")],
    "MariaDB": [("mariadb via Service", "sts/mariadb",
                 'mariadb -h mariadb -u"$MARIADB_USER" -p"$MARIADB_PASSWORD" "$MARIADB_DATABASE" -N -e "select 40+2"',
                 r"^42$")],
    "MongoDB": [("mongosh ping via Service", "sts/mongodb",
                 'mongosh "mongodb://$MONGO_INITDB_ROOT_USERNAME:$MONGO_INITDB_ROOT_PASSWORD@mongodb:27017/admin" '
                 '--quiet --eval "db.runCommand({ping:1}).ok"', r"^1$")],
    "Redis": [("redis-cli ping via Service", "sts/redis",
               'redis-cli -h redis -a "$REDIS_PASSWORD" --no-auth-warning ping', r"^PONG$"),
              ("password required", "sts/redis", "redis-cli -h redis ping", r"NOAUTH")],
    "Valkey": [("valkey-cli ping via Service", "sts/valkey",
                'valkey-cli -h valkey -a "$VALKEY_PASSWORD" --no-auth-warning ping', r"^PONG$")],
    "Memcached": [("memcached stats from debug pod", None,
                   "printf 'stats\\r\\nquit\\r\\n' | nc -w 3 memcached.t-memcached 11211", r"STAT version 1\.6\.45")],
    "nginx": [("GET /healthz from debug pod", None, "wget -qO- http://nginx.t-nginx/healthz", r"^ok$")],
    "Apache httpd": [("GET / from debug pod", None, "wget -qO- http://httpd.t-apache-httpd/", r"It works!")],
    "Caddy": [("GET /healthz from debug pod", None, "wget -qO- http://caddy.t-caddy/healthz", r"^ok$")],
    "HAProxy": [("stats /healthz from debug pod", None, "wget -qO- http://haproxy.t-haproxy:8404/healthz", r"^ok$")],
    "NATS (JetStream)": [("JetStream healthz", None,
                          "wget -qO- 'http://nats.t-nats-jetstream:8222/healthz?js-enabled-only=true'", r'"ok"'),
                         ("JetStream enabled", None, "wget -qO- http://nats.t-nats-jetstream:8222/jsz", r'"config"')],
    "Mosquitto (MQTT)": [("publish + subscribe (retained)", "deploy/mosquitto",
                          "mosquitto_pub -h mosquitto -t k8s/test -r -m hello-mqtt && "
                          "mosquitto_sub -h mosquitto -t k8s/test -C 1 -W 10", r"^hello-mqtt$")],
    "RabbitMQ": [("management API overview", None,
                  "wget -qO- http://admin:{RABBITMQ_DEFAULT_PASS}@rabbitmq.t-rabbitmq:15672/api/overview",
                  r'"rabbitmq_version":"4\.3\.6"'),
                 ("diagnostics check_running", "sts/rabbitmq", "rabbitmq-diagnostics -q check_running", r"running")],
    "Kafka (KRaft)": [("create topic + produce + consume", "sts/kafka",
                       "B=kafka.t-kafka-kraft.svc.cluster.local:9092; cd /opt/kafka/bin && "
                       "./kafka-topics.sh --bootstrap-server $B --create --topic smoke --partitions 1 "
                       "--replication-factor 1 >/dev/null && echo hello-kafka | ./kafka-console-producer.sh "
                       "--bootstrap-server $B --topic smoke && ./kafka-console-consumer.sh --bootstrap-server $B "
                       "--topic smoke --from-beginning --max-messages 1 --timeout-ms 30000 2>/dev/null",
                       r"hello-kafka")],
    "Prometheus": [("build info API", None, "wget -qO- http://prometheus.t-prometheus:9090/api/v1/status/buildinfo",
                    r'"version":"3\.5\.5"'),
                   ("self-scrape target up", None,
                    "for i in $(seq 1 12); do wget -qO- 'http://prometheus.t-prometheus:9090/api/v1/query?query=up' "
                    "| grep -q '\"1\"]' && { echo scraped; exit 0; }; sleep 5; done; exit 1", r"^scraped$")],
    "Grafana": [("datasource provisioned (admin auth)", None,
                 "wget -qO- http://admin:{GF_SECURITY_ADMIN_PASSWORD}@grafana.t-grafana/api/datasources",
                 r'"name":"Prometheus"')],
    "WordPress + MySQL": [("install page (needs the DB)", None,
                           "wget -T 90 -qO- http://wordpress.t-wordpress-mysql/wp-admin/install.php", r"WordPress")],
    "Gitea": [("api healthz", None, "wget -qO- http://gitea.t-gitea:3000/api/healthz", r'"status": ?"pass"')],
    "Keycloak": [("master realm", None, "wget -qO- http://keycloak.t-keycloak:8080/realms/master",
                  r'"realm":"master"')],
    "Elasticsearch": [("cluster health", None,
                       "wget -qO- http://elasticsearch.t-elasticsearch:9200/_cluster/health", r'"status":"(green|yellow)"')],
    "OpenSearch": [("cluster health", None, "wget -qO- http://opensearch.t-opensearch:9200/_cluster/health",
                    r'"status":"(green|yellow)"')],
    "Cassandra": [("cqlsh select", "sts/cassandra",
                   "cqlsh cassandra -e 'SELECT release_version FROM system.local'", r"5\.0\.9")],
    "Jenkins": [("login page", None, "wget -qO- http://jenkins.t-jenkins:8080/login", r"Jenkins")],
    "busybox debug pod": [("DNS lookup of kubernetes.default", None,
                           "nslookup kubernetes.default.svc.cluster.local", r"Address")],
}
SECRET_KEYS = {"RabbitMQ": ("rabbitmq-secret", "RABBITMQ_DEFAULT_PASS"),
               "Grafana": ("grafana-secret", "GF_SECURITY_ADMIN_PASSWORD")}

BATCHES = [
    ["PostgreSQL", "MySQL", "MongoDB", "Redis", "nginx", "MariaDB", "Valkey", "Memcached"],
    ["Apache httpd", "Caddy", "HAProxy", "NATS (JetStream)", "Mosquitto (MQTT)", "RabbitMQ", "Kafka (KRaft)"],
    ["Prometheus", "Grafana", "WordPress + MySQL", "Gitea", "Keycloak"],
    ["Elasticsearch"], ["OpenSearch"], ["Cassandra"], ["Jenkins"],
]


def wait_ready(k, ns, s, timeout=480):
    t0 = time.time()
    for m in s.manifests():
        if m["kind"] in WORKLOADS:
            rc, out = k("-n", ns, "rollout", "status", f"{m['kind'].lower()}/{m['metadata']['name']}",
                        f"--timeout={timeout}s", timeout=timeout + 30)
            if rc != 0:
                return False, out, time.time() - t0
    return True, "", time.time() - t0


def diagnose(k, ns):
    _, pods = k("-n", ns, "get", "pods", "-o", "wide")
    _, ev = k("-n", ns, "get", "events", "--sort-by=.lastTimestamp")
    _, logs = k("-n", ns, "logs", "--all-containers", "--tail=30", "-l", "app.kubernetes.io/managed-by=k8s-manifest-gui",
                "--prefix")
    print("    " + "\n    ".join((pods + "\n" + ev[-2500:] + "\n" + logs[-3000:]).splitlines()))


def run_client_checks(k, title, ns):
    for desc, target, cmd, expect in CLIENT_CHECKS.get(title, []):
        if title in SECRET_KEYS:
            sname, key = SECRET_KEYS[title]
            cmd = cmd.replace("{" + key + "}", secret_value(k, ns, sname, key))
        tns, tgt = (ns, target) if target else (DEBUG_NS, "deploy/debug")
        out = ""
        for attempt in range(6):    # services/DNS can lag a few seconds behind readiness
            rc, out = k("-n", tns, "exec", tgt, "--", "sh", "-c", cmd, timeout=120)
            lines = [ln for ln in out.splitlines() if "Defaulted container" not in ln]
            out = "\n".join(lines).strip()
            if rc == 0 and re.search(expect, out, re.M):
                break
            time.sleep(5)
        ok = rc == 0 and re.search(expect, out, re.M)
        if title == "Redis" and "NOAUTH" in expect:
            ok = re.search(expect, out, re.M)
        shown = out.replace(secret_value(k, ns, *SECRET_KEYS[title]), "***") if title in SECRET_KEYS else out
        print(f"    {'OK ' if ok else 'ERR'} {desc}: {shown[-160:]!r}")
        check(ok, f"[{title}] client check '{desc}' expected /{expect}/ got {shown[-500:]!r}")


def images_of(s):
    return sorted({c["image"] for m in s.manifests() if m["kind"] in WORKLOADS for c in containers(m)})


PULLED = []


def remove_pulled():
    if PULLED and not os.environ.get("KEEP_IMAGES"):
        rc, out = _run(_sudo() + ["docker", "rmi", *PULLED])
        print(f"== removed {len(PULLED)} preloaded image(s) from the host docker: rc={rc} ==")


def preload(images, cluster):
    """Pull on the host and `kind load` into the node: the kind node's own registry DNS may be unusable
    (it is on this test box), and preloading also avoids registry rate limits. PRELOAD=0 disables."""
    if os.environ.get("PRELOAD", "1") == "0" or not cluster:
        return
    kind = _bin("kind")
    for img in images:
        t0 = time.time()
        if _run(_sudo() + ["docker", "image", "inspect", img])[0] != 0:
            PULLED.append(img)       # removed again from the host at the end (KEEP_IMAGES=1 keeps them)
        rc, out = _run(_sudo() + ["docker", "pull", "-q", img], timeout=900)
        if rc == 0:
            rc, out = _run(_sudo() + [kind, "load", "docker-image", img, "--name", cluster], timeout=900)
        print(f"    preload {img}: {'ok' if rc == 0 else 'FAILED'} ({time.time() - t0:.0f}s)")
        check(rc == 0, f"preload {img}: {out[-300:]}")


def deploy(k, batches, cluster=None):
    print("== live deploy ==")
    debug = tpl.render("busybox debug pod", {"namespace": DEBUG_NS})
    preload(images_of(debug), cluster)
    path = os.path.join(OUT, "deploy-debug.yaml")
    open(path, "w").write(debug.all_in_one())
    rc, out = k("apply", "-f", path)
    ok, out, secs = wait_ready(k, DEBUG_NS, debug, timeout=300)
    check(ok, f"debug pod Ready: {out}")
    before = failed
    run_client_checks(k, "busybox debug pod", DEBUG_NS)
    if failed > before:
        print("  cluster DNS / Service networking is broken - skipping the live deploy")
        return
    for batch in batches:
        sets = {}
        print(f"  -- batch: {', '.join(batch)}")
        for title in batch:
            ns = "t-" + slug(title)
            s = tpl.render(title, {"namespace": ns})
            sets[title] = (ns, s)
        preload(sorted({i for ns_, s_ in sets.values() for i in images_of(s_)}), cluster)
        for title, (ns, s) in sets.items():
            path = os.path.join(OUT, f"deploy-{slug(title)}.yaml")
            open(path, "w").write(s.all_in_one())
            rc, out = k("apply", "-f", path)
            check(rc == 0, f"[{title}] kubectl apply: {out[-400:]}")
        for title, (ns, s) in sets.items():
            ok, out, secs = wait_ready(k, ns, s)
            _, pods = k("-n", ns, "get", "pods", "--no-headers")
            print(f"    {'READY' if ok else 'NOT READY'} {title} in {secs:.0f}s: "
                  + "; ".join(" ".join(ln.split()[:3]) for ln in pods.splitlines()))
            if not check(ok, f"[{title}] pods Ready: {out[-300:]}"):
                diagnose(k, ns)
                continue
            run_client_checks(k, title, ns)
        for title, (ns, s) in sets.items():
            k("delete", "namespace", ns, "--wait=false")
        for title, (ns, s) in sets.items():
            k("wait", "--for=delete", f"namespace/{ns}", "--timeout=300s", timeout=330)


def main():
    files = static_tests()
    use_kind = "--kind" in sys.argv
    deploy_arg = next((a for a in sys.argv if a.startswith("--deploy")), None)
    rc_extra = 0
    if use_kind or "--kubectl" in sys.argv:
        if not _bin("kubectl"):
            print("== kubectl not found - dry-run skipped ==")
        elif use_kind:
            kind = _bin("kind")
            if not kind:
                print("== kind not found - skipped ==")
            else:
                name = "tpl-test"
                tmp = tempfile.mkdtemp(prefix="tpl-kind-")
                kcfg = os.path.join(tmp, "kubeconfig")
                _run(_sudo() + [kind, "delete", "cluster", "--name", name])
                t0 = time.time()
                # kube-proxy nftables mode: on some kernels (this test box) iptables mode can't program
                # multi-endpoint Services (iptables-restore "RULE_APPEND failed"); KIND_PROXY_MODE overrides
                cfg = os.path.join(tmp, "kind-config.yaml")
                with open(cfg, "w") as fh:
                    yaml.safe_dump({"kind": "Cluster", "apiVersion": "kind.x-k8s.io/v1alpha4",
                                    "networking": {"kubeProxyMode": os.environ.get("KIND_PROXY_MODE", "nftables")},
                                    "nodes": [{"role": "control-plane"}]}, fh)
                rc, out = _run(_sudo() + [kind, "create", "cluster", "--name", name, "--kubeconfig", kcfg,
                                          "--config", cfg, "--wait", "180s"], timeout=600)
                print(f"== kind create cluster {name}: rc={rc} in {time.time() - t0:.0f}s ==")
                if check(rc == 0, f"kind create cluster: {out[-800:]}"):
                    if _sudo():
                        _run(["sudo", "chown", f"{os.getuid()}:{os.getgid()}", kcfg])
                    try:
                        k = Kube(kcfg)
                        _, ver = k("version")
                        print("  " + " / ".join(ver.splitlines()))
                        dry_run(k, files, server=True)
                        if deploy_arg:
                            sel = deploy_arg.partition("=")[2]
                            if sel == "core":
                                batches = BATCHES[:1]
                            elif sel:          # --deploy="PostgreSQL,nginx"
                                batches = [[x.strip() for x in sel.split(",") if x.strip()]]
                            else:
                                batches = BATCHES
                            deploy(k, batches, cluster=name)
                    finally:
                        if os.environ.get("KEEP_KIND"):
                            print(f"  KEEP_KIND set: cluster {name} left running (KUBECONFIG={kcfg})")
                        else:
                            rc, out = _run(_sudo() + [kind, "delete", "cluster", "--name", name])
                            print(f"== kind delete cluster {name}: rc={rc} ==")
                            check(rc == 0, f"kind delete cluster: {out}")
                        remove_pulled()
                if not os.environ.get("KEEP_KIND"):
                    _run(_sudo() + ["rm", "-rf", tmp])
        else:
            k = Kube()
            rc, _ = k("cluster-info", "--request-timeout=5s")
            if rc == 0:
                dry_run(k, files, server="--server" in sys.argv)
            else:
                print("== no cluster reachable: dry-run=client --validate=false ==")
                for f in files:
                    rc, out = k("apply", "--dry-run=client", "--validate=false", "-f", f)
                    if rc and re.search(r"connection refused|server API group list|no such host|i/o timeout", out):
                        # kubectl >= 1.2x needs API discovery even for client dry-run; nothing to test offline
                        print("  skipped: kubectl needs a reachable API server even for --dry-run=client "
                              "(run with --kind for a temporary cluster)")
                        break
                    check(rc == 0, f"dry-run client {f}: {out[-300:]}")
    print(f"\nRESULT: {passed} checks passed, {failed} failed")
    return 1 if failed or rc_extra else 0


if __name__ == "__main__":
    sys.exit(main())
