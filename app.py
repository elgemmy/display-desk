#!/usr/bin/python3
"""Display Desk — a small, native display layout editor for Niri."""
import copy
import math
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango
from backend import Backend, logical_size, lid_state

APP_ID = 'com.elgemmy.DisplayDesk'
TRANSFORMS = ['normal', '90', '180', '270', 'flipped', 'flipped-90', 'flipped-180', 'flipped-270']
TRANSFORM_LABELS = ['Landscape', 'Portrait · left', 'Upside down', 'Portrait · right', 'Mirror', 'Mirror · left', 'Mirror · inverted', 'Mirror · right']
COLORS = [(0.48, 0.72, 1), (0.61, 0.85, 0.68), (0.85, 0.68, 0.97), (1, 0.75, 0.49)]


def dimensions(d):
    return logical_size(d)


def title(d):
    if d.get('internal'):
        return 'Built-in display'
    return (d.get('model') or d.get('make') or d['connector']).strip()


def mode_label(m):
    return f"{m['width']} × {m['height']}  ·  {m['refresh_rate'] / 1000:g} Hz"


def button(label, callback, css=None):
    b = Gtk.Button(label=label)
    b.connect('clicked', callback)
    if css:
        b.add_css_class(css)
    return b


def text_label(text, css=None, xalign=0):
    label = Gtk.Label(label=text, xalign=xalign)
    if css:
        label.add_css_class(css)
    return label


def ellipsized_factory():
    factory=Gtk.SignalListItemFactory()
    def setup(_factory,item):
        label=Gtk.Label(xalign=0,hexpand=True,ellipsize=Pango.EllipsizeMode.END)
        label.set_max_width_chars(24)
        item.set_child(label)
    def bind(_factory,item):
        item.get_child().set_text(item.get_item().get_string())
    factory.connect('setup',setup)
    factory.connect('bind',bind)
    return factory


def append_flow_layout(flow, widget):
    """FlowBox is layout only; its controls own pointer and keyboard focus."""
    flow.append(widget)
    wrapper=widget.get_parent()
    wrapper.set_focusable(False)
    wrapper.set_focus_on_click(False)


class LayoutCanvas(Gtk.Overlay):
    """Cards are overlays so their last size never constrains a smaller tile."""
    def __init__(self, window):
        super().__init__(hexpand=True, vexpand=True)
        self.set_child(Gtk.Box())
        self.fixed=Gtk.Fixed(hexpand=True,vexpand=True)
        self.add_overlay(self.fixed)
        self.set_measure_overlay(self.fixed,False)
        self.set_clip_overlay(self.fixed,True)
        self.window = window
        self.factor = 1
        self.dragging = None
        self.cards = {}
        self.signature = None
        self.set_size_request(180, 180)
        self.set_focusable(True)
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.add_css_class('display-canvas')
        key = Gtk.EventControllerKey.new()
        key.connect('key-pressed', self.key_pressed)
        self.add_controller(key)
        gesture=Gtk.GestureDrag.new()
        gesture.connect('drag-begin',self.drag_begin)
        gesture.connect('drag-update',self.drag_update)
        gesture.connect('drag-end',self.drag_end)
        self.add_controller(gesture)
        GLib.timeout_add(100, self.layout_cards)

    def put(self, child, x, y):
        self.fixed.put(child,x,y)

    def move(self, child, x, y):
        self.fixed.move(child,x,y)

    def remove(self, child):
        self.fixed.remove(child)

    def layout_cards(self):
        if not self.get_mapped():
            return True
        width, height = self.get_width(), self.get_height()
        displays = self.window.displays
        signature = (width,height, self.window.selected, str(displays))
        if self.signature == signature:
            return True
        self.signature=signature
        active=[d for d in displays if d['enabled']]
        if not self.dragging and active:
            minx,miny=min(d['x'] for d in active),min(d['y'] for d in active)
            maxx=max(d['x']+dimensions(d)[0] for d in active)
            maxy=max(d['y']+dimensions(d)[1] for d in active)
            self.factor=min((width-80)/max(1,maxx-minx),(height-70)/max(1,maxy-miny),0.3)
            self.origin=((width-(maxx-minx)*self.factor)/2-minx*self.factor,
                         (height-(maxy-miny)*self.factor)/2-miny*self.factor)
        for connector in list(self.cards):
            if not any(d['connector']==connector and d['enabled'] for d in displays):
                self.remove(self.cards.pop(connector))
        for i,d in enumerate(displays):
            if not d['enabled']:
                continue
            connector=d['connector']
            if connector not in self.cards:
                card=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=4)
                card.add_css_class('monitor-card')
                card.add_css_class('monitor-'+str(i%4))
                card.set_cursor_from_name('grab')
                card.set_overflow(Gtk.Overflow.HIDDEN)
                number=text_label(str(i+1),'monitor-number')
                number.set_margin_top(10)
                number.set_margin_start(12)
                card.append(number)
                label=text_label(title(d),'heading',0.5)
                label.set_ellipsize(Pango.EllipsizeMode.END)
                label.set_margin_start(8)
                label.set_margin_end(8)
                label.set_vexpand(True)
                label.set_valign(Gtk.Align.CENTER)
                card.append(label)
                port=text_label(connector,'dim-label',0.5)
                card.append(port)
                detail=text_label('Built-in display' if d.get('internal') else 'External display','caption',0.5)
                detail.set_margin_bottom(12)
                detail.set_vexpand(True)
                detail.set_valign(Gtk.Align.START)
                card.append(detail)
                card.parts=(number,label,port,detail)
                card.set_tooltip_text(f"{i+1} · {title(d)} · {connector}")
                self.put(card,0,0)
                self.cards[connector]=card
            card=self.cards[connector]
            if self.window.selected==connector:
                card.add_css_class('selected')
            else:
                card.remove_css_class('selected')
            w,h=dimensions(d)
            card_width,card_height=round(w*self.factor),round(h*self.factor)
            # Reduce the card's content at small sizes rather than expanding
            # its real display footprint and visually overlapping its neighbor.
            number,label,port,detail=card.parts
            compact=card_height<65 or card_width<95
            if compact:
                card.add_css_class('compact')
            else:
                card.remove_css_class('compact')
            number.set_margin_start(8 if compact else 12)
            number.set_margin_top(6 if compact else 10)
            label.set_visible(card_height>=65 and card_width>=95)
            port.set_visible(card_height>=110 and card_width>=100)
            detail.set_visible(card_height>=155 and card_width>=120)
            label.set_margin_bottom(8 if not port.get_visible() else 0)
            port.set_margin_bottom(8 if not detail.get_visible() else 0)
            card.set_size_request(max(36,card_width),max(36,card_height))
            self.move(card,self.origin[0]+d['x']*self.factor,self.origin[1]+d['y']*self.factor)
        return True

    def drag_begin(self,gesture,x,y):
        if self.window.busy or self.window.transaction:
            return
        connector=None
        for d in reversed(self.window.displays):
            if not d['enabled']:
                continue
            w,h=dimensions(d)
            bx,by=self.origin[0]+d['x']*self.factor,self.origin[1]+d['y']*self.factor
            if bx<=x<=bx+w*self.factor and by<=y<=by+h*self.factor:
                connector=d['connector']
                break
        if connector is None:
            return
        self.grab_focus()
        self.window.select(connector)
        self.dragging=self.window.selected_display()
        self.start=(self.dragging['x'],self.dragging['y'])
        self.cards[connector].set_cursor_from_name('grabbing')

    def drag_update(self,gesture,dx,dy):
        if self.dragging:
            self.dragging['x']=round(self.start[0]+dx/self.factor)
            self.dragging['y']=round(self.start[1]+dy/self.factor)
            self.layout_cards()

    def drag_end(self,gesture,dx,dy):
        if not self.dragging:
            return
        d=self.dragging
        w,h=dimensions(d)
        threshold=16/self.factor
        xs,ys=[],[]
        for other in self.window.displays:
            if other is d or not other['enabled']:
                continue
            ow,oh=dimensions(other)
            xs += [other['x'],other['x']+ow,other['x']-w,other['x']+ow-w,other['x']+(ow-w)/2]
            ys += [other['y'],other['y']+oh,other['y']-h,other['y']+oh-h,other['y']+(oh-h)/2]
        for axis,values in [('x',xs),('y',ys)]:
            if values:
                nearest=min(values,key=lambda v:abs(v-d[axis]))
                if abs(nearest-d[axis])<threshold:
                    d[axis]=round(nearest)
        self.dragging=None
        self.cards[d['connector']].set_cursor_from_name('grab')
        self.window.changed()
        self.window.update_controls()
        self.layout_cards()

    def key_pressed(self,controller,keyval,keycode,state):
        d=self.window.selected_display()
        keys={Gdk.KEY_Left:(-1,0),Gdk.KEY_h:(-1,0),Gdk.KEY_Right:(1,0),Gdk.KEY_l:(1,0),Gdk.KEY_Up:(0,-1),Gdk.KEY_k:(0,-1),Gdk.KEY_Down:(0,1),Gdk.KEY_j:(0,1)}
        if d and keyval in keys and not self.window.busy and not self.window.transaction:
            step=1 if state & Gdk.ModifierType.SHIFT_MASK else 10
            dx,dy=keys[keyval]
            d['x']+=dx*step
            d['y']+=dy*step
            self.window.changed()
            self.window.update_controls()
            return True
        return False


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title='Display Desk')
        self.set_size_request(360,280)
        self.backend = Backend()
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.displays = []
        self.baseline = []
        self.selected = None
        self.busy = False
        self.dirty = False
        self.updating = False
        self.transaction = None
        self.profile_records = []
        self.connect('close-request',self.close_request)
        self.toast_overlay = Adw.ToastOverlay()
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.toast_overlay.set_child(root)
        self.set_content(self.toast_overlay)
        header = Adw.HeaderBar()
        window_title=text_label('Display Desk','heading')
        window_title.set_ellipsize(Pango.EllipsizeMode.END)
        header.set_title_widget(window_title)
        self.sidebar_button=Gtk.ToggleButton(icon_name='sidebar-show-symbolic',tooltip_text='Show saved setups',visible=False)
        header.pack_start(self.sidebar_button)
        refresh = Gtk.Button(icon_name='view-refresh-symbolic',tooltip_text='Reload connected displays')
        refresh.connect('clicked',lambda *_:self.reload())
        header.pack_start(refresh)
        self.auto = Gtk.Switch(valign=Gtk.Align.CENTER,tooltip_text='Automatically restore matching display setups')
        self.auto.connect('notify::active',self.auto_changed)
        auto_box = Gtk.Box(spacing=8)
        auto_label=text_label('Auto restore','dim-label')
        auto_box.append(auto_label)
        auto_box.append(self.auto)
        header.pack_end(auto_box)
        root.append(header)

        self.split=Adw.OverlaySplitView(vexpand=True)
        self.split.set_min_sidebar_width(260)
        self.split.set_max_sidebar_width(290)
        self.split.set_sidebar_width_unit(Adw.LengthUnit.PX)
        self.split.set_sidebar_width_fraction(.26)
        self.split.set_show_sidebar(True)
        self.sidebar_button.connect('toggled',lambda b:self.split.set_show_sidebar(b.get_active()))
        self.split.connect('notify::show-sidebar',lambda s,*_:self.sidebar_button.set_active(s.get_show_sidebar()))
        self.sidebar_button.set_active(True)
        root.append(self.split)
        sidebar_surface=Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sidebar_surface.add_css_class('sidebar-surface')
        sidebar_scroll=Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,vexpand=True)
        sidebar_surface.append(sidebar_scroll)
        sidebar=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=16,vexpand=True)
        for side in ('top','bottom','start','end'):
            getattr(sidebar,f'set_margin_{side}')(20)
        sidebar_scroll.set_child(sidebar)
        sidebar.append(text_label('YOUR SETUPS','caption-heading'))
        sidebar.append(text_label('Saved on this machine','dim-label'))
        self.profiles_list=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=10)
        sidebar.append(self.profiles_list)
        sidebar.append(Gtk.Box(vexpand=True))
        hint=text_label('Save each desk once. Layouts follow the monitors, even when ports change.','dim-label')
        hint.set_wrap(True)
        hint.set_max_width_chars(25)
        sidebar.append(hint)
        self.split.set_sidebar(sidebar_surface)

        content=Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.split.set_content(content)
        self.main_scroll=Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,vexpand=True)
        self.main_scroll.set_propagate_natural_height(False)
        self.main_scroll.set_propagate_natural_width(False)
        content.append(self.main_scroll)
        main=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=16,hexpand=True,vexpand=True)
        for side in ('top','bottom','start','end'):
            getattr(main,f'set_margin_{side}')(20)
        self.main_scroll.set_child(main)
        heading=Gtk.Box(spacing=8)
        heading_title=text_label('Arrange your displays','title-2')
        heading_title.set_wrap(True)
        heading.append(heading_title)
        self.connection_label=text_label('Connecting…','dim-label')
        self.connection_label.set_hexpand(True)
        self.connection_label.set_halign(Gtk.Align.END)
        self.connection_label.set_wrap(True)
        heading.append(self.connection_label)
        main.append(heading)
        intro=text_label('Drag screens to match your desk. Edges and centers snap together.','dim-label')
        intro.set_wrap(True)
        main.append(intro)
        self.canvas=LayoutCanvas(self)
        canvas_frame=Gtk.Frame(vexpand=True)
        canvas_frame.add_css_class('canvas-frame')
        canvas_frame.set_child(self.canvas)
        main.append(canvas_frame)
        tools=Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,homogeneous=True,min_children_per_line=1,max_children_per_line=4,column_spacing=8,row_spacing=8)
        tools.set_activate_on_single_click(False)
        tools.set_focusable(False)
        tools.add_css_class('arrange-tools')
        for label,kind in [('Laptop below','below'),('Side by side','row'),('Align tops','top')]:
            append_flow_layout(tools,button(label,lambda _,k=kind:self.arrange(k)))
        self.reset_button=button('Reset',lambda *_:self.reset())
        append_flow_layout(tools,self.reset_button)
        main.append(tools)
        self.display_picker=Gtk.DropDown(hexpand=True)
        self.display_picker.set_factory(ellipsized_factory())
        self.display_picker.connect('notify::selected',self.picked)
        main.append(self.display_picker)
        self.controls=Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,homogeneous=True,min_children_per_line=1,max_children_per_line=3,column_spacing=12,row_spacing=12)
        self.controls.set_activate_on_single_click(False)
        self.controls.set_focusable(False)
        self.controls.add_css_class('display-settings')
        main.append(self.controls)
        self.enabled=Gtk.Switch(halign=Gtk.Align.START,valign=Gtk.Align.CENTER)
        self.enabled.connect('notify::active',self.edit,'enabled')
        enabled_control=Gtk.Box(spacing=12,valign=Gtk.Align.CENTER,height_request=36)
        self.enabled_note=text_label('On','dim-label')
        enabled_control.append(self.enabled)
        enabled_control.append(self.enabled_note)
        self.mode=Gtk.DropDown(hexpand=True)
        self.mode.set_factory(ellipsized_factory())
        self.mode.connect('notify::selected',self.edit,'mode')
        self.scale=Gtk.SpinButton.new_with_range(0.5,4,0.05)
        self.scale.set_digits(2)
        self.scale.connect('value-changed',self.edit,'scale')
        self.rotation=Gtk.DropDown.new_from_strings(TRANSFORM_LABELS)
        self.rotation.set_factory(ellipsized_factory())
        self.rotation.connect('notify::selected',self.edit,'transform')
        self.x=Gtk.SpinButton.new_with_range(-32768,32768,1)
        self.y=Gtk.SpinButton.new_with_range(-32768,32768,1)
        self.x.connect('value-changed',self.edit,'x')
        self.y.connect('value-changed',self.edit,'y')
        for label,widget in [('Display enabled',enabled_control),('Resolution / refresh rate',self.mode),('Scale',self.scale),('Rotation',self.rotation),('Horizontal position',self.x),('Vertical position',self.y)]:
            field=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=8,hexpand=True)
            caption=text_label(label,'caption-heading')
            caption.set_wrap(True)
            field.append(caption)
            widget.set_hexpand(True)
            field.append(widget)
            append_flow_layout(self.controls,field)
        self.info=text_label('','dim-label')
        self.info.set_wrap(True)
        self.info.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        main.append(self.info)

        content.append(Gtk.Separator())
        actions=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=8)
        for side in ('top','bottom','start','end'):
            getattr(actions,f'set_margin_{side}')(16 if side in ('top','bottom') else 20)
        content.append(actions)
        footer=Gtk.Box(spacing=10)
        self.profile_name=Gtk.Entry(placeholder_text='Name this setup',hexpand=True)
        self.profile_name.set_width_chars(12)
        self.profile_name.set_max_length(80)
        footer.append(self.profile_name)
        self.apply_button=button('Test & save',lambda *_:self.apply(),'suggested-action')
        footer.append(self.apply_button)
        actions.append(footer)
        self.status=text_label('Changes are previewed for 20 seconds before saving.','dim-label')
        self.status.set_wrap(True)
        actions.append(self.status)
        narrow=Adw.Breakpoint.new(Adw.BreakpointCondition.parse('max-width: 1000px'))
        narrow.add_setter(self.split,'collapsed',True)
        narrow.add_setter(self.split,'show-sidebar',False)
        narrow.add_setter(self.sidebar_button,'visible',True)
        self.add_breakpoint(narrow)
        compact=Adw.Breakpoint.new(Adw.BreakpointCondition.parse('max-width: 600px'))
        compact.add_setter(self.split,'collapsed',True)
        compact.add_setter(self.split,'show-sidebar',False)
        compact.add_setter(self.sidebar_button,'visible',True)
        compact.add_setter(auto_label,'visible',False)
        compact.add_setter(heading,'orientation',Gtk.Orientation.VERTICAL)
        compact.add_setter(self.connection_label,'halign',Gtk.Align.START)
        compact.add_setter(footer,'orientation',Gtk.Orientation.VERTICAL)
        self.add_breakpoint(compact)
        self.load_initial()
        GLib.timeout_add_seconds(5,self.check_connection)

    def task(self, work, done=None, quiet=False):
        if self.busy:
            return
        self.busy=True
        self.apply_button.set_sensitive(False)
        if not quiet:
            self.status.set_text('Working…')
        future=self.pool.submit(work)
        def finish():
            self.busy=False
            self.apply_button.set_sensitive(True)
            try:
                value=future.result()
                if done:
                    done(value)
            except Exception as exc:
                self.status.set_text(str(exc))
                self.toast(str(exc))
            return False
        future.add_done_callback(lambda _:GLib.idle_add(finish))

    def toast(self,message):
        self.toast_overlay.add_toast(Adw.Toast(title=message[:240],timeout=6))

    def load_initial(self):
        def work():
            return self.backend.query(),self.backend.profiles(),self.backend.get_auto()
        def done(result):
            displays,profiles,auto=result
            self.set_displays(displays)
            self.update_profiles(profiles)
            matching=sorted((p for p in profiles if p.get('matches')),key=lambda p:p.get('saved_at',0),reverse=True)
            if matching:
                self.profile_name.set_text(matching[0]['name'])
            self.updating=True
            self.auto.set_active(auto)
            self.updating=False
        self.task(work,done)

    def reload(self):
        if self.transaction:
            return
        if self.dirty:
            dialog=Adw.AlertDialog(heading='Reload connected displays?',body='This discards your unsaved arrangement.')
            dialog.add_response('cancel','Keep editing')
            dialog.add_response('reload','Reload')
            dialog.set_response_appearance('reload',Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.connect('response',lambda _,response:self.task(self.backend.query,self.set_displays) if response=='reload' else None)
            dialog.present(self)
        else:
            self.task(self.backend.query,self.set_displays)

    def set_displays(self,displays):
        self.displays=copy.deepcopy(displays)
        self.baseline=copy.deepcopy(displays)
        self.dirty=False
        self.updating=True
        self.display_picker.set_model(Gtk.StringList.new([f"{i+1}   {title(d)}  ·  {d['connector']}" for i,d in enumerate(displays)]))
        self.updating=False
        if not displays:
            self.status.set_text('No connected displays found.')
            return
        self.selected=self.selected if any(d['connector']==self.selected for d in displays) else displays[0]['connector']
        self.select(self.selected)
        self.connection_label.set_text(f"{len(displays)} connected · lid {lid_state()}")
        self.status.set_text('Drag to arrange, then test and save your setup.')
        self.canvas.queue_draw()

    def selected_display(self):
        return next((d for d in self.displays if d['connector']==self.selected),None)

    def select(self,connector):
        self.selected=connector
        self.updating=True
        self.display_picker.set_selected(next(i for i,d in enumerate(self.displays) if d['connector']==connector))
        self.updating=False
        self.update_controls()
        self.canvas.queue_draw()

    def picked(self,*_):
        if not self.updating and self.display_picker.get_selected()<len(self.displays):
            self.select(self.displays[self.display_picker.get_selected()]['connector'])

    def update_controls(self):
        d=self.selected_display()
        if not d:
            return
        self.updating=True
        self.enabled.set_active(d['enabled'])
        self.enabled_note.set_text('On' if d['enabled'] else 'Off')
        self.mode.set_model(Gtk.StringList.new([mode_label(m) for m in d['modes']]))
        mode=d.get('mode') or d['modes'][0]
        idx=next((i for i,m in enumerate(d['modes']) if all(m[k]==mode[k] for k in ('width','height','refresh_rate'))),0)
        self.mode.set_selected(idx)
        self.scale.set_value(d['scale'])
        self.rotation.set_selected(TRANSFORMS.index(d.get('transform','normal')))
        self.x.set_value(d['x'])
        self.y.set_value(d['y'])
        w,h=dimensions(d)
        self.info.set_text(f"{d.get('make','')}  ·  {w} × {h} logical pixels  ·  {d.get('serial') or 'No serial number'}")
        self.updating=False

    def edit(self,widget,*args):
        field=args[-1]
        if self.updating or self.busy or self.transaction:
            return
        d=self.selected_display()
        if not d:
            return
        if field=='enabled':
            d[field]=widget.get_active()
            self.enabled_note.set_text('On' if d[field] else 'Off')
        elif field=='mode':
            i=widget.get_selected()
            if i<len(d['modes']):
                d[field]=copy.deepcopy(d['modes'][i])
        elif field=='transform':
            d[field]=TRANSFORMS[widget.get_selected()]
        elif field in ('x','y'):
            d[field]=widget.get_value_as_int()
        else:
            d[field]=round(widget.get_value(),2)
        self.changed()

    def changed(self):
        self.dirty=True
        self.status.set_text('Unsaved changes · H J K L move the selected screen; Shift moves one pixel.')
        self.canvas.queue_draw()

    def reset(self):
        if not self.busy and not self.transaction:
            self.set_displays(self.baseline)

    def arrange(self,kind):
        if self.busy or self.transaction:
            return
        active=[d for d in self.displays if d['enabled']]
        if kind=='below':
            internals=[d for d in active if d.get('internal')]
            external=[d for d in active if not d.get('internal')]
            if not internals or not external:
                self.toast('Enable the built-in display and an external display first.')
                return
            x=0
            for d in external:
                d['x'],d['y']=x,0
                x+=dimensions(d)[0]
            bottom=max(dimensions(d)[1] for d in external)
            for d in internals:
                d['x'],d['y']=round((x-dimensions(d)[0])/2),bottom
        elif kind=='row':
            x=0
            for d in active:
                d['x'],d['y']=x,0
                x+=dimensions(d)[0]
        elif active:
            top=min(d['y'] for d in active)
            for d in active:
                d['y']=top
        self.changed()
        self.update_controls()

    def update_profiles(self,records):
        self.profile_records=records
        while self.profiles_list.get_first_child():
            self.profiles_list.remove(self.profiles_list.get_first_child())
        if not records:
            empty=text_label('Your saved desks\nwill appear here.','dim-label')
            empty.set_wrap(True)
            self.profiles_list.append(empty)
        for profile in records:
            row=Gtk.Box(spacing=6)
            b=Gtk.Button()
            b.add_css_class('profile-load')
            if profile.get('matches'):
                b.add_css_class('profile-match')
            b.connect('clicked',lambda _,p=profile:self.load_profile(p))
            content=Gtk.Box(spacing=10)
            labels=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=4,hexpand=True)
            name=text_label(profile['name'],'heading')
            name.set_ellipsize(Pango.EllipsizeMode.END)
            name.set_wrap(True)
            name.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            name.set_lines(2)
            name.set_width_chars(12)
            name.set_max_width_chars(18)
            labels.append(name)
            count=len(profile.get('outputs',[]))
            detail=f"{count} display{'s' if count!=1 else ''} · lid {profile.get('lid','unknown')}"
            labels.append(text_label(detail,'caption'))
            content.append(labels)
            if profile.get('matches'):
                check=Gtk.Image.new_from_icon_name('object-select-symbolic')
                check.add_css_class('profile-check')
                check.set_tooltip_text('Matches the connected displays and lid state')
                content.append(check)
            b.set_child(content)
            b.set_hexpand(True)
            b.set_tooltip_text(f"Restore {profile['name']}")
            row.append(b)
            delete=Gtk.Button(icon_name='user-trash-symbolic',tooltip_text='Delete saved setup')
            delete.add_css_class('flat')
            delete.add_css_class('profile-delete')
            delete.set_valign(Gtk.Align.CENTER)
            delete.set_size_request(32,32)
            delete.connect('clicked',lambda _,p=profile:self.delete_profile(p))
            row.append(delete)
            self.profiles_list.append(row)

    def delete_profile(self,profile):
        if self.transaction or self.busy:
            return
        dialog=Adw.AlertDialog(heading=f"Delete {profile['name']}?",body='The saved profile will be removed. Your current display layout stays as it is.')
        dialog.add_response('cancel','Cancel')
        dialog.add_response('delete','Delete')
        dialog.set_response_appearance('delete',Adw.ResponseAppearance.DESTRUCTIVE)
        def response(_,which):
            if which=='delete':
                def work():
                    self.backend.delete_profile(profile['id'])
                    return self.backend.profiles()
                self.task(work,self.update_profiles)
        dialog.connect('response',response)
        dialog.present(self)

    def load_profile(self,profile):
        if self.transaction or self.busy:
            return
        def go():
            if self.split.get_collapsed():
                self.split.set_show_sidebar(False)
            self.profile_name.set_text(profile['name'])
            self.task(lambda:self.backend.apply_profile(profile['id']),self.preview_ready)
        if self.dirty:
            dialog=Adw.AlertDialog(heading='Load saved setup?',body='This replaces your unsaved arrangement with the saved profile.')
            dialog.add_response('cancel','Keep editing')
            dialog.add_response('load','Load setup')
            dialog.connect('response',lambda _,r:go() if r=='load' else None)
            dialog.present(self)
        else:
            go()

    def apply(self):
        if not self.profile_name.get_text().strip():
            self.toast('Give this setup a name first.')
            self.profile_name.grab_focus()
            return
        layout=copy.deepcopy(self.displays)
        self.task(lambda:self.backend.begin(layout,timeout=20),self.preview_ready)

    def preview_ready(self,token):
        self.transaction=token
        self.preview_dialog=Adw.AlertDialog(heading='Keep this display layout?',body='The previous layout will return automatically in 20 seconds.')
        self.preview_dialog.add_response('revert','Revert')
        self.preview_dialog.add_response('keep','Keep & save')
        self.preview_dialog.set_response_appearance('keep',Adw.ResponseAppearance.SUGGESTED)
        self.preview_dialog.set_default_response('revert')
        self.preview_dialog.set_close_response('revert')
        self.preview_dialog.connect('response',self.preview_response)
        self.preview_dialog.present(self)
        self.preview_until=token['deadline']-0.5
        GLib.timeout_add(250,self.preview_tick)
        self.status.set_text('Preview active · confirm to save this setup.')

    def preview_tick(self):
        if not self.transaction:
            return False
        remaining=max(0,math.ceil(self.preview_until-time.time()))
        self.preview_dialog.set_body(f'The previous layout will return automatically in {remaining} seconds.')
        if remaining<=0:
            self.preview_dialog.close()
            return False
        return True

    def preview_response(self,dialog,response):
        token=self.transaction
        if not token:
            return
        self.transaction=None
        name=self.profile_name.get_text().strip()
        def work():
            if response=='keep':
                self.backend.keep(token,name)
            else:
                self.backend.revert(token)
            return self.backend.query(),self.backend.profiles()
        def done(result):
            self.set_displays(result[0])
            self.update_profiles(result[1])
            self.status.set_text('Setup saved. It will restore when these monitors reconnect.' if response=='keep' and self.auto.get_active() else 'Setup saved.' if response=='keep' else 'Previous display layout restored.')
        self.task(work,done)

    def auto_changed(self,*_):
        if not self.updating:
            enabled=self.auto.get_active()
            self.task(lambda:self.backend.set_auto(enabled),lambda _:self.status.set_text('Automatic restore is on.' if enabled else 'Automatic restore is off.'))

    def check_connection(self):
        if self.busy or self.transaction or self.dirty or not self.displays:
            return True
        before=copy.deepcopy(self.baseline)
        def done(displays):
            def sig(ds):
                return [(d['connector'],d['enabled'],d['x'],d['y'],d['scale'],d['transform'],d.get('mode')) for d in ds]
            if sig(displays)!=sig(before):
                self.set_displays(displays)
        self.task(self.backend.query,done,quiet=True)
        return True

    def close_request(self,*_):
        if self.transaction:
            self.preview_dialog.close()
            return True
        if self.busy:
            self.toast('Wait for the current display operation to finish.')
            return True
        return False


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.connect('activate',self.activate)

    def activate(self,app):
        if self.get_active_window():
            self.get_active_window().present()
            return
        Gtk.Window.set_default_icon_name(APP_ID)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.PREFER_DARK)
        css=Gtk.CssProvider()
        css.load_from_data(b'''
            .sidebar-surface { background: alpha(@window_fg_color, .025); }
            /* Presentation wrappers must not masquerade as clickable rows. */
            .arrange-tools > flowboxchild, .display-settings > flowboxchild {
                background-color: transparent;
                background-image: none;
                box-shadow: none;
                outline-style: none;
            }
            .arrange-tools > flowboxchild { padding: 4px; }
            .display-settings > flowboxchild { padding: 6px; }
            .canvas-frame { border-radius: 12px; border: 1px solid alpha(@window_fg_color, .12); }
            .display-canvas { background: #131923; border-radius: 12px; }
            .display-canvas:focus-visible {
                outline: 2px solid @accent_color;
                outline-offset: -4px;
            }
            .monitor-card { background: #26354a; border: 2px solid #536f92; border-radius: 10px; color: #dceaff; }
            .monitor-card.selected { border-color: #9bcaff; box-shadow: 0 4px 12px alpha(black, .3); }
            .monitor-1 { background: #273c36; border-color: #527c65; color: #d6efdf; }
            .monitor-1.selected { border-color: #a0dbb8; }
            .monitor-2 { background: #392e48; border-color: #7e6597; }
            .monitor-2.selected { border-color: #d4b0f6; }
            .monitor-3 { background: #483b2e; border-color: #968365; }
            .monitor-number { font-size: 23px; font-weight: 600; }
            .monitor-card.compact .monitor-number { font-size: 16px; }
            .caption-heading { font-size: 11px; font-weight: 600; opacity: .7; }
            .profile-load { padding: 12px; border-radius: 10px; }
            .profile-match { background: alpha(@window_fg_color, .08); }
            .profile-load:hover { background: alpha(@window_fg_color, .12); }
            .profile-load:active { background: alpha(@window_fg_color, .18); }
            .profile-load .caption { opacity: .6; }
            .profile-check { color: #a0dbb8; }
            .profile-delete { padding: 6px; min-width: 20px; min-height: 20px; border-radius: 8px; }
            .profile-delete:not(:hover):not(:focus-visible) { opacity: .65; }
            spinbutton text { font-feature-settings: "tnum"; }
        ''')
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(),css,Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        if not os.environ.get('NIRI_SOCKET') or not shutil.which('niri'):
            win=Adw.ApplicationWindow(application=self,title='Display Desk',default_width=420,default_height=240)
            win.set_content(Gtk.Box())
            win.present()
            dialog=Adw.AlertDialog(heading='A Niri session is required',body='Display Desk manages displays in Niri 25.11 or later. Log in to a Niri desktop session, then open Display Desk again.')
            dialog.add_response('close','Close')
            dialog.set_close_response('close')
            dialog.connect('response',lambda *_:win.close())
            dialog.present(win)
            return
        win=Window(self)
        win.present()


if __name__=='__main__':
    if '--watch' in sys.argv:
        Backend().watch()
    else:
        sys.exit(App().run(sys.argv))
