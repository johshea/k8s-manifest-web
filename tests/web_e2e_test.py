#!/usr/bin/env python3
"""
Browser end-to-end test of the web UI (Playwright + headless Chromium/Chrome).

  pip install playwright && python -m playwright install chromium     # or use an installed Chrome:
  CHROME=/usr/bin/google-chrome python3 tests/web_e2e_test.py [--screenshots DIR]

Starts the Flask app on a random 127.0.0.1 port in a thread (stopped at the end), then drives the page:
mode switch (Manifests / Cluster config / Networking & Storage with cross-check warnings and session
StorageClass names in the PVC dropdown), type switch with value caching (also across a reload = localStorage), template apply +
parameter re-render, table row add/validate/duplicate/reorder, ⓘ tooltip hover/focus/click, downloads
(single file, multi-file zip, template set, bundle), clipboard copy, and no console errors.
Skips (exit 0) when Flask or Playwright / a browser is unavailable.
"""
import io
import os
import shutil
import sys
import threading
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

try:
    from playwright.sync_api import sync_playwright
    from werkzeug.serving import make_server
    from web.app import create_app
except ImportError as exc:
    print(f"== web e2e skipped: {exc} (pip install -r requirements.txt playwright) ==")
    print("\nWEB E2E RESULT: SKIPPED")
    sys.exit(0)

import manifest_gen as gen  # noqa: E402

fails = 0


def check(cond, msg):
    global fails
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails += 1


def find_browser():
    for c in (os.environ.get("CHROME"), shutil.which("google-chrome"), shutil.which("chromium"),
              shutil.which("chromium-browser"), "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
        if c and os.path.exists(c):
            return c
    return None      # Playwright's own bundled Chromium, if installed


def main():
    shots = None
    if "--screenshots" in sys.argv:
        shots = sys.argv[sys.argv.index("--screenshots") + 1]
        os.makedirs(shots, exist_ok=True)
    srv = make_server("127.0.0.1", 0, create_app(), threaded=True)
    port = srv.server_port
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    url = f"http://127.0.0.1:{port}/"
    print(f"== server on {url} ==")
    try:
        with sync_playwright() as pw:
            exe = find_browser()
            try:
                browser = pw.chromium.launch(executable_path=exe, args=["--no-sandbox"]) if exe else pw.chromium.launch()
            except Exception as exc:
                print(f"== web e2e skipped: no browser ({str(exc).splitlines()[0]}) ==\n\nWEB E2E RESULT: SKIPPED")
                return 0
            print(f"== browser: {exe or 'playwright chromium'} {browser.version} ==")
            ctx = browser.new_context(viewport={"width": 1400, "height": 900}, accept_downloads=True)
            ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin=url.rstrip("/"))
            page = ctx.new_page()
            errors, dialogs = [], []
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))
            run(page, url, errors, dialogs, shots)
            check(not errors, f"no JavaScript errors in the console {errors[:3]}")
            browser.close()
    finally:
        srv.shutdown()
        th.join(5)
    check(not th.is_alive(), "test server stopped")
    print(f"\nWEB E2E RESULT: {'PASS' if not fails else f'{fails} FAILURE(S)'}")
    return 1 if fails else 0


def ready(page):
    page.wait_for_selector('body[data-ready="1"]', timeout=15000)
    page.wait_for_function("() => !window.K8S.S.timers.refresh || true")


def wait_preview(page, text, timeout=8000):
    page.wait_for_function("t => document.querySelector('#preview').textContent.includes(t)", arg=text, timeout=timeout)


def field(page, key):
    return page.locator(f'#form [data-key="{key}"]').first


def run(page, url, errors, dialogs, shots):
    page.goto(url)
    ready(page)
    S = lambda expr: page.evaluate(f"() => window.K8S.S.{expr}")  # noqa: E731
    check(S("kind") == "Deployment" and page.input_value("#mode") == "Manifests", "starts on Manifests / Deployment")
    n_fields = sum(len(fs) for _, fs in gen.SCHEMA["Deployment"])
    n_icons = page.locator("#form button.info").count()
    n_secs = len(gen.SCHEMA["Deployment"])
    check(n_icons == n_fields + n_secs, f"ⓘ icon per field + section header ({n_icons} = {n_fields} + {n_secs})")
    check(page.locator("#form details.section").count() == n_secs, f"{n_secs} collapsible sections")
    check(not page.locator('details[data-section="Probes"]').get_attribute("open") is not None,
          "Probes section starts collapsed")
    # live preview + cache across type switches
    field(page, "replicas").fill("5")
    wait_preview(page, "replicas: 5")
    check(True, "live preview follows edits (replicas: 5)")
    field(page, "name").fill("shop-web")
    wait_preview(page, "name: shop-web")
    check(field(page, "labels").input_value() == "app=shop-web", "app=<name> label follows the name")
    page.select_option("#kind", "Service")
    wait_preview(page, "kind: Service")
    page.select_option('#form [data-key="service_type"]', "ClusterIP")
    check(field(page, "node_port").is_disabled(), "Service: nodePort disabled for ClusterIP")
    page.select_option("#kind", "Deployment")
    wait_preview(page, "replicas: 5")
    check(field(page, "replicas").input_value() == "5", "Deployment values kept after switching type")
    page.wait_for_function("() => document.querySelector('#edited').textContent.includes('Deployment')")
    check("Deployment" in page.inner_text("#edited"), "edited label lists Deployment")
    page.reload()
    ready(page)
    check(field(page, "replicas").input_value() == "5" and S("kind") == "Deployment",
          "values survive a page reload (localStorage)")
    if shots:
        page.screenshot(path=os.path.join(shots, "web-manifests.png"))

    # tooltips
    icon = page.locator("#form .lbl", has=page.locator('label:text-is("imagePullPolicy")')).locator("button.info")
    icon.scroll_into_view_if_needed()
    icon.hover()
    page.wait_for_timeout(150)
    check(page.locator("#tip").is_hidden(), "tooltip waits for the hover delay")
    page.wait_for_timeout(500)
    tip = page.inner_text("#tip")
    check(page.locator("#tip").is_visible() and "IfNotPresent" in tip and "Always" in tip and "imagePullPolicy" in tip,
          "hovering the imagePullPolicy ⓘ shows its field_docs tooltip")
    box = page.locator("#tip").bounding_box()
    check(box and box["x"] >= 0 and box["y"] >= 0 and box["x"] + box["width"] <= 1400 and box["y"] + box["height"] <= 900
          and box["width"] <= 400, "tooltip on screen, max 380px wide")
    if shots:
        page.screenshot(path=os.path.join(shots, "web-tooltip.png"))
    page.mouse.move(5, 890)
    page.wait_for_timeout(100)
    check(page.locator("#tip").is_hidden(), "leaving the icon hides the tooltip")
    icon.focus()
    check(page.locator("#tip").is_visible() and icon.get_attribute("aria-describedby") == "tip",
          "keyboard focus shows the tooltip (aria-describedby)")
    page.keyboard.press("Escape")
    check(page.locator("#tip").is_hidden(), "Escape hides it")
    icon.click()
    check(page.locator("#tip").is_visible(), "click / tap shows it")
    icon.click()
    check(page.locator("#tip").is_hidden(), "second tap hides it")
    page.hover("#info-mode")
    page.wait_for_timeout(550)
    check("Cluster config" in page.inner_text("#tip"), "Mode selector ⓘ")

    # table rows
    page.locator('details[data-section="Init & sidecar containers"] > summary').click()
    tbl = page.locator('.tablewrap', has=page.locator('table[data-table="extra_containers"]'))
    tbl.locator("button.t-add").click()
    page.wait_for_selector("#rowdlg[open]")
    check(page.locator("#rowdlg button.info").count() == len(gen.TABLE_FIELDS["extra_containers"]),
          "row dialog has an ⓘ per column")
    page.fill('#rowdlg [data-key="name"]', "log-shipper")
    page.fill('#rowdlg [data-key="image"]', "")
    page.click("#rowdlg-ok")
    page.wait_for_function("() => document.querySelector('#rowdlg-err').textContent.length > 0")
    check(page.locator("#rowdlg[open]").count() == 1 and "✖" in page.inner_text("#rowdlg-err"),
          "invalid row is rejected in the dialog (server check_row)")
    page.fill('#rowdlg [data-key="image"]', "fluent/fluent-bit:3.2")
    page.click("#rowdlg-ok")
    page.wait_for_selector("#rowdlg", state="hidden")
    wait_preview(page, "log-shipper")
    check(tbl.locator("tbody tr").count() == 1 and "log-shipper" in tbl.inner_text(), "row added to the table + preview")
    tbl.locator("tbody tr").first.click()
    tbl.locator("button.t-dup").click()
    wait_preview(page, "log-shipper-copy")
    tbl.locator("button.t-up").click()
    page.wait_for_timeout(400)
    names = [r.inner_text() for r in tbl.locator("tbody tr").all()]
    check(tbl.locator("tbody tr").count() == 2 and "log-shipper-copy" in names[0], f"duplicate + move up ({names})")
    tbl.locator("button.t-remove").click()
    page.wait_for_timeout(400)
    check(tbl.locator("tbody tr").count() == 1, "remove row")

    # downloads
    with page.expect_download() as dl:
        page.click("#btn-download")
    d = dl.value
    text = open(d.path(), encoding="utf-8").read()
    check(d.suggested_filename == "deployment-shop-web.yaml" and "log-shipper" in text and "replicas: 5" in text,
          f"Download -> {d.suggested_filename}")
    page.click("#btn-copy")
    page.wait_for_function("() => document.querySelector('#status').textContent.startsWith('Copied')")
    clip = page.evaluate("() => navigator.clipboard.readText()")
    check(clip == text, "Copy puts the same YAML on the clipboard")
    page.click("#btn-bundle-add")
    page.select_option("#kind", "Service")
    wait_preview(page, "kind: Service")
    page.click("#btn-bundle-add")
    page.wait_for_function("() => document.querySelector('#preview-title').textContent.includes('bundle: 2')")
    with page.expect_download() as dl:
        page.click("#btn-bundle-save")
    b = open(dl.value.path(), encoding="utf-8").read()
    check(dl.value.suggested_filename == "bundle.yaml" and b.count("---\n") == 2, "bundle.yaml with 2 documents")
    with page.expect_download() as dl:
        page.click("#btn-all")
    z = zipfile.ZipFile(dl.value.path())
    check({"deployment-shop-web.yaml", "all-in-one.yaml"} <= set(z.namelist()), f"all edited zip: {z.namelist()}")

    # templates
    page.select_option("#template", "PostgreSQL")
    page.wait_for_function("() => window.K8S.S.kind === 'StatefulSet'")
    wait_preview(page, "kind: StatefulSet")
    check("headless" in page.inner_text("#tpl-summary"), f"PostgreSQL applied: {page.inner_text('#tpl-summary')[:80]}")
    check(page.locator("#tpl-fields button.info").count() >= 4, "template parameters have ⓘ icons")
    page.fill('#tpl-fields [data-key="storage_size"]', "20Gi")
    wait_preview(page, "storage: 20Gi")
    check(True, "parameter change re-renders the set (storage: 20Gi)")
    pw1 = page.evaluate("() => window.K8S.S.tpl.secrets.password")
    page.fill('#tpl-fields [data-key="name"]', "orders-db")
    wait_preview(page, "orders-db")
    check(page.evaluate("() => window.K8S.S.tpl.secrets.password") == pw1, "generated password stays stable on re-render")
    if shots:
        page.screenshot(path=os.path.join(shots, "web-template.png"))
    with page.expect_download() as dl:
        page.click("#btn-tpl-download")
    z = zipfile.ZipFile(dl.value.path())
    names = z.namelist()
    sec = [i for i in z.infolist() if i.filename.startswith("orders-db/secret-")]
    check(dl.value.suggested_filename == "orders-db.zip" and "orders-db/all-in-one.yaml" in names and sec and
          (sec[0].external_attr >> 16) & 0o777 == 0o600, f"template set zip ({len(names)} files, Secret 0600)")
    page.select_option("#kind", "Deployment")
    page.select_option("#template", "nginx")     # Deployment was edited by hand -> confirm() (auto-accepted)
    page.wait_for_function("() => window.K8S.S.tpl && window.K8S.S.tpl.title === 'nginx' && window.K8S.S.kind === 'Deployment'")
    check(any("Overwrite" in m for m in dialogs), "applying over hand-edited forms asks first")
    page.select_option("#template", "None / blank")
    page.wait_for_function("() => document.querySelector('#tpl-panel').hidden")

    # cluster config mode
    page.select_option("#mode", "Cluster config")
    page.wait_for_function("() => window.K8S.S.mode === 'Cluster config'")
    ready(page)
    check(page.locator("#kind optgroup").count() >= 3, "grouped platform dropdown (optgroups)")
    check(page.locator("#tpl-row").is_hidden() and page.locator("#btn-bundle-add").is_disabled(),
          "template bar hidden + bundle disabled in Cluster config mode")
    page.select_option("#kind", "kubeadm")
    wait_preview(page, "kubeadm.k8s.io")
    if shots:
        page.locator('details[data-section="ClusterConfiguration"]').scroll_into_view_if_needed()
        page.screenshot(path=os.path.join(shots, "web-cluster-config.png"))
    page.select_option("#kind", "minikube")
    page.wait_for_function("() => document.querySelector('#preview-title').textContent.includes('start-minikube.sh')")
    with page.expect_download() as dl:
        page.click("#btn-download")
    z = zipfile.ZipFile(dl.value.path())
    modes = {i.filename: (i.external_attr >> 16) & 0o777 for i in z.infolist()}
    check(dl.value.suggested_filename == "minikube.zip" and modes.get("start-minikube.sh") == 0o755,
          f"multi-file type downloads as zip with script 0755 ({modes})")
    # Networking & Storage mode
    page.select_option("#kind", "kind")
    wait_preview(page, "kind.x-k8s.io")
    field(page, "pod_subnet").fill("10.244.0.0/16")
    wait_preview(page, "10.244.0.0/16")
    page.select_option("#mode", "Networking & Storage")
    page.wait_for_function("() => window.K8S.S.mode === 'Networking & Storage'")
    ready(page)
    check(page.locator("#kind optgroup").count() == 4, "grouped add-on dropdown: CNI + 3 CSI groups")
    check(S("kind") == "Calico", "N&S mode opens Calico")
    field(page, "cal_mtu").fill("1440")
    wait_preview(page, "mtu: 1440")
    page.wait_for_function("() => document.querySelector('#issues').textContent.includes('Cross-check')")
    iss = page.inner_text("#issues")
    check("192.168.0.0/16" in iss and "10.244.0.0/16" in iss and "kindnet" in iss,
          "Calico form shows the cross-check against the edited kind form")
    check("custom-resources.yaml" in page.inner_text("#preview-title"), "Calico preview lists its files")
    if shots:
        page.screenshot(path=os.path.join(shots, "web-addons-calico.png"))
    with page.expect_download() as dl:
        page.click("#btn-download")
    z = zipfile.ZipFile(dl.value.path())
    modes = {i.filename: (i.external_attr >> 16) & 0o777 for i in z.infolist()}
    check(dl.value.suggested_filename == "calico.zip" and modes.get("install.sh") == 0o755
          and "custom-resources.yaml" in modes, f"Calico downloads as zip, install.sh 0755 ({modes})")
    page.select_option("#kind", "NFS CSI")
    wait_preview(page, "nfs.csi.k8s.io")
    field(page, "name").fill("nfs-e2e")
    wait_preview(page, "name: nfs-e2e")
    if shots:
        page.select_option("#kind", "Nutanix")
        wait_preview(page, "csi.nutanix.com")
        page.click('details[data-section="Driver secret"] > summary')
        field(page, "nx_create_secret").check()
        field(page, "nx_prism").fill("10.0.0.50")
        field(page, "nx_password").fill("example-only")
        wait_preview(page, "secret.yaml")
        page.screenshot(path=os.path.join(shots, "web-addons-nutanix.png"))
        page.select_option("#kind", "Cilium")
        wait_preview(page, "kubeProxyReplacement")
        page.screenshot(path=os.path.join(shots, "web-addons-cilium.png"))
    page.select_option("#mode", "Manifests")
    page.wait_for_function("() => window.K8S.S.mode === 'Manifests'")
    ready(page)
    page.select_option("#kind", "PersistentVolumeClaim")
    wait_preview(page, "PersistentVolumeClaim")
    page.wait_for_function("() => [...document.querySelectorAll('[data-key=storage_class]')[0].list.options]"
                           ".some((o) => o.value === 'nfs-e2e')")
    check(True, "PVC storageClass dropdown offers the StorageClass created in this session (nfs-e2e)")
    page.select_option("#kind", "NetworkPolicy")
    wait_preview(page, "networking.k8s.io/v1")
    page.select_option('[data-key="np_default_deny"]', "all")
    wait_preview(page, "Egress")
    if shots:
        page.screenshot(path=os.path.join(shots, "web-networkpolicy.png"))
    page.select_option("#kind", "Deployment")
    wait_preview(page, "apps/v1")
    page.select_option("#mode", "Networking & Storage")
    page.wait_for_function("() => window.K8S.S.mode === 'Networking & Storage'")
    ready(page)

    page.select_option("#mode", "Manifests")
    page.wait_for_function("() => window.K8S.S.mode === 'Manifests'")
    ready(page)
    check(S("kind") == "Deployment", "back in Manifests mode on the last manifest kind")
    page.keyboard.press("Control+k")
    check(page.evaluate("() => document.activeElement.id") == "kind", "Ctrl+K focuses the type selector")
    page.click("#btn-reset")
    page.wait_for_function("() => document.querySelector('[data-key=replicas]').value === '2'")
    check(True, "Reset kind restores the defaults")


if __name__ == "__main__":
    sys.exit(main())
