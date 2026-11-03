# PDF Marker

A small native Linux app for reading, filling, and marking PDFs. Built with GTK4,
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

For an application menu entry and an “Open With” PDF handler, which you can
then choose as your default PDF viewer:

```sh
./install.sh
```

The installer uses `~/.local`; it does not require root or change your default
PDF viewer. Keep this directory in place after installing. To uninstall the
menu entry and command, remove `~/.local/bin/pdf-marker` and
`~/.local/share/applications/pdf-marker.desktop`.

## Read

Open a PDF, pass its path on the command line, or drop it into the window. The
start screen and **Menu → Open recent** list recent PDFs. Each file reopens at
the page, zoom, rotation, and page layout you last used.

- **Toolbar:** it slides down when the pointer reaches the top edge of the
  document area, or when you click the small handle there or press F10. The pin
  button keeps it open; that choice is remembered.
- **Select (V)** is the default tool. Drag across text to select it, including
  across pages; double-click selects a word, triple-click a line, and Ctrl+A
  everything. Ctrl+C copies. Click a link, note, or form field to use it.
- **Find (Ctrl+F)** highlights every match. Enter or F3 goes to the next match;
  Shift+Enter or Shift+F3 to the previous one.
- **Links:** links within the document jump to their target. Web and email links
  open in your browser or mail app. Links to another local PDF open it. Links
  that would run a program, a script, or any other kind of address are blocked.
- **Sidebar (F9):** the document outline, embedded attachments (which you can
  save), and the text of the current page. Screen readers can read the page
  text view.
- **View:** the bottom-right indicator holds the page list, zoom (fit width, fit
  page, fixed levels), page layout (single pages, two pages, two pages with a
  cover), rotation, full screen (F11), and presentation (F5). R and Shift+R
  rotate the view; rotation never changes the file. In a presentation, click,
  Space, or the arrow keys move between pages and Escape leaves.
- **Comments:** click a sticky note, highlight, or other annotation to read
  its text, edit it, or delete it.
- **Document properties (Alt+Enter)** shows metadata, page size, PDF version,
  encryption, and form type.

Scroll continuously with the mouse wheel, touchpad, scrollbar, arrow keys, or
Space. Page Up/Page Down and Home/End jump between pages. Ctrl+wheel zooms
around the pointer; middle-drag pans. Pages render in the background, and only
visible page images are kept in memory.

## Fill and mark

1. **Fill fields:** click a field with Select, or use **Fill fields (F)** to
   highlight only fields. Text, checkboxes, radio buttons, dropdowns, and list
   boxes are supported. Repeated fields update together. Acrobat's standard
   form scripts run natively: number, currency, percent, date, time, and special
   formats; input checks and range validation; and calculated totals
   (`AFSimple_Calculate` and simplified field notation). Custom JavaScript is
   never executed.
2. **Text box (T):** drag a rectangle or click to place a default box, then type
   directly on the page. Click outside or press Ctrl+Enter to finish; Escape
   cancels the current edit. Click existing text with the Text box tool, or
   double-click it with Select, to edit it again. Text boxes start compact, grow vertically while typing, and fit their
   text when you finish editing. Size and Ink also work on selected text. The editor and saved PDF
   share the same vector text layout, including line wrapping and font metrics.
3. **Draw (D):** draw a signature or any freehand mark directly on a page. Each
   mouse-down to mouse-up stroke is one undoable mark.
4. **Tick (K) / Cross (X):** click anywhere to place a mark. Size and Stroke control its
   dimensions and line width. Ink controls the color of new marks.
5. **Highlight (H):** drag across text. You can also right-click selected text
   and choose **Highlight**. Highlights are saved as standard PDF highlight
   annotations. The settings button changes their color.
6. **Select (V)** moves marks: drag any added text box, drawing, tick, or cross. Drag a
   selected text box's bottom-right handle to resize and reflow it. Delete,
   **Delete selected**, or the right-click menu removes selected marks, highlights, or
   the selected note. Ctrl-click
   selects multiple marks for moving or deleting together. Marks can also be
   dragged between pages. Ctrl+C copies selected marks; Ctrl+V pastes them onto
   the current page with a small offset. Groups paste together as one undo step.
   Text copy/paste works normally while editing text. Undo and redo apply to every edit; while typing,
   they undo and redo within the text editor.

The settings button appears for tools or selections with adjustable size,
stroke, or color. Click **Menu → Help** or press **?** with the canvas focused
for controls.

## Save and print

**Save (Ctrl+S)** asks where to save the first time, suggesting the current
file. After that it saves to the same file without asking. **Save as
(Ctrl+Shift+S)** always asks. Replacing a file asks for confirmation; the new
file is written completely before it replaces the old one, keeping the old
file's permissions. Closing with unsaved changes asks whether to save, discard,
or cancel.

**Print (Ctrl+P)** opens the system print dialog and includes filled fields,
marks, and highlights. Landscape pages print in landscape orientation.

## PDF behavior and limits

The opened file changes only if you save over it. Saving preserves page count, page rotation,
crop boxes, and interactive form fields. Added text and drawings are vector PDF
content, so recipients can see them without this app. Their handles and undo
history are available during the current session; reopening a saved PDF
does not recover editable overlay objects. Form fields, highlights, and notes
remain editable in any PDF app.

“Signing” here means adding a visible handwritten signature. It does not create
a certificate-based digital signature. Editing an already digitally signed PDF
can invalidate its existing cryptographic signature.

AcroForms are fully supported. Forms that also carry XFA data open normally;
saving filled values removes the stale XFA copy so that every viewer shows the
same values. Dynamic XFA forms, which only Adobe software can lay out, open with
a notice and show their fallback pages. Read-only fields stay read-only.
Encrypted PDFs prompt for their password; saved files are unencrypted.

Rendering and saving run on the UI thread, between frames. An unusually complex
page can still briefly pause the interface. Printing renders pages at up to
300 dpi.

## Development

```sh
/usr/bin/python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e '.[test]'
xvfb-run -a -s '-screen 0 1280x1000x24' .venv/bin/python -m pytest -q
```

GTK tests need Xvfb; the native input regression also needs `libXtst`. When
`DISPLAY` is set, the tests force the X11 backend so that no test window opens
on a Wayland desktop, and they keep recent files and settings in a temporary
directory.

The suite checks canonical form values and widget appearances after saving,
form scripts, multiple pages, text and drawing output, rotation/cropping,
passwords, atomic saving, history, inline editing, resizing, group moves and
deletion, text selection, links, search, annotations, highlights, view rotation
and spreads, presentation mode, the toolbar drawer, remembered positions,
printing, the sidebar, continuous wheel scrolling, and display/save text layout
agreement.

See [prior-art.md](docs/prior-art.md) for the research that informed this app.
This project is licensed under AGPL-3.0-or-later; see [LICENSE](LICENSE).
