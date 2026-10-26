#!/usr/bin/env bash
set -euo pipefail
APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PDF_MARKER_PYTHON:-/usr/bin/python3}"
"$APP_DIR/check-gtk.sh" "$PYTHON"
"$PYTHON" -m venv --system-site-packages "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install -e "$APP_DIR"
mkdir -p "${HOME}/.local/bin" "${HOME}/.local/share/applications"
ln -sfn "$APP_DIR/run.sh" "${HOME}/.local/bin/pdf-marker"
"$APP_DIR/.venv/bin/python" - "$APP_DIR" <<'PY'
from pathlib import Path
import sys
root = Path(sys.argv[1])
# Desktop Entry quoting is distinct from shell quoting.
launcher = str(root / 'run.sh').replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%')
entry = f'''[Desktop Entry]
Type=Application
Name=PDF Marker
Comment=Fill PDF forms and add text or handwritten marks
Exec="{launcher}" %f
Icon={root / 'assets/pdf-marker.svg'}
Terminal=false
Categories=Office;
MimeType=application/pdf;
StartupNotify=true
'''
(Path.home() / '.local/share/applications/pdf-marker.desktop').write_text(entry)
PY
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "${HOME}/.local/share/applications"
fi
echo "PDF Marker is installed in your application menu. Run: $APP_DIR/run.sh"
