# PDF Marker

A small native Linux app for filling and marking PDFs. Built with GTK4,
PyGObject, Pycairo, PyMuPDF, and pypdf. Documents are processed locally.

## Run

Requires Python 3.10+, GTK ≥4.10, PyGObject, Pycairo, the GI Cairo bridge,
`venv`, and a Linux graphical desktop (Wayland or X11).

```sh
./run.sh
./run.sh /path/to/form.pdf
# After installation:
pdf-marker /path/to/form.pdf
```

The launcher checks GTK before creating a local `.venv` with system site packages
so it can use your distribution’s Python GTK/Cairo bindings. It installs the Python
PDF dependencies with pip. It never installs system packages automatically.

On Debian/Ubuntu, install `python3-gi python3-gi-cairo python3-cairo
gir1.2-gtk-4.0 python3-venv` using your package manager; the distribution must
provide GTK ≥4.10. On Arch, use `gtk4 python-gobject python-cairo`. The default
interpreter is `/usr/bin/python3`; set `PDF_MARKER_PYTHON` to another system
Python if needed. `./check-gtk.sh` checks availability without changing anything.

For an application menu entry and an “Open With” PDF handler:

```sh
./install.sh
```

The installer uses `~/.local`; it does not require root or change your default
PDF viewer. Keep this directory in place after installing. To uninstall the
menu entry and command, remove `~/.local/bin/pdf-marker` and
`~/.local/share/applications/pdf-marker.desktop`.

## Fill and sign

1. Open a PDF, pass its path on the command line, or drop it into the window.
2. **Fill fields:** click a highlighted field. Text, checkboxes, radio buttons,
   dropdowns, and list boxes are supported. Repeated fields update together.
3. **Text box:** drag a rectangle or click to place a default box, then type
   directly on the page. Click outside or press Ctrl+Enter to finish; Escape
   cancels the current edit. Click existing text with the Text box tool, or
   double-click it with Select, to edit it again. Text boxes start compact, grow vertically while typing, and fit their
   text when you finish editing. Size and Ink also work on selected text. The editor and exported PDF
   share the same vector text layout, including line wrapping and font metrics.
4. **Draw:** draw a signature or any freehand mark directly on a page. Each
   mouse-down to mouse-up stroke is one undoable mark.
5. **Tick / Cross:** click anywhere to place a mark. Size and Stroke control its
   dimensions and line width. Ink controls the color of new marks.
6. **Select:** bounding boxes indicate all selectable marks. Drag any added text box, drawing, tick, or cross to move it. Drag a
   selected text box's bottom-right handle to resize and reflow it. Delete,
   **Delete selected**, or the right-click menu removes selected marks. Ctrl-click
   selects multiple marks for moving or deleting together. Marks can also be
   dragged between pages. Ctrl+C copies selected marks; Ctrl+V pastes them onto
   the current page with a small offset. Groups paste together as one undo step.
   Text copy/paste works normally while editing text. Undo and redo apply to moves, resizing, deletion, and
   completed text edits; while typing, they undo and redo within the text editor.
7. Scroll continuously through all pages with the mouse wheel, touchpad, or
   scrollbar. Extra space below the last page lets you scroll its bottom edge
   upward for easier editing. The page list and page number follow the visible page. The page
   list, page number, Previous/Next, and Page Up/Page Down also jump to a page.
   Click the bottom-right page/zoom indicator for the page list and fit modes.
   Ctrl+wheel zooms around the pointer; middle-drag pans.
   Zoom offers fit width, fit page, and fixed levels. Only visible page
   backgrounds are rendered and cached, to limit memory use in long documents.
8. **Export signed PDF** writes beside the original. **Menu → Export to folder**
   chooses a different destination. The filename is always generated:
   `application-signed-20261025-143022.pdf`. Time is local, to the second.
   Repeated exports in the same second add `-1`, `-2`, etc. Existing files are
   never overwritten. The source extension's case is preserved.

The floating strip holds the tools, Export, and Menu. The settings button appears
for tools or selections with adjustable size, stroke, or ink. Click **Menu → Help**
or press **?** with the canvas focused for controls.

Shortcuts: V/F/T/D/K/X select tools when the canvas has focus. Ctrl+O opens, Ctrl+S exports, Ctrl+Shift+S exports to a folder,
Ctrl+Z undoes, Ctrl+Shift+Z redoes, and Ctrl+W closes. Escape cancels an inline
text edit, a drawing gesture, or selection. Export commits text still being
edited. Closing with unexported changes prompts to export,
discard, or cancel.

## PDF behavior and limits

Original files remain untouched. Export preserves page count, page rotation,
crop boxes, and interactive form fields. Added text and drawings are vector PDF
content, so recipients can see them without this app. Their handles and undo
history are available during the current session; reopening an exported PDF
does not recover editable overlay objects. Form fields remain editable.

“Signing” here means adding a visible handwritten signature. It does not create
a certificate-based digital signature. Editing an already digitally signed PDF
can invalidate its existing cryptographic signature.

AcroForms are supported; XFA forms are rejected with an explanation. Embedded
PDF JavaScript is not executed, so automatic calculations and validation rules
are not run. Read-only fields stay read-only. Encrypted PDFs prompt for their
password; exports are unencrypted. Original page content and annotations can be
viewed but are not movable/editable with the Select tool. Rendering and export
run on the UI thread; very large documents can briefly pause the interface.

## Development

```sh
/usr/bin/python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e '.[test]'
xvfb-run -a -s '-screen 0 1280x1000x24' .venv/bin/python -m pytest -q
```

GTK tests need Xvfb; the native input regression also needs `libXtst`.

The suite checks canonical form values and widget appearances after export,
multiple pages, text and drawing output, rotation/cropping, passwords,
collision-safe filenames, history, inline editing, resizing, group moves and
deletion, continuous wheel scrolling, and display/export text layout agreement.

See [prior-art.md](docs/prior-art.md) for the research that informed this app.
This project is licensed under AGPL-3.0-or-later; see [LICENSE](LICENSE).
