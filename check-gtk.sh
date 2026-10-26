#!/usr/bin/env bash
# Check only: system packages are always installed by the user.
set -euo pipefail
"${1:-/usr/bin/python3}" - <<'PY'
import sys
try:
    import gi
    gi.require_version('Gtk', '4.0')
    gi.require_version('PangoCairo', '1.0')
    gi.require_foreign('cairo')
    from gi.repository import Gtk, PangoCairo
    import cairo
    if (Gtk.get_major_version(), Gtk.get_minor_version()) < (4, 10):
        raise RuntimeError('GTK 4.10 or newer is required')
except (ImportError, ValueError, RuntimeError) as error:
    print(f'PDF Marker cannot start: {error}\n'
          'Install GTK4 ≥4.10, PyGObject, Pycairo and the GI Cairo bridge with your system package manager.\n'
          'Debian/Ubuntu: python3-gi python3-gi-cairo python3-cairo gir1.2-gtk-4.0 python3-venv\n'
          'Arch: gtk4 python-gobject python-cairo\n'
          'Use PDF_MARKER_PYTHON=/path/to/system/python3 if needed.\n'
          'No system packages were installed.', file=sys.stderr)
    sys.exit(1)
PY
