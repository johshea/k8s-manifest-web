/* Kubernetes Manifest Generator - web UI. Vanilla JS, no dependencies.
 * All generation/validation is done by the Python generators through the JSON API (web/app.py);
 * this file builds the forms from the same field definitions and keeps the values in localStorage. */
"use strict";

const STORE_KEY = "k8s-manifest-web.v1";
const TIP_DELAY = 400, PREVIEW_DELAY = 300, TEMPLATE_DELAY = 600;
const $ = (sel, el = document) => el.querySelector(sel);
const el = (tag, attrs = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids) if (c !== null && c !== undefined) e.append(c);
  return e;
};

// ------------------------------------------------------------------ state
const S = {
  meta: null, schemas: {}, mode: "Manifests", kind: null, form: null,
  lastKind: {}, cache: {}, collapsed: {}, bundle: [], tpl: null, tplDef: null,
  live: true, last: null, timers: {}, tplLoading: false,
};

function load() {
  try {
    const d = JSON.parse(localStorage.getItem(STORE_KEY) || "{}");
    Object.assign(S, {lastKind: d.lastKind || {}, cache: d.cache || {}, collapsed: d.collapsed || {},
                      bundle: d.bundle || [], tpl: d.tpl || null, live: d.live !== false});
    return d;
  } catch (e) { return {}; }
}
function persist() {
  clearTimeout(S.timers.persist);
  S.timers.persist = setTimeout(persistNow, 150);
}
function persistNow() {
  if (S.form) S.cache[S.kind] = currentValues();
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify({mode: S.mode, kind: S.kind, lastKind: S.lastKind, cache: S.cache,
      collapsed: S.collapsed, bundle: S.bundle, tpl: S.tpl, live: S.live}));
  } catch (e) { setStatus("Could not save values in the browser: " + e.message); }
}

// ------------------------------------------------------------------ API
async function api(path, body) {
  const opt = body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)};
  const r = await fetch(path, opt);
  const ct = r.headers.get("Content-Type") || "";
  if (ct.includes("application/json")) {
    const j = await r.json();
    if (!r.ok) throw Object.assign(new Error(j.error || r.statusText), {data: j, status: r.status});
    return j;
  }
  if (!r.ok) throw new Error(r.status + " " + r.statusText);
  return r;
}
async function schemaFor(kind) {
  if (!S.schemas[kind]) S.schemas[kind] = await api("/api/schema?kind=" + encodeURIComponent(kind));
  return S.schemas[kind];
}
const modeInfo = (m) => S.meta.modes.find((x) => x.mode === m);
const modeOf = (kind) => S.meta.modes.find((m) => m.kinds.includes(kind)).mode;
const isBundleMode = () => modeInfo(S.mode).bundle;
const clone = (o) => JSON.parse(JSON.stringify(o));

// ------------------------------------------------------------------ tooltips
const Tip = {
  node: null, owner: null, timer: null,
  init() {
    this.node = $("#tip");
    document.addEventListener("scroll", () => this.hide(), true);
    window.addEventListener("resize", () => this.hide());
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") this.hide(); });
    document.addEventListener("pointerdown", (e) => { if (this.owner && !this.owner.contains(e.target)) this.hide(); }, true);
  },
  attach(btn, text) {
    btn.dataset.tip = typeof text === "function" ? "" : text;
    btn._tipText = text;
    const label = (typeof text === "function" ? text() : text).split("\n")[0];
    btn.setAttribute("aria-label", "Help: " + label);
    btn.addEventListener("mouseenter", () => { clearTimeout(this.timer); this.timer = setTimeout(() => this.show(btn), TIP_DELAY); });
    btn.addEventListener("mouseleave", () => this.hide());
    btn.addEventListener("focus", () => this.show(btn));
    btn.addEventListener("blur", () => this.hide());
    btn.addEventListener("click", (e) => { e.preventDefault(); this.owner === btn ? this.hide() : this.show(btn); });
    btn.addEventListener("wheel", () => this.hide(), {passive: true});
  },
  text(btn) { return typeof btn._tipText === "function" ? btn._tipText() : btn._tipText; },
  show(btn) {
    clearTimeout(this.timer);
    if (!btn.isConnected) return;
    const t = this.text(btn) || "";
    const i = t.indexOf("\n");
    this.node.replaceChildren(el("span", {class: "tip-title", text: i < 0 ? t : t.slice(0, i)}),
                              document.createTextNode(i < 0 ? "" : t.slice(i + 1)));
    this.node.hidden = false;
    this.node.style.left = "0px"; this.node.style.top = "0px";
    const r = btn.getBoundingClientRect(), w = this.node.offsetWidth, h = this.node.offsetHeight;
    const vw = document.documentElement.clientWidth, vh = document.documentElement.clientHeight;
    let x = r.right + 6, y = r.bottom + 4;
    if (x + w > vw - 4) x = r.left - w - 6;
    if (y + h > vh - 4) y = r.top - h - 4;
    x = Math.min(Math.max(4, x), Math.max(4, vw - w - 4));
    y = Math.min(Math.max(4, y), Math.max(4, vh - h - 4));
    this.node.style.left = x + "px"; this.node.style.top = y + "px";
    if (!btn.id) btn.id = "i" + Math.random().toString(36).slice(2);
    btn.setAttribute("aria-describedby", "tip");
    this.owner = btn;
  },
  hide() {
    clearTimeout(this.timer);
    if (this.owner) this.owner.removeAttribute("aria-describedby");
    this.owner = null;
    if (this.node) this.node.hidden = true;
  },
};
function infoIcon(text) {
  const b = el("button", {type: "button", class: "info", text: "ⓘ"});
  Tip.attach(b, text);
  return b;
}

// ------------------------------------------------------------------ widgets
let uid = 0;
function makeWidget(f, value, onChange) {
  const id = "w" + (++uid);
  let w, get, set;
  if (f.kind === "text") {
    w = el("textarea", {id, rows: Math.max(2, f.height || 3), spellcheck: "false", "data-key": f.key, wrap: "off"});
    w.value = value ?? ""; get = () => w.value; set = (v) => { w.value = v ?? ""; };
    w.addEventListener("input", onChange);
  } else if (f.kind === "check") {
    w = el("input", {id, type: "checkbox", "data-key": f.key});
    w.checked = !!value; get = () => w.checked; set = (v) => { w.checked = !!v; };
    w.addEventListener("change", onChange);
  } else if (f.kind === "combo") {
    w = el("select", {id, "data-key": f.key});
    const opts = [...f.options];
    if (value !== undefined && value !== null && !opts.includes(String(value))) opts.push(String(value));
    for (const o of opts) w.append(el("option", {value: o, text: o === "" ? "(none)" : o}));
    w.value = String(value ?? ""); get = () => w.value; set = (v) => { w.value = String(v ?? ""); };
    w.addEventListener("change", onChange);
  } else {   // entry / ecombo (editable dropdown -> input + datalist)
    w = el("input", {id, type: "text", "data-key": f.key, spellcheck: "false", autocomplete: "off"});
    if (f.kind === "ecombo" && f.options.length) {
      const dl = el("datalist", {id: id + "-dl"});
      for (const o of f.options) if (o !== "") dl.append(el("option", {value: o}));
      w.setAttribute("list", id + "-dl");
      w._datalist = dl;
    }
    w.value = value ?? ""; get = () => w.value; set = (v) => { w.value = v ?? ""; };
    w.addEventListener("input", onChange);
  }
  return {w, get, set, id};
}
function fieldRow(grid, f, value, onChange, icons) {
  const {w, get, set, id} = makeWidget(f, value, onChange);
  const ic = infoIcon(f.tooltip);
  icons[f.key] = ic;
  grid.append(el("div", {class: "lbl"}, el("label", {for: id, text: f.label + (f.required ? " *" : "")}), ic));
  const ctl = el("div", {class: "ctl"}, w, w._datalist || null);
  if (f.help) ctl.append(el("div", {class: "fhelp", text: f.help}));
  grid.append(ctl);
  return {w, get, set};
}

// ------------------------------------------------------------------ table editor
class TableEditor {
  constructor(f, rows, onChange) {
    this.f = f; this.rows = clone(rows || []); this.onChange = onChange; this.sel = null;
    this.labels = Object.fromEntries(f.columns.map((c) => [c.key, c.label]));
    this.tbody = el("tbody");
    const thead = el("thead", {}, el("tr", {}, ...f.summary.map((k) => el("th", {text: this.labels[k] || k}))));
    this.table = el("table", {"data-table": f.key}, thead, this.tbody);
    const btn = (text, fn, cls) => el("button", {type: "button", text, class: cls, onclick: fn});
    this.buttons = el("div", {class: "tablebtns"},
      btn("Add…", () => this.add(), "t-add"), btn("Edit…", () => this.edit(), "t-edit"),
      btn("Duplicate", () => this.duplicate(), "t-dup"), btn("Remove", () => this.remove(), "t-remove"),
      btn("▲", () => this.move(-1), "t-up"), btn("▼", () => this.move(1), "t-down"));
    this.node = el("div", {class: "tablewrap"}, this.table, this.buttons);
    this.render();
  }
  render() {
    this.tbody.replaceChildren();
    if (!this.rows.length) {
      this.tbody.append(el("tr", {class: "empty"}, el("td", {colspan: this.f.summary.length || 1, text: "no rows - click Add…"})));
    }
    this.rows.forEach((r, i) => {
      const tr = el("tr", {class: i === this.sel ? "sel" : null, tabindex: "0"},
        ...this.f.summary.map((k) => {
          let v = r[k];
          v = typeof v === "boolean" ? (v ? "✓" : "") : String(v ?? "").split("\n")[0];
          return el("td", {text: v.length > 40 ? v.slice(0, 37) + "…" : v, title: String(r[k] ?? "")});
        }));
      tr.addEventListener("click", () => { this.sel = i; this.render(); });
      tr.addEventListener("dblclick", () => { this.sel = i; this.edit(); });
      tr.addEventListener("keydown", (e) => { if (e.key === "Enter") { this.sel = i; this.edit(); } });
      this.tbody.append(tr);
    });
  }
  get() { return clone(this.rows); }
  set(rows) { this.rows = clone(rows || []); this.sel = null; this.render(); }
  changed() { this.render(); this.onChange(); }
  async add() { const r = await RowDialog.open(this.f, null); if (r) { this.rows.push(r); this.sel = this.rows.length - 1; this.changed(); } }
  async edit() {
    if (this.sel === null) { if (this.rows.length === 1) this.sel = 0; else return setStatus("Select a row first."); }
    const r = await RowDialog.open(this.f, this.rows[this.sel]);
    if (r) { this.rows[this.sel] = r; this.changed(); }
  }
  duplicate() {
    if (this.sel === null) return setStatus("Select a row first.");
    const r = clone(this.rows[this.sel]);
    if ("name" in r && r.name) r.name = r.name + "-copy";
    this.rows.splice(this.sel + 1, 0, r); this.sel += 1; this.changed();
  }
  remove() { if (this.sel === null) return setStatus("Select a row first."); this.rows.splice(this.sel, 1); this.sel = this.rows.length ? Math.min(this.sel, this.rows.length - 1) : null; this.changed(); }
  move(d) {
    const i = this.sel, j = i + d;
    if (i === null || j < 0 || j >= this.rows.length) return;
    [this.rows[i], this.rows[j]] = [this.rows[j], this.rows[i]]; this.sel = j; this.changed();
  }
}

const RowDialog = {
  open(f, row) {
    const dlg = $("#rowdlg"), body = $("#rowdlg-body"), err = $("#rowdlg-err");
    $("#rowdlg-title").textContent = (row ? "Edit" : "Add") + " - " + f.label;
    body.replaceChildren(); err.textContent = "";
    const values = Object.assign({}, f.row_defaults, row || {});
    const getters = {};
    this.icons = {};
    for (const c of f.columns) getters[c.key] = fieldRow(body, c, values[c.key], () => {}, this.icons).get;
    Tip.hide();
    dlg.showModal();
    const first = body.querySelector("input,select,textarea"); if (first) first.focus();
    return new Promise((resolve) => {
      const collect = () => Object.fromEntries(Object.entries(getters).map(([k, g]) => [k, g()]));
      const done = (v) => { cleanup(); dlg.close(); Tip.hide(); resolve(v); };
      const ok = async () => {
        const r = collect();
        try {
          const j = await api("/api/check_row", {table: f.key, row: r});
          if (j.errors.length) { err.textContent = "✖ " + j.errors.join("\n✖ "); return; }
        } catch (e) { err.textContent = "✖ " + e.message; return; }
        done(r);
      };
      const cancel = () => done(null);
      const onCancel = (e) => { e.preventDefault(); cancel(); };
      const onKey = (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) ok(); };
      $("#rowdlg-ok").onclick = ok; $("#rowdlg-cancel").onclick = cancel;
      dlg.addEventListener("cancel", onCancel); dlg.addEventListener("keydown", onKey);
      const cleanup = () => { dlg.removeEventListener("cancel", onCancel); dlg.removeEventListener("keydown", onKey); };
      RowDialog.ok = ok; RowDialog.cancel = cancel;
    });
  },
};

// ------------------------------------------------------------------ form
class KindForm {
  constructor(schema, values) {
    this.kind = schema.kind; this.getters = {}; this.setters = {}; this.widgets = {}; this.tables = {}; this.icons = {};
    this.sections = {};
    const root = $("#form"); root.replaceChildren();
    const collapsed = new Set(S.collapsed[this.kind] || schema.sections.filter((s) => s.collapsed).map((s) => s.title));
    const onChange = () => scheduleRefresh();
    for (const sec of schema.sections) {
      const sum = el("summary", {}, el("span", {text: sec.title}));
      const icon = infoIcon(sec.help ? sec.title + "\n" + sec.help : sec.title);
      sum.append(icon);
      const grid = el("div", {class: "grid"});
      const det = el("details", {class: "section", open: !collapsed.has(sec.title), "data-section": sec.title}, sum, grid);
      det.icon = icon;
      det.addEventListener("toggle", () => {
        const st = new Set(S.collapsed[this.kind] || [...collapsed]);
        det.open ? st.delete(sec.title) : st.add(sec.title);
        S.collapsed[this.kind] = [...st]; persist();
      });
      this.sections[sec.title] = det;
      root.append(det);
      for (const f of sec.fields) {
        if (f.kind === "table") {
          const ic = infoIcon(f.tooltip); this.icons[f.key] = ic;
          const te = new TableEditor(f, values[f.key], onChange);
          const wrap = el("div", {class: "full tablefield"}, el("div", {class: "thead"}, el("span", {text: f.label}), ic),
                          f.help ? el("div", {class: "fhelp", text: f.help}) : null, te.node);
          grid.append(wrap);
          this.tables[f.key] = te; this.getters[f.key] = () => te.get(); this.setters[f.key] = (v) => te.set(v);
          this.widgets[f.key] = te.node;
          continue;
        }
        const {w, get, set} = fieldRow(grid, f, values[f.key], onChange, this.icons);
        this.getters[f.key] = get; this.setters[f.key] = set; this.widgets[f.key] = w;
      }
    }
    // form behaviour (name -> app=<name> label sync etc.)
    this.prevName = String(values.name ?? "");
    if (this.widgets.name) this.widgets.name.addEventListener("input", () => this.nameChanged());
    if (this.kind === "Service" && this.widgets.service_type) {
      this.widgets.service_type.addEventListener("change", () => this.serviceTypeChanged(true));
      this.serviceTypeChanged(false);
    }
  }
  values() { return Object.fromEntries(Object.entries(this.getters).map(([k, g]) => [k, g()])); }
  nameChanged() {
    const nw = this.getters.name().trim(), old = this.prevName;
    if (nw === old) return;
    for (const [key, fmt] of [["labels", "app={}"], ["selector", "app={}"], ["service_name", "{}"], ["backend_service", "{}"]]) {
      if (this.getters[key] && String(this.getters[key]()).trim() === fmt.replace("{}", old)) this.setters[key](fmt.replace("{}", nw));
    }
    this.prevName = nw;
  }
  serviceTypeChanged(clear) {
    const t = this.getters.service_type(), np = ["NodePort", "LoadBalancer"].includes(t);
    this.widgets.node_port.disabled = !np;
    this.widgets.external_name.disabled = t !== "ExternalName";
    this.widgets.external_traffic_policy.disabled = !np;
    if (clear && !np) { this.setters.node_port(""); this.setters.external_traffic_policy(""); }
  }
}
const currentValues = () => (S.form ? S.form.values() : {});
const valuesFor = (kind) => (kind === S.kind && S.form ? currentValues() : (S.cache[kind] || {}));

// ------------------------------------------------------------------ selectors
function fillKindSelector() {
  const m = modeInfo(S.mode), sel = $("#kind");
  sel.replaceChildren();
  const groups = Object.entries(m.groups || {});
  if (groups.length) {
    for (const [g, ts] of groups) sel.append(el("optgroup", {label: g}, ...ts.map((t) => el("option", {value: t, text: t}))));
  } else {
    for (const k of m.kinds) sel.append(el("option", {value: k, text: k}));
  }
  $("#type-label").textContent = m.label;
}
function fillTemplateSelector() {
  const sel = $("#template"), t = S.meta.templates;
  sel.replaceChildren(el("option", {value: t.none, text: t.none}));
  for (const [g, ts] of Object.entries(t.groups)) sel.append(el("optgroup", {label: g}, ...ts.map((x) => el("option", {value: x, text: x}))));
}

async function selectMode(mode) {
  if (mode === S.mode && S.form) return;
  await selectKind(S.lastKind[mode] || modeInfo(mode).kinds[0]);
}

async function selectKind(kind, {force = false} = {}) {
  if (!S.meta.modes.some((m) => m.kinds.includes(kind))) throw new Error("unknown kind " + kind);
  Tip.hide();
  clearTimeout(S.timers.refresh);
  if (S.form && (kind !== S.kind || force)) S.cache[S.kind] = currentValues();
  const schema = await schemaFor(kind);
  if (!S.form || kind !== S.kind || force) {
    S.kind = kind;
    S.form = new KindForm(schema, Object.assign({}, schema.defaults, S.cache[kind] || {}));
    $("#form-pane").scrollTop = 0;
  }
  const newMode = modeOf(kind);
  const modeChanged = newMode !== S.mode;
  S.mode = newMode; S.lastKind[S.mode] = kind;
  if (modeChanged || !$("#kind").options.length) fillKindSelector();
  $("#mode").value = S.mode; $("#kind").value = kind;
  const bundle = isBundleMode();
  $("#btn-reset").textContent = bundle ? "Reset kind" : "Reset type";
  $("#btn-copy").textContent = bundle ? "Copy YAML" : "Copy";
  for (const b of document.querySelectorAll("button.bundle")) b.disabled = !bundle;
  $("#tpl-row").hidden = !bundle;
  $("#tpl-panel").hidden = !bundle || !S.tplDef;
  persist();
  return refresh();
}

// ------------------------------------------------------------------ preview / validation
function scheduleRefresh() {
  persist();
  if (!S.live) return;
  clearTimeout(S.timers.refresh);
  S.timers.refresh = setTimeout(refresh, PREVIEW_DELAY);
}
function modeCache() {
  const out = {};
  for (const k of modeInfo(S.mode).kinds) if (S.cache[k]) out[k] = S.cache[k];
  return out;
}
function sessionCache() {      // every form opened in this session (all modes): cross-checks, StorageClass names
  const out = Object.assign({}, S.cache);
  if (S.form) out[S.kind] = currentValues();
  return out;
}
async function generate(kind = S.kind) {
  const body = {kind, values: valuesFor(kind)};
  if (kind === S.kind) { body.cache = modeCache(); body.session = sessionCache(); }
  return api("/api/generate", body);
}
function applyDynamicOptions(opts) {   // e.g. StorageClass names created in this session -> PVC dropdown
  if (!opts || !S.form) return;
  for (const [key, values] of Object.entries(opts)) {
    const w = S.form.widgets[key];
    if (!w || !w._datalist) continue;
    const have = [...w._datalist.options].map((o) => o.value);
    const want = values.filter((v) => v !== "");
    if (have.join("\n") === want.join("\n")) continue;
    w._datalist.replaceChildren(...want.map((v) => el("option", {value: v})));
  }
}
async function refresh() {
  clearTimeout(S.timers.refresh);
  if (!S.form) return null;
  const kind = S.kind;
  let r;
  try { r = await generate(kind); } catch (e) { setStatus("Preview failed: " + e.message); return null; }
  if (kind !== S.kind) return null;      // switched meanwhile
  S.last = r;
  applyDynamicOptions(r.options);
  $("#preview").textContent = r.text;
  showIssues(r);
  const bundle = isBundleMode();
  const names = r.files.map((f) => f.name);
  $("#preview-title").textContent = "Preview - " + kind + (bundle ? `   (bundle: ${S.bundle.length} doc${S.bundle.length !== 1 ? "s" : ""})`
                                                       : names.length ? "   -> " + names.join(" + ") : "");
  $("#api-info").textContent = r.info;
  const edited = r.edited || [];
  const what = bundle ? "kinds" : (S.mode === "Networking & Storage" ? "add-on types" : "config types");
  let label = edited.length ? `edited ${what}: ${edited.join(", ")}` : `no ${what} edited yet`;
  if (bundle && S.tpl && S.tpl.applied) label += `   ·   template: ${S.tpl.title} (${S.tpl.name})`;
  $("#edited").textContent = label;
  setStatus(`${kind}: ${r.errors.length} error(s), ${r.warnings.length} warning(s)`);
  document.body.dataset.ready = "1";
  return r;
}
function showIssues(r, verboseOk = false) {
  const ul = $("#issues"); ul.replaceChildren();
  for (const e of r.errors) ul.append(el("li", {class: "err", text: "✖ " + e}));
  for (const w of r.warnings) ul.append(el("li", {class: "warn", text: "⚠ " + w}));
  if (r.ok) {
    const ready = isBundleMode() ? "ready to save / kubectl apply" : "ready to save";
    ul.append(el("li", {class: "ok", text: verboseOk || !r.warnings.length ? `✔ Valid - ${ready}.` : "✔ No errors."}));
  }
}
async function validOrWarn() {
  const r = await refresh();
  if (!r) return null;
  showIssues(r, true);
  if (!r.ok) { alert(`${S.kind} has ${r.errors.length} error(s):\n\n• ` + r.errors.slice(0, 12).join("\n• ")); return null; }
  return r;
}
function setStatus(t) { $("#status").textContent = t; }

// ------------------------------------------------------------------ downloads
async function downloadFrom(path, body, fallbackName) {
  let r;
  try { r = await api(path, body); } catch (e) {
    const errs = (e.data && (e.data.errors || e.data.skipped)) || [];
    alert(e.message + (errs.length ? ":\n\n• " + errs.slice(0, 12).join("\n• ") : ""));
    return null;
  }
  const blob = await r.blob();
  const cd = r.headers.get("Content-Disposition") || "";
  const m = /filename\*=UTF-8''([^;]+)/.exec(cd) || /filename="([^"]+)"/.exec(cd);
  const name = m ? decodeURIComponent(m[1]) : fallbackName;
  const url = URL.createObjectURL(blob);
  const a = el("a", {href: url, download: name}); document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
  return {name, headers: r.headers};
}
async function download() {
  const r = await validOrWarn(); if (!r) return;
  const d = await downloadFrom("/api/download", {kind: S.kind, values: currentValues()}, "manifest.yaml");
  if (d) setStatus(`Downloaded ${d.name}` + (r.files.length > 1 ? ` (${r.files.length} files: ${r.files.map((f) => f.name).join(", ")})` : "") +
                   (r.files.some((f) => f.mode) ? " - see README: run chmod after a single-file download" : ""));
}
async function downloadAllEdited() {
  persistNow();
  const d = await downloadFrom("/api/download/all-edited", {mode: S.mode, cache: modeCache()}, "edited.zip");
  if (d) {
    const skipped = JSON.parse(decodeURIComponent(d.headers.get("X-Skipped") || "%5B%5D"));
    setStatus(`Downloaded ${d.name}: ${decodeURIComponent(d.headers.get("X-Saved-Kinds") || "")}` +
              (skipped.length ? `  (skipped: ${skipped.join("; ")})` : ""));
  }
}
async function copyYaml() {
  const r = await validOrWarn(); if (!r) return;
  try { await navigator.clipboard.writeText(r.text); }
  catch (e) {
    const ta = el("textarea", {style: "position:fixed;left:-9999px"}); ta.value = r.text; document.body.append(ta);
    ta.select(); document.execCommand("copy"); ta.remove();
  }
  setStatus(`Copied ${r.text.split("\n").length - 1} lines to the clipboard.`);
}

// ------------------------------------------------------------------ bundle
async function addToBundle() {
  if (!isBundleMode()) return;
  const r = await validOrWarn(); if (!r) return;
  const v = currentValues();
  S.bundle.push({kind: S.kind, values: clone(v), label: `${S.kind}/${v.name || ""}`});
  persist(); await refresh();
  setStatus(`Added ${S.kind}/${v.name} to bundle (${S.bundle.length} docs).`);
}
async function saveBundle() {
  if (!S.bundle.length) return alert("The bundle is empty - use 'Add to bundle' first.");
  const d = await downloadFrom("/api/download/bundle", {items: S.bundle}, "bundle.yaml");
  if (d) setStatus(`Downloaded ${S.bundle.length}-document bundle (${d.name}).`);
}
function clearBundle() { S.bundle = []; persist(); refresh(); setStatus("Bundle cleared."); }

// ------------------------------------------------------------------ templates
async function selectTemplate(title, apply = true) {
  const t = S.meta.templates;
  if (!title || title === t.none) {
    S.tpl = null; S.tplDef = null;
    $("#template").value = t.none; $("#tpl-panel").hidden = true;
    $("#tpl-desc").textContent = "no template - forms keep their values (use Reset kind to clear)";
    persist(); return refresh();
  }
  const def = S.tplDef = await api("/api/template?title=" + encodeURIComponent(title));
  $("#template").value = def.title;
  $("#tpl-desc").textContent = def.image;
  const params = S.tpl && S.tpl.title === def.title ? S.tpl.params : def.defaults;
  buildTemplateParams(def, params);
  $("#tpl-panel").hidden = false;
  if (apply) return applyTemplate(false);
  return null;
}
function buildTemplateParams(def, params) {
  const box = $("#tpl-fields"); box.replaceChildren();
  S.tplVars = {}; S.tplIcons = {};
  S.tplLoading = true;
  for (const p of def.params) {
    const f = {key: p.key, kind: p.type === "check" ? "check" : p.type, options: p.options, height: 1};
    const val = params[p.key] ?? p.default;
    const {w, get, set, id} = makeWidget(f, typeof val === "boolean" ? val : String(val), () => paramChanged());
    const ic = infoIcon(p.tooltip); S.tplIcons[p.key] = ic;
    box.append(el("div", {class: "lbl"}, el("label", {for: id, text: p.label + ":"}), ic), el("div", {}, w, w._datalist || null));
    S.tplVars[p.key] = {w, get, set};
  }
  S.tplLoading = false;
  updateHostState();
}
function templateParams() { return Object.fromEntries(Object.entries(S.tplVars || {}).map(([k, v]) => [k, v.get()])); }
function updateHostState() {
  if (!S.tplVars || !S.tplVars.ingress) return;
  const on = S.tplVars.ingress.get();
  for (const k of ["host", "ingress_class"]) if (S.tplVars[k]) S.tplVars[k].w.disabled = !on;
}
function paramChanged() {
  if (S.tplLoading) return;
  updateHostState();
  clearTimeout(S.timers.tpl);
  S.timers.tpl = setTimeout(() => applyTemplate(true), TEMPLATE_DELAY);
}
async function applyTemplate(auto) {
  clearTimeout(S.timers.tpl);
  const def = S.tplDef; if (!def) return null;
  const params = templateParams();
  const same = S.tpl && S.tpl.applied && S.tpl.title === def.title;
  if (S.form) S.cache[S.kind] = currentValues();
  const current = Object.fromEntries(S.meta.modes[0].kinds.map((k) => [k, valuesFor(k)]));
  let r;
  try {
    r = await api("/api/template/render", {title: def.title, params, secrets: same ? S.tpl.secrets : null,
                                            current, previous: (S.tpl && S.tpl.values) || {}});
  } catch (e) { $("#tpl-summary").textContent = "✖ " + e.message; return null; }
  if (!r.ok) {
    $("#tpl-summary").textContent = "✖ " + r.errors[0];
    if (!auto) alert(`${def.title} has ${r.errors.length} problem(s):\n\n• ` + r.errors.slice(0, 10).join("\n• "));
    return null;
  }
  if (r.conflicts.length) {
    if (auto) {
      $("#tpl-summary").textContent = "⚠ forms were edited by hand: " + r.conflicts.join(", ") + " - click 'Apply / re-render' to overwrite them";
      return null;
    }
    if (!confirm(`Applying the ${def.title} template replaces the values you entered for:\n\n• ${r.conflicts.join("\n• ")}\n\nOverwrite them?`)) {
      setStatus(`${def.title} template not applied (kept your edits).`);
      return null;
    }
  }
  const keep = auto && r.values[S.kind] ? S.kind : null;
  for (const k of r.stale) delete S.cache[k];
  for (const [k, v] of Object.entries(r.values)) S.cache[k] = clone(v);
  S.tpl = {title: r.title, name: r.name, params: r.params, secrets: r.secrets, values: clone(r.values), applied: true};
  S.form = null;      // rebuild from the cache
  await selectKind(keep || r.primary_kind, {force: true});
  $("#tpl-summary").textContent = r.summary;
  setStatus(r.status);
  persist();
  return r;
}
async function downloadTemplateSet() {
  if (!S.tplDef) return alert("Pick a template first (Template: dropdown).");
  if (S.form) S.cache[S.kind] = currentValues();
  const current = Object.fromEntries(S.meta.modes[0].kinds.map((k) => [k, valuesFor(k)]));
  const same = S.tpl && S.tpl.title === S.tplDef.title;
  const d = await downloadFrom("/api/download/template",
    {title: S.tplDef.title, params: templateParams(), secrets: same ? S.tpl.secrets : null, current}, "template.zip");
  if (d) setStatus(`Downloaded ${d.name} - unzip, then: kubectl apply -f ${d.name.replace(/\.zip$/, "")}/all-in-one.yaml`);
}

// ------------------------------------------------------------------ misc actions
async function resetKind() {
  delete S.cache[S.kind];
  if (S.collapsed[S.kind]) delete S.collapsed[S.kind];
  S.form = null;
  await selectKind(S.kind, {force: true});
  setStatus(`${S.kind} reset to defaults.`);
}
function forgetAll() {
  if (!confirm("Remove every value, the bundle and the template state this page stored in your browser?")) return;
  localStorage.removeItem(STORE_KEY);
  Object.assign(S, {cache: {}, collapsed: {}, bundle: [], tpl: null, tplDef: null, lastKind: {}, form: null});
  $("#template").value = S.meta.templates.none; $("#tpl-panel").hidden = true;
  selectKind(S.kind, {force: true}).then(() => { localStorage.removeItem(STORE_KEY); setStatus("Saved browser values removed."); });
}
async function validate() {
  const r = await refresh(); if (!r) return;
  showIssues(r, true);
  if (r.ok) alert((isBundleMode() ? "Manifest is valid." : `${S.kind} config is valid.`) + (r.warnings.length ? "\n\nWarnings:\n• " + r.warnings.join("\n• ") : ""));
  else alert("Please fix:\n\n• " + r.errors.slice(0, 15).join("\n• "));
}

// ------------------------------------------------------------------ boot
async function boot() {
  Tip.init();
  const saved = load();
  S.meta = await api("/api/meta");
  const modeSel = $("#mode");
  for (const m of S.meta.modes) modeSel.append(el("option", {value: m.mode, text: m.mode}));
  fillTemplateSelector();
  Tip.attach($("#info-mode"), S.meta.top_help.mode);
  Tip.attach($("#info-kind"), () => S.meta.top_help.kind + (S.kind ? "\nNow editing: " + S.kind : ""));
  Tip.attach($("#info-template"), () => S.meta.top_help.template + (S.tplDef ? `\nSelected: ${S.tplDef.title} - ${(S.tplDef.description || "").split("\n")[0]}` : ""));
  modeSel.addEventListener("change", () => selectMode(modeSel.value));
  $("#kind").addEventListener("change", (e) => selectKind(e.target.value));
  $("#template").addEventListener("change", (e) => selectTemplate(e.target.value));
  $("#live").checked = S.live;
  $("#live").addEventListener("change", (e) => { S.live = e.target.checked; persist(); if (S.live) refresh(); });
  const on = (id, fn) => $(id).addEventListener("click", fn);
  on("#btn-refresh", refresh); on("#btn-validate", validate); on("#btn-copy", copyYaml); on("#btn-download", download);
  on("#btn-all", downloadAllEdited); on("#btn-bundle-add", addToBundle); on("#btn-bundle-save", saveBundle);
  on("#btn-bundle-clear", clearBundle); on("#btn-reset", resetKind); on("#btn-forget", forgetAll);
  on("#btn-tpl-apply", () => applyTemplate(false)); on("#btn-tpl-download", downloadTemplateSet);
  document.addEventListener("keydown", (e) => {
    const mod = e.ctrlKey || e.metaKey;
    if ($("#rowdlg").open) return;
    if (mod && e.key.toLowerCase() === "k") { e.preventDefault(); $("#kind").focus(); }
    else if (mod && e.key.toLowerCase() === "s") { e.preventDefault(); download(); }
    else if (mod && e.key === "Enter") { e.preventDefault(); refresh(); }
  });
  window.addEventListener("beforeunload", persistNow);
  const startMode = saved.mode && modeInfo(saved.mode) ? saved.mode : "Manifests";
  S.mode = startMode;
  fillKindSelector();
  let kind = saved.kind && modeInfo(startMode).kinds.includes(saved.kind) ? saved.kind : (S.lastKind[startMode] || modeInfo(startMode).kinds[0]);
  if (S.tpl && S.tpl.title) {   // restore the template panel (without re-applying)
    try { await selectTemplate(S.tpl.title, false); } catch (e) { S.tpl = null; }
  }
  await selectKind(kind);
}
window.K8S = {S, selectKind, selectMode, selectTemplate, applyTemplate, refresh, RowDialog, Tip, currentValues};
boot().catch((e) => { setStatus("Failed to start: " + e.message); console.error(e); });
