#!/usr/bin/env bash
# Symlink (or copy) a plugin from this repo into X-Plane's PythonPlugins folder.
# Usage: ./scripts/link_plugin.sh <PLUGIN_FILE> [XPLANE_ROOT]
#   or:  XPLANE_ROOT=/path/to/X-Plane\ 12 ./scripts/link_plugin.sh <PLUGIN_FILE>
#   or:  ./scripts/link_plugin.sh --copy <PLUGIN_FILE> /path/to/X-Plane\ 12
#
# After running: Reload Python plugins in X-Plane (Plugins → XPPython3 → Reload)
# or restart X-Plane. Delete *.pyc in PythonPlugins if reload doesn't pick up changes.

set -e

USE_COPY=false
[[ "$1" == "--copy" ]] && { USE_COPY=true; shift; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLUGIN_NAME="${1}"
XPLANE_ROOT="${XPLANE_ROOT:-$2}"
PLUGINS_DIR="${XPLANE_ROOT}/Resources/plugins/PythonPlugins"

if [[ -z "$PLUGIN_NAME" ]]; then
  echo "Usage: $0 <PLUGIN_FILE> [XPLANE_ROOT]"
  echo "   or: XPLANE_ROOT=/path/to/X-Plane\\ 12 $0 <PLUGIN_FILE>"
  echo ""
  echo "Available plugins:"
  ls "${REPO_ROOT}"/PI_*.py 2>/dev/null | xargs -n1 basename
  exit 1
fi

PLUGIN_SRC="${REPO_ROOT}/${PLUGIN_NAME}"

if [[ -z "$XPLANE_ROOT" ]]; then
  echo "Usage: $0 <PLUGIN_FILE> <XPLANE_ROOT>"
  echo "   or: XPLANE_ROOT=/path/to/X-Plane\\ 12 $0 <PLUGIN_FILE>"
  echo ""
  echo "Example: $0 PI_CockpitdecksFMS.py \"$HOME/X-Plane 12\""
  exit 1
fi

if [[ ! -f "$PLUGIN_SRC" ]]; then
  echo "ERROR: Plugin source not found: $PLUGIN_SRC"
  exit 1
fi

if [[ ! -d "$PLUGINS_DIR" ]]; then
  echo "ERROR: X-Plane PythonPlugins dir not found: $PLUGINS_DIR"
  echo "       Is XPLANE_ROOT correct? $XPLANE_ROOT"
  exit 1
fi

PLUGIN_DST="${PLUGINS_DIR}/${PLUGIN_NAME}"

if [[ -L "$PLUGIN_DST" ]]; then
  rm "$PLUGIN_DST"
elif [[ -f "$PLUGIN_DST" ]]; then
  echo "Removing existing plugin (backup to ${PLUGIN_DST}.bak)"
  mv "$PLUGIN_DST" "${PLUGIN_DST}.bak"
fi

# Remove bytecode cache so X-Plane reloads the .py (avoids stale .pyc)
BASENAME="${PLUGIN_NAME%.py}"
rm -f "${PLUGINS_DIR}/${BASENAME}".cpython-*.pyc 2>/dev/null || true

if [[ "$USE_COPY" == true ]]; then
  cp "$PLUGIN_SRC" "$PLUGIN_DST"
  echo "Copied: $PLUGIN_SRC -> $PLUGIN_DST"
else
  ln -s "$PLUGIN_SRC" "$PLUGIN_DST"
  echo "Linked: $PLUGIN_DST -> $PLUGIN_SRC"
fi

RELEASE=$(grep -E '^\s*RELEASE\s*=' "$PLUGIN_SRC" | head -1 | sed 's/.*"\([^"]*\)".*/\1/')
echo "Plugin version: $RELEASE"
echo "Restart Python plugins (Plugins → XPPython3 → Reload plugins) or X-Plane to use the development plugin."
