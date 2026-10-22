#!/usr/bin/env python3
"""
Web UI API tests (Flask test client, no browser, no network).

  python3 tests/test_web.py         # needs Flask (pip install -r requirements.txt); skips if missing

Checks every endpoint against the generators called directly (byte-identical YAML / files / modes)
for every Manifest kind, every Cluster config platform, every Networking & Storage add-on and every
application template (plus cross-mode warnings and session StorageClass options), plus the
validation error paths, zip downloads and the request guards (JSON-only POSTs, size limit, Host check,
security headers).
"""
import io
import json
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

try:
    import flask  # noqa: F401
except ImportError:
    print("== Flask not installed: web tests skipped (pip install -r requirements.txt) ==")
    print("\nWEB RESULT: SKIPPED")
    sys.exit(0)

import addons_gen as agen  # noqa: E402
import app_templates as tpl  # noqa: E402
import cluster_config_gen as ccg  # noqa: E402
import field_docs  # noqa: E402
import manifest_gen as gen  # noqa: E402
import providers as P  # noqa: E402
from web.app import MAX_BODY, create_app  # noqa: E402

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("  FAIL " + msg)


def ok(msg):
    print("  ok   " + msg)


def unzip(data: bytes) -> dict:
    z = zipfile.ZipFile(io.BytesIO(data))
    return {i.filename: (z.read(i).decode("utf-8"), (i.external_attr >> 16) & 0o777) for i in z.infolist()}


def plan_dict(files) -> dict:
    return {n: (t, m if m is not None else 0o644) for n, t, m in files}


def expected_preview(kind, values):
    p = P.provider_for(kind)
    res = p.generate(kind, P.full_values(kind, values))
    text = p.render(res) if res.manifest is not None else ""
    if res.errors:
        text = "# ⚠ Fix the errors below before saving\n" + text
    return res, text


# variants per kind/type beyond the defaults (all valid)
VARIANTS = {
    "Deployment": {"name": "web-x", "replicas": "3", "image": "nginx:1.30"},
    "Secret": {"name": "creds", "data": "user=admin\npassword=s3cr3t"},
    "Service": {"service_type": "NodePort", "node_port": "30080"},
    "kubeconfig": {"ca_mode": "insecure-skip-tls-verify", "token": "abc.def"},
    "k3s / RKE2": {"token": "K10supersecret", "role": "server"},
    "kind": {"name": "dev"},
    "minikube": {"mk_cpus": "4"},
}


def main():
    app = create_app()
    c = app.test_client()

    # ---------------------------------------------------------------- page, static, headers
    r = c.get("/")
    check(r.status_code == 200 and b"app.js" in r.data and b"<dialog" in r.data, "index page")
    for h in ("Content-Security-Policy", "X-Content-Type-Options", "X-Frame-Options", "Referrer-Policy"):
        check(h in r.headers, f"security header {h}")
    check("script-src 'self'" in r.headers["Content-Security-Policy"], "CSP allows only same-origin scripts")
    for s in ("/static/app.js", "/static/app.css"):
        r = c.get(s)
        check(r.status_code == 200 and len(r.data) > 1000, f"static {s}")
        r.close()
    js = open(os.path.join(HERE, "..", "web", "static", "app.js"), encoding="utf-8").read()
    html = open(os.path.join(HERE, "..", "web", "templates", "index.html"), encoding="utf-8").read()
    css = open(os.path.join(HERE, "..", "web", "static", "app.css"), encoding="utf-8").read()
    check(not any(u in js + html + css for u in ("http://", "https://", "//cdn", "@import")),
          "no external URLs / CDN in the frontend (works offline)")
    check(c.get("/healthz").json == {"ok": True}, "healthz")
    ok("page, static files, security headers")

    # ---------------------------------------------------------------- meta
    m = c.get("/api/meta").json
    check([x["mode"] for x in m["modes"]] == P.MODES, "meta modes")
    check(m["modes"][0]["kinds"] == gen.KINDS and m["modes"][1]["kinds"] == ccg.CONFIG_TYPES, "meta kinds")
    check(m["modes"][1]["groups"] == {g: list(t) for g, t in ccg.GROUPS.items()}, "meta cluster groups")
    check(m["modes"][2]["mode"] == "Networking & Storage" and m["modes"][2]["kinds"] == agen.ADDON_TYPES and
          m["modes"][2]["groups"] == {g: list(t) for g, t in agen.GROUPS.items()} and not m["modes"][2]["bundle"],
          "meta add-on mode, kinds, groups")
    check(m["templates"]["values"] == P.template_values(), "meta template dropdown values")
    check(all(m["top_help"][k] == field_docs.top_help(k) for k in ("mode", "kind", "template")), "meta top help")
    r = c.get("/api/meta")
    check(r.headers.get("Cache-Control") == "no-store", "API responses are no-store")
    ok("meta")

    # ---------------------------------------------------------------- every kind / platform
    n_kinds = n_files = 0
    for kind in P.MODE_OF:
        n_kinds += 1
        p = P.provider_for(kind)
        s = c.get("/api/schema", query_string={"kind": kind}).json
        fields = [f for _, fs in p.schema(kind) for f in fs]
        jf = [f for sec in s["sections"] for f in sec["fields"]]
        check([f["key"] for f in jf] == [f.key for f in fields], f"{kind}: schema field keys")
        check(all(a["tooltip"] == field_docs.tooltip(b) and a["kind"] == b.kind and a["options"] == list(b.options)
                  for a, b in zip(jf, fields)), f"{kind}: schema tooltips / kinds / options")
        for a, b in zip(jf, fields):
            if b.kind == "table":
                check([x["key"] for x in a["columns"]] == [x.key for x in b.columns] and
                      all(x["tooltip"] == field_docs.tooltip(y) for x, y in zip(a["columns"], b.columns)),
                      f"{kind}.{b.key}: table columns + tooltips")
                check(a["row_defaults"] == P.row_defaults_any(b.key), f"{kind}.{b.key}: row defaults")
        check(json.loads(json.dumps(p.defaults(kind))) == s["defaults"], f"{kind}: defaults")
        check([sec["title"] for sec in s["sections"]] == [t for t, _ in p.schema(kind)] and
              all(sec["help"] == field_docs.section_help(kind, sec["title"]) for sec in s["sections"]),
              f"{kind}: sections + section help")
        check({sec["title"] for sec in s["sections"] if sec["collapsed"]} == P.collapsed_sections(kind),
              f"{kind}: collapsed sections")
        for label, vals in (("defaults", {}), ("variant", VARIANTS.get(kind))):
            if vals is None:
                continue
            res, text = expected_preview(kind, vals)
            g = c.post("/api/generate", json={"kind": kind, "values": vals}).json
            check(g["text"] == text, f"{kind} {label}: preview identical to the generator")
            check(g["errors"] == res.errors and g["warnings"] == res.warnings and g["ok"] == res.ok,
                  f"{kind} {label}: errors / warnings identical")
            check(g["info"] == p.info(kind, res), f"{kind} {label}: info")
            if not res.ok:
                d = c.post("/api/download", json={"kind": kind, "values": vals})
                check(d.status_code == 422 and d.json["errors"] == res.errors, f"{kind} {label}: invalid -> 422")
                continue
            files = P.output_files(kind, res)
            check([f["name"] for f in g["files"]] == [n for n, _, _ in files], f"{kind} {label}: file list")
            d = c.post("/api/download", json={"kind": kind, "values": vals})
            check(d.status_code == 200, f"{kind} {label}: download status {d.status_code}")
            if len(files) == 1 and "/" not in files[0][0]:
                name, txt, mode = files[0]
                check(d.data.decode("utf-8") == txt, f"{kind} {label}: downloaded file identical")
                check(f'filename="{name}"' in d.headers["Content-Disposition"], f"{kind} {label}: file name {name}")
                check(d.headers.get("X-File-Mode") == (oct(mode) if mode else ""), f"{kind} {label}: mode header")
            else:
                check(d.mimetype == "application/zip" and unzip(d.data) == plan_dict(files),
                      f"{kind} {label}: zip identical incl. modes ({len(files)} files)")
            n_files += len(files)
    ok(f"{n_kinds} kinds/platforms: schema, tooltips, preview, validation, downloads ({n_files} files compared)")

    # ---------------------------------------------------------------- validation error paths
    g = c.post("/api/generate", json={"kind": "Deployment", "values": {"name": "Bad_Name", "replicas": "x"}}).json
    exp = gen.generate("Deployment", P.full_values("Deployment", {"name": "Bad_Name", "replicas": "x"}))
    check(not g["ok"] and g["errors"] == exp.errors and len(g["errors"]) >= 2 and g["text"].startswith("# ⚠"),
          "invalid Deployment: errors + warning header")
    d = c.post("/api/download", json={"kind": "Deployment", "values": {"name": "Bad_Name"}})
    check(d.status_code == 422 and d.json["errors"], "download of an invalid kind -> 422 with errors")
    check(c.post("/api/generate", json={"kind": "Nope", "values": {}}).status_code == 400, "unknown kind -> 400")
    check(c.post("/api/generate", json={"kind": "Deployment", "values": []}).status_code == 400, "values not an object -> 400")
    check(c.post("/api/generate", data="kind=Deployment",
                 content_type="application/x-www-form-urlencoded").status_code == 415, "form POST (CSRF-able) -> 415")
    check(c.post("/api/generate", data="[1]", content_type="application/json").status_code == 400, "JSON array -> 400")
    big = json.dumps({"kind": "ConfigMap", "values": {"data": "x" * (MAX_BODY + 10)}})
    check(c.post("/api/generate", data=big, content_type="application/json").status_code == 413, "oversized body -> 413")
    check(c.get("/api/meta", headers={"Host": "evil.example:8080"}).status_code == 400, "foreign Host header -> 400")
    check(c.get("/api/meta", headers={"Host": "127.0.0.1:8080"}).status_code == 200, "127.0.0.1 Host accepted")
    check(create_app({"*"}).test_client().get("/api/meta", headers={"Host": "k8s.lan"}).status_code == 200,
          "allowed_hosts={'*'} disables the Host check (Docker)")
    check(c.get("/api/schema", query_string={"kind": "x"}).status_code == 400, "schema of unknown kind -> 400")
    r = c.post("/api/check_row", json={"table": "tolerations", "row": {"key": "", "operator": "Equal", "value": "x"}}).json
    check(r["errors"] == gen.check_row("tolerations", {**gen.row_defaults("tolerations"), "key": "", "operator": "Equal",
                                                      "value": "x"}) and r["errors"], "check_row reports row errors")
    r = c.post("/api/check_row", json={"table": "extra_containers", "row": {"name": "side", "image": "busybox"}}).json
    check(r["errors"] == [], "check_row accepts a valid container row")
    r = c.post("/api/check_row", json={"table": "nodes", "row": {"role": "worker"}}).json
    check(r["errors"] == ccg.check_row("nodes", {**ccg.row_defaults("nodes"), "role": "worker"}), "check_row cluster table")
    check(c.post("/api/check_row", json={"table": "nope", "row": {}}).status_code == 400, "check_row unknown table -> 400")
    ok("validation error paths and request guards")

    # ---------------------------------------------------------------- templates
    n_t = 0
    for title in tpl.titles():
        n_t += 1
        t = c.get("/api/template", query_string={"title": title}).json
        check([p_["key"] for p_ in t["params"]] == [p_.key for p_ in tpl.params_for(title)] and
              all(a["tooltip"] == field_docs.tooltip(b) for a, b in zip(t["params"], tpl.params_for(title))),
              f"{title}: params + tooltips")
        ref = tpl.render(title, None)
        r = c.post("/api/template/render", json={"title": title, "params": None, "secrets": ref.secrets}).json
        check(r["ok"] and r["values"] == json.loads(json.dumps(ref.primary_values())), f"{title}: primary values identical")
        check(r["primary_kind"] == ref.primary_kind and r["summary"] == P.template_summary(ref) and r["secrets"] == ref.secrets,
              f"{title}: primary kind / summary / secrets kept")
        d = c.post("/api/download/template", json={"title": title, "secrets": ref.secrets, "current": r["values"]})
        exp_files = P.template_set_plan(ref, lambda k: ref.primary_values()[k])
        got = unzip(d.data) if d.status_code == 200 else {}
        check(got == plan_dict(exp_files), f"{title}: template-set zip identical ({len(exp_files)} files)")
        check(got.get(f"{ref.name}/all-in-one.yaml", ("", 0))[0] == ref.all_in_one(), f"{title}: all-in-one identical")
        sec = [n for n, (txt, mode) in got.items() if "kind: Secret" in txt.split("---")[-1][:400]]
        check(all(got[n][1] == 0o600 for n in sec), f"{title}: Secrets are 0600 in the zip")
    p2 = c.post("/api/template/render", json={"title": "PostgreSQL", "params": {"name": "BAD"}}).json
    check(not p2["ok"] and p2["errors"], "template with a bad app name -> errors")
    check(c.post("/api/template/render", json={"title": "Nope"}).status_code == 400, "unknown template -> 400")
    # conflicts / stale detection
    ref = tpl.render("nginx", None)
    edited = {"Deployment": {**gen.defaults("Deployment"), "replicas": "7"}}
    r = c.post("/api/template/render", json={"title": "nginx", "current": edited, "previous": {}}).json
    check(r["conflicts"] == ["Deployment"], f"hand-edited Deployment is a conflict: {r['conflicts']}")
    r2 = c.post("/api/template/render", json={"title": "nginx", "current": r["values"], "previous": r["values"]}).json
    check(r2["conflicts"] == [], "re-render over the template's own values: no conflict")
    r3 = c.post("/api/template/render", json={"title": "Redis", "current": r["values"], "previous": r["values"]}).json
    check(set(r3["stale"]) == set(r["values"]) - set(r3["values"]), f"stale kinds from the previous template: {r3['stale']}")
    ok(f"{n_t} templates: params, render, conflicts/stale, template-set zips")

    # ---------------------------------------------------------------- save all edited / bundle
    cache = {"Deployment": {"name": "web", "replicas": "3"}, "Secret": {"name": "creds", "data": "a=b"},
             "Service": {"name": "Bad_Name"}, "ConfigMap": gen.defaults("ConfigMap")}
    d = c.post("/api/download/all-edited", json={"mode": "Manifests", "cache": cache})
    got = unzip(d.data)
    res = {k: gen.generate(k, P.full_values(k, cache[k])) for k in gen.KINDS if k in cache and P.is_edited(k, cache[k])}
    files, bad = P.save_all_plan("Manifests", res)
    exp = plan_dict(files)
    check(set(res) == {"Deployment", "Secret", "Service"}, "untouched kinds are not 'edited'")
    check({k: v for k, v in got.items() if k != "SKIPPED.txt"} == exp, "all-edited zip identical to providers.save_all_plan")
    check("SKIPPED.txt" in got and "Service" in got["SKIPPED.txt"][0], "invalid kind listed in SKIPPED.txt")
    check(got["all-in-one.yaml"][1] == 0o600 and got["secret-creds.yaml"][1] == 0o600, "Secret + all-in-one are 0600")
    check(json.loads(__import__("urllib.parse").parse.unquote(d.headers["X-Skipped"])) == bad, "X-Skipped header")
    ccache = {"Kubespray": {"ks_inventory": "lab"}, "kubeconfig": VARIANTS["kubeconfig"], "minikube": {"mk_cpus": "4"}}
    d = c.post("/api/download/all-edited", json={"mode": "Cluster config", "cache": ccache})
    got = unzip(d.data)
    res = {k: ccg.generate(k, P.full_values(k, ccache[k])) for k in ccg.CONFIG_TYPES if k in ccache}
    files, _ = P.save_all_plan("Cluster config", res)
    check(got == plan_dict(files), "cluster all-edited zip identical")
    check(any(n.startswith("kubespray/") for n in got) and any(n.startswith("minikube/") for n in got),
          "multi-file types get a sub-folder")
    check(got["kubeconfig"][1] == 0o600 if "kubeconfig" in got else
          all(m == 0o600 for n, (_, m) in got.items() if "kubeconfig" in n), "kubeconfig is 0600")
    check(any(m == 0o755 for n, (_, m) in got.items() if n.endswith(".sh")), "scripts are 0755")
    check("all-in-one.yaml" not in got, "no all-in-one for cluster configs")
    check(c.post("/api/download/all-edited", json={"mode": "Manifests", "cache": {}}).status_code == 400,
          "nothing edited -> 400")
    items = [{"kind": "Deployment", "values": {"name": "a"}}, {"kind": "Service", "values": {"name": "a"}}]
    d = c.post("/api/download/bundle", json={"items": items})
    exp = gen.to_multi_yaml([gen.generate(i["kind"], P.full_values(i["kind"], i["values"])).manifest for i in items])
    check(d.status_code == 200 and d.data.decode() == exp, "bundle.yaml identical")
    check(c.post("/api/download/bundle", json={"items": []}).status_code == 400, "empty bundle -> 400")
    check(c.post("/api/download/bundle", json={"items": [{"kind": "kind", "values": {}}]}).status_code == 400,
          "cluster type in a bundle -> 400")
    check(c.post("/api/download/bundle", json={"items": [{"kind": "Service", "values": {"name": "B_"}}]}).status_code == 422,
          "invalid bundle item -> 422")
    g = c.post("/api/generate", json={"kind": "Deployment", "values": cache["Deployment"], "cache": cache}).json
    check(g["edited"] == ["Deployment", "Service", "Secret"], f"edited list in the preview response: {g['edited']}")
    ok("save-all-edited zips, bundle, edited list")

    # ---------------------------------------------------------------- Networking & Storage
    acache = {"Calico": {"cal_mtu": "1440"}, "NFS CSI": {"nfs_demo_server": True},
              "SMB CSI": {"smb_create_secret": True, "smb_user": "u", "smb_password": "p"}}
    d = c.post("/api/download/all-edited", json={"mode": "Networking & Storage", "cache": acache})
    got = unzip(d.data)
    res = {k: agen.generate(k, P.full_values(k, acache[k])) for k in agen.ADDON_TYPES if k in acache}
    files, _ = P.save_all_plan("Networking & Storage", res)
    check(got == plan_dict(files) and got["smb-csi/secret.yaml"][1] == 0o600 and got["calico/install.sh"][1] == 0o755,
          "add-on all-edited zip identical, secret 0600, install.sh 0755")
    one = c.post("/api/download", json={"kind": "Cilium", "values": {"cil_hubble": True}})
    check(one.status_code == 200 and set(unzip(one.data)) == {"values.yaml", "install.sh"}, "single add-on zip")
    sess = {"kind": {"disable_default_cni": False, "pod_subnet": "10.244.0.0/16"}, "Calico": {"cal_mtu": "1440"}}
    g = c.post("/api/generate", json={"kind": "Calico", "values": sess["Calico"], "session": sess}).json
    check(any("Cross-check" in w and "10.244.0.0/16" in w for w in g["warnings"]) and
          any("kindnet" in w for w in g["warnings"]), f"cross-check warnings on the Calico form: {g['warnings']}")
    exp = P.generate_in_session("Calico", P.full_values("Calico", sess["Calico"]),
                                {k: P.full_values(k, v) for k, v in sess.items()})
    check(g["warnings"] == exp.warnings, "web warnings identical to providers.generate_in_session")
    g = c.post("/api/generate", json={"kind": "kind", "values": sess["kind"], "session": sess}).json
    check(any("Calico" in w for w in g["warnings"]), "cross-check on the kind form too")
    g = c.post("/api/generate", json={"kind": "Calico", "values": {}}).json
    check(not any("Cross-check" in w for w in g["warnings"]), "no session -> no cross-check")
    sess2 = {"StorageClass": {"name": "fast"}, "Longhorn": {"name": "lh-ssd"}, "PersistentVolumeClaim": {}}
    g = c.post("/api/generate", json={"kind": "PersistentVolumeClaim", "values": {}, "session": sess2}).json
    check(g["options"].get("storage_class", [])[-2:] == ["fast", "lh-ssd"], f"PVC storageClass options: {g.get('options')}")
    g = c.post("/api/generate", json={"kind": "Deployment", "values": {}, "session": sess2}).json
    check(g["options"] == {}, "no dynamic options for other kinds")
    r = c.post("/api/check_row", json={"table": "cal_pools", "row": {"name": "p", "cidr": "10.0.0.1/16"}}).json
    check(r["errors"] and "CIDR" in r["errors"][0], f"check_row for Calico IP pools: {r}")
    r = c.post("/api/check_row", json={"table": "ingress_rules", "row": {"peer": "ipBlock (CIDR)", "cidr": "x"}}).json
    check(r["errors"], "check_row for NetworkPolicy rules")
    ok("Networking & Storage: zips, cross-checks, session StorageClass options, row checks")

    print(f"\nWEB RESULT: {'PASS' if not failed else 'FAIL'}  ({passed} passed, {failed} failed)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
