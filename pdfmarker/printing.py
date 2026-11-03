"""Print the filled and marked document through the GTK print dialog."""
import pymupdf as pdf
from .gtk import Gtk
from .canvas import pixmap_surface

MAX_DPI = 300


def print_document(window, document, on_error, export_to=None):
    """Show the print dialog, or with `export_to` write the print output as a PDF."""
    view = pdf.open(stream=document.pdf_bytes(), filetype="pdf")
    operation = Gtk.PrintOperation(job_name=document.path.stem, n_pages=len(view),
                                   allow_async=True, embed_page_setup=True, unit=Gtk.Unit.POINTS)
    operation.set_current_page(window.canvas.page)
    if export_to:
        operation.set_export_filename(str(export_to))

    def page_setup(operation, context, number, setup):
        rect = view[number].rect
        setup.set_orientation(Gtk.PageOrientation.LANDSCAPE if rect.width > rect.height else Gtk.PageOrientation.PORTRAIT)

    def draw(operation, context, number):
        page = view[number]
        width, height = context.get_width(), context.get_height()
        scale = min(width/page.rect.width, height/page.rect.height)
        dpi = min(MAX_DPI, max(context.get_dpi_x(), context.get_dpi_y(), 72))
        pix = page.get_pixmap(matrix=pdf.Matrix(scale*dpi/72, scale*dpi/72), alpha=False, annots=True)
        surface, _data = pixmap_surface(pix)
        cr = context.get_cairo_context()
        cr.translate((width-page.rect.width*scale)/2, (height-page.rect.height*scale)/2)
        cr.scale(page.rect.width*scale/pix.width, page.rect.height*scale/pix.height)
        cr.set_source_surface(surface)
        cr.paint()

    def done(operation, result):
        if result == Gtk.PrintOperationResult.ERROR:
            try:
                operation.get_error()
            except Exception as error:
                on_error(error)
        if not view.is_closed:
            view.close()

    operation.connect("request-page-setup", page_setup)
    operation.connect("draw-page", draw)
    operation.connect("done", done)
    try:
        action = Gtk.PrintOperationAction.EXPORT if export_to else Gtk.PrintOperationAction.PRINT_DIALOG
        result = operation.run(action, window)
    except Exception:
        view.close()
        raise
    if result in (Gtk.PrintOperationResult.CANCEL, Gtk.PrintOperationResult.ERROR) and not view.is_closed:
        # "done" may not fire for an immediate cancel.
        view.close()
    return operation
