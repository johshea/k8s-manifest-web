"""
addons.common - shared helpers for the Networking & Storage add-on generators (no GUI dependency).

Pinned upstream versions live in VERSIONS; every URL built from them was checked to exist when the
version was pinned (Oct 2026). install.sh scripts honour $KUBECTL / $HELM overrides.
"""
from __future__ import annotations

import ipaddress
import json
import re

from manifest_gen import (Field, _Ctx, _SubCtx, _check_dns, _check_label_key, _parse_kv, to_yaml,  # noqa: F401
                          to_multi_yaml)
from clusterplatforms.common import ConfigResult, OutFile, _csv, _lines, sh_quote  # noqa: F401

# upstream release / chart versions (verified Oct 21 2026)
VERSIONS = {
    "calico": "v3.33.0", "cilium": "1.20.3", "cilium-cli": "v0.20.1", "flannel": "v0.28.10",
    "canal": "v3.33.0", "antrea": "2.7.0", "multus": "v4.3.1", "whereabouts": "v0.9.4",
    "gateway-api": "v1.6.3", "snapshotter": "v8.7.0",
    "csi-driver-nfs": "4.13.5", "csi-driver-smb": "1.20.3", "local-path": "v0.0.37", "longhorn": "1.13.0",
    "rook-ceph": "1.21.0", "openebs-localpv": "4.6.0", "aws-ebs-csi-driver": "2.67.1",
    "aws-efs-csi-driver": "4.5.2", "nutanix-csi-storage": "2.6.10", "topolvm": "17.3.0",
}
HELM_REPOS = {
    "cilium": "https://helm.cilium.io", "antrea": "https://charts.antrea.io",
    "csi-driver-nfs": "https://raw.githubusercontent.com/kubernetes-csi/csi-driver-nfs/master/charts",
    "csi-driver-smb": "https://raw.githubusercontent.com/kubernetes-csi/csi-driver-smb/master/charts",
    "longhorn": "https://charts.longhorn.io", "rook-release": "https://charts.rook.io/release",
    "openebs-localpv": "https://openebs.github.io/dynamic-localpv-provisioner",
    "aws-ebs-csi-driver": "https://kubernetes-sigs.github.io/aws-ebs-csi-driver",
    "aws-efs-csi-driver": "https://kubernetes-sigs.github.io/aws-efs-csi-driver",
    "nutanix": "https://nutanix.github.io/helm", "topolvm": "https://topolvm.github.io/topolvm",
}
SNAPSHOT_BASE = "https://raw.githubusercontent.com/kubernetes-csi/external-snapshotter/{v}"
SNAPSHOT_CRDS = ["client/config/crd/snapshot.storage.k8s.io_volumesnapshotclasses.yaml",
                 "client/config/crd/snapshot.storage.k8s.io_volumesnapshotcontents.yaml",
                 "client/config/crd/snapshot.storage.k8s.io_volumesnapshots.yaml"]
SNAPSHOT_CONTROLLER = ["deploy/kubernetes/snapshot-controller/rbac-snapshot-controller.yaml",
                       "deploy/kubernetes/snapshot-controller/setup-snapshot-controller.yaml"]

# Row definitions for add-on table fields (keys must not clash with manifest / cluster tables)
TABLE_FIELDS: dict[str, list[Field]] = {}

_SEMVER = re.compile(r"^v?\d+\.\d+\.\d+(-[0-9A-Za-z.\-]+)?$")


def row_defaults(key: str) -> dict:
    return {f.key: f.default for f in TABLE_FIELDS[key]}


def table(key, label, summary, help_="", default=None):
    return Field(key, label, "table", default=list(default or []), columns=TABLE_FIELDS[key],
                 summary=summary, help=help_)


def rows(c: _Ctx, key: str) -> list[dict]:
    base = row_defaults(key)
    return [{**base, **(r or {})} for r in (c.v.get(key) or [])]


def version(c: _Ctx, key: str, label: str, v_prefix: bool | None = None) -> str:
    v = c.s(key)
    if not v:
        c.err(f"{label} is required.")
    elif not _SEMVER.match(v):
        c.err(f"{label} '{v}' must be a release version like 1.2.3.")
    elif v_prefix is True and not v.startswith("v"):
        c.err(f"{label} '{v}' needs the leading 'v' (release tags look like v1.2.3).")
    elif v_prefix is False and v.startswith("v"):
        c.err(f"{label} '{v}' is a Helm chart version: drop the leading 'v'.")
    return v


def cidr(c: _Ctx, val: str, label: str, required=True):
    if not val:
        if required:
            c.err(f"{label} is required.")
        return None
    try:
        return ipaddress.ip_network(val.strip(), strict=True)
    except ValueError as exc:
        hint = ""
        try:
            hint = f" Did you mean {ipaddress.ip_network(val.strip(), strict=False)}?"
        except ValueError:
            pass
        c.err(f"{label} '{val}' is not a valid CIDR ({exc}).{hint}")
        return None


def int_opt(c: _Ctx, key: str, label: str, lo=None, hi=None):
    return c.int(key, label, lo=lo, hi=hi)


def dns(c: _Ctx, val: str, label: str, rule="subdomain"):
    return _check_dns(c, val, label, rule)


def kv(c: _Ctx, text: str, what: str, labels=False) -> dict:
    return _parse_kv(c, text, what, label_rules=labels)


def to_json(obj, indent=2) -> str:
    return json.dumps(obj, indent=indent)


def yaml_file(name: str, docs: list, mode=None, header: str = "") -> OutFile:
    body = to_yaml(docs[0]) if len(docs) == 1 else to_multi_yaml(docs)
    return OutFile(name, header + body, mode)


WAIT_FN = [
    "# wait until an object exists (operators create some objects asynchronously)",
    'wait_exists() { local i; for i in $(seq 1 90); do "$KUBECTL" get "$@" >/dev/null 2>&1 && return 0; '
    'sleep 5; done; echo "timed out waiting for: $*" >&2; return 1; }',
    "",
]


def script(title: str, comments: list[str], body: list[str], helm=False, wait=False) -> OutFile:
    lines = ["#!/usr/bin/env bash", f"# {title}"] + [f"# {x}" if x else "#" for x in comments]
    lines += ["set -euo pipefail", 'cd "$(dirname "$0")"', 'KUBECTL="${KUBECTL:-kubectl}"']
    if helm:
        lines += ['HELM="${HELM:-helm}"',
                  '# extra helm flags (mirrors, --set overrides): HELM_EXTRA_ARGS="--set a=b" ./install.sh']
    lines.append("")
    if wait:
        lines += WAIT_FN
    lines += body
    return OutFile("install.sh", "\n".join(lines).rstrip() + "\n", 0o755)


def helm_install(release: str, repo: str, chart: str, version_: str, namespace: str, values_file: str | None,
                 extra: list[str] | None = None, wait=True, timeout="10m") -> list[str]:
    cmd = (f'"$HELM" upgrade --install {release} {repo}/{chart} --version {version_} '
           f'--namespace {namespace} --create-namespace')
    if values_file:
        cmd += f" -f {values_file}"
    for e in extra or []:
        cmd += f" {e}"
    if wait:
        cmd += f" --wait --timeout {timeout}"
    cmd += " ${HELM_EXTRA_ARGS:-}"      # e.g. HELM_EXTRA_ARGS="--set image.repository=mirror/..." (air-gapped)
    return [f'"$HELM" repo add {repo} {HELM_REPOS[repo]} --force-update >/dev/null',
            f'"$HELM" repo update {repo} >/dev/null', cmd]


def snapshot_install_lines(v: str) -> list[str]:
    base = SNAPSHOT_BASE.format(v=v)
    out = [f"# CSI snapshot CRDs + snapshot-controller (external-snapshotter {v}); skip if your platform ships them",
           f'SNAP_BASE="{base}"']
    out += [f'"$KUBECTL" apply --server-side -f "$SNAP_BASE/{p}"' for p in SNAPSHOT_CRDS]
    out += [f'"$KUBECTL" apply --server-side -f "$SNAP_BASE/{p}"' for p in SNAPSHOT_CONTROLLER]
    out += ['"$KUBECTL" -n kube-system rollout status deployment/snapshot-controller --timeout=5m', ""]
    return out


def cidr_list(c: _Ctx, text: str, label: str) -> list:
    nets = []
    for part in _csv(text):
        n = cidr(c, part, label)
        if n:
            nets.append(n)
    return nets
