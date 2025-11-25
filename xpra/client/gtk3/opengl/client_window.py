# This file is part of Xpra.
# Copyright (C) 2012 Serviware (Arthur Huillet, <ahuillet@serviware.com>)
# Copyright (C) 2012 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

from collections import namedtuple

from xpra.client.gtk3.window import ClientWindow
from xpra.gtk.window import set_visual
from xpra.util.objects import typedict
from xpra.util.env import envbool
from xpra.log import Logger
from xpra.os_util import gi_import

GLib = gi_import("GLib")
Gtk = gi_import("Gtk")
Gdk = gi_import("Gdk")

log = Logger("opengl", "window")

Rectangle = namedtuple("Rectangle", ("x", "y", "width", "height"))
DrawEvent = namedtuple("DrawEvent", ("area", ))

MONITOR_REINIT = envbool("XPRA_OPENGL_MONITOR_REINIT", False)


class GLClientWindowBase(ClientWindow):

    def __repr__(self):
        return f"GLClientWindow({self.wid} : {self._backing})"

    def get_backing_class(self) -> type:
        raise NotImplementedError()

    def is_GL(self) -> bool:
        return True

    def spinner(self, ok: bool) -> None:
        b = self._backing
        log("spinner(%s) opengl window %s: backing=%s", ok, self.wid, b)
        if not b:
            return
        b.paint_spinner = self.can_have_spinner() and not ok
        log("spinner(%s) backing=%s, paint_screen=%s, paint_spinner=%s",
            ok, b._backing, b.paint_screen, b.paint_spinner)
        if b._backing and b.paint_screen:
            w, h = self.get_size()
            self.repaint(0, 0, w, h)

    def queue_draw_area(self, x: int, y: int, w: int, h: int) -> None:
        b = self._backing
        if not b:
            return
        b.gl_expose_rect(x, y, w, h)

    def monitor_changed(self, monitor) -> None:
        super().monitor_changed(monitor)
        da = self.drawing_area
        if da and MONITOR_REINIT:
            # re-create the drawing area,
            # which will re-create the opengl context:
            try:
                # PATCH: Remove from overlay container if it exists
                if self.overlay_container:
                    self.overlay_container.remove(da)
                else:
                    self.remove(da)
            except Exception:
                log("monitor_changed: failed to remove %s", da)
            self.drawing_area = None
            w, h = self.get_size()
            self.new_backing(w, h)

    def remove_backing(self) -> None:
        b = self._backing
        log("remove_backing() backing=%s", b)
        if b:
            self._backing = None
            b.paint_screen = False
            b.close()
            glarea = b._backing
            log("remove_backing() glarea=%s", glarea)
            if glarea:
                try:
                    # PATCH: Remove from overlay container if it exists
                    if self.overlay_container:
                        self.overlay_container.remove(glarea)
                    else:
                        self.remove(glarea)
                except Exception:
                    log.warn("Warning: cannot remove %s", glarea, exc_info=True)

    def magic_key(self, *args) -> None:
        b = self._backing
        if self.border:
            self.border.toggle()
            if b:
                with b.gl_context() as ctx:
                    b.gl_init(ctx)
                    b.present_fbo(ctx, 0, 0, *b.size)
                self.repaint(0, 0, *self._size)
        log("gl magic_key%s border=%s, backing=%s", args, self.border, b)

    def set_alpha(self) -> None:
        super().set_alpha()
        rgb_formats = self._client_properties.setdefault("encodings.rgb_formats", [])
        # gl.backing supports BGR(A) too:
        if "RGBA" in rgb_formats:
            rgb_formats.append("BGRA")
        if "RGB" in rgb_formats:
            rgb_formats.append("BGR")

    def do_configure_event(self, event) -> None:
        log("GL do_configure_event(%s)", event)
        ClientWindow.do_configure_event(self, event)
        self._backing.paint_screen = True

    def destroy(self) -> None:
        self.remove_backing()
        super().destroy()

    def init_drawing_area(self) -> None:
        self.drawing_area = None

    def new_backing(self, bw: int, bh: int) -> None:
        widget = super().new_backing(bw, bh)
        
        # PATCH: Handle overlay container for OpenGL windows
        # Remove old widget from overlay container or window
        if self.drawing_area:
            if self.overlay_container:
                # Remove from overlay container if it exists
                self.overlay_container.remove(self.drawing_area)
            else:
                # Remove from window directly
                self.remove(self.drawing_area)
        
        set_visual(widget, self._has_alpha)
        widget.show()
        self.init_widget_events(widget)
        if self.drawing_area and self.size_constraints:
            # apply min size to the drawing_area:
            thints = typedict(self.size_constraints)
            minsize = thints.intpair("minimum-size", (0, 0))
            self.drawing_area.set_size_request(*minsize)
        
        # PATCH: Create overlay container if it doesn't exist (for OpenGL windows)
        if not self.overlay_container:
            # Create overlay container
            self.overlay_container = Gtk.Overlay()
            self.overlay_container.add(widget)
            
            # Create loading overlay with color #EEF9F3
            self.loading_overlay = Gtk.EventBox()
            self.loading_overlay.set_size_request(*self._size)
            rgba = Gdk.RGBA()
            rgba.parse("#EEF9F3")
            self.loading_overlay.override_background_color(Gtk.StateFlags.NORMAL, rgba)
            self.loading_overlay.show()
            self.overlay_container.add_overlay(self.loading_overlay)
            
            # Track overlay state
            self._overlay_visible = True
            self._first_frame_received = False
            self._frame_count = 0
            
            # Timeout fallback: remove the overlay after 2.5s if no frame arrives
            self._overlay_timeout = GLib.timeout_add(2500, self._remove_loading_overlay_fallback)
            
            # Add overlay container to window
            self.add(self.overlay_container)
        else:
            # Overlay container exists, just add widget to it
            self.overlay_container.add(widget)
            # Update loading overlay size if needed
            if self.loading_overlay:
                self.loading_overlay.set_size_request(*self._size)
        
        self.drawing_area = widget
        # maybe redundant?:
        self.apply_geometry_hints(self.geometry_hints)

    def draw_widget(self, widget, context) -> bool:
        mapped = self.get_mapped()
        backing = self._backing
        log(f"draw_widget({widget}, {context}) {mapped=}, {backing=}", )
        if not mapped:
            return False
        if not backing:
            return False
        return backing.draw_fbo(context)
