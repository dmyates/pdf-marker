"""System GTK bindings shared by the native interface."""
try:
    import gi
    gi.require_version('Gtk', '4.0')
    gi.require_version('PangoCairo', '1.0')
    gi.require_foreign('cairo')
    from gi.repository import Gtk, Gdk, Gio, GLib, Gsk, Graphene, Pango, PangoCairo
    import cairo
    if (Gtk.get_major_version(), Gtk.get_minor_version()) < (4, 10):
        raise RuntimeError('GTK 4.10 or newer is required')
except (ImportError, ValueError, RuntimeError) as error:
    raise SystemExit(
        f'PDF Marker cannot start: {error}\n'
        'Install system GTK4 ≥4.10, PyGObject, Pycairo and the GI Cairo bridge.\n'
        'Run ./check-gtk.sh for package names, and create the virtual environment\n'
        'with /usr/bin/python3 -m venv --system-site-packages .venv.'
    ) from None
