"""A small GTK4 window around a continuous PDF canvas."""
from __future__ import annotations
import argparse
import sys
import pymupdf as pdf
from .gtk import Gtk, Gdk, Gio, GLib
from .document import Document, PasswordRequired
from .canvas import Canvas

TOOLS = [
    ('select','input-mouse-symbolic','Select','V'),
    ('fill','view-list-symbolic','Fill fields','F'),
    ('text','insert-text-symbolic','Text','T'),
    ('draw','document-edit-symbolic','Draw','D'),
    ('tick','object-select-symbolic','Tick','K'),
    ('cross','window-close-symbolic','Cross','X'),
]
CSS = '''
.document-overlay, .document-scroll { background: #292b2e; }
.tool-strip { padding: 5px; border-radius: 12px; background: @theme_bg_color;
  box-shadow: 0 3px 12px alpha(black, .4); }
.tool-strip button { min-width: 26px; min-height: 30px; padding: 3px 5px; }
.page-indicator, .help-panel, .feedback { padding: 10px; border-radius: 10px;
  background: @theme_bg_color; box-shadow: 0 2px 8px alpha(black, .35); }
.empty-state { color: #eeeeee; }
.inline-editor { padding: 0; margin: 0; border: 0; }
'''


def button(label, callback, icon=None, tooltip=None):
    b = Gtk.Button(icon_name=icon) if icon else Gtk.Button(label=label)
    b.set_tooltip_text(tooltip or label)
    b.update_property([Gtk.AccessibleProperty.LABEL],[label])
    b.connect('clicked',lambda *_:callback())
    return b


def box(vertical=False, spacing=8):
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL,spacing=spacing)


class Window(Gtk.ApplicationWindow):
    def __init__(self, application=None):
        super().__init__(application=application,title='PDF Marker',default_width=1024,default_height=780)
        self.document = None
        self.last_export = None
        self.size,self.stroke,self.ink = 12.,1.5,(0.,0.,0.)
        self._syncing = False
        self._feedback_timer = None
        self._closing = False
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_display(self.get_display(),provider,Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.overlay = Gtk.Overlay()
        self.overlay.add_css_class('document-overlay')
        self.set_child(self.overlay)
        self.scroll = Gtk.ScrolledWindow(hexpand=True,vexpand=True)
        self.scroll.add_css_class('document-scroll')
        self.canvas = Canvas(self)
        self.scroll.set_child(self.canvas)
        # Canvas focus spans the whole document; navigation owns scrolling.
        self.scroll.get_child().set_scroll_to_focus(False)
        self.overlay.set_child(self.scroll)
        self.scroll.get_vadjustment().connect('value-changed',self.canvas.view_changed)
        self.scroll.get_hadjustment().connect('value-changed',lambda *_:self.canvas.area.queue_draw())
        self._viewport_size = (0, 0)
        self.add_tick_callback(self.viewport_resized)
        self.zoom_mode = None
        self.actions = {}
        for name, callback, shortcuts in [
            ('open',self.open_dialog,['<Control>o']),
            ('export',self.export,['<Control>s']),
            ('export-folder',self.export_to,['<Control><Shift>s']),
            ('undo',lambda:self.history(False),['<Control>z']),
            ('redo',lambda:self.history(True),['<Control><Shift>z','<Control>y']),
            ('delete',self.canvas.delete_selected,[]),
            ('help',self.toggle_help,[]),
            ('close',self.close,['<Control>w']),
        ]:
            action = Gio.SimpleAction.new(name,None)
            action.connect('activate',lambda a,p,fn=callback:fn())
            self.add_action(action)
            self.actions[name] = action
            if application:
                application.set_accels_for_action('win.'+name,shortcuts)
        self.build_strip()
        self.build_navigation()
        self.empty = box(True,12)
        self.empty.set_halign(Gtk.Align.CENTER)
        self.empty.set_valign(Gtk.Align.CENTER)
        self.empty.add_css_class('empty-state')
        self.empty.append(button('Open PDF',self.open_dialog,tooltip='Open PDF (Ctrl+O)'))
        self.empty.append(Gtk.Label(label='or drop a PDF here'))
        self.overlay.add_overlay(self.empty)
        self.build_help()
        self.feedback = box(False,8)
        self.feedback.add_css_class('feedback')
        self.feedback.set_halign(Gtk.Align.CENTER)
        self.feedback.set_valign(Gtk.Align.END)
        self.feedback.set_margin_bottom(64)
        self.feedback_label = Gtk.Label(wrap=True,max_width_chars=42)
        self.feedback.append(self.feedback_label)
        self.reveal = button('Show in folder',self.reveal_export)
        self.feedback.append(self.reveal)
        self.feedback.append(button('Dismiss',lambda:self.feedback.set_visible(False),'window-close-symbolic'))
        self.overlay.add_overlay(self.feedback)
        self.feedback.set_visible(False)
        drop = Gtk.DropTarget.new(Gdk.FileList,Gdk.DragAction.COPY)
        drop.connect('drop',self.drop)
        self.overlay.add_controller(drop)
        self.connect('close-request',self.on_close)
        self.update_state()

    def build_strip(self):
        self.strip = box(False,1)
        self.strip.add_css_class('tool-strip')
        self.strip.set_halign(Gtk.Align.CENTER)
        self.strip.set_valign(Gtk.Align.START)
        self.strip.set_margin_top(12)
        self.tool_buttons = {}
        for name,icon,label,key in TOOLS:
            b = Gtk.ToggleButton(icon_name=icon)
            b.set_tooltip_text(f'{label} ({key})')
            b.update_property([Gtk.AccessibleProperty.LABEL],[label])
            b.connect('clicked',lambda _,tool=name:self.canvas.set_tool(tool) if not self._syncing else None)
            self.strip.append(b)
            self.tool_buttons[name] = b
        self.settings = Gtk.MenuButton(icon_name='emblem-system-symbolic',tooltip_text='Mark settings')
        self.settings.update_property([Gtk.AccessibleProperty.LABEL],['Mark settings'])
        pop = Gtk.Popover()
        settings = box(True,10)
        for setter in ('set_margin_top','set_margin_bottom','set_margin_start','set_margin_end'):
            getattr(settings,setter)(10)
        self.size_control = Gtk.SpinButton.new_with_range(4,144,1)
        self.size_control.set_value(self.size)
        self.stroke_control = Gtk.SpinButton.new_with_range(.25,12,.25)
        self.stroke_control.set_value(self.stroke)
        self.size_control.connect('value-changed',lambda s:self.change_style(size=s.get_value()))
        self.stroke_control.connect('value-changed',lambda s:self.change_style(width=s.get_value()))
        for label,control in [('Text / mark size',self.size_control),('Stroke width',self.stroke_control)]:
            row = box()
            text = Gtk.Label(label=label,xalign=0,hexpand=True)
            control.update_property([Gtk.AccessibleProperty.LABEL],[label])
            row.append(text)
            row.append(control)
            settings.append(row)
        self.color = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(title='Ink color',with_alpha=False))
        rgba = Gdk.RGBA()
        rgba.parse('black')
        self.color.set_rgba(rgba)
        self.color.connect('notify::rgba',self.ink_changed)
        row = box()
        row.append(Gtk.Label(label='Ink color',xalign=0,hexpand=True))
        row.append(self.color)
        settings.append(row)
        pop.set_child(settings)
        self.settings.set_popover(pop)
        self.strip.append(self.settings)
        self.export_button = button('Export signed PDF',self.export,'document-save-symbolic','Export signed PDF (Ctrl+S)')
        self.strip.append(self.export_button)
        menu = Gio.Menu()
        for label,action in [('Open…','open'),('Undo','undo'),('Redo','redo'),('Delete selected','delete'),('Export to folder…','export-folder'),('Help','help')]:
            menu.append(label,'win.'+action)
        self.menu = Gtk.MenuButton(icon_name='open-menu-symbolic',menu_model=menu,tooltip_text='Menu')
        self.menu.update_property([Gtk.AccessibleProperty.LABEL],['Menu'])
        self.strip.append(self.menu)
        self.overlay.add_overlay(self.strip)

    def build_navigation(self):
        self.indicator = Gtk.MenuButton()
        self.indicator.add_css_class('page-indicator')
        self.indicator.set_halign(Gtk.Align.END)
        self.indicator.set_valign(Gtk.Align.END)
        self.indicator.set_margin_end(16)
        self.indicator.set_margin_bottom(16)
        self.indicator.set_tooltip_text('Page navigation and zoom')
        pop = Gtk.Popover(position=Gtk.PositionType.TOP)
        content = box(True,10)
        row = box()
        row.append(button('Previous page',lambda:self.canvas.scroll_to_page(self.canvas.page-1),'go-previous-symbolic'))
        self.page_number = Gtk.SpinButton.new_with_range(1,1,1)
        self.page_number.update_property([Gtk.AccessibleProperty.LABEL],['Page number'])
        self.page_number.connect('value-changed',lambda s:self.canvas.scroll_to_page(s.get_value_as_int()-1) if not self._syncing else None)
        row.append(self.page_number)
        row.append(button('Next page',lambda:self.canvas.scroll_to_page(self.canvas.page+1),'go-next-symbolic'))
        content.append(row)
        self.pages = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.pages.connect('row-activated',lambda _,row:self.canvas.scroll_to_page(row.get_index()))
        scroller = Gtk.ScrolledWindow(min_content_height=100,max_content_height=200,propagate_natural_height=True)
        scroller.set_child(self.pages)
        content.append(scroller)
        self.zoom = Gtk.DropDown.new_from_strings(['Fit width','Fit page','50%','75%','100%','125%','150%','200%','300%','400%'])
        self.zoom.set_selected(4)
        self.zoom.update_property([Gtk.AccessibleProperty.LABEL],['Zoom'])
        self.zoom.connect('notify::selected',self.apply_zoom)
        content.append(self.zoom)
        pop.set_child(content)
        self.indicator.set_popover(pop)
        self.overlay.add_overlay(self.indicator)

    def build_help(self):
        self.help = box(True,12)
        self.help.add_css_class('help-panel')
        self.help.set_halign(Gtk.Align.CENTER)
        self.help.set_valign(Gtk.Align.CENTER)
        self.help.append(Gtk.Label(label='PDF Marker · Controls',xalign=0))
        self.help.append(Gtk.Label(label='V  Select     F  Fill fields     T  Text\nD  Draw       K  Tick            X  Cross\n\nScroll through pages · Ctrl+wheel zooms\nMiddle-drag pans · Page Up/Down jumps pages\nCtrl-click selects marks together\nCtrl+C / Ctrl+V copies / pastes marks\nDrag the text handle to resize\n\nCtrl+Enter finishes text · Escape cancels\nCtrl+Z / Ctrl+Shift+Z undo / redo\nCtrl+O opens · Ctrl+S exports\nCtrl+Shift+S exports to a folder\n? shows these controls',xalign=0))
        self.help.append(button('Close help',self.toggle_help))
        self.overlay.add_overlay(self.help)
        self.help.set_visible(False)

    def toggle_help(self):
        self.help.set_visible(not self.help.get_visible())

    def change_style(self,**style):
        if self._syncing:
            return
        if 'size' in style:
            self.size = style['size']
        if 'width' in style:
            self.stroke = style['width']
        self.canvas.style_selected(**style)

    def ink_changed(self,*_):
        if not self._syncing:
            color = self.color.get_rgba()
            self.ink = (color.red,color.green,color.blue)
            self.canvas.style_selected(color=self.ink)

    def apply_zoom(self,*_,navigate=None):
        if not self.document or self._syncing or self.scroll.get_width() < 1:
            return
        index = self.zoom.get_selected()
        self.zoom_mode = index if index < 2 else None
        if index < 2:
            w,h = self.document.pages[self.canvas.page]
            z = self.scroll.get_width() / ((max(w for w,h in self.document.pages) if index==0 else w) + 48)
            if index==1:
                z = min(z,(self.scroll.get_height()-112)/h)
        else:
            z = [.5,.75,1,1.25,1.5,2,3,4][index-2]
        self.canvas.set_zoom(z)
        if navigate is not None:
            self.canvas._zoom_anchor = (self.scroll.get_hadjustment().get_value(), self.canvas.origins[navigate][1]*self.canvas.zoom-72)

    def viewport_resized(self, widget, clock):
        size = (self.scroll.get_width(), self.scroll.get_height())
        if size != self._viewport_size and min(size) > 0:
            self._viewport_size = size
            self.refit()
            self.canvas.resize_canvas()
        return True

    def refit(self):
        if self.document and self.zoom_mode is not None:
            self.apply_zoom()

    def update_state(self):
        if not hasattr(self,'indicator'):
            return
        self._syncing = True
        doc = self.document
        self.empty.set_visible(doc is None)
        self.indicator.set_visible(doc is not None)
        for name in ('export','export-folder','undo','redo','delete'):
            enabled = bool(doc)
            if name == 'undo':
                enabled = bool(doc and (doc.can_undo or self.canvas.pending))
            elif name == 'redo':
                enabled = bool(doc and (doc.can_redo or self.canvas.editor))
            elif name == 'delete':
                enabled = bool(self.canvas.selected or self.canvas.pending)
            self.actions[name].set_enabled(enabled)
        self.export_button.set_sensitive(doc is not None)
        for tool,b in self.tool_buttons.items():
            b.set_active(self.canvas.tool == tool)
            b.set_sensitive(doc is not None)
        mark = self.canvas.edit_mark or next((doc.marks[i] for i in sorted(self.canvas.selected) if doc and i<len(doc.marks)),None)
        kind = mark.kind if mark else self.canvas.tool
        self.settings.set_visible(bool(doc and kind in ('text','draw','tick','cross')))
        self.size_control.set_sensitive(kind in ('text','tick','cross'))
        self.stroke_control.set_sensitive(kind in ('draw','tick','cross'))
        self.size_control.set_value(mark.size if mark and kind=='text' else mark.rect[2]-mark.rect[0] if mark and kind in ('tick','cross') else self.size)
        self.stroke_control.set_value(mark.width if mark else self.stroke)
        rgba = Gdk.RGBA()
        rgba.red,rgba.green,rgba.blue = mark.color if mark else self.ink
        rgba.alpha = 1
        self.color.set_rgba(rgba)
        if doc:
            self.indicator.set_label(f'{self.canvas.page+1} / {len(doc.pages)} · {round(self.canvas.zoom*100)}%')
            self.page_number.set_range(1,len(doc.pages))
            self.page_number.set_value(self.canvas.page+1)
            self.pages.select_row(self.pages.get_row_at_index(self.canvas.page))
        self.set_title(('• ' if doc and (doc.dirty or self.canvas.pending) else '')+(doc.path.name+' — ' if doc else '')+'PDF Marker')
        self._syncing = False

    def perform(self,operation):
        try:
            operation()
            self.canvas.refresh()
            return True
        except Exception as error:
            self.error('Could not apply edit',error)
            self.canvas.refresh()
            return False

    def history(self,redo):
        if self.canvas.editor:
            buffer = self.canvas.editor.get_buffer()
            if redo and buffer.get_can_redo():
                buffer.redo()
            elif not redo and buffer.get_can_undo():
                buffer.undo()
        elif self.document:
            self.canvas.cancel_gesture()
            self.canvas.selected.clear()
            self.perform(self.document.redo if redo else self.document.undo)

    def dialog(self,title,child,callback,buttons=(('Cancel',Gtk.ResponseType.CANCEL),('OK',Gtk.ResponseType.OK))):
        dialog = Gtk.Dialog(title=title,transient_for=self,modal=True,application=self.get_application())
        content = dialog.get_content_area()
        for setter in ('set_margin_top','set_margin_bottom','set_margin_start','set_margin_end'):
            getattr(content,setter)(16)
        content.set_spacing(12)
        content.append(child)
        for label,response in buttons:
            dialog.add_button(label,response)
        dialog.set_default_response(Gtk.ResponseType.OK)
        def response(d,result):
            if callback(result) is not False:
                d.destroy()
        dialog.connect('response',response)
        dialog.present()
        return dialog

    def confirm_discard(self,continuation):
        if not self.canvas.finish_edit():
            return
        if not self.document or not self.document.dirty:
            continuation()
            return
        def response(result):
            if result == Gtk.ResponseType.REJECT or result == Gtk.ResponseType.ACCEPT and self.export():
                continuation()
        self.dialog('Unexported changes',Gtk.Label(label='Export your changes before closing this document?'),response,
                    (('Cancel',Gtk.ResponseType.CANCEL),('Discard',Gtk.ResponseType.REJECT),('Export',Gtk.ResponseType.ACCEPT)))

    def load(self,path):
        self.confirm_discard(lambda:self._load(path))

    def _load(self,path,password=''):
        try:
            document = Document(path,password)
            document.render(0,.5)
        except PasswordRequired:
            entry = Gtk.PasswordEntry(show_peek_icon=True,activates_default=True)
            self.dialog('PDF password',entry,lambda result:self._load(path,entry.get_text()) if result==Gtk.ResponseType.OK else None)
            return
        except Exception as error:
            self.error('Could not open PDF',error)
            return
        self.document = document
        self.last_export = None
        self.feedback.set_visible(False)
        self.canvas.reset_document()
        while row := self.pages.get_row_at_index(0):
            self.pages.remove(row)
        for p in range(len(document.pages)):
            self.pages.append(Gtk.Label(label=f'Page {p+1}',xalign=0,margin_top=6,margin_bottom=6,margin_start=8,margin_end=8))
        self.canvas.set_tool('fill' if document.fields else 'text')
        self.zoom_mode = 0
        self.zoom.set_selected(0)
        self.apply_zoom()
        GLib.idle_add(lambda:self.canvas.scroll_to_page(0))
        self.update_state()

    def open_dialog(self):
        dialog = Gtk.FileDialog(title='Open PDF')
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filter = Gtk.FileFilter(name='PDF documents')
        filter.add_mime_type('application/pdf')
        filters.append(filter)
        dialog.set_filters(filters)
        def done(d,result):
            try:
                file = d.open_finish(result)
                if file.get_path():
                    self.load(file.get_path())
            except GLib.Error as error:
                if not error.matches(Gtk.dialog_error_quark(),Gtk.DialogError.DISMISSED):
                    self.error('Could not open PDF',error)
        dialog.open(self,None,done)

    def export_to(self):
        if not self.document:
            return
        dialog = Gtk.FileDialog(title='Export signed PDF to folder',initial_folder=Gio.File.new_for_path(str(self.document.path.parent)))
        def done(d,result):
            try:
                folder = d.select_folder_finish(result)
                self.export(folder.get_path())
            except GLib.Error as error:
                if not error.matches(Gtk.dialog_error_quark(),Gtk.DialogError.DISMISSED):
                    self.error('Could not select folder',error)
        dialog.select_folder(self,None,done)

    def export(self,folder=None):
        if not self.document or not self.canvas.finish_edit():
            return False
        try:
            self.last_export = self.document.export(folder)
        except Exception as error:
            self.error('Could not export PDF',error)
            return False
        self.notify(f'Exported {self.last_export.name}',export=True)
        self.update_state()
        return True

    def reveal_export(self):
        if self.last_export:
            Gio.AppInfo.launch_default_for_uri(self.last_export.parent.as_uri(),None)

    def notify(self,message,export=False):
        if self._feedback_timer:
            GLib.source_remove(self._feedback_timer)
        self.feedback_label.set_text(message)
        self.reveal.set_visible(export)
        self.feedback.set_visible(True)
        def hide():
            self.feedback.set_visible(False)
            self._feedback_timer = None
            return False
        self._feedback_timer = GLib.timeout_add_seconds(15 if export else 10,hide)

    def error(self,title,error):
        self.notify(f'{title}: {error}')

    def on_close(self,*_):
        if self._closing:
            return False
        def close():
            self._closing = True
            self.close()
        self.confirm_discard(close)
        return True

    def drop(self,target,value,x,y):
        for file in value.get_files():
            path = file.get_path()
            if path and path.lower().endswith('.pdf'):
                self.load(path)
                return True
        return False

    def mark_menu(self,index,x,y):
        pop = Gtk.Popover()
        pop.set_parent(self.canvas.area)
        rect = Gdk.Rectangle()
        rect.x,rect.y,rect.width,rect.height = round(x),round(y),1,1
        pop.set_pointing_to(rect)
        content = box(True,4)
        def run(fn):
            pop.popdown()
            fn()
        mark = self.document.marks[index]
        if mark.kind == 'text':
            content.append(button('Edit text',lambda:run(lambda:self.canvas.begin_text(mark,index))))
        content.append(button('Delete selected',lambda:run(self.canvas.delete_selected)))
        pop.set_child(content)
        pop.connect('closed',lambda p:p.unparent())
        pop.popup()

    def fill_field(self,field):
        if field.flags & 1:
            self.notify('This field is read-only.')
            return
        value = self.document.values.get(field.name,field.value)
        if field.kind in (pdf.PDF_WIDGET_TYPE_CHECKBOX,pdf.PDF_WIDGET_TYPE_RADIOBUTTON):
            on = 'Yes' if field.on_value is True else field.on_value
            result = on if field.kind==pdf.PDF_WIDGET_TYPE_RADIOBUTTON or value in ('Off','',None,False) else 'Off'
            self.perform(lambda:self.document.fill(field,result))
            return
        content = box(True,8)
        content.append(Gtk.Label(label=field.label,xalign=0,wrap=True))
        if field.kind == pdf.PDF_WIDGET_TYPE_TEXT:
            editor = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
            editor.get_buffer().set_text(str(value or ''))
            scroller = Gtk.ScrolledWindow(min_content_width=360,min_content_height=120)
            scroller.set_child(editor)
            get_value = lambda:editor.get_buffer().get_text(editor.get_buffer().get_start_iter(),editor.get_buffer().get_end_iter(),True)
            content.append(scroller)
        elif field.kind == pdf.PDF_WIDGET_TYPE_LISTBOX:
            multiple = bool(field.flags & (1<<21))
            editor = Gtk.ListBox(selection_mode=Gtk.SelectionMode.MULTIPLE if multiple else Gtk.SelectionMode.SINGLE)
            selected = value if isinstance(value,list) else [value]
            for i,(export,label) in enumerate(field.choices):
                editor.append(Gtk.Label(label=label,xalign=0))
                if export in selected:
                    editor.select_row(editor.get_row_at_index(i))
            def get_value():
                values = [field.choices[row.get_index()][0] for row in editor.get_selected_rows()]
                return values if multiple else next(iter(values),'')
            content.append(editor)
        elif field.kind == pdf.PDF_WIDGET_TYPE_COMBOBOX:
            editor = Gtk.DropDown.new_from_strings([label for export,label in field.choices])
            editor.set_selected(next((i for i,(export,label) in enumerate(field.choices) if export==value),Gtk.INVALID_LIST_POSITION))
            content.append(editor)
            custom = Gtk.Entry(text=str(value or '')) if field.flags & (1<<18) else None
            if custom:
                content.append(custom)
                editor.connect('notify::selected',lambda *_:custom.set_text(field.choices[editor.get_selected()][0]) if editor.get_selected()<len(field.choices) else None)
            get_value = lambda:custom.get_text() if custom else field.choices[editor.get_selected()][0] if editor.get_selected()<len(field.choices) else ''
        else:
            self.notify('Use Draw to add a handwritten signature at this position.')
            return
        editor.update_property([Gtk.AccessibleProperty.LABEL],[field.label])
        def save(result):
            if result not in (Gtk.ResponseType.OK,Gtk.ResponseType.REJECT):
                return
            answer = ( [] if field.kind==pdf.PDF_WIDGET_TYPE_LISTBOX and field.flags & (1<<21) else '') if result==Gtk.ResponseType.REJECT else get_value()
            if field.max_length and isinstance(answer,str) and len(answer)>field.max_length:
                self.error('Text is too long',f'This field allows {field.max_length} characters.')
                return False
            return self.perform(lambda:self.document.fill(field,answer))
        self.dialog(field.label,content,save,(('Clear',Gtk.ResponseType.REJECT),('Cancel',Gtk.ResponseType.CANCEL),('OK',Gtk.ResponseType.OK)))


def main():
    parser = argparse.ArgumentParser(prog='pdf-marker',description='Fill PDF forms and add handwritten signatures.')
    parser.add_argument('pdf',nargs='?',help='PDF file to open')
    args = parser.parse_args()
    application = Gtk.Application(application_id='io.github.pdfmarker.PDFMarker',flags=Gio.ApplicationFlags.NON_UNIQUE)
    def activate(app):
        window = Window(app)
        window.present()
        if args.pdf:
            window.load(args.pdf)
    application.connect('activate',activate)
    return application.run([sys.argv[0]])
