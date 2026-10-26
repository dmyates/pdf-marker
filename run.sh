#!/usr/bin/env bash
set -euo pipefail
APP_DIR="$(cd -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")" && pwd)"
PYTHON="${PDF_MARKER_PYTHON:-/usr/bin/python3}"
"$APP_DIR/check-gtk.sh" "$PYTHON"
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
    "$PYTHON" -m venv --system-site-packages "$APP_DIR/.venv"
    "$APP_DIR/.venv/bin/python" -m pip install -e "$APP_DIR"
elif ! "$APP_DIR/.venv/bin/python" -c "import gi, cairo" 2>/dev/null; then
    "$PYTHON" -m venv --system-site-packages "$APP_DIR/.venv"
fi
cd "$APP_DIR"
exec "$APP_DIR/.venv/bin/python" -m pdfmarker "$@"
