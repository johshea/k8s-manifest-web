"""
providers.py - the editing modes (Manifests / Cluster config / Networking & Storage) and the file-output rules shared by
every front-end.
No GUI toolkit or web framework imports.

Each provider exposes the kinds/types it edits, their field schema and defaults, generate/render, and
the list of files to write. The helpers below decide file names, permission modes, which kinds count
as "edited", the save-all-edited layout and template-set files, so every front-end produces
byte-for-byte identical output.
"""
from __future__ import annotations

import copy
import re

import ipaddress

import addons_gen as agen
import app_templates as tpl
import cluster_config_gen as ccg
import manifest_gen as gen

GROUP_HEADER = "── {} ──"
# config types whose output carries credentials, tokens or private keys -> saved 0600
PRIVATE_TYPES = {"kubeconfig", "k3s / RKE2", "kubeadm", "OpenShift", "MicroK8s"}


# Sections that start collapsed (click the header to expand). State is remembered per kind.
COLLAPSED_BY_DEFAULT = {"Probes", "Init & sidecar containers", "Tolerations", "Node affinity",
                        "Pod affinity / anti-affinity", "Topology spread", "Allowed topologies"} | ccg.DEFAULT_COLLAPSED
TEMPLATE_HEADER = "── {} ──"


def _grouped_values(groups: dict) -> list[str]:
    out = []
    for g, ts in groups.items():
        out.append(GROUP_HEADER.format(g))
        out += ts
    return out


class ManifestProvider:
    """Mode 'Manifests': Kubernetes resources via manifest_gen."""
    mode, label, bundle = "Manifests", "Resource type:", True
    kinds = gen.KINDS
    schema = staticmethod(lambda k: gen.SCHEMA[k])
    defaults = staticmethod(gen.defaults)
    generate = staticmethod(gen.generate)
    render = staticmethod(lambda res: gen.to_yaml(res.manifest))
    filename = staticmethod(lambda res: gen.filename_for(res.manifest))
    info = staticmethod(lambda kind, res=None: f"apiVersion: {gen.API_VERSIONS[kind]}")
    private = staticmethod(lambda kind, res: kind == "Secret")
    selector_values = list(gen.KINDS)
    group_of = {}
    collapsed = staticmethod(lambda kind: set())

    @staticmethod
    def files(res):
        """[(name, text, mode or None)] to write for a valid result."""
        return [(gen.filename_for(res.manifest), gen.to_yaml(res.manifest), None)]


class ClusterProvider:
    """Mode 'Cluster config': every platform in cluster_config_gen (grouped Local / Self-managed / ...)."""
    mode, label, bundle = "Cluster config", "Config type:", False
    kinds = ccg.CONFIG_TYPES
    schema = staticmethod(lambda k: ccg.SCHEMA[k])
    defaults = staticmethod(ccg.defaults)
    generate = staticmethod(ccg.generate)
    render = staticmethod(ccg.render)
    filename = staticmethod(lambda res: res.filename)
    info = staticmethod(lambda kind, res=None: (res.info if res is not None and res.info else kind))
    private = staticmethod(lambda kind, res: kind in PRIVATE_TYPES)
    selector_values = _grouped_values(ccg.GROUPS)
    group_of = ccg.GROUPS
    collapsed = staticmethod(lambda kind: ccg.COLLAPSED_BY_TYPE.get(kind, set()))

    @staticmethod
    def files(res):
        return [(o.name, o.text, o.mode) for o in ccg.outputs(res)]


class AddonProvider:
    """Mode 'Networking & Storage': CNI plugins and CSI StorageClasses via addons_gen (grouped)."""
    mode, label, bundle = "Networking & Storage", "Add-on type:", False
    kinds = agen.ADDON_TYPES
    schema = staticmethod(lambda k: agen.SCHEMA[k])
    defaults = staticmethod(agen.defaults)
    generate = staticmethod(agen.generate)
    render = staticmethod(agen.render)
    filename = staticmethod(lambda res: res.filename)
    info = staticmethod(lambda kind, res=None: (res.info if res is not None and res.info else kind))
    private = staticmethod(lambda kind, res: False)   # Secrets (Nutanix / SMB) carry an explicit 0600
    selector_values = _grouped_values(agen.GROUPS)
    group_of = agen.GROUPS
    collapsed = staticmethod(lambda kind: agen.COLLAPSED_BY_TYPE.get(kind, set()))

    @staticmethod
    def files(res):
        return [(o.name, o.text, o.mode) for o in agen.outputs(res)]


PROVIDERS = {p.mode: p for p in (ManifestProvider, ClusterProvider, AddonProvider)}
MODES = list(PROVIDERS)
MODE_OF = {k: p.mode for p in PROVIDERS.values() for k in p.kinds}


def provider_for(kind: str):
    return PROVIDERS[MODE_OF[kind]]


def row_defaults_any(key: str) -> dict:
    if key in agen.ADDON_TABLES:
        return agen.row_defaults(key)
    return ccg.row_defaults(key) if key in ccg.TABLE_FIELDS else gen.row_defaults(key)


def check_row_any(key: str, row: dict) -> list[str]:
    if key in agen.ADDON_TABLES:
        return agen.check_row(key, row)
    return ccg.check_row(key, row) if key in ccg.TABLE_FIELDS else gen.check_row(key, row)


# --------------------------------------------------------------------------- #
# Cross-links between modes (soft warnings / dynamic dropdown options)
# --------------------------------------------------------------------------- #
def _nets(text) -> list:
    out = []
    for part in re.split(r"[,\s]+", str(text or "")):
        try:
            if part:
                out.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            pass
    return out


def cluster_network(ctype: str, values: dict | None) -> tuple[list, str | None]:
    """(pod CIDRs, default-CNI problem or None) for a cluster config type; ([], None) if not applicable."""
    if ctype not in ccg.SCHEMA:
        return [], None
    v = {**ccg.defaults(ctype), **(values or {})}
    s = lambda k: str(v.get(k) or "").strip()  # noqa: E731
    if ctype == "kind":
        return _nets(s("pod_subnet") or "10.244.0.0/16"), (
            None if v.get("disable_default_cni") else "kind's default CNI (kindnet) is still enabled - tick "
            "'disableDefaultCNI'")
    if ctype == "kubeadm":
        return _nets(s("pod_subnet")), None
    if ctype == "k3s / RKE2":
        nets = _nets(s("cluster_cidr") or "10.42.0.0/16")
        if s("distro") == "rke2":
            return nets, (None if s("cni") == "none" else f"RKE2 still installs its own CNI "
                          f"({s('cni') or 'canal'}) - set cni: none")
        return nets, (None if s("flannel_backend") == "none" else "k3s still runs its bundled flannel - set "
                      "flannel-backend: none (and disable-network-policy)")
    if ctype == "k0s":
        return _nets(s("k0_pod_cidr")), (None if s("k0_provider") == "custom" else
                                         f"k0s still installs {s('k0_provider')} - set the provider to custom")
    if ctype == "Talos":
        return _nets(s("tl_pod_subnets")), (None if s("tl_cni").startswith("none") else
                                            "Talos still installs flannel - set CNI to none")
    if ctype == "Kubespray":
        return _nets(s("ks_pods")), (None if s("ks_plugin") in ("none", "cni", "custom_cni") else
                                     f"Kubespray still deploys {s('ks_plugin')} - set kube_network_plugin to "
                                     "none / custom_cni")
    if ctype == "Cluster API":
        return _nets(s("ca_pods")), None
    if ctype == "AKS":
        return _nets(s("ak_pod_cidr")), (None if s("ak_plugin") == "none" else
                                         f"AKS network plugin is '{s('ak_plugin')}' - bring-your-own CNI needs "
                                         "network plugin none")
    if ctype == "GKE":
        return _nets(s("gk_pod_cidr")), "GKE manages the pod network (VPC-native"\
            + (" + Dataplane V2 / Cilium" if v.get("gk_dpv2") else "") + "); self-installed CNIs are not supported"
    if ctype == "minikube":
        mk = s("mk_cni")
        return [], (None if mk == "false" or mk.endswith((".yaml", ".yml")) else
                    f"minikube --cni={mk or 'auto'} installs its own CNI - use --cni=false (or a manifest path)")
    if ctype == "EKS":
        addons = [a.strip() for a in s("ek_addons").split(",")]
        return [], ("EKS add-on vpc-cni is still enabled - remove it for an overlay CNI (or run the add-on "
                    "in chaining / policy-only mode)" if "vpc-cni" in addons else None)
    return [], None


def _cidr_mismatch(cni_nets: list, cl_nets: list) -> bool:
    if not cni_nets or not cl_nets:
        return False
    for a in cni_nets:
        if not any(a.version == b.version and (a.subnet_of(b) or b.subnet_of(a)) for b in cl_nets):
            return True
    return False


def _fmt(nets) -> str:
    return ", ".join(str(n) for n in nets)


def cross_warnings(kind: str, values: dict | None, cache: dict | None) -> list[str]:
    """Soft warnings linking a primary CNI add-on and the cluster configs the user edited in this session
    (pod CIDR mismatch, default CNI still enabled). *cache* maps kind -> values entered so far; forms still
    at their defaults are ignored."""
    # only forms the user actually changed count (visiting a form caches its defaults)
    cache = {k: v for k, v in (cache or {}).items() if k in MODE_OF and is_edited(k, v or {})}
    out: list[str] = []
    if kind in agen.PRIMARY_CNI:
        pairs = [(kind, values, ct, cache.get(ct)) for ct in ccg.CONFIG_TYPES if ct in cache]
    elif kind in ccg.SCHEMA:
        pairs = [(a, cache.get(a), kind, values) for a in agen.PRIMARY_CNI if a in cache]
    else:
        return []
    for addon, av, ctype, cv in pairs:
        cni_nets = agen.pod_cidrs(addon, av)
        cl_nets, problem = cluster_network(ctype, cv)
        if _cidr_mismatch(cni_nets, cl_nets):
            out.append(f"Cross-check: {addon} pod CIDR {_fmt(cni_nets)} does not match the {ctype} pod subnet "
                       f"{_fmt(cl_nets)} (both edited in this session).")
        if problem:
            out.append(f"Cross-check ({addon} + {ctype}): {problem}.")
    return out


def generate_in_session(kind: str, values: dict | None, cache: dict | None):
    """provider.generate + the cross-mode soft warnings for the session (*cache*: kind -> values)."""
    res = provider_for(kind).generate(kind, values)
    try:
        extra = cross_warnings(kind, values, {k: v for k, v in (cache or {}).items() if k != kind})
    except Exception:  # never break generation over a hint
        extra = []
    res.warnings.extend(w for w in extra if w not in res.warnings)
    return res


STORAGE_CLASS_FIELDS = {"PersistentVolumeClaim": "storage_class", "StatefulSet": "vct_storage_class"}


def session_storage_classes(cache: dict | None) -> list[str]:
    """StorageClass names created in this session (StorageClass kind + CSI add-on types)."""
    names: list[str] = []
    for k, v in (cache or {}).items():
        n = ""
        if k == "StorageClass":
            n = str({**gen.defaults(k), **(v or {})}.get("name") or "").strip()
        elif k in agen.CSI_TYPES:
            n = agen.storage_class_name(k, v)
        if n and n not in names:
            names.append(n)
    return names


def dynamic_options(kind: str, cache: dict | None) -> dict[str, list[str]]:
    """{field key: extra dropdown options} computed from the session (e.g. StorageClass names for PVCs)."""
    key = STORAGE_CLASS_FIELDS.get(kind)
    if not key:
        return {}
    base = next((f.options for _, fs in gen.SCHEMA[kind] for f in fs if f.key == key), [])
    extra = [n for n in session_storage_classes(cache) if n not in base]
    return {key: list(base) + extra} if extra else {}


def collapsed_sections(kind: str) -> set:
    p = provider_for(kind)
    return {t for t, _ in p.schema(kind) if t in COLLAPSED_BY_DEFAULT or t in p.collapsed(kind)}


def template_values() -> list[str]:
    """Template dropdown entries: 'None / blank', then '── Group ──' headers followed by titles."""
    vals = [tpl.NONE_CHOICE]
    for g, ts in tpl.grouped().items():
        vals.append(TEMPLATE_HEADER.format(g))
        vals += ts
    return vals


def resolve_template(value: str) -> str:
    for g, ts in tpl.grouped().items():
        if value == TEMPLATE_HEADER.format(g):
            return ts[0]
    return value


def resolve_selector(mode: str, value: str) -> str:
    """A group header ('── Local ──') stands for that group's first type."""
    for g, ts in PROVIDERS[mode].group_of.items():
        if value == GROUP_HEADER.format(g):
            return ts[0]
    return value


# --------------------------------------------------------------------------- #
# Values helpers
# --------------------------------------------------------------------------- #
def norm(d: dict) -> dict:
    """Comparable form of a values dict (GUI widgets hand back strings / bools / row lists)."""
    return {k: (bool(v) if isinstance(v, bool) else str(v)) for k, v in d.items()}


def full_values(kind: str, values: dict | None) -> dict:
    """Defaults overlaid with *values* (unknown keys dropped)."""
    out = provider_for(kind).defaults(kind)
    for k, v in (values or {}).items():
        if k in out:
            out[k] = v
    return out


def is_edited(kind: str, values: dict) -> bool:
    return norm(full_values(kind, values)) != norm(provider_for(kind).defaults(kind))


# --------------------------------------------------------------------------- #
# Output files (names, text, permission modes)
# --------------------------------------------------------------------------- #
def output_files(kind: str, res) -> list[tuple[str, str, int | None]]:
    """Every file to write for a valid *res*: (relative name, text, mode). Files of private kinds
    (Secret, kubeconfig, ...) without an explicit mode get 0600; scripts carry 0755 from the generator."""
    p = provider_for(kind)
    out = []
    for name, text, mode in p.files(res):
        if mode is None and p.private(kind, res):
            mode = 0o600
        out.append((name, text, mode))
    return out


def type_folder(kind: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", kind).strip("-").lower()


def save_all_plan(mode: str, results: dict) -> tuple[list[tuple[str, str, int | None]], list[str]]:
    """Files for 'Save all edited': {kind: Result} -> ([(relpath, text, mode)], [skipped msgs]).
    Multi-file cluster types get their own sub-folder; manifests also get all-in-one.yaml."""
    p = PROVIDERS[mode]
    files, bad, docs, secret = [], [], [], False
    for k, res in results.items():
        if not res.ok:
            bad.append(f"{k}: {res.errors[0]}")
            continue
        outs = output_files(k, res)
        sub = type_folder(k) + "/" if len(outs) > 1 else ""
        files += [(sub + n, t, m) for n, t, m in outs]
        docs.append(res.manifest)
        secret = secret or any(m == 0o600 for _, _, m in outs)
    if docs and p.bundle:
        files.append(("all-in-one.yaml", gen.to_multi_yaml(docs), 0o600 if secret else None))
    return files, bad


def template_set_plan(tset, values_for) -> list[tuple[str, str, int | None]]:
    """Files for 'Save template set' under <app name>/: every object (primary items use the forms'
    current values via *values_for(kind)*) + all-in-one.yaml. Secrets (and the all-in-one file that
    contains them) are 0600. Raises ValueError listing invalid objects or duplicate file names."""
    out, errs, docs, secret = [], [], [], False
    for r in tset.items:
        res = gen.generate(r.item.kind, values_for(r.item.kind)) if r.primary else r.result
        if not res.ok:
            errs.append(f"{r.item.kind}/{r.values.get('name')}: {res.errors[0]}")
            continue
        m = res.manifest
        is_secret = m["kind"] == "Secret"
        secret = secret or is_secret
        out.append((gen.filename_for(m), gen.to_yaml(m), 0o600 if is_secret else None))
        docs.append(m)
    if errs:
        raise ValueError("\n• ".join(errs))
    names = [n for n, _, _ in out]
    dups = sorted({n for n in names if names.count(n) > 1})
    if dups:
        raise ValueError("two objects would share a file name: " + ", ".join(dups))
    out.append(("all-in-one.yaml", gen.to_multi_yaml(docs), 0o600 if secret else None))
    return [(f"{tset.name}/{n}", t, m) for n, t, m in out]


def template_conflicts(tset, values_for, prev: dict) -> list[str]:
    """Kinds whose current values were edited by hand (differ from both the defaults and what the
    last applied template put there, *prev*) and would be overwritten by *tset*."""
    out = []
    for kind in tset.primary_values():
        cur = norm(values_for(kind))
        if cur == norm(gen.defaults(kind)):
            continue
        if kind in prev and cur == norm(prev[kind]):
            continue
        out.append(kind)
    return out


def template_stale(tset, values_for, prev: dict) -> list[str]:
    """Kinds the previous template filled, that *tset* doesn't, and that are untouched since."""
    pv = tset.primary_values()
    return [k for k, v in prev.items() if k not in pv and norm(values_for(k)) == norm(v)]


def template_summary(tset) -> str:
    counts: dict[str, int] = {}
    for r in tset.items:
        counts[r.item.kind] = counts.get(r.item.kind, 0) + 1
    parts = [k if n == 1 else f"{k}×{n}" for k, n in counts.items()]
    extras = [f"{r.item.kind}/{r.values['name']}" for r in tset.items if not r.primary]
    summary = f"set: {', '.join(parts)}"
    if extras:
        summary += f"   (only in the saved set: {', '.join(extras)})"
    if tset.warnings:
        summary += "\n⚠ " + tset.warnings[-1]
    return summary


def copy_values(d: dict) -> dict:
    return copy.deepcopy(d)


__all__ = ["COLLAPSED_BY_DEFAULT", "TEMPLATE_HEADER", "collapsed_sections", "template_values", "resolve_template", "GROUP_HEADER", "PRIVATE_TYPES", "ManifestProvider", "ClusterProvider", "PROVIDERS", "MODES", "MODE_OF",
           "provider_for", "row_defaults_any", "check_row_any", "resolve_selector", "norm", "full_values",
           "is_edited", "output_files", "type_folder", "save_all_plan", "template_set_plan",
           "template_conflicts", "template_stale", "template_summary", "tpl"]
