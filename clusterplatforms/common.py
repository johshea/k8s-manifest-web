"""
clusterplatforms.common - shared result type, rendering and validation helpers for
cluster_config_gen and the per-platform modules. No GUI dependency.
"""
from __future__ import annotations

import copy
import ipaddress
import json
import re
import shlex
from dataclasses import dataclass, field
from typing import Any

from manifest_gen import (Field, _Ctx, _SubCtx, _check_dns, _check_label_key, _parse_kv, _LABEL_NAME,  # noqa: F401
                          _DNS1123_SUB, _DNS1123_LABEL, to_yaml)

TAINT_EFFECTS = ("NoSchedule", "PreferNoSchedule", "NoExecute")

# Row definitions for every table field of every config type (keys must be globally unique).
TABLE_FIELDS: dict[str, list[Field]] = {}


def row_defaults(key: str) -> dict:
    return {f.key: f.default for f in TABLE_FIELDS[key]}


def table(key, label, summary, help_="", default=None):
    return Field(key, label, "table", default=list(default or []), columns=TABLE_FIELDS[key],
                 summary=summary, help=help_)


@dataclass
class OutFile:
    """One generated file. mode: None (default umask), 0o600 (secrets) or 0o755 (scripts)."""
    name: str
    text: str
    mode: int | None = None


@dataclass
class ConfigResult:
    docs: list | None
    errors: list[str]
    warnings: list[str]
    filename: str = "config.yaml"
    header: str = ""          # comment prepended to the YAML (e.g. destination path)
    info: str = ""            # short label for the GUI header
    files: list = field(default_factory=list)   # [OutFile] for non-YAML / multi-file outputs

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def manifest(self):
        return self.docs if self.docs else (self.files or None)


def render_docs(res: ConfigResult) -> str:
    if not res.docs:
        return ""
    body = "---\n".join(to_yaml(d) for d in res.docs)
    return (res.header + body) if res.header else body


def outputs(res: ConfigResult) -> list[OutFile]:
    """Every file to write for *res* (single-YAML results become one OutFile)."""
    if res.files:
        return list(res.files)
    if not res.docs:
        return []
    return [OutFile(res.filename, render_docs(res))]


def render(res: ConfigResult) -> str:
    """Preview text: the single file, or every file under a '# ===== name =====' banner."""
    outs = outputs(res)
    if len(outs) == 1:
        return outs[0].text
    return "\n".join(f"# ===== {o.name} =====\n{o.text}" for o in outs)


def to_json(obj) -> str:
    return json.dumps(obj, indent=4, sort_keys=True) + "\n"


def sh_quote(v) -> str:
    v = str(v)
    return v if re.match(r"^[A-Za-z0-9_@%+=:,./-]+$", v) else shlex.quote(v)


def shell_script(title: str, comments: list[str], cmd: list[str], flags: list, pre: list[str] | None = None,
                 post: list[str] | None = None, passthrough: bool = True) -> str:
    """Render a bash script that runs *cmd* with *flags*, one flag per continuation line.

    flags: [(name, value)] - value None -> bare flag (`--ha`), str -> `--name=value`,
    list -> `--name v1 v2` (space-separated multi-value, e.g. az --zones 1 2 3).
    With passthrough, "$@" forwards extra flags (e.g. `./start-minikube.sh --dry-run`).
    """
    lines = ["#!/usr/bin/env bash", f"# {title}"] + [f"# {c}" if c else "#" for c in comments]
    lines += ["set -euo pipefail", ""] + list(pre or [])
    parts = [" ".join(w if w.startswith('"$') else sh_quote(w) for w in cmd)]
    for name, val in flags:
        if val is None:
            parts.append(name)
        elif isinstance(val, (list, tuple)):
            parts.append(name + " " + " ".join(sh_quote(v) for v in val))
        else:
            parts.append(f"{name}={sh_quote(val)}")
    if passthrough:
        parts.append('"$@"')
    lines.append(" \\\n  ".join(parts))
    lines += list(post or [])
    return "\n".join(lines) + "\n"


def parse_script_flags(text: str, command_word: str | None = None) -> list[tuple[str, Any]]:
    """Inverse of shell_script (for tests): [(flag, value)] of the first command line.

    value is None for bare flags, a str for --flag=value, a list for `--flag v1 v2`."""
    body = text.replace("\\\n", " ")
    for line in body.splitlines():
        st = line.strip()
        if not st or st.startswith("#") or st.startswith("set ") or "=" in st.split()[0]:
            continue
        if command_word and command_word not in st:
            continue
        toks = shlex.split(st)
        out: list = []
        for tok in toks:
            if tok.startswith("--"):
                k, eq, v = tok.partition("=")
                out.append([k, v if eq else None])
            elif out and out[-1][1] is None and tok != "$@":
                out[-1][1] = [tok]
            elif out and isinstance(out[-1][1], list) and tok != "$@":
                out[-1][1].append(tok)
        return [tuple(x) for x in out]
    return []


def did_you_mean(word: str, choices) -> str:
    import difflib
    m = difflib.get_close_matches(word, list(choices), n=1)
    return f" Did you mean '{m[0]}'?" if m else ""


def required(c: _Ctx, schema, skip=()):
    """Generic 'required' check for every Field marked required=True in *schema*."""
    for _, fields in schema:
        for f in fields:
            if f.required and f.kind not in ("check", "table") and f.key not in skip and not c.s(f.key):
                c.err(f"{f.label} is required.")


_VERSION_K8S = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?$")


def k8s_minor(v: str):
    m = _VERSION_K8S.match(v or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


# --------------------------------------------------------------------------- helpers
_TOKEN = re.compile(r"^[a-z0-9]{6}\.[a-z0-9]{16}$")
_DURATION = re.compile(r"^(?=.)(\d+h)?(\d+m)?(\d+(\.\d+)?s)?$")
_VERSION = re.compile(r"^(v?\d+\.\d+\.\d+(-[0-9A-Za-z.\-]+)?|stable(-\d+(\.\d+)?)?|latest(-\d+(\.\d+)?)?)$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_GATE = re.compile(r"^[A-Z][A-Za-z0-9]+$")


def _rows(c: _Ctx, key: str) -> list[dict]:
    rows = c.v.get(key) or []
    base = row_defaults(key)
    return [{**base, **(r or {})} for r in rows]


def _csv(s):
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def _lines(s):
    return [x.strip() for x in (s or "").splitlines() if x.strip() and not x.strip().startswith("#")]


def _ip(c, val, label, required=False):
    if not val:
        if required:
            c.err(f"{label} is required.")
        return None
    try:
        return ipaddress.ip_address(val)
    except ValueError:
        c.err(f"{label} '{val}' is not a valid IPv4/IPv6 address.")
        return None


def _cidrs(c, val, label):
    nets = []
    for part in _csv(val):
        try:
            nets.append(ipaddress.ip_network(part, strict=True))
        except ValueError as exc:
            hint = ""
            try:
                n = ipaddress.ip_network(part, strict=False)
                hint = f" Did you mean {n}?"
            except ValueError:
                pass
            c.err(f"{label} '{part}' is not a valid CIDR ({exc}).{hint}")
    if len(nets) > 2 or (len(nets) == 2 and nets[0].version == nets[1].version):
        c.err(f"{label}: use one CIDR, or one IPv4 + one IPv6 CIDR for dual-stack.")
    return nets


def _no_overlap(c, a, b, la, lb):
    for x in a:
        for y in b:
            if x.version == y.version and x.overlaps(y):
                c.err(f"{la} {x} overlaps {lb} {y}; they must be disjoint.")


def _port(c, val, label, required=False, default=None):
    if not val:
        if required:
            c.err(f"{label} is required.")
        return default
    if not val.isdigit() or not 1 <= int(val) <= 65535:
        c.err(f"{label} '{val}' must be a number 1-65535.")
        return default
    return int(val)


def _hostport(c, val, label):
    """host[:port] (IPv6 in brackets). Returns True if valid."""
    m = re.match(r"^(\[[0-9a-fA-F:.]+\]|[^:\[\]]+)(?::(\d+))?$", val)
    if not m:
        c.err(f"{label} '{val}' must be host[:port] (IPv6 as [addr]:port).")
        return False
    host = m.group(1).strip("[]")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not _DNS1123_SUB.match(host.lower()):
            c.err(f"{label} host '{host}' is not a valid IP or DNS name.")
            return False
    if m.group(2) and not 1 <= int(m.group(2)) <= 65535:
        c.err(f"{label} port {m.group(2)} must be 1-65535.")
        return False
    return True


def _san(c, val, label):
    try:
        ipaddress.ip_address(val)
        return
    except ValueError:
        pass
    v = val.lower()
    if v.startswith("*."):
        v = v[2:]
    if not _DNS1123_SUB.match(v):
        c.err(f"{label} '{val}' must be an IP address or DNS name.")


def _taints(c, text, label):
    out = []
    for line in _lines(text):
        m = re.match(r"^([^=:\s]+)(?:=([^:\s]*))?:(\S+)$", line)
        if not m:
            c.err(f"{label} '{line}' must be key[=value]:Effect.")
            continue
        key, val, eff = m.groups()
        _check_label_key(c, key, label)
        if eff not in TAINT_EFFECTS:
            c.err(f"{label} '{line}': effect must be one of {', '.join(TAINT_EFFECTS)}.")
        t = {"key": key}
        if val:
            t["value"] = val
        t["effect"] = eff
        out.append(t)
    return out


