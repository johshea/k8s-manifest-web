"""
bootstrap.py - first-run dependency checks for the web UI (stdlib only).

* ensure_pyyaml()  -> imports PyYAML, pip-installing it on first run if needed.
* ensure_flask()   -> same for Flask.

Install strategy (PyYAML, then Flask; stops at the first that works):
  1. inside a virtualenv:      python -m pip install pyyaml
  2. otherwise:                python -m pip install --user pyyaml
  3. PEP 668 "externally-managed-environment" (Homebrew Python, Ubuntu 23.04+):
                               python -m pip install --user --break-system-packages pyyaml
     (installs only into ~/.local, never touches system site-packages;
      disable with K8S_GUI_NO_BREAK_SYSTEM=1)
  4. pip missing:              python -m ensurepip --user, then retry
  If everything fails, prints apt / brew / venv instructions and exits.
"""
from __future__ import annotations

import importlib
import os
import platform
import site
import subprocess
import sys

IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


def _in_venv() -> bool:
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix) or bool(os.environ.get("VIRTUAL_ENV"))


def _pip(args: list[str], spec: str = "pyyaml>=5.4") -> tuple[int, str]:
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *args, spec]
    print("  $ " + " ".join(cmd), file=sys.stderr)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as exc:
        return 1, str(exc)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def _refresh_paths():
    importlib.invalidate_caches()
    try:
        usp = site.getusersitepackages()
        if usp and os.path.isdir(usp) and usp not in sys.path:
            site.addsitedir(usp)
    except Exception:
        pass


def _manual_help(display: str = "Flask") -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    lines = [f"Could not install {display} automatically. Options:"]
    if IS_LINUX:
        lines.append("  * Ubuntu/Debian packages: sudo apt install -y python3-flask python3-yaml")
    lines += [
        "  * Virtualenv (recommended):",
        f"      cd {here}",
        "      python3 -m venv .venv && source .venv/bin/activate",
        "      pip install -r requirements.txt",
        "      python k8s_manifest_web.py",
        "    (Ubuntu needs `sudo apt install python3-venv` for venv.)",
    ]
    return "\n".join(lines)


def ensure_package(module: str, spec: str, display: str, auto_install: bool = True):
    """Import *module*, pip-installing *spec* first if needed (strategy in the module docstring)."""
    try:
        return importlib.import_module(module)
    except ImportError:
        pass
    if not auto_install or os.environ.get("K8S_GUI_NO_AUTO_INSTALL"):
        print(f"{display} is not installed.\n" + _manual_help(display), file=sys.stderr)
        raise SystemExit(2)

    print(f"{display} not found - installing it (first run only)...", file=sys.stderr)
    attempts: list[list[str]] = [[]] if _in_venv() else [["--user"]]
    tried_ensurepip = False
    i = 0
    last_out = ""
    while i < len(attempts):
        rc, out = _pip(attempts[i], spec)
        last_out = out
        if rc == 0:
            _refresh_paths()
            try:
                mod = importlib.import_module(module)
                print(f"{display} installed.", file=sys.stderr)
                return mod
            except ImportError:
                pass
        low = out.lower()
        if "externally-managed-environment" in low or "externally managed" in low:
            if os.environ.get("K8S_GUI_NO_BREAK_SYSTEM"):
                break
            nxt = ["--user", "--break-system-packages"]
            if nxt not in attempts:
                print("  Python is 'externally managed' (PEP 668); retrying a --user install with "
                      "--break-system-packages (only writes to ~/.local).", file=sys.stderr)
                attempts.append(nxt)
        elif "no module named pip" in low and not tried_ensurepip:
            tried_ensurepip = True
            print("  pip is missing; trying `python -m ensurepip --user`...", file=sys.stderr)
            subprocess.run([sys.executable, "-m", "ensurepip", "--user"], capture_output=True)
            attempts.append(attempts[i])
        elif "--user" in attempts[i] and ("user site-packages are not visible" in low
                                          or "can not perform a '--user' install" in low):
            attempts.append([])  # e.g. inside a venv we failed to detect
        i += 1
    print(last_out[-2000:], file=sys.stderr)
    print(_manual_help(display), file=sys.stderr)
    raise SystemExit(2)


def ensure_pyyaml(auto_install: bool = True):
    return ensure_package("yaml", "pyyaml>=5.4", "PyYAML", auto_install)


def ensure_flask(auto_install: bool = True):
    return ensure_package("flask", "flask>=2.2", "Flask", auto_install)


def summary() -> str:
    return f"Python {platform.python_version()} ({sys.executable}) on {platform.system()} {platform.machine()}"


if __name__ == "__main__":
    print(summary())
    y = ensure_pyyaml()
    f = ensure_flask()
    from importlib.metadata import version
    print(f"PyYAML: {y.__version__}; Flask: {version('flask')}")
