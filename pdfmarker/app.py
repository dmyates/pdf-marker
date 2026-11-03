"""A GTK4 window around a continuous PDF canvas."""
from __future__ import annotations
import argparse
import sys
from pathlib import Path
from urllib.parse import urlparse
import pymupdf as pdf
from .gtk import Gtk, Gdk, Gio, GLib, Pango
from .document import Document, PasswordRequired, HIGHLIGHT
from .canvas import Canvas
from .sidebar import Sidebar
from .printing import print_document
from . import state

ICONS = Path(__file__).parent / 'icons'
TOOLS = [
    ('select','input-mouse-symbolic','Select','V'),
    ('fill','view-list-symbolic','Fill fields','F'),
    ('text','insert-text-symbolic','Text','T'),
    ('draw','document-edit-symbolic','Draw','D'),
    ('tick','object-select-symbolic','Tick','K'),
    ('cross','window-close-symbolic','Cross','X'),
    ('highlight','pdfmarker-highlight-symbolic','Highlight','H'),
]
ZOOMS = ['Fit width','Fit page','50%','75%','100%','125%','150%','200%','300%','400%']
SPREADS = [('none','Single pages'),('odd','Two pages'),('even','Two pages with cover')]
SAFE_SCHEMES = {'http', 'https', 'mailto'}
EDGE = 8
CSS = '''
.document-overlay, .document-scroll { background: #292b2e; }
.document-overlay.presenting, .document-scroll.presenting { background: black; }
.tool-strip { padding: 5px; border-radius: 12px; background: @theme_bg_color;
  box-shadow: 0 3px 12px alpha(black, .4); margin: 12px 16px 16px; }
.tool-strip button { min-width: 26px; min-height: 30px; padding: 3px 5px; }
.drawer-handle { min-width: 56px; min-height: 5px; padding: 0; margin-top: 3px; border-radius: 3px;
  background: alpha(white, .28); box-shadow: none; }
.drawer-handle:hover { background: alpha(white, .6); }
.page-indicator, .help-panel, .feedback, .search-panel { padding: 10px; border-radius: 10px;
  background: @theme_bg_color; box-shadow: 0 2px 8px alpha(black, .35); }
.search-panel { padding: 6px; }
.sidebar { background: @theme_bg_color; border-right: 1px solid alpha(black, .25); }
.empty-state { color: #eeeeee; }
.empty-state .recent { color: #eeeeee; }
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


def add_recent(path):
    data = Gtk.RecentData()
    data.display_name = Path(path).name
    data.mime_type = 'application/pdf'
    data.app_name = 'PDF Marker'
    data.app_exec = 'pdf-marker %f'
    Gtk.RecentManager.get_default().add_full(Gio.File.new_for_path(str(path)).get_uri(), data)


def recent_pdfs(limit=8):
    items = []
    for info in Gtk.RecentManager.get_default().get_items():
        if info.get_mime_type() == 'application/pdf' and info.is_local() and info.exists():
            path = Gio.File.new_for_uri(info.get_uri()).get_path()
            if path:
                items.append((info.get_modified().to_unix(), path))
    return [path for _, path in sorted(items, reverse=True)[:limit]]


class Window(Gtk.ApplicationWindow):
    def __init__(self, application=None):
        super().__init__(application=application,title='PDF Marker',default_width=1024,default_height=780)
        self.document = None
        self.size,self.stroke,self.ink = 12.,1.5,(0.,0.,0.)
        self.highlight_color = HIGHLIGHT
        self.presenting = False
        self._syncing = False
        self._feedback_timer = None
        self._drawer_timer = None
        self._search_source = None
        self._search = None
        self._closing = False
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_display(self.get_display(),provider,Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Gtk.IconTheme.get_for_display(self.get_display()).add_search_path(str(ICONS))
        root = box(False,0)
        self.set_child(root)
        self.sidebar = Sidebar(self)
        self.sidebar_revealer = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_RIGHT,child=self.sidebar)
        self.sidebar_revealer.connect('notify::child-revealed',lambda *_:self.sidebar.update_text())
        root.append(self.sidebar_revealer)
        self.overlay = Gtk.Overlay(hexpand=True)
        self.overlay.add_css_class('document-overlay')
        root.append(self.overlay)
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
            ('save',self.save,['<Control>s']),
            ('save-as',self.save_as,['<Control><Shift>s']),
            ('print',self.print,['<Control>p']),
            ('find',self.show_search,['<Control>f']),
            ('find-next',lambda:self.next_match(1),['F3','<Control>g']),
            ('find-previous',lambda:self.next_match(-1),['<Shift>F3','<Control><Shift>g']),
            ('undo',lambda:self.history(False),['<Control>z']),
            ('redo',lambda:self.history(True),['<Control><Shift>z','<Control>y']),
            ('delete',self.canvas.delete_selected,[]),
            ('sidebar',self.toggle_sidebar,['F9']),
            ('rotate-left',lambda:self.canvas.set_rotation(self.canvas.rotation-90),['<Control>bracketleft']),
            ('rotate-right',lambda:self.canvas.set_rotation(self.canvas.rotation+90),['<Control>bracketright']),
            ('fullscreen',self.toggle_fullscreen,['F11']),
            ('present',self.start_presentation,['F5']),
            ('toolbar',self.focus_toolbar,['F10']),
            ('properties',self.show_properties,['<Alt>Return']),
            ('help',self.toggle_help,[]),
            ('close',self.close,['<Control>w']),
        ]:
            action = Gio.SimpleAction.new(name,None)
            action.connect('activate',lambda a,p,fn=callback:fn())
            self.add_action(action)
            self.actions[name] = action
            if application:
                application.set_accels_for_action('win.'+name,shortcuts)
        spread = Gio.SimpleAction.new_stateful('spread',GLib.VariantType.new('s'),GLib.Variant.new_string('none'))
        spread.connect('change-state',lambda a,value:self.canvas.set_spread(value.get_string()))
        self.add_action(spread)
        self.actions['spread'] = spread
        recent = Gio.SimpleAction.new('open-recent',GLib.VariantType.new('s'))
        recent.connect('activate',lambda a,value:self.load(value.get_string()))
        self.add_action(recent)
        self.build_strip()
        self.build_navigation()
        self.build_search()
        self.empty = box(True,12)
        self.empty.set_halign(Gtk.Align.CENTER)
        self.empty.set_valign(Gtk.Align.CENTER)
        self.empty.add_css_class('empty-state')
        self.empty.append(button('Open PDF',self.open_dialog,tooltip='Open PDF (Ctrl+O)'))
        self.empty.append(Gtk.Label(label='or drop a PDF here'))
        self.recent_list = box(True,2)
        self.empty.append(self.recent_list)
        self.overlay.add_overlay(self.empty)
        self.build_help()
        self.feedback = box(False,8)
        self.feedback.add_css_class('feedback')
        self.feedback.set_halign(Gtk.Align.CENTER)
        self.feedback.set_valign(Gtk.Align.END)
        self.feedback.set_margin_bottom(64)
        self.feedback_label = Gtk.Label(wrap=True,max_width_chars=48)
        self.feedback.append(self.feedback_label)
        self.feedback.append(button('Dismiss',lambda:self.feedback.set_visible(False),'window-close-symbolic'))
        self.overlay.add_overlay(self.feedback)
        self.feedback.set_visible(False)
        drop = Gtk.DropTarget.new(Gdk.FileList,Gdk.DragAction.COPY)
        drop.connect('drop',self.drop)
        self.overlay.add_controller(drop)
        motion = Gtk.EventControllerMotion()
        motion.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        motion.connect('motion',lambda c,x,y:self.drawer_motion(y))
        motion.connect('leave',lambda c:self.hide_drawer_later())
        self.overlay.add_controller(motion)
        self.connect('close-request',self.on_close)
        self.update_recent()
        self.update_state()

    # Floating toolbar drawer.

    def build_strip(self):
        self.strip = box(False,1)
        self.strip.add_css_class('tool-strip')
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
        self.strip.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL,margin_start=4,margin_end=4))
        self.sidebar_button = Gtk.ToggleButton(icon_name='sidebar-show-symbolic',tooltip_text='Sidebar (F9)')
        self.sidebar_button.update_property([Gtk.AccessibleProperty.LABEL],['Sidebar'])
        self.sidebar_button.connect('toggled',lambda b:self.toggle_sidebar(b.get_active()) if not self._syncing else None)
        self.strip.append(self.sidebar_button)
        self.strip.append(button('Find',self.show_search,'edit-find-symbolic','Find (Ctrl+F)'))
        self.save_button = button('Save',self.save,'document-save-symbolic','Save (Ctrl+S)')
        self.strip.append(self.save_button)
        self.strip.append(button('Print',self.print,'document-print-symbolic','Print (Ctrl+P)'))
        self.menu = Gtk.MenuButton(icon_name='open-menu-symbolic',tooltip_text='Menu')
        self.menu.update_property([Gtk.AccessibleProperty.LABEL],['Menu'])
        self.build_menu()
        self.strip.append(self.menu)
        self.pin = Gtk.ToggleButton(icon_name='view-pin-symbolic',tooltip_text='Keep toolbar open')
        self.pin.update_property([Gtk.AccessibleProperty.LABEL],['Keep toolbar open'])
        self.pin.set_active(bool(state.setting('toolbar_pinned',False)))
        self.pin.connect('toggled',self.pin_toggled)
        self.strip.append(self.pin)
        # Clicking a tool must not hold focus in the strip and keep the drawer open.
        child = self.strip.get_first_child()
        while child:
            child.set_focus_on_click(False)
            child = child.get_next_sibling()
        for popover_button in (self.settings,self.menu):
            popover_button.get_popover().connect('closed',lambda *_:self.hide_drawer_later())
        focus = Gtk.EventControllerFocus()
        focus.connect('enter',lambda *_:self.show_drawer())
        focus.connect('leave',lambda *_:self.hide_drawer_later())
        self.strip.add_controller(focus)
        self.drawer = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,transition_duration=160,
                                   halign=Gtk.Align.CENTER,valign=Gtk.Align.START,child=self.strip,reveal_child=True)
        self.overlay.add_overlay(self.drawer)
        self.handle = Gtk.Button(halign=Gtk.Align.CENTER,valign=Gtk.Align.START,tooltip_text='Show toolbar (F10)')
        self.handle.add_css_class('drawer-handle')
        self.handle.update_property([Gtk.AccessibleProperty.LABEL],['Show toolbar'])
        self.handle.connect('clicked',lambda *_:self.focus_toolbar())
        self.overlay.add_overlay(self.handle)
        self.drawer.connect('notify::child-revealed',lambda *_:self.update_handle())
        self.update_handle()

    def build_menu(self):
        menu = Gio.Menu()
        files = Gio.Menu()
        files.append('Open…','win.open')
        self.recent_menu = Gio.Menu()
        files.append_submenu('Open recent',self.recent_menu)
        files.append('Save','win.save')
        files.append('Save as…','win.save-as')
        files.append('Print…','win.print')
        menu.append_section(None,files)
        edit = Gio.Menu()
        for label,action in [('Find…','find'),('Undo','undo'),('Redo','redo'),('Delete selected','delete')]:
            edit.append(label,'win.'+action)
        menu.append_section(None,edit)
        view = Gio.Menu()
        for label,action in [('Sidebar','sidebar'),('Rotate left','rotate-left'),('Rotate right','rotate-right')]:
            view.append(label,'win.'+action)
        for mode,label in SPREADS:
            view.append(label,f'win.spread::{mode}')
        view.append('Full screen','win.fullscreen')
        view.append('Presentation','win.present')
        menu.append_section(None,view)
        info = Gio.Menu()
        info.append('Document properties','win.properties')
        info.append('Help','win.help')
        menu.append_section(None,info)
        self.menu.set_menu_model(menu)

    def update_recent(self):
        self.recent_menu.remove_all()
        paths = recent_pdfs()
        for path in paths:
            item = Gio.MenuItem.new(Path(path).name,None)
            item.set_action_and_target_value('win.open-recent',GLib.Variant.new_string(path))
            self.recent_menu.append_item(item)
        while child := self.recent_list.get_first_child():
            self.recent_list.remove(child)
        if paths:
            heading = Gtk.Label(label='Recent',xalign=0,margin_top=16)
            heading.add_css_class('dim-label')
            self.recent_list.append(heading)
        for path in paths[:6]:
            b = Gtk.Button(tooltip_text=path)
            b.add_css_class('flat')
            b.add_css_class('recent')
            b.set_child(Gtk.Label(label=Path(path).name,xalign=0,ellipsize=Pango.EllipsizeMode.MIDDLE,max_width_chars=48))
            b.connect('clicked',lambda _,p=path:self.load(p))
            self.recent_list.append(b)

    def pin_toggled(self,pin):
        state.set_setting('toolbar_pinned',pin.get_active())
        if pin.get_active():
            self.show_drawer()
        else:
            self.hide_drawer_later()

    def drawer_wanted(self):
        """Whether the toolbar must stay open regardless of the pointer."""
        return (not self.document or self.pin.get_active() or self.menu.get_active() or self.settings.get_active()
                or self.strip.get_focus_child() is not None)

    def show_drawer(self):
        if self.presenting:
            return
        if self._drawer_timer:
            GLib.source_remove(self._drawer_timer)
            self._drawer_timer = None
        self.drawer.set_reveal_child(True)
        self.update_handle()

    def hide_drawer_later(self,delay=600):
        if self._drawer_timer:
            GLib.source_remove(self._drawer_timer)
        def hide():
            self._drawer_timer = None
            if not self.drawer_wanted():
                self.drawer.set_reveal_child(False)
                self.update_handle()
            return False
        self._drawer_timer = GLib.timeout_add(delay,hide)

    def drawer_motion(self,y):
        if self.presenting:
            return
        if y <= EDGE:
            self.show_drawer()
        elif self.drawer.get_reveal_child():
            if y > self.strip.get_height()+48:
                if not self._drawer_timer:
                    self.hide_drawer_later()
            elif self._drawer_timer:
                GLib.source_remove(self._drawer_timer)
                self._drawer_timer = None

    def update_handle(self):
        self.handle.set_visible(bool(self.document) and not self.presenting and not self.drawer.get_reveal_child())

    def focus_toolbar(self):
        self.show_drawer()
        self.tool_buttons[self.canvas.tool].grab_focus()

    # Page indicator, zoom, and view layout.

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
        self.zoom = Gtk.DropDown.new_from_strings(ZOOMS)
        self.zoom.set_selected(4)
        self.zoom.update_property([Gtk.AccessibleProperty.LABEL],['Zoom'])
        self.zoom.connect('notify::selected',self.apply_zoom)
        content.append(self.zoom)
        self.layout_control = Gtk.DropDown.new_from_strings([label for _,label in SPREADS])
        self.layout_control.update_property([Gtk.AccessibleProperty.LABEL],['Page layout'])
        self.layout_control.connect('notify::selected',lambda d,*_:self.actions['spread'].change_state(GLib.Variant.new_string(SPREADS[d.get_selected()][0])) if not self._syncing else None)
        content.append(self.layout_control)
        row = box(False,4)
        row.append(button('Rotate left',lambda:self.canvas.set_rotation(self.canvas.rotation-90),'object-rotate-left-symbolic','Rotate left (Shift+R)'))
        row.append(button('Rotate right',lambda:self.canvas.set_rotation(self.canvas.rotation+90),'object-rotate-right-symbolic','Rotate right (R)'))
        row.append(Gtk.Box(hexpand=True))
        row.append(button('Full screen',self.toggle_fullscreen,'view-fullscreen-symbolic','Full screen (F11)'))
        row.append(button('Presentation',self.start_presentation,'x-office-presentation-symbolic','Presentation (F5)'))
        content.append(row)
        pop.set_child(content)
        self.indicator.set_popover(pop)
        self.overlay.add_overlay(self.indicator)

    def apply_zoom(self,*_,navigate=None):
        if not self.document or self._syncing or self.scroll.get_width() < 1:
            return
        index = self.zoom.get_selected()
        self.zoom_mode = index if index < 2 else None
        if index < 2:
            z = self.canvas.fit_zoom(index,self.scroll.get_width(),self.scroll.get_height())
        else:
            z = [.5,.75,1,1.25,1.5,2,3,4][index-2]
        self.canvas.set_zoom(z)
        if navigate is not None:
            self.canvas._zoom_anchor = (self.scroll.get_hadjustment().get_value(), self.canvas.row_top[navigate]*self.canvas.zoom-72)

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

    def toggle_sidebar(self,show=None):
        show = not self.sidebar_revealer.get_reveal_child() if show is None else show
        self.sidebar_revealer.set_reveal_child(show and not self.presenting)
        self.update_state()

    def toggle_fullscreen(self):
        if self.is_fullscreen():
            self.unfullscreen()
        else:
            self.fullscreen()

    def start_presentation(self):
        if not self.document or self.presenting or not self.canvas.finish_edit():
            return
        self.presenting = self.canvas.presenting = True
        self._was_fullscreen = self.is_fullscreen()
        self._had_sidebar = self.sidebar_revealer.get_reveal_child()
        self.fullscreen()
        self.close_search()
        self.drawer.set_reveal_child(False)
        self.sidebar_revealer.set_reveal_child(False)
        for widget in (self.help,self.feedback):
            widget.set_visible(False)
        for widget in (self.overlay,self.scroll):
            widget.add_css_class('presenting')
        self.scroll.set_policy(Gtk.PolicyType.EXTERNAL,Gtk.PolicyType.EXTERNAL)
        self.canvas.set_cursor('default')
        self.canvas.area.grab_focus()
        self.update_state()
        self.canvas.area.queue_draw()

    def stop_presentation(self):
        if not self.presenting:
            return
        self.presenting = self.canvas.presenting = False
        if not self._was_fullscreen:
            self.unfullscreen()
        for widget in (self.overlay,self.scroll):
            widget.remove_css_class('presenting')
        self.scroll.set_policy(Gtk.PolicyType.AUTOMATIC,Gtk.PolicyType.AUTOMATIC)
        self.sidebar_revealer.set_reveal_child(self._had_sidebar)
        page = self.canvas.page
        GLib.idle_add(lambda:self.canvas.scroll_to_page(page) and False)
        self.show_drawer()
        self.hide_drawer_later(1500)
        self.update_state()

    # Find.

    def build_search(self):
        self.search_box = box(False,4)
        self.search_box.add_css_class('search-panel')
        self.search_box.set_halign(Gtk.Align.END)
        self.search_box.set_valign(Gtk.Align.START)
        self.search_box.set_margin_top(68)
        self.search_box.set_margin_end(16)
        self.search_entry = Gtk.SearchEntry(placeholder_text='Find in document',width_chars=24)
        self.search_entry.update_property([Gtk.AccessibleProperty.LABEL],['Find in document'])
        self.search_entry.connect('search-changed',lambda *_:self.start_search())
        self.search_entry.connect('activate',lambda *_:self.next_match(1))
        self.search_entry.connect('next-match',lambda *_:self.next_match(1))
        self.search_entry.connect('previous-match',lambda *_:self.next_match(-1))
        self.search_entry.connect('stop-search',lambda *_:self.close_search())
        keys = Gtk.EventControllerKey()
        keys.connect('key-pressed',lambda c,key,code,mods:key in (Gdk.KEY_Return,Gdk.KEY_KP_Enter) and mods & Gdk.ModifierType.SHIFT_MASK and (self.next_match(-1) or True))
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        self.search_entry.add_controller(keys)
        self.search_count = Gtk.Label(width_chars=9)
        self.search_count.add_css_class('dim-label')
        self.search_box.append(self.search_entry)
        self.search_box.append(self.search_count)
        self.search_box.append(button('Previous match',lambda:self.next_match(-1),'go-up-symbolic','Previous match (Shift+Enter)'))
        self.search_box.append(button('Next match',lambda:self.next_match(1),'go-down-symbolic','Next match (Enter)'))
        self.search_box.append(button('Close find',self.close_search,'window-close-symbolic','Close (Escape)'))
        self.overlay.add_overlay(self.search_box)
        self.search_box.set_visible(False)

    def show_search(self):
        if not self.document or self.presenting:
            return
        self.search_box.set_visible(True)
        self.search_entry.grab_focus()
        selected = self.canvas.selected_text().strip()
        if selected and '\n' not in selected and len(selected) < 100:
            self.search_entry.set_text(selected)
        self.search_entry.select_region(0,-1)

    def close_search(self):
        self.cancel_search()
        self.search_box.set_visible(False)
        self.canvas.search_hits, self.canvas.search_current = [], -1
        self.canvas.area.queue_draw()
        if self.document:
            self.canvas.area.grab_focus()

    def cancel_search(self):
        if self._search_source:
            GLib.source_remove(self._search_source)
            self._search_source = None

    def start_search(self):
        self.cancel_search()
        canvas = self.canvas
        canvas.search_hits, canvas.search_current = [], -1
        query = self.search_entry.get_text().strip()
        self._search = dict(query=query, page=0, origin=canvas.page)
        if query and self.document:
            self._search_source = GLib.idle_add(self.search_step)
        self.update_search_count()
        canvas.area.queue_draw()

    def search_step(self):
        s, canvas = self._search, self.canvas
        end = min(len(self.document.pages), s['page']+16)
        for p in range(s['page'], end):
            canvas.search_hits.extend((p, rect) for rect in self.document.search(p, s['query']))
        s['page'] = end
        done = end >= len(self.document.pages)
        if canvas.search_current < 0 and canvas.search_hits:
            first = next((i for i,(p,_) in enumerate(canvas.search_hits) if p >= s['origin']), None)
            if first is not None or done:
                self.select_match(first or 0)
        self.update_search_count(done)
        canvas.area.queue_draw()
        if done:
            self._search_source = None
        return not done

    def update_search_count(self,done=True):
        hits = self.canvas.search_hits
        if not self.search_entry.get_text().strip():
            text = ''
        elif not hits:
            text = 'No matches' if done else 'Searching…'
        else:
            text = f'{self.canvas.search_current+1} of {len(hits)}' + ('' if done else '+')
        self.search_count.set_text(text)

    def select_match(self,index):
        hits = self.canvas.search_hits
        if not hits:
            return
        self.canvas.search_current = index % len(hits)
        page,rect = hits[self.canvas.search_current]
        self.canvas.reveal(page,rect)
        self.update_search_count(self._search_source is None)

    def next_match(self,step):
        if not self.search_box.get_visible():
            self.show_search()
            return
        if self.canvas.search_hits:
            self.select_match(self.canvas.search_current+step)

    # Help, styles, and state.

    def build_help(self):
        self.help = box(True,12)
        self.help.add_css_class('help-panel')
        self.help.set_halign(Gtk.Align.CENTER)
        self.help.set_valign(Gtk.Align.CENTER)
        self.help.append(Gtk.Label(label='PDF Marker · Controls',xalign=0))
        self.help.append(Gtk.Label(label=(
            'V  Select     F  Fill fields     T  Text\n'
            'D  Draw       K  Tick            X  Cross\n'
            'H  Highlight\n\n'
            'Move to the top edge to show the toolbar · F10\n'
            'Select: drag to select text, click links,\n'
            'notes and fields · Ctrl+C copies · Ctrl+A all\n'
            'Ctrl-click selects marks together\n'
            'Ctrl+C / Ctrl+V copies / pastes marks\n'
            'Drag the text handle to resize\n\n'
            'Scroll through pages · Ctrl+wheel zooms\n'
            'Middle-drag pans · Page Up/Down jumps pages\n'
            'R / Shift+R rotate · F9 sidebar · F11 full screen\n'
            'F5 presentation · Escape leaves it\n\n'
            'Ctrl+Enter finishes text · Escape cancels\n'
            'Ctrl+Z / Ctrl+Shift+Z undo / redo\n'
            'Ctrl+O opens · Ctrl+S saves · Ctrl+Shift+S saves as\n'
            'Ctrl+F finds · Ctrl+P prints · Alt+Enter properties\n'
            '? shows these controls'),xalign=0))
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
        if self._syncing:
            return
        color = self.color.get_rgba()
        color = (color.red,color.green,color.blue)
        doc = self.document
        marks = [doc.marks[i] for i in self.canvas.selected if doc and i < len(doc.marks)]
        if self.canvas.tool == 'highlight' or marks and all(m.kind == 'highlight' for m in marks):
            self.highlight_color = color
        else:
            self.ink = color
        self.canvas.style_selected(color=color)

    def update_state(self):
        if not hasattr(self,'search_box'):
            return
        self._syncing = True
        doc = self.document
        self.empty.set_visible(doc is None)
        self.indicator.set_visible(doc is not None and not self.presenting)
        for name in ('save','save-as','print','find','find-next','find-previous','undo','redo','delete','sidebar',
                     'rotate-left','rotate-right','present','properties','spread'):
            enabled = bool(doc)
            if name == 'undo':
                enabled = bool(doc and (doc.can_undo or self.canvas.pending))
            elif name == 'redo':
                enabled = bool(doc and (doc.can_redo or self.canvas.editor))
            elif name == 'delete':
                enabled = bool(self.canvas.selected or self.canvas.pending or self.canvas.selected_annotation)
            self.actions[name].set_enabled(enabled)
        self.save_button.set_sensitive(doc is not None)
        for tool,b in self.tool_buttons.items():
            b.set_active(self.canvas.tool == tool)
            b.set_sensitive(doc is not None)
        self.sidebar_button.set_active(self.sidebar_revealer.get_reveal_child())
        mark = self.canvas.edit_mark or next((doc.marks[i] for i in sorted(self.canvas.selected) if doc and i<len(doc.marks)),None)
        kind = mark.kind if mark else self.canvas.tool
        self.settings.set_visible(bool(doc and kind in ('text','draw','tick','cross','highlight')))
        self.size_control.set_sensitive(kind in ('text','tick','cross'))
        self.stroke_control.set_sensitive(kind in ('draw','tick','cross'))
        self.size_control.set_value(mark.size if mark and kind=='text' else mark.rect[2]-mark.rect[0] if mark and kind in ('tick','cross') else self.size)
        self.stroke_control.set_value(mark.width if mark and kind != 'highlight' else self.stroke)
        rgba = Gdk.RGBA()
        rgba.red,rgba.green,rgba.blue = mark.color if mark else self.highlight_color if kind == 'highlight' else self.ink
        rgba.alpha = 1
        self.color.set_rgba(rgba)
        self.actions['spread'].set_state(GLib.Variant.new_string(self.canvas.spread))
        self.layout_control.set_selected([m for m,_ in SPREADS].index(self.canvas.spread))
        if doc:
            self.indicator.set_label(f'{self.canvas.page+1} / {len(doc.pages)} · {round(self.canvas.zoom*100)}%')
            self.page_number.set_range(1,len(doc.pages))
            self.page_number.set_value(self.canvas.page+1)
            self.pages.select_row(self.pages.get_row_at_index(self.canvas.page))
            self.canvas.area.update_property([Gtk.AccessibleProperty.LABEL,Gtk.AccessibleProperty.DESCRIPTION],
                [f'Page {self.canvas.page+1} of {len(doc.pages)}','Press F9 and choose Page text to read this page.'])
            self.sidebar.update_text()
        self.update_handle()
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
            self.canvas.selected_annotation = None
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
        dialog.set_default_response(buttons[-1][1])
        def response(d,result):
            if callback(result) is not False:
                d.destroy()
        dialog.connect('response',response)
        dialog.present()
        return dialog

    # Opening, saving, and printing.

    def confirm_discard(self,continuation):
        if not self.canvas.finish_edit():
            return
        if not self.document or not self.document.dirty:
            continuation()
            return
        def response(result):
            if result == Gtk.ResponseType.REJECT:
                continuation()
            elif result == Gtk.ResponseType.ACCEPT:
                self.save(then=continuation)
        self.dialog('Unsaved changes',Gtk.Label(label=f'Save changes to “{self.document.path.name}” before closing?'),response,
                    (('Cancel',Gtk.ResponseType.CANCEL),('Discard',Gtk.ResponseType.REJECT),('Save…',Gtk.ResponseType.ACCEPT)))

    def load(self,path):
        self.confirm_discard(lambda:self._load(path))

    def _load(self,path,password=''):
        try:
            document = Document(path,password)
            document.render(0,.5,include_marks=False)
        except PasswordRequired:
            entry = Gtk.PasswordEntry(show_peek_icon=True,activates_default=True)
            self.dialog('PDF password',entry,lambda result:self._load(path,entry.get_text()) if result==Gtk.ResponseType.OK else None)
            return
        except Exception as error:
            self.error('Could not open PDF',error)
            return
        self.remember_position()
        self.close_search()
        if self.document:
            self.document.close()
        self.document = document
        self.feedback.set_visible(False)
        self.canvas.reset_document()
        while row := self.pages.get_row_at_index(0):
            self.pages.remove(row)
        for p in range(len(document.pages)):
            self.pages.append(Gtk.Label(label=f'Page {p+1}',xalign=0,margin_top=6,margin_bottom=6,margin_start=8,margin_end=8))
        self.canvas.set_tool('select')
        self.sidebar.load(document)
        self.restore_position()
        add_recent(document.path)
        self.update_recent()
        if document.notice:
            self.notify(document.notice)
        self.show_drawer()
        self.hide_drawer_later(1500)
        self.update_state()

    def remember_position(self):
        doc, canvas = self.document, self.canvas
        if not doc or not canvas.origins:
            return
        offset = self.scroll.get_vadjustment().get_value()/canvas.zoom - canvas.row_top[canvas.page]
        state.remember_position(doc.path, dict(page=canvas.page, offset=offset, zoom=canvas.zoom,
                                zoom_index=self.zoom.get_selected(), zoom_mode=self.zoom_mode,
                                rotation=canvas.rotation, spread=canvas.spread))

    def restore_position(self):
        canvas = self.canvas
        entry = state.position(self.document.path) or {}
        page = entry.get('page',0)
        if not isinstance(page,int) or not 0 <= page < len(self.document.pages):
            entry, page = {}, 0
        canvas.rotation = entry.get('rotation',0) if entry.get('rotation') in (0,90,180,270) else 0
        canvas.spread = entry.get('spread','none') if entry.get('spread') in dict(SPREADS) else 'none'
        canvas.layout()
        canvas.page = page
        index = entry.get('zoom_index',0)
        index = index if isinstance(index,int) and 0 <= index < len(ZOOMS) else 0
        self._syncing = True
        self.zoom.set_selected(index)
        self._syncing = False
        if entry and entry.get('zoom_mode') is None and isinstance(entry.get('zoom'),(int,float)):
            self.zoom_mode = None
            canvas.set_zoom(entry['zoom'])
        else:
            self.zoom_mode = index if index < 2 else None
            self.apply_zoom()
        offset = entry.get('offset',-72/canvas.zoom) if isinstance(entry.get('offset',0),(int,float)) else 0
        canvas._zoom_anchor = (self.scroll.get_hadjustment().get_value(),(canvas.row_top[page]+offset)*canvas.zoom)
        GLib.idle_add(canvas.apply_anchor)

    def pdf_filters(self):
        filters = Gio.ListStore.new(Gtk.FileFilter)
        pdf_filter = Gtk.FileFilter(name='PDF documents')
        pdf_filter.add_mime_type('application/pdf')
        pdf_filter.add_suffix('pdf')
        filters.append(pdf_filter)
        return filters

    def open_dialog(self):
        dialog = Gtk.FileDialog(title='Open PDF',filters=self.pdf_filters())
        if self.document:
            dialog.set_initial_folder(Gio.File.new_for_path(str(self.document.path.parent)))
        def done(d,result):
            try:
                file = d.open_finish(result)
                if file.get_path():
                    self.load(file.get_path())
            except GLib.Error as error:
                if not error.matches(Gtk.dialog_error_quark(),Gtk.DialogError.DISMISSED):
                    self.error('Could not open PDF',error)
        dialog.open(self,None,done)

    def save(self,then=None):
        """Save to the file chosen earlier in this session, or ask where to save."""
        if not self.document or not self.canvas.finish_edit():
            return False
        if self.document.saved_path:
            return self.save_to(self.document.saved_path,then)
        self.save_as(then)
        return False

    def save_as(self,then=None):
        if not self.document or not self.canvas.finish_edit():
            return
        dialog = Gtk.FileDialog(title='Save PDF',filters=self.pdf_filters(),
                                initial_file=Gio.File.new_for_path(str(self.document.path)))
        def done(d,result):
            try:
                file = d.save_finish(result)
            except GLib.Error as error:
                if not error.matches(Gtk.dialog_error_quark(),Gtk.DialogError.DISMISSED):
                    self.error('Could not save PDF',error)
                return
            path = file.get_path()
            if not path:
                self.error('Could not save PDF','Choose a local folder.')
            else:
                self.save_to(path,then)
        dialog.save(self,None,done)

    def save_to(self,path,then=None):
        if not self.document or not self.canvas.finish_edit():
            return False
        try:
            saved = self.document.save(path)
        except Exception as error:
            self.error('Could not save PDF',error)
            return False
        add_recent(saved)
        self.update_recent()
        self.remember_position()
        self.notify(f'Saved {saved.name}')
        self.update_state()
        if then:
            then()
        return True

    def print(self):
        if not self.document or not self.canvas.finish_edit():
            return
        try:
            print_document(self,self.document,lambda error:self.error('Could not print',error))
        except Exception as error:
            self.error('Could not print',error)

    def save_attachment(self,attachment):
        dialog = Gtk.FileDialog(title='Save attachment',initial_name=Path(attachment.name).name,
                                initial_folder=Gio.File.new_for_path(str(self.document.path.parent)))
        document = self.document
        def done(d,result):
            try:
                path = d.save_finish(result).get_path()
                Path(path).write_bytes(document.attachment_bytes(attachment))
                self.notify(f'Saved {Path(path).name}')
            except GLib.Error as error:
                if not error.matches(Gtk.dialog_error_quark(),Gtk.DialogError.DISMISSED):
                    self.error('Could not save attachment',error)
            except Exception as error:
                self.error('Could not save attachment',error)
        dialog.save(self,None,done)

    def show_properties(self):
        if not self.document:
            return
        grid = Gtk.Grid(column_spacing=16,row_spacing=6)
        for row,(name,value) in enumerate(self.document.properties()):
            key = Gtk.Label(label=name,xalign=1,yalign=0)
            key.add_css_class('dim-label')
            grid.attach(key,0,row,1,1)
            grid.attach(Gtk.Label(label=value,xalign=0,selectable=True,wrap=True,max_width_chars=60),1,row,1,1)
        self.dialog('Document properties',grid,lambda result:None,(('Close',Gtk.ResponseType.CLOSE),))

    # Links, notes, and menus.

    def launch_uri(self,uri):
        def done(launcher,result):
            try:
                launcher.launch_finish(result)
            except GLib.Error as error:
                if not error.matches(Gtk.dialog_error_quark(),Gtk.DialogError.DISMISSED):
                    self.error('Could not open link',error)
        Gtk.UriLauncher.new(uri).launch(self,None,done)

    def follow_link(self,link):
        canvas = self.canvas
        if link.kind == 'page':
            canvas.scroll_to_page(link.dest_page,link.dest_y)
        elif link.kind == 'named':
            last = len(self.document.pages)-1
            pages = {'nextpage':canvas.page+1,'prevpage':canvas.page-1,'firstpage':0,'lastpage':last}
            if link.target.lower() in pages:
                canvas.scroll_to_page(max(0,min(last,pages[link.target.lower()])))
        elif link.kind == 'uri':
            uri = link.target.strip()
            if uri.lower().startswith('www.'):
                uri = 'https://'+uri
            if urlparse(uri).scheme.lower() in SAFE_SCHEMES:
                self.launch_uri(uri)
            else:
                self.notify(f'Blocked a link to {uri[:120]}. Only web and email links open.')
        elif link.kind == 'file':
            target = Path(link.target).expanduser()
            target = target if target.is_absolute() else self.document.path.parent/target
            if target.suffix.lower() == '.pdf' and target.is_file():
                self.load(str(target))
            else:
                self.notify('This link tries to open another file or program. PDF Marker only follows links to other PDFs.')

    def popover_at(self,x,y,child):
        pop = Gtk.Popover()
        pop.set_parent(self.canvas.area)
        rect = Gdk.Rectangle()
        rect.x,rect.y,rect.width,rect.height = round(x),round(y),1,1
        pop.set_pointing_to(rect)
        pop.set_child(child)
        pop.connect('closed',lambda p:GLib.idle_add(p.unparent))
        pop.popup()
        return pop

    def mark_menu(self,index,x,y):
        content = box(True,4)
        pop = None
        def run(fn):
            pop.popdown()
            fn()
        mark = self.document.marks[index]
        if mark.kind == 'text':
            content.append(button('Edit text',lambda:run(lambda:self.canvas.begin_text(mark,index))))
        content.append(button('Delete highlight' if mark.kind == 'highlight' and len(self.canvas.selected) == 1 else 'Delete selected',
                              lambda:run(self.canvas.delete_selected)))
        pop = self.popover_at(x,y,content)

    def selection_menu(self,x,y):
        content = box(True,4)
        pop = None
        def run(fn):
            pop.popdown()
            fn()
        content.append(button('Copy',lambda:run(self.canvas.copy_text)))
        content.append(button('Highlight',lambda:run(self.canvas.highlight_selection)))
        pop = self.popover_at(x,y,content)

    def annotation_popover(self,annotation,x,y):
        content = box(True,8)
        for setter in ('set_margin_top','set_margin_bottom','set_margin_start','set_margin_end'):
            getattr(content,setter)(4)
        header = ' · '.join(v for v in (annotation.label,annotation.author,annotation.modified) if v)
        content.append(Gtk.Label(label=header,xalign=0,ellipsize=Pango.EllipsizeMode.END,max_width_chars=44))
        editor = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR,top_margin=4,bottom_margin=4,left_margin=4,right_margin=4)
        editor.update_property([Gtk.AccessibleProperty.LABEL],[f'{annotation.label} text'])
        original = self.document.annotation_content(annotation) or ''
        editor.get_buffer().set_text(original)
        content.append(Gtk.ScrolledWindow(child=editor,min_content_width=300,min_content_height=90,max_content_height=260,propagate_natural_height=True))
        row = box(False,6)
        row.set_halign(Gtk.Align.END)
        pop = None
        def text():
            b = editor.get_buffer()
            return b.get_text(b.get_start_iter(),b.get_end_iter(),True)
        def delete():
            pop.popdown()
            self.canvas.selected_annotation = None
            self.perform(lambda:self.document.delete_annotation(annotation))
        def save():
            pop.popdown()
            self.perform(lambda:self.document.edit_annotation(annotation,text()))
        save_button = button('Save',save)
        save_button.add_css_class('suggested-action')
        save_button.set_sensitive(False)
        editor.get_buffer().connect('changed',lambda *_:save_button.set_sensitive(text() != original))
        row.append(button('Delete',delete))
        row.append(save_button)
        content.append(row)
        pop = self.popover_at(x,y,content)
        return pop

    def notify(self,message):
        if self._feedback_timer:
            GLib.source_remove(self._feedback_timer)
        self.feedback_label.set_text(message)
        self.feedback.set_visible(not self.presenting)
        def hide():
            self.feedback.set_visible(False)
            self._feedback_timer = None
            return False
        self._feedback_timer = GLib.timeout_add_seconds(10,hide)

    def error(self,title,error):
        self.notify(f'{title}: {error}')

    def on_close(self,*_):
        if self._closing:
            return False
        def close():
            self.remember_position()
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

    def fill_field(self,field):
        if field.flags & 1:
            self.notify('This field is read-only.')
            return
        value = self.document.value_of(field)
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
        elif field.kind == pdf.PDF_WIDGET_TYPE_SIGNATURE:
            self.notify('Use Draw to add a handwritten signature at this position.')
            return
        else:
            self.notify('This button runs a script, which PDF Marker does not do.')
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
    parser = argparse.ArgumentParser(prog='pdf-marker',description='View, fill and mark PDF documents.')
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
