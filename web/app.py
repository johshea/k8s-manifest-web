"""
web/app.py - Flask front-end for the generator core.

All generation happens server-side through providers.py / manifest_gen / cluster_config_gen /
app_templates / field_docs, so the YAML is identical to what the core produces from Python. The browser keeps the
values (localStorage); the server stores nothing and never logs request bodies.

    from web.app import create_app
    create_app().run("127.0.0.1", 8080)          # or: python3 k8s_manifest_web.py
"""
from __future__ import annotations

import io
import json
import logging
import os
import sys
import time
import zipfile
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from flask import Flask, Response, abort, jsonify, render_template, request  # noqa: E402

import app_templates as tpl  # noqa: E402
import field_docs  # noqa: E402
import manifest_gen as gen  # noqa: E402
import providers as P  # noqa: E402

log = logging.getLogger("k8s_manifest_web")
MAX_BODY = 2 * 1024 * 1024          # requests above this get 413
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


# --------------------------------------------------------------------------- #
# JSON views of the shared definitions
# --------------------------------------------------------------------------- #
def field_json(f, table_key: str | None = None) -> dict:
    d = {"key": f.key, "label": f.label, "kind": f.kind, "default": f.default, "options": list(f.options),
         "help": f.help, "required": f.required, "height": f.height, "summary": list(f.summary),
         "tooltip": field_docs.tooltip(f)}
    if f.kind == "table":
        d["columns"] = [field_json(c, f.key) for c in f.columns]
        d["row_defaults"] = P.row_defaults_any(f.key)
    return d


def schema_json(kind: str) -> dict:
    p = P.provider_for(kind)
    collapsed = P.collapsed_sections(kind)
    return {
        "kind": kind, "mode": p.mode, "label": p.label, "bundle": p.bundle, "info": p.info(kind),
        "defaults": p.defaults(kind),
        "sections": [{"title": t, "help": field_docs.section_help(kind, t), "collapsed": t in collapsed,
                      "fields": [field_json(f) for f in fs]} for t, fs in p.schema(kind)],
    }


def param_json(prm) -> dict:
    return {"key": prm.key, "label": prm.label, "type": prm.type, "default": prm.default,
            "options": list(prm.options), "help": prm.help, "tooltip": field_docs.tooltip(prm)}


def meta_json() -> dict:
    return {
        "modes": [{"mode": p.mode, "label": p.label, "bundle": p.bundle, "kinds": list(p.kinds),
                   "selector": list(p.selector_values),
                   "groups": {g: list(ts) for g, ts in p.group_of.items()}}
                  for p in P.PROVIDERS.values()],
        "group_header": P.GROUP_HEADER,
        "templates": {"none": tpl.NONE_CHOICE, "values": P.template_values(),
                      "groups": {g: list(ts) for g, ts in tpl.grouped().items()}},
        "top_help": {k: field_docs.top_help(k) for k in ("mode", "kind", "template")},
    }


# --------------------------------------------------------------------------- #
# Generation helpers (pure functions of the request; nothing is stored)
# --------------------------------------------------------------------------- #
def _kind(name) -> str:
    if not isinstance(name, str) or name not in P.MODE_OF:
        abort(_json_error(400, f"unknown kind / config type {name!r}"))
    return name


def _values(v) -> dict:
    if v is None:
        return {}
    if not isinstance(v, dict):
        abort(_json_error(400, "values must be an object"))
    return v


def _generate(kind: str, values: dict):
    p = P.provider_for(kind)
    try:
        return p.generate(kind, P.full_values(kind, values))
    except Exception as exc:      # never echo values: they may hold secrets
        log.error("generate(%s) raised %s", kind, type(exc).__name__)
        abort(_json_error(500, f"internal error while generating {kind} ({type(exc).__name__})"))


def preview_json(kind: str, res) -> dict:
    p = P.provider_for(kind)
    text = ""
    errors = list(res.errors)
    if res.manifest is not None:
        try:
            text = p.render(res)
        except Exception as exc:
            errors.append(f"YAML rendering failed: {exc}")
    if errors:
        text = "# ⚠ Fix the errors below before saving\n" + text
    files = [{"name": n, "mode": (oct(m) if m is not None else None)}
             for n, _, m in P.output_files(kind, res)] if res.manifest is not None and not errors else []
    return {"kind": kind, "ok": not errors, "errors": errors, "warnings": list(res.warnings), "text": text,
            "files": files, "info": p.info(kind, res)}


def zip_bytes(files: list[tuple[str, str, int | None]]) -> bytes:
    """In-memory zip; Unix permission bits (0600 secrets, 0755 scripts) are stored in the entries,
    so `unzip` / Archive Utility restore them (a plain browser download can't set modes)."""
    buf = io.BytesIO()
    now = time.localtime()[:6]
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text, mode in files:
            zi = zipfile.ZipInfo(name, date_time=now)
            zi.create_system = 3                      # Unix, so external_attr carries the mode
            zi.external_attr = (0o100000 | (mode if mode is not None else 0o644)) << 16
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, text.encode("utf-8"))
    return buf.getvalue()


def _attachment(data: bytes, filename: str, mimetype: str, extra: dict | None = None) -> Response:
    r = Response(data, mimetype=mimetype)
    r.headers["Content-Disposition"] = f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}"
    for k, v in (extra or {}).items():
        r.headers[k] = v
    return r


def _json_error(status: int, msg: str, **extra) -> Response:
    r = jsonify({"error": msg, **extra})
    r.status_code = status
    return r


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
def create_app(allowed_hosts: set | None = None) -> Flask:
    """*allowed_hosts*: Host header names accepted (DNS-rebinding guard). Defaults to loopback names,
    or the comma-separated K8SGUI_ALLOWED_HOSTS env var ('*' disables the check, e.g. in Docker)."""
    app = Flask(__name__, template_folder=os.path.join(HERE, "templates"),
                static_folder=os.path.join(HERE, "static"))
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY
    app.config["JSON_SORT_KEYS"] = False
    app.json.sort_keys = False
    app.json.ensure_ascii = False
    if allowed_hosts is None:
        env = os.environ.get("K8SGUI_ALLOWED_HOSTS", "")
        allowed_hosts = {h.strip() for h in env.split(",") if h.strip()} or set(LOOPBACK_HOSTS)
    app.config["ALLOWED_HOSTS"] = allowed_hosts

    @app.before_request
    def _guard():
        hosts = app.config["ALLOWED_HOSTS"]
        if "*" not in hosts:
            host = (request.host or "").rsplit(":", 1)[0] if not (request.host or "").startswith("[") \
                else request.host.split("]")[0] + "]"
            if host not in hosts:
                return _json_error(400, "unexpected Host header (start with --host or set K8SGUI_ALLOWED_HOSTS)")
        if request.method == "POST":
            # JSON-only POSTs: a cross-site page can't send application/json without a CORS preflight,
            # which this app never grants - a cheap CSRF guard for a local tool.
            if not request.is_json:
                return _json_error(415, "POST bodies must be application/json")
            if request.content_length is not None and request.content_length > MAX_BODY:
                return _json_error(413, "request too large")

    @app.after_request
    def _headers(r: Response):
        r.headers.setdefault("Content-Security-Policy",
                             "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                             "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        r.headers.setdefault("X-Content-Type-Options", "nosniff")
        r.headers.setdefault("X-Frame-Options", "DENY")
        r.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.path.startswith("/api/"):
            r.headers["Cache-Control"] = "no-store"
        return r

    @app.errorhandler(413)
    def _too_large(_e):
        return _json_error(413, f"request too large (limit {MAX_BODY // 1024} KiB)")

    def body() -> dict:
        d = request.get_json(silent=True)
        if not isinstance(d, dict):
            abort(_json_error(400, "expected a JSON object"))
        return d

    # ---------------------------------------------------------------- pages
    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True})

    # ---------------------------------------------------------------- definitions
    @app.get("/api/meta")
    def api_meta():
        return jsonify(meta_json())

    @app.get("/api/schema")
    def api_schema():
        return jsonify(schema_json(_kind(request.args.get("kind"))))

    @app.get("/api/template")
    def api_template():
        title = P.resolve_template(request.args.get("title", ""))
        if title not in tpl.titles():
            return _json_error(400, f"unknown template {title!r}")
        t = tpl.get(title)
        return jsonify({"title": title, "image": t.image, "description": t.description,
                        "primary": t.primary, "params": [param_json(p) for p in tpl.params_for(title)],
                        "defaults": tpl.default_params(title)})

    # ---------------------------------------------------------------- generation
    @app.post("/api/generate")
    def api_generate():
        d = body()
        kind = _kind(d.get("kind"))
        session = d.get("session")
        if isinstance(session, dict):   # {kind: values} of every form opened (all modes) -> cross-mode hints
            sess = {k: P.full_values(k, v) for k, v in session.items() if k in P.MODE_OF and isinstance(v, dict)}
            vals = P.full_values(kind, _values(d.get("values")))
            sess[kind] = vals
            try:
                res = P.generate_in_session(kind, vals, sess)
            except Exception as exc:      # never echo values: they may hold secrets
                log.error("generate(%s) raised %s", kind, type(exc).__name__)
                return _json_error(500, f"internal error while generating {kind} ({type(exc).__name__})")
            out = preview_json(kind, res)
            out["options"] = P.dynamic_options(kind, sess)
        else:
            out = preview_json(kind, _generate(kind, _values(d.get("values"))))
        cache = d.get("cache")
        if isinstance(cache, dict):     # {kind: values} of the same mode -> which ones differ from defaults
            mode = P.MODE_OF[kind]
            vals = {k: v for k, v in cache.items() if k in P.MODE_OF and P.MODE_OF[k] == mode and isinstance(v, dict)}
            vals[kind] = _values(d.get("values"))
            out["edited"] = [k for k in P.PROVIDERS[mode].kinds if k in vals and P.is_edited(k, vals[k])]
        return jsonify(out)

    @app.post("/api/check_row")
    def api_check_row():
        d = body()
        table = d.get("table")
        row = d.get("row")
        if not isinstance(table, str) or not isinstance(row, dict):
            return _json_error(400, "need table and row")
        try:
            defaults = P.row_defaults_any(table)
        except KeyError:
            return _json_error(400, f"unknown table {table!r}")
        return jsonify({"errors": P.check_row_any(table, {**defaults, **row})})

    def _render_template(d: dict):
        title = P.resolve_template(d.get("title", ""))
        if title not in tpl.titles():
            abort(_json_error(400, f"unknown template {title!r}"))
        params = d.get("params") if isinstance(d.get("params"), dict) else None
        secrets = d.get("secrets") if isinstance(d.get("secrets"), dict) else None
        return tpl.render(title, params, secrets=secrets)

    def _current(d: dict):
        cur = d.get("current") if isinstance(d.get("current"), dict) else {}
        return lambda k: P.full_values(k, cur.get(k) if isinstance(cur.get(k), dict) else {})

    @app.post("/api/template/render")
    def api_template_render():
        d = body()
        tset = _render_template(d)
        if tset.errors:
            return jsonify({"ok": False, "errors": tset.errors, "warnings": tset.warnings})
        prev = d.get("previous") if isinstance(d.get("previous"), dict) else {}
        values_for = _current(d)
        counts: dict[str, int] = {}
        for r in tset.items:
            counts[r.item.kind] = counts.get(r.item.kind, 0) + 1
        parts = [k if n == 1 else f"{k}×{n}" for k, n in counts.items()]
        return jsonify({
            "ok": True, "errors": [], "warnings": tset.warnings, "title": tset.title, "name": tset.name,
            "params": tset.params, "secrets": tset.secrets, "primary_kind": tset.primary_kind,
            "values": tset.primary_values(), "kinds": tset.kinds,
            "conflicts": P.template_conflicts(tset, values_for, prev),
            "stale": P.template_stale(tset, values_for, prev),
            "summary": P.template_summary(tset),
            "status": f"Applied {tset.title} template '{tset.name}': {len(tset.items)} objects ({', '.join(parts)}).",
            "files": [r.filename for r in tset.items],
        })

    # ---------------------------------------------------------------- downloads
    @app.post("/api/download")
    def api_download():
        d = body()
        kind = _kind(d.get("kind"))
        res = _generate(kind, _values(d.get("values")))
        if not res.ok:
            return _json_error(422, f"{kind} has {len(res.errors)} error(s)", errors=res.errors)
        files = P.output_files(kind, res)
        if len(files) == 1 and "/" not in files[0][0]:
            name, text, mode = files[0]
            mt = "application/json" if name.endswith(".json") else \
                "text/x-shellscript" if name.endswith(".sh") else "application/yaml"
            return _attachment(text.encode("utf-8"), name, mt, {"X-File-Mode": oct(mode) if mode else ""})
        return _attachment(zip_bytes(files), f"{P.type_folder(kind)}.zip", "application/zip")

    @app.post("/api/download/all-edited")
    def api_download_all():
        d = body()
        mode = d.get("mode")
        if mode not in P.PROVIDERS:
            return _json_error(400, f"unknown mode {mode!r}")
        cache = d.get("cache") if isinstance(d.get("cache"), dict) else {}
        p = P.PROVIDERS[mode]
        kinds = [k for k in p.kinds if isinstance(cache.get(k), dict) and P.is_edited(k, cache[k])]
        if not kinds:
            return _json_error(400, "No kinds have been edited yet - download the current kind, or use the bundle.")
        results = {k: _generate(k, cache[k]) for k in kinds}
        files, bad = P.save_all_plan(mode, results)
        if not files:
            return _json_error(422, "every edited type has errors", skipped=bad)
        if bad:
            files.append(("SKIPPED.txt", "Not included (fix the errors first):\n" + "".join(f"- {b}\n" for b in bad),
                          None))
        name = "edited-manifests.zip" if p.bundle else "edited-cluster-configs.zip"
        return _attachment(zip_bytes(files), name, "application/zip",
                           {"X-Saved-Kinds": quote(",".join(k for k in kinds if results[k].ok)),
                            "X-Skipped": quote(json.dumps(bad))})

    @app.post("/api/download/template")
    def api_download_template():
        d = body()
        tset = _render_template(d)
        if tset.errors:
            return _json_error(422, "template has errors", errors=tset.errors)
        try:
            files = P.template_set_plan(tset, _current(d))
        except ValueError as exc:
            return _json_error(422, "fix these first", errors=str(exc).split("\n• "))
        return _attachment(zip_bytes(files), f"{tset.name}.zip", "application/zip")

    @app.post("/api/download/bundle")
    def api_download_bundle():
        d = body()
        items = d.get("items")
        if not isinstance(items, list) or not items:
            return _json_error(400, "The bundle is empty - use 'Add to bundle' first.")
        docs, errs = [], []
        for it in items:
            if not isinstance(it, dict):
                return _json_error(400, "bad bundle item")
            kind = _kind(it.get("kind"))
            if not P.provider_for(kind).bundle:
                return _json_error(400, "bundles combine Kubernetes manifests only")
            res = _generate(kind, _values(it.get("values")))
            if res.ok:
                docs.append(res.manifest)
            else:
                errs.append(f"{kind}: {res.errors[0]}")
        if errs:
            return _json_error(422, "bundle items have errors", errors=errs)
        secret = any(m["kind"] == "Secret" for m in docs)
        return _attachment(gen.to_multi_yaml(docs).encode("utf-8"), "bundle.yaml", "application/yaml",
                           {"X-File-Mode": "0o600" if secret else ""})

    return app
