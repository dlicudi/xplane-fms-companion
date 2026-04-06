#!/usr/bin/env python3
"""Deploy Cockpitdecks FMS plugin to X-Plane PythonPlugins folder."""

import shutil
import sys
from pathlib import Path

# --- CONFIGURATION ---
DEFAULT_XPLANE_PATH = Path.home() / "X-Plane 12/Resources/plugins/PythonPlugins"
PLUGIN_FILES = ["PI_CockpitdecksFMS.py"]
PLUGIN_FOLDERS = ["cockpitdecksfms"]
# ---------------------


def deploy():
    repo_root = Path(__file__).parent.parent / "CockpitdecksFMS"
    target_dir = Path(DEFAULT_XPLANE_PATH)

    if not target_dir.exists():
        print(f"Error: Target directory not found: {target_dir}")
        sys.exit(1)

    print(f"Deploying Cockpitdecks FMS to: {target_dir}")

    for item in PLUGIN_FILES + PLUGIN_FOLDERS:
        dest = target_dir / item
        if dest.exists() or dest.is_symlink():
            print(f"  Removing existing {item}...")
            if dest.is_dir() and not dest.is_symlink():
                shutil.rmtree(dest)
            else:
                dest.unlink()

    for folder in PLUGIN_FOLDERS:
        src = repo_root / folder
        dest = target_dir / folder
        print(f"  Copying folder {folder}...")
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    for file in PLUGIN_FILES:
        src = repo_root / file
        dest = target_dir / file
        print(f"  Copying file {file}...")
        shutil.copy2(src, dest)

    print("\nDeployment complete. Reload Python plugins in X-Plane (Plugins → XPPython3 → Reload).")


if __name__ == "__main__":
    deploy()
