#!/usr/bin/env python3
"""
Hover-help coverage test (headless): every field of every Manifest kind, every Cluster config
platform, every table-row dialog column and every application-template parameter must carry
non-empty, non-placeholder help (Field.description / maps_to / option_help from field_docs.py),
every readonly dropdown option must be explained, and every section header / top selector must
have help too.

  python3 tests/test_field_docs.py [-v]      (-v prints every tooltip)
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import addons_gen as agen  # noqa: E402
import app_templates as tpl  # noqa: E402
import cluster_config_gen as ccg  # noqa: E402
import field_docs as fd  # noqa: E402
import manifest_gen as gen  # noqa: E402

VERBOSE = "-v" in sys.argv
fails = 0
problems = []


def check(cond, msg):
    global fails
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails += 1


def bad_text(s: str) -> str | None:
    s = (s or "").strip()
    if not s:
        return "empty"
    if fd.PLACEHOLDER_RE.search(s):
        return "placeholder text"
    return None


def examine(where: str, f, combo_needs_options=True):
    """Return a list of problems with one Field/Param's help."""
    out = []
    d = getattr(f, "description", "")
    why = bad_text(d)
    if why:
        return [f"{where}: description {why}"]
    oh = getattr(f, "option_help", {}) or {}
    kind = getattr(f, "kind", None) or getattr(f, "type", "")
    if kind == "combo" and combo_needs_options:
        missing = [o for o in f.options if o not in oh]
        if missing:
            out.append(f"{where}: options without explanation {missing}")
    # explained by the text itself, or by a bullet per option
    if len(d) < 40 and not (kind == "combo" and oh):
        out.append(f"{where}: description too short ({len(d)} chars): {d!r}")
    if len(d) > 420:
        out.append(f"{where}: description too long ({len(d)} chars)")
    for o, e in oh.items():
        if bad_text(e):
            out.append(f"{where}: option {o!r} explanation {bad_text(e)}")
    tip = fd.tooltip(f)
    n = len(tip.splitlines())
    help_lines = len((getattr(f, "help", "") or "").splitlines())
    if n < 2 or n > 3 + len(oh) + max(help_lines, 1):
        out.append(f"{where}: tooltip has {n} lines")
    if f.label.strip() and not tip.startswith(f.label.strip()):
        out.append(f"{where}: tooltip doesn't start with the label")
    if VERBOSE:
        print(f"--- {where}\n{tip}")
    return out


def main():
    counts = {"manifest": 0, "cluster": 0, "addon": 0}
    cols = {"manifest": 0, "cluster": 0, "addon": 0}
    unique = set()
    no_path = []
    for mode, scope, title, f, table in fd.iter_fields(gen.SCHEMA, ccg.SCHEMA, agen.SCHEMA):
        where = f"{scope}/{title}/{table + '.' if table else ''}{f.key}"
        counts[mode] += 1
        if table:
            cols[mode] += 1
        unique.add((mode, scope if mode != "manifest" else "", table, f.key))
        problems.extend(examine(where, f))
        if not f.maps_to:
            no_path.append(where)
    for kind in gen.KINDS:
        for title, _ in gen.SCHEMA[kind]:
            if bad_text(fd.section_help(kind, title)):
                problems.append(f"{kind}: section {title!r} has no help")
    for ctype in ccg.CONFIG_TYPES:
        for title, _ in ccg.SCHEMA[ctype]:
            if bad_text(fd.section_help(ctype, title)):
                problems.append(f"{ctype}: section {title!r} has no help")
    for atype in agen.ADDON_TYPES:
        for title, _ in agen.SCHEMA[atype]:
            if bad_text(fd.section_help(atype, title)):
                problems.append(f"{atype}: section {title!r} has no help")
    for key in ("mode", "kind", "template"):
        if bad_text(fd.top_help(key)) or len(fd.top_help(key)) < 60:
            problems.append(f"top selector {key} has no help")
    params = {}
    for title in tpl.titles():
        for p in tpl.params_for(title):
            params[p.key] = p
            for pr in examine(f"template {title}/param {p.key}", p):
                if pr not in problems:
                    problems.append(pr)
            if not p.maps_to:
                no_path.append(f"template param {p.key}")
    # the GUI shows the same text the Field carries
    f0 = dict((f.key, f) for _, fs in gen.SCHEMA["Deployment"] for f in fs)["image_pull_policy"]
    tip = fd.tooltip(f0)
    check(all(s in tip for s in ("Always", "IfNotPresent", "Never", "spec.template.spec.containers[0].imagePullPolicy")),
          "imagePullPolicy tooltip explains Always/IfNotPresent/Never and gives the YAML path")
    cj = dict((f.key, f) for _, fs in gen.SCHEMA["CronJob"] for f in fs)["image"]
    check(cj.maps_to.startswith("spec.jobTemplate.spec.template.spec.containers[0]"), "CronJob paths use jobTemplate")
    svc = dict((f.key, f) for _, fs in gen.SCHEMA["Service"] for f in fs)["selector"]
    check("spec.selector" in svc.maps_to and "matchLabels" not in svc.maps_to, "Service.selector has its own text")
    gk = dict((f.key, f) for _, fs in ccg.SCHEMA["GKE"] for f in fs)["gk_nodes"]
    check(gk.maps_to == "gcloud container clusters create --num-nodes", f"GKE flag mapping: {gk.maps_to!r}")
    ebs = dict((f.key, f) for _, fs in agen.SCHEMA["AWS EBS"] for f in fs)
    check(ebs["ebs_type"].maps_to == "StorageClass parameters.type" and "gp3" in fd.tooltip(ebs["ebs_type"]),
          f"CSI parameter mapping: {ebs['ebs_type'].maps_to!r}")
    enc = dict((f.key, f.columns) for _, fs in agen.SCHEMA["Calico"] for f in fs)["cal_pools"]
    check(all(o in dict((c.key, c) for c in enc)["encapsulation"].option_help for o in ("VXLAN", "IPIP", "None")),
          "Calico IP pool encapsulation options explained")
    np_ = dict((f.key, f.columns) for _, fs in gen.SCHEMA["NetworkPolicy"] for f in fs)["egress_rules"]
    check(dict((c.key, c) for c in np_)["ports"].maps_to.startswith("spec.egress[]"), "NetworkPolicy egress columns map to spec.egress")
    check(not fd.PLACEHOLDER_RE.search("Example: nginx:1.30") and fd.PLACEHOLDER_RE.search("TODO later"),
          "placeholder detector")

    check(not problems, f"every field has real help: {counts['manifest']} manifest field instances "
                        f"({cols['manifest']} table columns) in {len(gen.KINDS)} kinds, {counts['cluster']} cluster "
                        f"field instances ({cols['cluster']} table columns) in {len(ccg.CONFIG_TYPES)} platforms, "
                        f"{counts['addon']} add-on field instances ({cols['addon']} table columns) in "
                        f"{len(agen.ADDON_TYPES)} CNI/CSI types, "
                        f"{len(params)} template parameters; {len(unique)} distinct (scope, field) pairs")
    for p in problems[:40]:
        print("       ", p)
    check(not no_path, f"every field names the YAML key / config key / flag it maps to ({no_path[:5]})")
    print(f"\nFIELD DOCS RESULT: {'PASS' if not fails else f'{fails} FAILURE(S)'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
