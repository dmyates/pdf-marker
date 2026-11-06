# PDF Marker

A small native Linux app for reading, filling, and marking PDFs. Built with GTK4,
PyGObject, Pycairo, PyMuPDF, and pypdf. Documents are processed locally.

![PDF Marker with an added text box selected for resizing, a hand-drawn circle and arrow, and a handwritten signature on a sample document.](docs/screenshots/text-boxes.png)

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

### Text boxes and freehand drawings

The screenshot above shows a text box added to a document without form fields.
The selected box has a resize handle; the circle, arrow, and signature are
freehand marks drawn directly on the page.

With **Draw** selected, add signatures, circle details, or sketch wherever you
need to. Each stroke can be undone, moved, or deleted.

![PDF Marker with Draw selected, showing a freehand circle, an arrow pointing to an added text box, and a handwritten signature.](docs/screenshots/drawing.png)

### Forms and highlights

Use **Fill fields** to identify interactive fields, and **Highlight** to mark
existing text. This sample shows filled fields and a highlighted reminder.

![PDF Marker showing three filled form fields outlined in blue and a reminder highlighted in yellow.](docs/screenshots/forms-and-highlights.png)

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
same values. Read-only fields stay read-only.
Encrypted PDFs prompt for their password; saved files are unencrypted.

Rendering and saving run on the UI thread, between frames. An unusually complex
page can still briefly pause the interface. Printing renders pages at up to
300 dpi.

## Dynamic XFA forms

Some forms, including those from SARS and many other government agencies, are dynamic XFA forms. Their only real page is a placeholder,[^xfa] and the form itself is an XML template plus data that Adobe Reader lays out when the file opens. That's why reading them on Linux has usually meant finding a copy of Acrobat.

PDF Marker lays these forms out itself, which takes a moment when the file opens. It places the form's sections, fields, text, borders, boxed (comb) fields, check boxes and images, fills them with the form's data, repeats sections once for each entry in the data and divides the result into pages using the form's own page templates and page breaks. Read-only values become ordinary page text, so you can select, copy and print them. Editable fields become standard form fields that you fill in as usual. Values in boxed fields are drawn one character per box, so they're copied and searched with a space between each character.

The form's scripts are never run, so anything it works out with JavaScript or FormCalc, such as page numbers, totals, barcodes or the wording for a code, stays blank. PDF Marker handles two common jobs of these scripts without running them: a hidden section that contains data is shown, and a value kept in a hidden field is shown in the visible field drawn at the same place. Where a script would turn a code into words, like an account type of 2 into "Savings", you'll see the code. Fonts that the form names but your system doesn't have are replaced with similar ones, so text can wrap slightly differently than in Adobe Reader.

Saving creates a standard PDF without the XFA form, which any PDF app can open. So that you don't replace the original by accident, the first save suggests a new file name. If a form can't be laid out, it opens with a notice and shows its placeholder page.

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
form scripts, dynamic XFA layout, multiple pages, text and drawing output, rotation/cropping,
passwords, atomic saving, history, inline editing, resizing, group moves and
deletion, text selection, links, search, annotations, highlights, view rotation
and spreads, presentation mode, the toolbar drawer, remembered positions,
printing, the sidebar, continuous wheel scrolling, and display/save text layout
agreement.

See [prior-art.md](docs/prior-art.md) for the research that informed this app.
This project is licensed under AGPL-3.0-or-later; see [LICENSE](LICENSE).

[^xfa]: The placeholder page is often just a message saying "Please wait..." and suggesting you upgrade to the latest version of Adobe Reader. Waiting does not help.
