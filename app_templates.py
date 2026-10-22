"""Application templates for the Kubernetes manifest generator (no GUI dependency).

A *template* turns a handful of parameters (app name, namespace, storage size, ...)
into a coherent *set* of manifest_gen form values, e.g. PostgreSQL ->
StatefulSet + headless Service + ClusterIP Service + Secret + ConfigMap.
Every item is plain manifest_gen field values, so every object goes through
manifest_gen.generate() and gets exactly the same validation as hand-filled forms.

Adding a template = write a ``build(p)`` function returning ``[Item, ...]`` and
decorate it with ``@template(...)`` (see README "Adding an application template").
"""
from __future__ import annotations

import base64
import secrets as _secrets
import string
import uuid
from dataclasses import dataclass, field
from typing import Callable

import manifest_gen as gen

NONE_CHOICE = "None / blank"
GROUPS = ["Databases", "Messaging", "Web / proxy", "Apps / tools"]

# kind order inside a set / all-in-one.yaml (dependencies first)
KIND_ORDER = ["Namespace", "Secret", "ConfigMap", "PersistentVolumeClaim", "Service",
              "StatefulSet", "Deployment", "DaemonSet", "Job", "CronJob", "Ingress"]


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Param:
    key: str
    label: str
    type: str = "entry"          # entry | check | combo | ecombo
    default: object = ""
    options: list = field(default_factory=list)
    help: str = ""
    description: str = ""                 # hover help, filled in from field_docs.TEMPLATE_PARAMS
    maps_to: str = ""
    option_help: dict = field(default_factory=dict)

    def __post_init__(self):
        import field_docs
        field_docs.apply_param(self)


@dataclass
class Item:
    """One object of a set: a kind plus manifest_gen field values (overrides of the defaults)."""
    kind: str
    values: dict
    note: str = ""               # short human description shown in the GUI / README


@dataclass
class Template:
    title: str                   # shown in the dropdown, e.g. "PostgreSQL"
    group: str                   # one of GROUPS
    image: str                   # main image, pinned
    description: str
    build: Callable[[dict], list[Item]]
    default_name: str
    primary: str = "Deployment"  # kind the GUI jumps to after applying
    storage: str = ""            # default storage size ("" = no persistent storage)
    replicas: int | None = 1     # default replicas; None = fixed single instance (not scalable)
    web: bool = False            # has an HTTP UI worth exposing via Ingress
    secrets: tuple = ()          # names of generated random values
    caveats: str = ""

    @property
    def scalable(self) -> bool:
        return self.replicas is not None


REGISTRY: dict[str, Template] = {}


def template(title, group, image, default_name, **kw):
    """Decorator registering a build function as an application template."""
    if group not in GROUPS:
        raise ValueError(f"unknown template group {group!r}")

    def deco(fn):
        if title in REGISTRY:
            raise ValueError(f"duplicate template {title!r}")
        REGISTRY[title] = Template(title=title, group=group, image=image, description=(fn.__doc__ or "").strip(),
                                   build=fn, default_name=default_name, **kw)
        return fn
    return deco


def titles() -> list[str]:
    return list(REGISTRY)


def grouped() -> dict[str, list[str]]:
    out = {g: [] for g in GROUPS}
    for t in REGISTRY.values():
        out[t.group].append(t.title)
    return {g: ts for g, ts in out.items() if ts}


def get(title: str) -> Template:
    try:
        return REGISTRY[title]
    except KeyError:
        raise KeyError(f"unknown template {title!r}; known: {', '.join(REGISTRY)}") from None


def params_for(title: str) -> list[Param]:
    t = get(title)
    ps = [Param("name", "App name / release prefix", default=t.default_name,
                help="prefix for every object name (DNS-1123 label)"),
          Param("namespace", "Namespace", "ecombo", default="default", options=["default"],
                help="a Namespace object is added to the set when this isn't 'default'")]
    if t.scalable:
        ps.append(Param("replicas", "Replicas", default=str(t.replicas)))
    if t.storage:
        ps.append(Param("storage_size", "Storage size", default=t.storage))
        ps.append(Param("storage_class", "storageClass", "ecombo", default="", options=gen.STORAGE_CLASSES,
                        help="blank = cluster default StorageClass"))
    if t.web:
        ps += [Param("ingress", "Expose via Ingress", "check", default=False),
               Param("host", "Ingress host", default=f"{t.default_name}.example.com"),
               Param("ingress_class", "ingressClassName", "ecombo", default="nginx",
                     options=["nginx", "traefik", "haproxy", ""])]
    return ps


def default_params(title: str) -> dict:
    return {p.key: p.default for p in params_for(title)}


def random_password(n: int = 24) -> str:
    """URL/shell-safe random password (letters + digits) from the OS CSPRNG."""
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = "".join(_secrets.choice(alphabet) for _ in range(n))
        if any(ch.isdigit() for ch in pw) and any(ch.isalpha() for ch in pw):
            return pw


def _secret_value(name: str) -> str:
    if name == "kafka_cluster_id":       # KRaft cluster id = base64url (no padding) of a UUID
        return base64.urlsafe_b64encode(uuid.uuid4().bytes).decode().rstrip("=")
    if name == "erlang_cookie":
        return random_password(32)
    return random_password()


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
@dataclass
class RenderedItem:
    item: Item
    values: dict                 # full manifest_gen values (defaults + overrides)
    result: gen.Result
    primary: bool                # first item of its kind -> fills that kind's GUI form

    @property
    def manifest(self):
        return self.result.manifest

    @property
    def filename(self) -> str:
        return gen.filename_for(self.manifest) if self.manifest else f"{self.item.kind.lower()}.yaml"


@dataclass
class TemplateSet:
    title: str
    params: dict
    secrets: dict
    items: list[RenderedItem]
    errors: list[str]
    warnings: list[str]

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def name(self) -> str:
        return self.params["name"]

    @property
    def kinds(self) -> list[str]:
        return list(dict.fromkeys(r.item.kind for r in self.items))

    @property
    def primary_kind(self) -> str:
        return get(self.title).primary

    def primary_values(self) -> dict[str, dict]:
        """kind -> full values of the first item of that kind (what fills the GUI form)."""
        return {r.item.kind: r.values for r in self.items if r.primary}

    def manifests(self) -> list[dict]:
        return [r.manifest for r in self.items if r.manifest]

    def all_in_one(self) -> str:
        return gen.to_multi_yaml(self.manifests())


def _clean_params(title: str, params: dict | None, errs: list) -> dict:
    p = default_params(title)
    p.update({k: v for k, v in (params or {}).items() if k in p})
    for k, v in list(p.items()):
        if isinstance(v, str):
            p[k] = v.strip()
    if isinstance(p.get("ingress"), str):
        p["ingress"] = p["ingress"].lower() in ("1", "true", "yes", "on")
    name = p["name"]
    if not gen._DNS1123_LABEL.match(name or "") or len(name) > 40:
        errs.append(f"App name '{name}' must be a lowercase DNS-1123 label of at most 40 characters "
                    "(it is used as a prefix for every object name).")
        p["name"] = get(title).default_name
    ns = p.get("namespace") or "default"
    if not gen._DNS1123_LABEL.match(ns):
        errs.append(f"Namespace '{ns}' must be a DNS-1123 label.")
        ns = "default"
    p["namespace"] = ns
    if "replicas" in p:
        try:
            r = int(str(p["replicas"]))
            if r < 0:
                raise ValueError
        except ValueError:
            errs.append(f"Replicas must be a whole number >= 0 (got '{p['replicas']}').")
            r = 1
        p["replicas"] = r
    else:
        p["replicas"] = 1
    if "storage_size" in p and not gen._QUANTITY.match(p["storage_size"] or ""):
        errs.append(f"Storage size '{p['storage_size']}' is not a valid quantity (e.g. 8Gi).")
        p["storage_size"] = get(title).storage
    if p.get("ingress"):
        host = (p.get("host") or "").lower()
        if host and not gen._HOST.match(host):
            errs.append(f"Ingress host '{host}' is not a valid DNS name.")
        p["host"] = host
    return p


def render(title: str, params: dict | None = None, secrets: dict | None = None) -> TemplateSet:
    """Build a template set. Pass back ``set.secrets`` to keep passwords stable across re-renders."""
    t = get(title)
    errs: list[str] = []
    warns: list[str] = []
    p = _clean_params(title, params, errs)
    sec = dict(secrets or {})
    for s in t.secrets:
        if not sec.get(s):
            sec[s] = _secret_value(s)
    p["_secrets"] = sec
    items = t.build(p)
    if p["namespace"] != "default":
        items.insert(0, Item("Namespace", {"name": p["namespace"], "labels": f"kubernetes.io/metadata.name={p['namespace']}"},
                             "namespace for the set"))
    items.sort(key=lambda it: KIND_ORDER.index(it.kind))
    seen: set[str] = set()
    out: list[RenderedItem] = []
    for it in items:
        values = gen.defaults(it.kind)
        unknown = set(it.values) - set(values)
        if unknown:
            raise KeyError(f"template {title}: unknown {it.kind} field(s) {sorted(unknown)}")
        values.update(it.values)
        res = gen.generate(it.kind, values)
        label = f"{it.kind}/{values.get('name', '')}"
        errs += [f"{label}: {e}" for e in res.errors]
        warns += [f"{label}: {w}" for w in res.warnings]
        out.append(RenderedItem(it, values, res, it.kind not in seen))
        seen.add(it.kind)
    del p["_secrets"]
    if t.caveats:
        warns.append(f"{title}: {t.caveats}")
    return TemplateSet(title, p, sec, out, errs, warns)


# --------------------------------------------------------------------------- #
# Builders (shared helpers)
# --------------------------------------------------------------------------- #
MANAGED_BY = "app.kubernetes.io/managed-by=k8s-manifest-gui"


def _kv(d: dict) -> str:
    return "\n".join(f"{k}={v}" for k, v in d.items())


def selector(p, app: str, component: str = "") -> dict:
    s = {"app.kubernetes.io/name": app, "app.kubernetes.io/instance": p["name"]}
    if component:
        s["app.kubernetes.io/component"] = component
    return s


def labels(p, app: str, version: str = "", component: str = "") -> str:
    d = selector(p, app, component)
    if version:
        d["app.kubernetes.io/version"] = version
    return _kv(d) + "\n" + MANAGED_BY


def _tag(image: str) -> str:
    tag = image.rsplit(":", 1)[-1]
    return gen._LABEL_NAME.match(tag) and tag[:63] or ""


def workload(p, kind: str, name: str, app: str, image: str, *, component: str = "",
             ports: str, replicas=None, **values) -> dict:
    """Common workload values: metadata, labels/selector, image, resources."""
    v = {
        "name": name, "namespace": p["namespace"],
        "labels": labels(p, app, _tag(image), component),
        "selector": _kv(selector(p, app, component)),
        "container_name": app, "image": image, "ports": ports,
        "replicas": str(p["replicas"] if replicas is None else replicas),
    }
    if kind == "StatefulSet":
        v.update(service_name=f"{name}-headless", vct_name="", vct_mount="", vct_size=p.get("storage_size", "1Gi"),
                 vct_storage_class=p.get("storage_class", ""))
    v.update(values)
    return v


def statefulset(p, name, app, image, *, data_mount: str, **values) -> Item:
    v = workload(p, "StatefulSet", name, app, image, vct_name="data", vct_mount=data_mount, **values)
    return Item("StatefulSet", v, f"{image}, 1 PVC per pod via volumeClaimTemplate ({p.get('storage_size')})")


def service(p, name, app, *, port_name, port, target=None, extra="", component="", headless=False,
            publish_not_ready=False, note="") -> Item:
    v = {"name": name, "namespace": p["namespace"], "labels": labels(p, app, component=component),
         "selector": _kv(selector(p, app, component)), "service_type": "ClusterIP",
         "port_name": port_name, "port": str(port), "target_port": str(target or port_name),
         "extra_ports": extra, "cluster_ip": "None" if headless else "",
         "publish_not_ready": publish_not_ready}
    return Item("Service", v, note or ("headless Service (stable pod DNS for the StatefulSet)" if headless
                                       else f"ClusterIP Service {name}:{port}"))


def db_services(p, app, *, port_name, port, extra="", component="", base=None, publish_not_ready=False) -> list[Item]:
    base = base or p["name"]
    return [service(p, base, app, port_name=port_name, port=port, extra=extra, component=component,
                    note=f"ClusterIP Service for clients ({base}:{port})"),
            service(p, f"{base}-headless", app, port_name=port_name, port=port, extra=extra, component=component,
                    headless=True, publish_not_ready=publish_not_ready)]


def secret(p, data: dict, name=None, note="random credentials (base64-encoded in the manifest)") -> Item:
    return Item("Secret", {"name": name or f"{p['name']}-secret", "namespace": p["namespace"],
                           "labels": MANAGED_BY + f"\napp.kubernetes.io/instance={p['name']}",
                           "secret_type": "Opaque", "data": _kv(data)}, note)


def configmap(p, file_key: str, content: str, name=None, data: dict | None = None, note="") -> Item:
    return Item("ConfigMap", {"name": name or f"{p['name']}-config", "namespace": p["namespace"],
                              "labels": MANAGED_BY + f"\napp.kubernetes.io/instance={p['name']}",
                              "data": _kv(data or {}), "file_key": file_key, "file_content": content},
                note or f"{file_key}")


def pvc(p, name=None, size=None, note="") -> Item:
    return Item("PersistentVolumeClaim", {"name": name or f"{p['name']}-data", "namespace": p["namespace"],
                                          "labels": MANAGED_BY + f"\napp.kubernetes.io/instance={p['name']}",
                                          "size": size or p["storage_size"], "access_mode": "ReadWriteOnce",
                                          "storage_class": p.get("storage_class", "")},
                note or f"PersistentVolumeClaim ({size or p['storage_size']})")


def ingress(p, service_name, port) -> list[Item]:
    if not p.get("ingress"):
        return []
    return [Item("Ingress", {"name": p["name"], "namespace": p["namespace"],
                             "labels": MANAGED_BY + f"\napp.kubernetes.io/instance={p['name']}",
                             "ingress_class": p.get("ingress_class", ""), "host": p.get("host", ""), "path": "/",
                             "path_type": "Prefix", "backend_service": service_name, "backend_port": str(port)},
                 f"Ingress {p.get('host') or '*'} -> {service_name}:{port}")]


def sec_ref(p, key, secret_name=None) -> str:
    return f"@secret:{secret_name or p['name'] + '-secret'}/{key}"


def svc_host(p, name) -> str:
    return f"{name}.{p['namespace']}.svc.cluster.local"


def res(cpu_req, mem_req, cpu_lim, mem_lim) -> dict:
    return {"cpu_request": cpu_req, "memory_request": mem_req, "cpu_limit": cpu_lim, "memory_limit": mem_lim}


def exec_probes(cmd, live_cmd=None, *, ready_delay=5, live_delay=30, period=10, timeout=5, failures=6) -> dict:
    return {"readiness_type": "exec", "readiness_command": cmd, "liveness_type": "exec",
            "liveness_command": live_cmd or cmd, "readiness_initial_delay": str(ready_delay),
            "liveness_initial_delay": str(live_delay), "probe_period": str(period),
            "probe_timeout": str(timeout), "probe_failure_threshold": str(failures)}


def http_probes(ready_path, live_path=None, port="", *, ready_delay=5, live_delay=15, period=10, timeout=3,
                failures=3, live_type="httpGet") -> dict:
    d = {"readiness_type": "httpGet", "readiness_path": ready_path, "liveness_type": live_type,
         "liveness_path": live_path or ready_path if live_type == "httpGet" else "", "probe_port": str(port),
         "readiness_initial_delay": str(ready_delay), "liveness_initial_delay": str(live_delay),
         "probe_period": str(period), "probe_timeout": str(timeout), "probe_failure_threshold": str(failures)}
    return d


def tcp_probes(port, *, ready_delay=5, live_delay=15, period=10, timeout=3, failures=3) -> dict:
    return {"readiness_type": "tcpSocket", "liveness_type": "tcpSocket", "probe_port": str(port),
            "readiness_initial_delay": str(ready_delay), "liveness_initial_delay": str(live_delay),
            "probe_period": str(period), "probe_timeout": str(timeout), "probe_failure_threshold": str(failures)}


def _pw(p, key):
    return p["_secrets"][key]


# --------------------------------------------------------------------------- #
# Databases
# --------------------------------------------------------------------------- #
POSTGRES_IMAGE = "postgres:18.6"
MYSQL_IMAGE = "mysql:8.4.11"


@template("PostgreSQL", "Databases", POSTGRES_IMAGE, "postgres", primary="StatefulSet", storage="8Gi",
          replicas=None, secrets=("password",))
def _postgres(p):
    """StatefulSet (PGDATA on a volumeClaimTemplate, pg_isready probes), ClusterIP + headless Service,
    Secret with a random password, ConfigMap with an init SQL script (runs on first start)."""
    n = p["name"]
    sts = statefulset(p, n, "postgresql", POSTGRES_IMAGE, data_mount="/var/lib/postgresql", ports="postgres:5432",
                      replicas=1,
                      env="PGDATA=/var/lib/postgresql/18/docker", env_from=f"secret:{n}-secret",
                      volumes=f"initdb:configMap:{n}-config:/docker-entrypoint-initdb.d:ro",
                      **res("250m", "256Mi", "1", "1Gi"),
                      **exec_probes("sh -c 'pg_isready -h 127.0.0.1 -U \"$POSTGRES_USER\" -d \"$POSTGRES_DB\"'",
                                    ready_delay=5, live_delay=30))
    return [sts, *db_services(p, "postgresql", port_name="postgres", port=5432),
            secret(p, {"POSTGRES_USER": "app", "POSTGRES_PASSWORD": _pw(p, "password"), "POSTGRES_DB": "app"}),
            configmap(p, "00-init.sql", "-- Runs once, when the data directory is first initialised.\n"
                                        "-- Add CREATE TABLE / CREATE EXTENSION statements here.\n"
                                        "SELECT current_database() AS initialised_db;\n",
                      note="init SQL mounted at /docker-entrypoint-initdb.d")]


@template("MySQL", "Databases", MYSQL_IMAGE, "mysql", primary="StatefulSet", storage="8Gi", replicas=None,
          secrets=("root_password", "password"))
def _mysql(p):
    """StatefulSet (datadir on a volumeClaimTemplate, mysqladmin ping probes), ClusterIP + headless Service,
    Secret (root + app user passwords), ConfigMap with a my.cnf override."""
    return _mysql_items(p, p["name"], "mysql")


def _mysql_items(p, n, app, component=""):
    sec = f"{p['name']}-secret"
    sts = statefulset(p, n, app, MYSQL_IMAGE, data_mount="/var/lib/mysql", ports="mysql:3306", component=component,
                      replicas=1, env_from=f"secret:{sec}",
                      volumes=f"conf:configMap:{n}-config:/etc/mysql/conf.d:ro",
                      **res("250m", "512Mi", "1", "1Gi"),
                      **exec_probes("sh -c 'mysqladmin ping -h 127.0.0.1 -uroot -p\"$MYSQL_ROOT_PASSWORD\" --silent'",
                                    "sh -c 'mysqladmin ping -uroot -p\"$MYSQL_ROOT_PASSWORD\" --silent'",
                                    ready_delay=10, live_delay=60, failures=10))
    cfg = configmap(p, "custom.cnf", "[mysqld]\n# tuned for a small pod; adjust to the memory limit\n"
                                     "innodb_buffer_pool_size=256M\nmax_connections=200\n",
                    name=f"{n}-config", note="my.cnf override mounted at /etc/mysql/conf.d")
    return [sts, *db_services(p, app, port_name="mysql", port=3306, component=component, base=n),
            secret(p, {"MYSQL_ROOT_PASSWORD": _pw(p, "root_password"), "MYSQL_DATABASE": "app",
                       "MYSQL_USER": "app", "MYSQL_PASSWORD": _pw(p, "password")}, name=sec), cfg]


@template("MariaDB", "Databases", "mariadb:11.8.9", "mariadb", primary="StatefulSet", storage="8Gi", replicas=None,
          secrets=("root_password", "password"))
def _mariadb(p):
    """StatefulSet (healthcheck.sh probes), ClusterIP + headless Service, Secret, ConfigMap (my.cnf override)."""
    n = p["name"]
    sts = statefulset(p, n, "mariadb", "mariadb:11.8.9", data_mount="/var/lib/mysql", ports="mysql:3306",
                      replicas=1, env_from=f"secret:{n}-secret",
                      volumes=f"conf:configMap:{n}-config:/etc/mysql/conf.d:ro",
                      **res("250m", "256Mi", "1", "1Gi"),
                      **exec_probes("healthcheck.sh --connect --innodb_initialized",
                                    ready_delay=10, live_delay=60, failures=10))
    return [sts, *db_services(p, "mariadb", port_name="mysql", port=3306),
            secret(p, {"MARIADB_ROOT_PASSWORD": _pw(p, "root_password"), "MARIADB_DATABASE": "app",
                       "MARIADB_USER": "app", "MARIADB_PASSWORD": _pw(p, "password")}),
            configmap(p, "custom.cnf", "[mariadb]\ninnodb_buffer_pool_size=256M\nmax_connections=200\n",
                      note="my.cnf override mounted at /etc/mysql/conf.d")]


@template("MongoDB", "Databases", "mongo:8.0.34", "mongodb", primary="StatefulSet", storage="8Gi", replicas=None,
          secrets=("password",))
def _mongodb(p):
    """StatefulSet (/data/db on a volumeClaimTemplate, mongosh ping probes), ClusterIP + headless Service,
    Secret with the root user/password."""
    n = p["name"]
    sts = statefulset(p, n, "mongodb", "mongo:8.0.34", data_mount="/data/db", ports="mongodb:27017", replicas=1,
                      env_from=f"secret:{n}-secret", **res("250m", "512Mi", "1", "1Gi"),
                      **exec_probes("mongosh --quiet --norc --eval \"db.adminCommand('ping').ok\"",
                                    ready_delay=10, live_delay=30, timeout=10))
    return [sts, *db_services(p, "mongodb", port_name="mongodb", port=27017),
            secret(p, {"MONGO_INITDB_ROOT_USERNAME": "root", "MONGO_INITDB_ROOT_PASSWORD": _pw(p, "password")})]


def _redis_like(p, app, image, server, cli, env_key):
    n = p["name"]
    sts = statefulset(p, n, app, image, data_mount="/data", ports=f"{app}:6379", replicas=1,
                      args=f"{server}\n--appendonly\nyes\n--requirepass\n$({env_key})",
                      env=f"{env_key}={sec_ref(p, env_key)}", **res("100m", "128Mi", "500m", "512Mi"),
                      **exec_probes(f"sh -c '{cli} -a \"${env_key}\" --no-auth-warning ping | grep -q PONG'",
                                    ready_delay=5, live_delay=15))
    return [sts, *db_services(p, app, port_name=app, port=6379),
            secret(p, {env_key: _pw(p, "password")})]


@template("Redis", "Databases", "redis:8.10.2", "redis", primary="StatefulSet", storage="2Gi", replicas=None,
          secrets=("password",))
def _redis(p):
    """StatefulSet (AOF persistence on a volumeClaimTemplate, requirepass from the Secret, redis-cli ping
    probes), ClusterIP + headless Service, Secret."""
    return _redis_like(p, "redis", "redis:8.10.2", "redis-server", "redis-cli", "REDIS_PASSWORD")


@template("Valkey", "Databases", "valkey/valkey:9.1.2", "valkey", primary="StatefulSet", storage="2Gi",
          replicas=None, secrets=("password",))
def _valkey(p):
    """Redis-compatible Valkey: StatefulSet (AOF, requirepass, valkey-cli ping probes), Services, Secret."""
    return _redis_like(p, "valkey", "valkey/valkey:9.1.2", "valkey-server", "valkey-cli", "VALKEY_PASSWORD")


@template("Cassandra", "Databases", "cassandra:5.0.9", "cassandra", primary="StatefulSet", storage="10Gi",
          replicas=1, caveats="authentication is off (AllowAllAuthenticator) - enable PasswordAuthenticator "
                              "before exposing it; each node wants >= 2 GiB RAM.")
def _cassandra(p):
    """StatefulSet (seed = pod 0 via the headless Service, nodetool readiness), ClusterIP CQL Service,
    headless Service with publishNotReadyAddresses (gossip), small heap for test clusters."""
    n = p["name"]
    seed = f"{n}-0.{n}-headless.{p['namespace']}.svc.cluster.local"
    sts = statefulset(p, n, "cassandra", "cassandra:5.0.9", data_mount="/var/lib/cassandra",
                      ports="cql:9042\nintra:7000", pod_management="OrderedReady",
                      env=_kv({"CASSANDRA_CLUSTER_NAME": n, "CASSANDRA_SEEDS": seed, "CASSANDRA_DC": "dc1",
                               "CASSANDRA_RACK": "rack1", "CASSANDRA_ENDPOINT_SNITCH": "GossipingPropertyFileSnitch",
                               "MAX_HEAP_SIZE": "512M", "HEAP_NEWSIZE": "128M"}),
                      termination_grace="60", **res("500m", "1Gi", "2", "2Gi"),
                      readiness_type="exec",
                      readiness_command="sh -c 'nodetool info | grep -q \"Native Transport active: true\"'",
                      liveness_type="tcpSocket", probe_port="intra", readiness_initial_delay="30",
                      liveness_initial_delay="90", probe_period="15", probe_timeout="10",
                      probe_failure_threshold="10")
    return [sts, service(p, n, "cassandra", port_name="cql", port=9042, note=f"ClusterIP CQL Service {n}:9042"),
            service(p, f"{n}-headless", "cassandra", port_name="cql", port=9042, extra="intra:7000:intra",
                    headless=True, publish_not_ready=True)]


def _search(p, app, image, data_dir, env):
    n = p["name"]
    sts = statefulset(p, n, app, image, data_mount=data_dir, ports="http:9200\ntransport:9300", replicas=1,
                      env=_kv(env), fs_group="1000", **res("500m", "1Gi", "2", "2Gi"),
                      **http_probes("/_cluster/health?local=true", port="http", ready_delay=20,
                                    live_delay=90, period=10, timeout=5, failures=10, live_type="tcpSocket"))
    return [sts, *db_services(p, app, port_name="http", port=9200)]


@template("Elasticsearch", "Databases", "elasticsearch:9.5.5", "elasticsearch", primary="StatefulSet",
          storage="10Gi", replicas=None,
          caveats="single-node with xpack security disabled (plain HTTP, no auth) - for dev/test only.")
def _elasticsearch(p):
    """Single-node StatefulSet (discovery.type=single-node, 512m heap, fsGroup 1000), HTTP health probes,
    ClusterIP + headless Service."""
    return _search(p, "elasticsearch", "elasticsearch:9.5.5", "/usr/share/elasticsearch/data",
                   {"discovery.type": "single-node", "cluster.name": p["name"], "xpack.security.enabled": "false",
                    "ES_JAVA_OPTS": "-Xms512m -Xmx512m"})


@template("OpenSearch", "Databases", "opensearchproject/opensearch:3.9.0", "opensearch", primary="StatefulSet",
          storage="10Gi", replicas=None,
          caveats="single-node with the security plugin disabled (plain HTTP, no auth) - for dev/test only.")
def _opensearch(p):
    """Single-node StatefulSet (security plugin off, 512m heap, fsGroup 1000), HTTP health probes,
    ClusterIP + headless Service."""
    return _search(p, "opensearch", "opensearchproject/opensearch:3.9.0", "/usr/share/opensearch/data",
                   {"discovery.type": "single-node", "cluster.name": p["name"],
                    "DISABLE_SECURITY_PLUGIN": "true", "DISABLE_INSTALL_DEMO_CONFIG": "true",
                    "OPENSEARCH_JAVA_OPTS": "-Xms512m -Xmx512m"})


@template("Memcached", "Databases", "memcached:1.6.45", "memcached", replicas=1)
def _memcached(p):
    """Deployment (64 MiB cache, TCP probes) + ClusterIP Service on 11211."""
    n = p["name"]
    d = workload(p, "Deployment", n, "memcached", "memcached:1.6.45", ports="memcache:11211",
                 args="-m 64 -c 1024", **res("50m", "96Mi", "500m", "128Mi"), **tcp_probes("memcache"))
    return [Item("Deployment", d, "memcached:1.6.45, -m 64"),
            service(p, n, "memcached", port_name="memcache", port=11211)]


# --------------------------------------------------------------------------- #
# Messaging
# --------------------------------------------------------------------------- #
@template("RabbitMQ", "Messaging", "rabbitmq:4.3.6-management", "rabbitmq", primary="StatefulSet", storage="2Gi",
          replicas=None, web=True, secrets=("password",))
def _rabbitmq(p):
    """StatefulSet with the management plugin (rabbitmq-diagnostics probes, data on a volumeClaimTemplate),
    ClusterIP (AMQP + management UI) and headless Service, Secret with the default user, optional Ingress
    for the management UI."""
    n = p["name"]
    sts = statefulset(p, n, "rabbitmq", "rabbitmq:4.3.6-management", data_mount="/var/lib/rabbitmq",
                      ports="amqp:5672\nmanagement:15672", replicas=1, env_from=f"secret:{n}-secret",
                      **res("250m", "512Mi", "1", "1Gi"),
                      **exec_probes("rabbitmq-diagnostics -q ping", "rabbitmq-diagnostics -q status",
                                    ready_delay=15, live_delay=60, period=15, timeout=15, failures=6))
    return [sts, *db_services(p, "rabbitmq", port_name="amqp", port=5672, extra="management:15672:management"),
            secret(p, {"RABBITMQ_DEFAULT_USER": "admin", "RABBITMQ_DEFAULT_PASS": _pw(p, "password")}),
            *ingress(p, n, 15672)]


@template("Kafka (KRaft)", "Messaging", "apache/kafka:4.3.2", "kafka", primary="StatefulSet", storage="8Gi",
          replicas=None, secrets=("kafka_cluster_id",),
          caveats="single combined broker+controller with PLAINTEXT listeners - add SASL/TLS for anything real.")
def _kafka(p):
    """Single-node KRaft StatefulSet (broker+controller, fixed CLUSTER_ID, replication factor 1,
    enableServiceLinks off so Kubernetes' KAFKA_* env vars don't leak into the config), ClusterIP + headless."""
    n = p["name"]
    env = {
        "KAFKA_NODE_ID": "1", "KAFKA_PROCESS_ROLES": "broker,controller", "CLUSTER_ID": _pw(p, "kafka_cluster_id"),
        "KAFKA_LISTENERS": "PLAINTEXT://:9092,CONTROLLER://:9093",
        "KAFKA_ADVERTISED_LISTENERS": f"PLAINTEXT://{svc_host(p, n)}:9092",
        "KAFKA_CONTROLLER_LISTENER_NAMES": "CONTROLLER",
        "KAFKA_LISTENER_SECURITY_PROTOCOL_MAP": "CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT",
        "KAFKA_CONTROLLER_QUORUM_VOTERS": "1@localhost:9093",
        "KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR": "1", "KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR": "1",
        "KAFKA_TRANSACTION_STATE_LOG_MIN_ISR": "1", "KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS": "0",
        "KAFKA_NUM_PARTITIONS": "3", "KAFKA_LOG_DIRS": "/var/lib/kafka/data",
        "KAFKA_HEAP_OPTS": "-Xms512m -Xmx512m",
    }
    sts = statefulset(p, n, "kafka", "apache/kafka:4.3.2", data_mount="/var/lib/kafka/data",
                      ports="kafka:9092\ncontroller:9093", replicas=1, env=_kv(env), fs_group="1000",
                      enable_service_links=False, **res("500m", "1Gi", "1", "1536Mi"),
                      **tcp_probes("kafka", ready_delay=10, live_delay=60, failures=6))
    return [sts, *db_services(p, "kafka", port_name="kafka", port=9092)]


@template("NATS (JetStream)", "Messaging", "nats:2.14.8", "nats", primary="StatefulSet", storage="2Gi",
          replicas=None)
def _nats(p):
    """Single-server StatefulSet with JetStream (file store on a volumeClaimTemplate), /healthz probes on the
    monitoring port, ClusterIP + headless Service."""
    n = p["name"]
    sts = statefulset(p, n, "nats", "nats:2.14.8", data_mount="/data", replicas=1,
                      ports="client:4222\nmonitor:8222", args="--jetstream\n--store_dir=/data\n--http_port=8222",
                      **res("100m", "128Mi", "500m", "512Mi"),
                      **http_probes("/healthz?js-enabled-only=true", "/healthz", port="monitor"))
    return [sts, *db_services(p, "nats", port_name="client", port=4222, extra="monitor:8222:monitor")]


@template("Mosquitto (MQTT)", "Messaging", "eclipse-mosquitto:2.0.22", "mosquitto", storage="1Gi", replicas=None,
          caveats="allow_anonymous is on - add a password_file before exposing it.")
def _mosquitto(p):
    """Deployment (Recreate) + PVC for persistence, ConfigMap with mosquitto.conf, ClusterIP Service on 1883."""
    n = p["name"]
    d = workload(p, "Deployment", n, "mosquitto", "eclipse-mosquitto:2.0.22", ports="mqtt:1883", replicas=1,
                 strategy="Recreate", fs_group="1883",
                 volumes=f"config:configMap:{n}-config:/mosquitto/config:ro\ndata:pvc:{n}-data:/mosquitto/data",
                 **res("50m", "64Mi", "500m", "256Mi"), **tcp_probes("mqtt"))
    conf = ("listener 1883\nallow_anonymous true\npersistence true\npersistence_location /mosquitto/data/\n"
            "log_dest stdout\n")
    return [Item("Deployment", d, "eclipse-mosquitto:2.0.22"), service(p, n, "mosquitto", port_name="mqtt", port=1883),
            configmap(p, "mosquitto.conf", conf, note="mosquitto.conf"), pvc(p)]


# --------------------------------------------------------------------------- #
# Web / proxy
# --------------------------------------------------------------------------- #
NGINX_CONF = """server {
    listen       80;
    server_name  _;

    location = /healthz {
        access_log off;
        default_type text/plain;
        return 200 "ok\\n";
    }

    location / {
        root   /usr/share/nginx/html;
        index  index.html;
        try_files $uri $uri/ =404;
    }
}
"""


def _web(p, app, image, *, port, conf_key, conf, conf_dir, probe, cm_note, **extra):
    n = p["name"]
    d = workload(p, "Deployment", n, app, image, ports=f"http:{port}",
                 volumes=f"config:configMap:{n}-config:{conf_dir}:ro",
                 **{**res("50m", "64Mi", "500m", "256Mi"), **http_probes(probe, port="http"), **extra})
    return [Item("Deployment", d, image), service(p, n, app, port_name="http", port=80, target="http"),
            configmap(p, conf_key, conf, note=cm_note), *ingress(p, n, 80)]


@template("nginx", "Web / proxy", "nginx:1.30.6", "nginx", replicas=2, web=True)
def _nginx(p):
    """Deployment (2 replicas, /healthz probes) + ClusterIP Service + ConfigMap with default.conf mounted at
    /etc/nginx/conf.d + optional Ingress."""
    return _web(p, "nginx", "nginx:1.30.6", port=80, conf_key="default.conf", conf=NGINX_CONF,
                conf_dir="/etc/nginx/conf.d", probe="/healthz", cm_note="default.conf (with /healthz)")


@template("Apache httpd", "Web / proxy", "httpd:2.4.69", "httpd", replicas=2, web=True)
def _httpd(p):
    """Deployment + ClusterIP Service + ConfigMap with an index.html mounted as the htdocs dir + optional Ingress."""
    html = "<!doctype html>\n<html><head><title>It works</title></head>\n<body><h1>It works!</h1>" \
           "<p>Served by Apache httpd on Kubernetes.</p></body></html>\n"
    return _web(p, "httpd", "httpd:2.4.69", port=80, conf_key="index.html", conf=html,
                conf_dir="/usr/local/apache2/htdocs", probe="/", cm_note="index.html (htdocs)")


@template("HAProxy", "Web / proxy", "haproxy:3.2.26", "haproxy", replicas=2, web=True)
def _haproxy(p):
    """Deployment (non-root, :8080 frontend, :8404 stats + /healthz) + Service + ConfigMap haproxy.cfg whose
    backend points at '<name>-backend:80' (edit it) + optional Ingress."""
    n = p["name"]
    cfg = f"""global
    log stdout format raw local0

defaults
    mode http
    log global
    option httplog
    timeout connect 5s
    timeout client 30s
    timeout server 30s
    # start even when the backend name does not resolve yet
    default-server init-addr last,libc,none

frontend stats
    bind :8404
    http-request return status 200 content-type text/plain string ok if {{ path /healthz }}
    stats enable
    stats uri /stats
    stats refresh 10s

frontend web
    bind :8080
    default_backend app

backend app
    balance roundrobin
    server app1 {svc_host(p, n + '-backend')}:80 check
"""
    items = _web(p, "haproxy", "haproxy:3.2.26", port=8080, conf_key="haproxy.cfg", conf=cfg,
                 conf_dir="/usr/local/etc/haproxy", probe="/healthz", cm_note="haproxy.cfg",
                 probe_port="stats")
    items[0].values["ports"] = "http:8080\nstats:8404"
    items[1].values["extra_ports"] = "stats:8404:stats"
    return items


@template("Caddy", "Web / proxy", "caddy:2.11.7", "caddy", replicas=2, web=True)
def _caddy(p):
    """Deployment + ClusterIP Service + ConfigMap with a Caddyfile (static file server, /healthz) mounted at
    /etc/caddy + optional Ingress (TLS is left to the Ingress; Caddy serves plain :80)."""
    cfg = ":80 {\n\trespond /healthz \"ok\" 200\n\troot * /usr/share/caddy\n\tfile_server\n}\n"
    items = _web(p, "caddy", "caddy:2.11.7", port=80, conf_key="Caddyfile", conf=cfg, conf_dir="/etc/caddy",
                 probe="/healthz", cm_note="Caddyfile")
    items[0].values["volumes"] += "\ndata:emptyDir::/data"
    return items


# --------------------------------------------------------------------------- #
# Apps / tools
# --------------------------------------------------------------------------- #
@template("WordPress + MySQL", "Apps / tools", "wordpress:7.1.3-php8.3-apache", "wordpress", storage="5Gi",
          replicas=None, web=True, secrets=("root_password", "password"))
def _wordpress(p):
    """WordPress Deployment (Recreate, wp-content on a PVC) + Service + optional Ingress, plus a MySQL 8.4
    StatefulSet '<name>-mysql' with its ClusterIP/headless Services, ConfigMap and one shared Secret."""
    n = p["name"]
    db = f"{n}-mysql"
    sec = f"{n}-secret"
    env = {"WORDPRESS_DB_HOST": f"{db}:3306", "WORDPRESS_DB_NAME": f"@secret:{sec}/MYSQL_DATABASE",
           "WORDPRESS_DB_USER": f"@secret:{sec}/MYSQL_USER", "WORDPRESS_DB_PASSWORD": f"@secret:{sec}/MYSQL_PASSWORD"}
    d = workload(p, "Deployment", n, "wordpress", "wordpress:7.1.3-php8.3-apache", component="web",
                 ports="http:80", replicas=1, strategy="Recreate", env=_kv(env),
                 volumes=f"content:pvc:{n}-data:/var/www/html", **res("100m", "256Mi", "1", "512Mi"),
                 # a static file: /wp-login.php redirects to install.php until WordPress is set up, and
                 # install.php calls api.wordpress.org (slow / times out without internet egress)
                 **http_probes("/wp-includes/images/blank.gif", port="http", ready_delay=5, live_delay=30,
                               timeout=5, failures=6, live_type="tcpSocket"))
    mysql = _mysql_items(p, db, "mysql", component="database")
    mysql[-2].values["data"] = _kv({"MYSQL_ROOT_PASSWORD": _pw(p, "root_password"), "MYSQL_DATABASE": "wordpress",
                                    "MYSQL_USER": "wordpress", "MYSQL_PASSWORD": _pw(p, "password")})
    return [Item("Deployment", d, "wordpress:7.1.3-php8.3-apache, wp-content on a PVC"),
            service(p, n, "wordpress", port_name="http", port=80, component="web"),
            pvc(p, note=f"wp-content PVC ({p['storage_size']})"), *ingress(p, n, 80), *mysql]


@template("Grafana", "Apps / tools", "grafana/grafana:13.2.4", "grafana", storage="2Gi", replicas=None, web=True,
          secrets=("password",))
def _grafana(p):
    """Deployment (Recreate, fsGroup 472, /api/health probes) + PVC for /var/lib/grafana + Secret (admin
    password) + ConfigMap provisioning a Prometheus datasource (http://prometheus:9090) + Service + Ingress."""
    n = p["name"]
    ds = ("apiVersion: 1\ndatasources:\n  - name: Prometheus\n    type: prometheus\n    access: proxy\n"
          f"    url: http://prometheus.{p['namespace']}.svc.cluster.local:9090\n    isDefault: true\n")
    d = workload(p, "Deployment", n, "grafana", "grafana/grafana:13.2.4", ports="http:3000", replicas=1,
                 strategy="Recreate", fs_group="472", env_from=f"secret:{n}-secret",
                 volumes=(f"storage:pvc:{n}-data:/var/lib/grafana\n"
                          f"datasources:configMap:{n}-config:/etc/grafana/provisioning/datasources:ro"),
                 **res("100m", "128Mi", "1", "512Mi"),
                 **http_probes("/api/health", port="http", ready_delay=10, live_delay=30, failures=6))
    return [Item("Deployment", d, "grafana/grafana:13.2.4"),
            service(p, n, "grafana", port_name="http", port=80, target="http"),
            secret(p, {"GF_SECURITY_ADMIN_USER": "admin", "GF_SECURITY_ADMIN_PASSWORD": _pw(p, "password")}),
            configmap(p, "prometheus.yaml", ds, note="datasource provisioning (Prometheus)"), pvc(p),
            *ingress(p, n, 80)]


@template("Prometheus", "Apps / tools", "prom/prometheus:v3.5.5", "prometheus", storage="10Gi", replicas=None,
          web=True, caveats="scrapes only itself; Kubernetes service discovery needs a ServiceAccount + RBAC "
                            "(not generated by this app).")
def _prometheus(p):
    """Deployment (Recreate, runs as nobody/fsGroup 65534, /-/ready and /-/healthy probes) + PVC for the TSDB
    + ConfigMap prometheus.yml (self-scrape) + Service + optional Ingress."""
    n = p["name"]
    cfg = ("global:\n  scrape_interval: 30s\n  evaluation_interval: 30s\n\nscrape_configs:\n"
           "  - job_name: prometheus\n    static_configs:\n      - targets: ['localhost:9090']\n")
    d = workload(p, "Deployment", n, "prometheus", "prom/prometheus:v3.5.5", ports="http:9090", replicas=1,
                 strategy="Recreate", fs_group="65534", run_as_user="65534", run_as_group="65534",
                 run_as_non_root=True,
                 args="--config.file=/etc/prometheus/prometheus.yml\n--storage.tsdb.path=/prometheus\n"
                      "--storage.tsdb.retention.time=15d\n--web.enable-lifecycle",
                 volumes=f"config:configMap:{n}-config:/etc/prometheus:ro\ntsdb:pvc:{n}-data:/prometheus",
                 **res("200m", "256Mi", "1", "1Gi"),
                 **http_probes("/-/ready", "/-/healthy", port="http", ready_delay=5, live_delay=30))
    return [Item("Deployment", d, "prom/prometheus:v3.5.5 (LTS)"),
            service(p, n, "prometheus", port_name="http", port=9090, target="http"),
            configmap(p, "prometheus.yml", cfg, note="prometheus.yml (self-scrape)"), pvc(p),
            *ingress(p, n, 9090)]


@template("Jenkins", "Apps / tools", "jenkins/jenkins:2.580.1-lts-jdk21", "jenkins", storage="10Gi",
          replicas=None, web=True,
          caveats="the setup wizard's initial admin password is in the pod log / "
                  "/var/jenkins_home/secrets/initialAdminPassword.")
def _jenkins(p):
    """Deployment (Recreate, fsGroup 1000, /login probes with a long start-up allowance) + PVC for
    /var/jenkins_home + Service (UI 8080 + agent 50000) + optional Ingress."""
    n = p["name"]
    d = workload(p, "Deployment", n, "jenkins", "jenkins/jenkins:2.580.1-lts-jdk21", replicas=1,
                 ports="http:8080\nagent:50000", strategy="Recreate", fs_group="1000",
                 env="JAVA_OPTS=-Xmx1g", volumes=f"home:pvc:{n}-data:/var/jenkins_home",
                 **res("500m", "1Gi", "2", "2Gi"),
                 **http_probes("/login", port="http", ready_delay=30, live_delay=120, timeout=5, failures=10))
    return [Item("Deployment", d, "jenkins/jenkins LTS (JDK 21)"),
            service(p, n, "jenkins", port_name="http", port=8080, extra="agent:50000:agent"), pvc(p),
            *ingress(p, n, 8080)]


@template("Gitea", "Apps / tools", "gitea/gitea:28.2.0", "gitea", storage="10Gi", replicas=None, web=True,
          caveats="SQLite database; the install page appears on first visit (point Gitea at a PostgreSQL/MySQL "
                  "template for production).")
def _gitea(p):
    """Deployment (Recreate, SQLite on a PVC at /data, /api/healthz probes) + Service (HTTP 3000 + SSH 22) +
    optional Ingress; ROOT_URL follows the Ingress host."""
    n = p["name"]
    root = f"http://{p['host']}/" if p.get("ingress") and p.get("host") else ""
    env = {"GITEA__database__DB_TYPE": "sqlite3", "GITEA__server__HTTP_PORT": "3000",
           "GITEA__server__SSH_PORT": "22", "USER_UID": "1000", "USER_GID": "1000"}
    if root:
        env["GITEA__server__ROOT_URL"] = root
    d = workload(p, "Deployment", n, "gitea", "gitea/gitea:28.2.0", replicas=1, ports="http:3000\nssh:22",
                 strategy="Recreate", env=_kv(env), volumes=f"data:pvc:{n}-data:/data",
                 **res("100m", "256Mi", "1", "1Gi"),
                 **http_probes("/api/healthz", port="http", ready_delay=10, live_delay=60, failures=6))
    return [Item("Deployment", d, "gitea/gitea:28.2.0"),
            service(p, n, "gitea", port_name="http", port=3000, extra="ssh:22:ssh"), pvc(p),
            *ingress(p, n, 3000)]


@template("Keycloak", "Apps / tools", "quay.io/keycloak/keycloak:26.8.1", "keycloak", replicas=None, web=True,
          secrets=("password",),
          caveats="runs 'start-dev' (embedded dev database, HTTP); use 'start' with a real database + TLS for "
                  "production.")
def _keycloak(p):
    """Deployment running 'start-dev' (health on the management port 9000), Secret with the bootstrap admin,
    Service + optional Ingress (proxy headers enabled)."""
    n = p["name"]
    env = {"KC_HEALTH_ENABLED": "true", "KC_HTTP_ENABLED": "true", "KC_PROXY_HEADERS": "xforwarded",
           "KC_HOSTNAME_STRICT": "false"}
    d = workload(p, "Deployment", n, "keycloak", "quay.io/keycloak/keycloak:26.8.1", replicas=1,
                 ports="http:8080\nmanagement:9000", args="start-dev", env=_kv(env), env_from=f"secret:{n}-secret",
                 **res("250m", "512Mi", "2", "1Gi"),
                 **http_probes("/health/ready", "/health/live", port="management", ready_delay=20,
                               live_delay=60, timeout=5, failures=10))
    return [Item("Deployment", d, "keycloak start-dev"),
            service(p, n, "keycloak", port_name="http", port=8080),
            secret(p, {"KC_BOOTSTRAP_ADMIN_USERNAME": "admin", "KC_BOOTSTRAP_ADMIN_PASSWORD": _pw(p, "password")}),
            *ingress(p, n, 8080)]


@template("busybox debug pod", "Apps / tools", "busybox:1.37.0", "debug", replicas=None)
def _debug(p):
    """A sleeping busybox Deployment to 'kubectl exec' into for DNS / network debugging (nslookup, wget, nc)."""
    n = p["name"]
    d = workload(p, "Deployment", n, "debug", "busybox:1.37.0", ports="", replicas=1,
                 command="sh -c 'trap \"exit 0\" TERM INT; while true; do sleep 3600 & wait $!; done'",
                 termination_grace="5", **res("10m", "16Mi", "100m", "64Mi"),
                 readiness_type="exec", readiness_command="true", liveness_type="exec", liveness_command="true",
                 readiness_initial_delay="0", liveness_initial_delay="10", probe_period="30")
    return [Item("Deployment", d, "busybox; kubectl exec -it deploy/" + n + " -- sh")]


SKIPPED = {
    "Traefik": "as an ingress controller it needs a ServiceAccount, ClusterRole/Binding and CRDs, which this "
               "generator does not produce - install it with its Helm chart.",
    "MinIO": "MinIO no longer publishes official container images (minio/minio is gone from Docker Hub and "
             "quay.io), so there is no official image to pin.",
}
