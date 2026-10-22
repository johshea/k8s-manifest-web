"""
addons_gen - "Networking & Storage" mode: CNI plugin configs (Calico, Cilium, Flannel, Canal, Antrea, Multus)
and CSI StorageClass builders (15 drivers) with install scripts. Mirrors cluster_config_gen's API:
SCHEMA / GROUPS / ADDON_TYPES / defaults / generate / render / outputs / check_row / row_defaults.
No GUI dependency.
"""
from __future__ import annotations

import copy

import manifest_gen as gen
from manifest_gen import Field, _Ctx, _SubCtx  # noqa: F401
from clusterplatforms.common import ConfigResult, OutFile, outputs, render as _render  # noqa: F401
from addons import cni, csi
from addons.common import TABLE_FIELDS as ADDON_TABLES, VERSIONS  # noqa: F401

GROUPS = {
    "CNI": ["Calico", "Cilium", "Flannel", "Canal", "Antrea", "Multus"],
    "CSI - cloud": ["AWS EBS", "AWS EFS", "GCE PD", "Azure Disk", "Azure File"],
    "CSI - on-prem": ["vSphere", "Nutanix", "NFS CSI", "SMB CSI"],
    "CSI - in-cluster": ["Longhorn", "Rook-Ceph RBD", "Rook-Ceph CephFS", "OpenEBS LocalPV", "TopoLVM", "local-path"],
}
ADDON_TYPES = [t for ts in GROUPS.values() for t in ts]
GROUP_OF = {t: g for g, ts in GROUPS.items() for t in ts}
CNI_TYPES = list(GROUPS["CNI"])
CSI_TYPES = [t for t in ADDON_TYPES if t not in CNI_TYPES]
PRIMARY_CNI = list(cni.PRIMARY_CNI)

SCHEMA: dict[str, list[tuple[str, list[Field]]]] = {}
_BUILDERS: dict = {}
COLLAPSED_BY_TYPE: dict[str, set] = {}
for _t, (_schema, _build, _pc) in cni.TYPES.items():
    SCHEMA[_t] = _schema
    _BUILDERS[_t] = _build
    COLLAPSED_BY_TYPE[_t] = set(cni.COLLAPSED)
for _t, _d in csi.DRIVERS.items():
    SCHEMA[_t] = csi.schema_for(_d)
    _BUILDERS[_t] = (lambda t: (lambda c: csi.build(t, c)))(_t)
    COLLAPSED_BY_TYPE[_t] = set(csi.COLLAPSED)
assert set(ADDON_TYPES) == set(SCHEMA), set(ADDON_TYPES) ^ set(SCHEMA)
SCHEMA = {t: SCHEMA[t] for t in ADDON_TYPES}

TABLE_KEYS = set(ADDON_TABLES) | {"allowed_topologies"}


def defaults(atype: str) -> dict:
    return {f.key: copy.deepcopy(f.default) for _, fields in SCHEMA[atype] for f in fields}


def render(res: ConfigResult) -> str:
    return _render(res)


def generate(atype: str, values: dict | None = None) -> ConfigResult:
    if atype not in SCHEMA:
        return ConfigResult(None, [f"Unknown add-on type '{atype}'. Supported: {', '.join(ADDON_TYPES)}"], [])
    merged = defaults(atype)
    merged.update(values or {})
    c = _Ctx(merged)
    try:
        return _BUILDERS[atype](c)
    except Exception as exc:  # never crash the GUI
        c.err(f"Internal error while building {atype}: {exc!r}")
        return ConfigResult(None, c.errors, c.warnings)


def row_defaults(table_key: str) -> dict:
    if table_key in ADDON_TABLES:
        return {f.key: copy.deepcopy(f.default) for f in ADDON_TABLES[table_key]}
    return gen.row_defaults(table_key)


def check_row(table_key: str, row: dict) -> list[str]:
    if table_key in gen.TABLE_FIELDS:
        return gen.check_row(table_key, row)
    c = _Ctx({})
    sc = _SubCtx(c, {**row_defaults(table_key), **row}, "")
    cni.check_row(table_key, sc)
    return c.errors


def pod_cidrs(atype: str, values: dict | None) -> list:
    """Pod CIDRs (ipaddress networks) the primary CNI add-on would use, [] if unknown / not applicable."""
    if atype not in cni.TYPES or atype not in PRIMARY_CNI:
        return []
    merged = defaults(atype)
    merged.update(values or {})
    try:
        return cni.TYPES[atype][2](_Ctx(merged)) or []
    except Exception:
        return []


def storage_class_name(atype: str, values: dict | None) -> str:
    if atype not in CSI_TYPES:
        return ""
    v = defaults(atype)
    v.update(values or {})
    return str(v.get("name") or "").strip()


# Hover help for every field (see field_docs.py)
import field_docs as _field_docs  # noqa: E402
if hasattr(_field_docs, "apply_addons"):
    _field_docs.apply_addons(SCHEMA)
