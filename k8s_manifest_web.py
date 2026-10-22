#!/usr/bin/env python3
"""
k8s_manifest_web.py - web (Flask) version of the Kubernetes manifest / cluster config generator.

    python3 k8s_manifest_web.py                 # http://127.0.0.1:8080 (opens nothing, prints the URL)
    python3 k8s_manifest_web.py --open          # ... and opens it in your browser
    python3 k8s_manifest_web.py --port 9000
    python3 k8s_manifest_web.py --host 0.0.0.0  # reachable from other machines (no auth! see README)
    python3 k8s_manifest_web.py --check         # dependency check only
    python3 k8s_manifest_web.py --no-install    # don't auto pip-install Flask / PyYAML

First run pip-installs PyYAML and Flask if missing
(inside a venv; otherwise --user, then --break-system-packages on PEP 668 systems; see bootstrap.py). A venv is cleanest.
"""
import argparse
import ipaddress
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bootstrap  # noqa: E402


def _is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def main(argv=None):
    ap = argparse.ArgumentParser(description="Kubernetes manifest generator - web UI")
    ap.add_argument("--host", default="127.0.0.1", help="interface to bind (default 127.0.0.1 = this machine only)")
    ap.add_argument("--port", type=int, default=8080, help="port (default 8080)")
    ap.add_argument("--open", action="store_true", help="open the page in the default browser")
    ap.add_argument("--debug", action="store_true", help="Flask debug mode (auto-reload, debugger) - local use only")
    ap.add_argument("--check", action="store_true", help="check dependencies and exit")
    ap.add_argument("--no-install", action="store_true", help="do not auto-install Flask / PyYAML")
    args = ap.parse_args(argv)

    if sys.version_info < (3, 8):
        print("Python 3.8+ is required.", file=sys.stderr)
        return 1
    yaml = bootstrap.ensure_pyyaml(auto_install=not args.no_install)
    flask = bootstrap.ensure_flask(auto_install=not args.no_install)
    if args.check:
        from importlib.metadata import version
        print(bootstrap.summary())
        print(f"PyYAML: {yaml.__version__}   Flask: {version('flask')}")
        return 0
    del flask

    from web.app import LOOPBACK_HOSTS, create_app
    if _is_loopback(args.host):
        allowed = set(LOOPBACK_HOSTS)
    else:
        env = os.environ.get("K8SGUI_ALLOWED_HOSTS", "")
        allowed = {h.strip() for h in env.split(",") if h.strip()} or {"*"}
        print(f"WARNING: listening on {args.host}: anyone who can reach this port can use the generator "
              "(there is no login). Prefer an SSH tunnel: ssh -L 8080:127.0.0.1:8080 host", file=sys.stderr)
    if args.debug and not _is_loopback(args.host):
        print("Refusing --debug on a non-loopback address (the debugger allows code execution).", file=sys.stderr)
        return 2
    app = create_app(allowed_hosts=allowed)
    shown = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{'[' + shown + ']' if ':' in shown else shown}:{args.port}/"
    print(f"Kubernetes Manifest Generator (web) on {url}  - Ctrl+C to stop", file=sys.stderr)
    if args.open:
        import threading
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=args.debug, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
