"""One unscaled Pango layout for the canvas and searchable PDF appearances."""
from dataclasses import replace
from io import BytesIO
from math import ceil
from .gtk import cairo, Pango, PangoCairo


def text_layout(mark, context=None):
    context = context or cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1))
    # GTK4 TextView uses unhinted metrics and fractional glyph positions.
    # Keep those metrics when painting to a scaled canvas or a PDF surface.
    options = cairo.FontOptions()
    options.set_hint_metrics(cairo.HINT_METRICS_OFF)
    options.set_hint_style(cairo.HINT_STYLE_SLIGHT)
    context.set_font_options(options)
    layout = PangoCairo.create_layout(context)
    layout.get_context().set_round_glyph_positions(False)
    font = Pango.FontDescription('DejaVu Sans')
    font.set_absolute_size(mark.size * Pango.SCALE)
    layout.set_font_description(font)
    layout.set_width(round((mark.rect[2] - mark.rect[0]) * Pango.SCALE))
    layout.set_wrap(Pango.WrapMode.WORD_CHAR)
    layout.set_text(mark.text, -1)
    return layout


def paint_text(context, mark):
    context.set_source_rgb(*mark.color)
    PangoCairo.show_layout(context, text_layout(mark))


def laid_out(mark):
    height = max(mark.rect[3] - mark.rect[1], ceil(text_layout(mark).get_size()[1] / Pango.SCALE))
    mark = replace(mark, rect=(*mark.rect[:3], mark.rect[1] + height))
    stream = BytesIO()
    surface = cairo.PDFSurface(stream, mark.rect[2] - mark.rect[0], height)
    paint_text(cairo.Context(surface), mark)
    surface.finish()
    return replace(mark, appearance=stream.getvalue())


def fitted_text(mark):
    """Trim unused box space while preserving the editor's wrapped lines."""
    width, height = text_layout(mark).get_pixel_size()
    x, y, right, _ = mark.rect
    # One document unit prevents rounding a line's last glyph onto a new line.
    width = min(right-x, max(8, width+1))
    return laid_out(replace(mark, rect=(x, y, x+width, y+max(1, height))))
