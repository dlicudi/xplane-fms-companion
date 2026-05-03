#!/usr/bin/env python3
"""Deploy FMS Companion to X-Plane PythonPlugins folder.

Usage:
    python scripts/deploy.py
    python scripts/deploy.py --xplane-path /path/to/X-Plane/Resources/plugins/PythonPlugins
"""

import shutil
import sys
import argparse
import socket
from pathlib import Path

DEFAULT_XPLANE_PATH = Path.home() / "X-Plane 12/Resources/plugins/PythonPlugins"
REPO_ROOT = Path(__file__).parent.parent
XPLANE_UDP_HOST = "127.0.0.1"
XPLANE_UDP_PORT = 49000

PLUGIN_FILES   = ["PI_FMSCompanion.py"]
PLUGIN_FOLDERS = ["fmscompanion"]


def reload_python_plugins() -> bool:
    command = "XPPython3/reloadScripts"
    payload = b"CMND\0" + command.encode("utf-8") + b"\0"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(payload, (XPLANE_UDP_HOST, XPLANE_UDP_PORT))
        print(f"Sent reload command: {command}")
        return True
    except OSError as exc:
        print(f"Could not send reload command: {exc}", file=sys.stderr)
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--xplane-path", type=Path, default=DEFAULT_XPLANE_PATH,
                        help=f"PythonPlugins directory (default: {DEFAULT_XPLANE_PATH})")
    parser.add_argument("--no-reload", action="store_true",
                        help="Deploy files without sending XPPython3/reloadScripts")
    args = parser.parse_args()

    target_dir = args.xplane_path
    if not target_dir.exists():
        print(f"Error: target directory not found: {target_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"Deploying FMS Companion → {target_dir}")

    for item in PLUGIN_FILES + PLUGIN_FOLDERS:
        dest = target_dir / item
        if dest.exists() or dest.is_symlink():
            print(f"  Removing existing {item} ...")
            if dest.is_dir() and not dest.is_symlink():
                shutil.rmtree(dest)
            else:
                dest.unlink()

    for folder in PLUGIN_FOLDERS:
        src  = REPO_ROOT / folder
        dest = target_dir / folder
        print(f"  Copying folder {folder} ...")
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    for file in PLUGIN_FILES:
        src  = REPO_ROOT / file
        dest = target_dir / file
        print(f"  Copying file {file} ...")
        shutil.copy2(src, dest)

    print("\nDeployment complete.")
    if not args.no_reload:
        reload_python_plugins()
    print("If reload did not take effect, use: Plugins → XPPython3 → Reload scripts")


if __name__ == "__main__":
    main()
