"""Side panel: document outline, attachments, and the current page's text."""
from .gtk import Gtk, Gio, GObject, Pango


class OutlineItem(GObject.Object):
    def __init__(self, entry):
        super().__init__()
        self.entry = entry
        self.children = Gio.ListStore.new(OutlineItem)


def outline_tree(entries):
    """Nest flat (level, title) entries; skipped levels attach to the nearest parent."""
    root = Gio.ListStore.new(OutlineItem)
    stack = [(0, root)]
    for entry in entries:
        item = OutlineItem(entry)
        while stack[-1][0] >= entry.level:
            stack.pop()
        stack[-1][1].append(item)
        stack.append((entry.level, item.children))
    return root


def size_text(size):
    for unit in ('bytes', 'KB', 'MB', 'GB'):
        if size < 1024 or unit == 'GB':
            return f'{size:.0f} {unit}' if unit == 'bytes' else f'{size:.1f} {unit}'
        size /= 1024


def placeholder(text):
    label = Gtk.Label(label=text, wrap=True, justify=Gtk.Justification.CENTER, vexpand=True)
    label.add_css_class('dim-label')
    return label


class Sidebar(Gtk.Box):
    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6, width_request=280)
        self.add_css_class('sidebar')
        self.window = window
        self.text_page = None
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
        switcher = Gtk.StackSwitcher(stack=self.stack, halign=Gtk.Align.CENTER, margin_top=8)
        self.append(switcher)
        self.append(self.stack)
        self.outline = Gtk.Stack()
        self.stack.add_titled(self.outline, 'outline', 'Outline')
        self.attachments = Gtk.Stack()
        self.stack.add_titled(self.attachments, 'attachments', 'Attachments')
        self.text = Gtk.TextView(editable=False, wrap_mode=Gtk.WrapMode.WORD_CHAR, left_margin=12, right_margin=12,
                                 top_margin=8, bottom_margin=8, monospace=False)
        self.text.update_property([Gtk.AccessibleProperty.LABEL], ['Page text'])
        scroller = Gtk.ScrolledWindow(child=self.text, vexpand=True)
        self.stack.add_titled(scroller, 'text', 'Page text')
        self.stack.connect('notify::visible-child-name', lambda *_: self.update_text())

    def load(self, document):
        self.text_page = None
        for stack in (self.outline, self.attachments):
            while child := stack.get_first_child():
                stack.remove(child)
        entries = document.outline()
        if entries:
            self.outline.add_child(self.outline_view(entries))
        else:
            self.outline.add_child(placeholder('This document has no outline.'))
        items = document.attachments()
        if items:
            listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
            listbox.add_css_class('navigation-sidebar')
            for item in items:
                listbox.append(self.attachment_row(item))
            self.attachments.add_child(Gtk.ScrolledWindow(child=listbox, vexpand=True))
        else:
            self.attachments.add_child(placeholder('This document has no attachments.'))
        self.update_text()

    def outline_view(self, entries):
        root = outline_tree(entries)
        model = Gtk.TreeListModel.new(root, False, False,
                                      lambda item: item.children if item.children.get_n_items() else None)
        # Expand what the PDF marks as open, up to two levels deep.
        i = 0
        while i < model.get_n_items():
            row = model.get_row(i)
            entry = row.get_item().entry
            if row.is_expandable() and entry.expanded and row.get_depth() < 2:
                row.set_expanded(True)
            i += 1
        factory = Gtk.SignalListItemFactory()

        def setup(factory, item):
            row = Gtk.Box(spacing=8)
            title = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
            page = Gtk.Label(xalign=1)
            page.add_css_class('dim-label')
            row.append(title)
            row.append(page)
            item.set_child(Gtk.TreeExpander(child=row))

        def bind(factory, item):
            expander = item.get_child()
            row = item.get_item()
            expander.set_list_row(row)
            entry = row.get_item().entry
            title, page = expander.get_child().get_first_child(), expander.get_child().get_last_child()
            title.set_text(entry.title)
            title.set_tooltip_text(entry.title)
            page.set_text(str(entry.page+1) if entry.page >= 0 else '')

        factory.connect('setup', setup)
        factory.connect('bind', bind)
        view = Gtk.ListView(model=Gtk.SingleSelection(model=model, autoselect=False, can_unselect=True),
                            factory=factory, single_click_activate=True)
        view.add_css_class('navigation-sidebar')
        view.update_property([Gtk.AccessibleProperty.LABEL], ['Document outline'])

        def activate(view, position):
            entry = model.get_row(position).get_item().entry
            if entry.page >= 0:
                self.window.canvas.scroll_to_page(entry.page, entry.y)
        view.connect('activate', activate)
        return Gtk.ScrolledWindow(child=view, vexpand=True)

    def attachment_row(self, item):
        row = Gtk.Box(spacing=8, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        name = Gtk.Label(label=item.name, xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        details = [size_text(item.size)] + ([f'page {item.page+1}'] if item.page >= 0 else []) + ([item.description] if item.description else [])
        info = Gtk.Label(label=' · '.join(details), xalign=0, ellipsize=Pango.EllipsizeMode.END)
        info.add_css_class('dim-label')
        text.append(name)
        text.append(info)
        save = Gtk.Button(icon_name='document-save-symbolic', tooltip_text=f'Save {item.name}…', valign=Gtk.Align.CENTER)
        save.update_property([Gtk.AccessibleProperty.LABEL], [f'Save {item.name}'])
        save.connect('clicked', lambda *_: self.window.save_attachment(item))
        row.append(text)
        row.append(save)
        return row

    def update_text(self):
        canvas = self.window.canvas
        document = self.window.document
        if not document or self.stack.get_visible_child_name() != 'text' or not self.get_mapped():
            return
        if self.text_page != canvas.page:
            self.text_page = canvas.page
            content = document.text(canvas.page).text()
            self.text.get_buffer().set_text(content or 'This page has no selectable text.')
            self.text.update_property([Gtk.AccessibleProperty.LABEL], [f'Text of page {canvas.page+1}'])
