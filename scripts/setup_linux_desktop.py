#!/usr/bin/env python3
"""Expose the system GTK/WebKit bindings to a virtualenv for the Linux desktop UI.

pywebview renders the desktop window through the SYSTEM PyGObject (``gi``) plus
WebKit2GTK. Neither is installable from PyPI, and a virtualenv created without
``--system-site-packages`` (the default, and what README's source setup produces)
cannot import them at all — so ``python launcher.py`` dies inside pywebview with a
traceback that names none of this.

This script adds one ``.pth`` file pointing at the system ``dist-packages`` so the
venv interpreter can import ``gi``. The venv keeps priority for everything else:
``.pth`` entries are appended AFTER the venv's own site-packages, and the ``zz-``
name prefix keeps this file last in the alphabetical ``.pth`` processing order.

Packaged releases do not need any of this — it is purely for running from source.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import subprocess
import sys


PTH_NAME = "zz-system-gi.pth"
APT_PACKAGES = ("python3-gi", "python3-gi-cairo", "gir1.2-webkit2-4.1")
# Probe run inside a target interpreter: reports the GTK stack it can actually
# reach. Printing JSON keeps the parsing here honest about partial failures.
_PROBE = """
import json, sys
out = {"version": "%d.%d" % sys.version_info[:2], "gi": None, "webkit": None, "error": ""}
try:
    import gi
    out["gi"] = getattr(gi, "__file__", "") or ""
    gi.require_version("Gtk", "3.0")
    gi.require_version("WebKit2", "4.1")
    from gi.repository import Gtk, WebKit2
    out["webkit"] = True
except Exception as exc:
    out["error"] = "%s: %s" % (type(exc).__name__, exc)
print(json.dumps(out))
"""


def _probe(python: str) -> dict:
    """Run the GTK probe inside `python`, returning its report."""
    try:
        result = subprocess.run(
            [python, "-c", _PROBE], check=False, capture_output=True, text=True, timeout=60
        )
    except Exception as exc:
        return {"version": "", "gi": None, "webkit": None, "error": f"could not run {python}: {exc}"}
    line = (result.stdout or "").strip().splitlines()
    if not line:
        return {
            "version": "",
            "gi": None,
            "webkit": None,
            "error": (result.stderr or "").strip() or "probe produced no output",
        }
    try:
        return json.loads(line[-1])
    except Exception as exc:
        return {"version": "", "gi": None, "webkit": None, "error": f"unparsable probe output: {exc}"}


def _system_python() -> str:
    """The interpreter that owns the distro's dist-packages."""
    base = pathlib.Path(sys.base_prefix) / "bin" / f"python{sys.version_info.major}.{sys.version_info.minor}"
    if base.is_file():
        return str(base)
    for name in (f"python{sys.version_info.major}.{sys.version_info.minor}", "python3"):
        found = shutil.which(name)
        if found:
            return found
    return sys.executable


def _venv_site_packages(venv: pathlib.Path) -> pathlib.Path | None:
    matches = sorted(venv.glob("lib/python*/site-packages"))
    return matches[0] if matches else None


def _venv_python(venv: pathlib.Path) -> pathlib.Path | None:
    candidate = venv / "bin" / "python"
    return candidate if candidate.is_file() else None


def _resolve_venv(explicit: str) -> pathlib.Path:
    if explicit:
        return pathlib.Path(explicit).expanduser().resolve()
    if sys.prefix != sys.base_prefix:
        return pathlib.Path(sys.prefix).resolve()
    raise SystemExit(
        "Not running inside a virtualenv and --venv was not given.\n"
        "Either activate the venv first (source .venv/bin/activate) or pass --venv .venv"
    )


def _dist_packages_of(gi_file: str) -> pathlib.Path | None:
    """The directory to put on sys.path, derived from the located `gi` package."""
    if not gi_file:
        return None
    # <dist-packages>/gi/__init__.py -> <dist-packages>
    return pathlib.Path(gi_file).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--venv", default="", help="Target virtualenv (default: the active one).")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Only report whether the venv can already reach GTK/WebKit; write nothing.",
    )
    args = parser.parse_args(argv)

    venv = _resolve_venv(args.venv)
    venv_python = _venv_python(venv)
    site_packages = _venv_site_packages(venv)
    if venv_python is None or site_packages is None:
        raise SystemExit(f"{venv} does not look like a virtualenv (no bin/python or lib/python*/site-packages).")

    venv_report = _probe(str(venv_python))
    if venv_report.get("webkit"):
        print(f"OK: {venv_python} can already import gi + WebKit2 4.1.")
        print(f"    backend check: {_backend_line(str(venv_python))}")
        return 0
    if args.check:
        print(f"MISSING: {venv_python} cannot reach GTK/WebKit ({venv_report.get('error') or 'unknown'}).")
        print("Run this script without --check to wire it up.")
        return 1

    system_python = _system_python()
    system_report = _probe(system_python)
    if not system_report.get("webkit"):
        print(f"The system interpreter {system_python} cannot import gi + WebKit2 4.1 either:")
        print(f"  {system_report.get('error') or 'unknown error'}")
        print("\nInstall the distro packages first:")
        print(f"  sudo apt install {' '.join(APT_PACKAGES)}")
        print("(On non-Debian hosts install the equivalent PyGObject + WebKit2GTK 4.1 packages.)")
        return 1

    # Hard ABI gate: `gi` ships a compiled `_gi` extension built for one CPython
    # minor version. Mixing 3.11 dist-packages into a 3.12 venv fails at import
    # with a far more confusing error than this message.
    venv_version = venv_report.get("version") or ""
    system_version = system_report.get("version") or ""
    if venv_version != system_version:
        print(f"Python version mismatch: venv is {venv_version or '?'}, system is {system_version or '?'}.")
        print("The system gi ships a compiled extension that only loads on its own CPython minor version.")
        print(f"Recreate the venv with the system interpreter:\n  {system_python} -m venv {venv}")
        return 1

    dist_packages = _dist_packages_of(str(system_report.get("gi") or ""))
    if dist_packages is None or not dist_packages.is_dir():
        print(f"Could not locate the system dist-packages directory from gi at {system_report.get('gi')!r}.")
        return 1

    pth_path = site_packages / PTH_NAME
    payload = f"{dist_packages}\n"
    if pth_path.is_file() and pth_path.read_text(encoding="utf-8") == payload:
        print(f"Unchanged: {pth_path} already points at {dist_packages}.")
    else:
        pth_path.write_text(payload, encoding="utf-8")
        print(f"Wrote {pth_path} -> {dist_packages}")

    verify = _probe(str(venv_python))
    if not verify.get("webkit"):
        print(f"FAILED: {venv_python} still cannot import gi + WebKit2 ({verify.get('error') or 'unknown'}).")
        return 1

    print(f"OK: {venv_python} can now import gi + WebKit2 4.1.")
    print(f"    backend check: {_backend_line(str(venv_python))}")
    print("\nStart the desktop app with:\n  python launcher.py")
    return 0


def _backend_line(python: str) -> str:
    """Which renderer pywebview resolves to, or why it cannot be determined."""
    code = (
        "import importlib, sys\n"
        "try:\n"
        "    importlib.import_module('webview.guilib')\n"
        # webview/__init__.py keeps a module-level `guilib = None` that shadows the
        # submodule attribute, so go through sys.modules rather than the attribute.
        "    g = sys.modules['webview.guilib'].initialize()\n"
        "    print(getattr(g, 'renderer', None) or g.__name__)\n"
        "except Exception as exc:\n"
        "    print('unavailable (%s: %s)' % (type(exc).__name__, exc))\n"
    )
    try:
        result = subprocess.run([python, "-c", code], check=False, capture_output=True, text=True, timeout=60)
    except Exception as exc:
        return f"unavailable ({exc})"
    out = (result.stdout or "").strip()
    return out or "unavailable (pywebview not installed?)"


if __name__ == "__main__":
    raise SystemExit(main())
