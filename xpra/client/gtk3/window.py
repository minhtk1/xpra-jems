# This file is part of Xpra.
# Copyright (C) 2011 Serviware (Arthur Huillet, <ahuillet@serviware.com>)
# Copyright (C) 2010 Antoine Martin <antoine@xpra.org>
# Copyright (C) 2008 Nathaniel Smith <njs@pobox.com>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import sys
import ctypes
from ctypes import wintypes

from xpra.client.gtk3.window_base import GTKClientWindowBase, HAS_X11_BINDINGS
from xpra.gtk.widget import scaled_image
from xpra.gtk.pixbuf import get_icon_pixbuf
from xpra.scripts.config import FALSE_OPTIONS
from xpra.util.objects import typedict
from xpra.util.env import envbool
from xpra.os_util import gi_import, OSX
from xpra.util.str_fn import bytestostr
from xpra.log import Logger

paintlog = Logger("paint")
metalog = Logger("metadata")
geomlog = Logger("geometry")
winimelog = Logger("win32", "ime")

WIN32_PLATFORM = sys.platform == "win32"
HIMC = wintypes.HANDLE
HWND = wintypes.HWND
NULL_HIMC = HIMC(0)
if WIN32_PLATFORM:
    try:
        _IMM32 = ctypes.WinDLL("imm32", use_last_error=True)
    except OSError:
        _IMM32 = None
    try:
        from xpra.platform.win32.gui import get_window_handle as _win32_gui_get_window_handle
    except Exception as e:  # pragma: no cover - defensive import
        _win32_gui_get_window_handle = None
        winimelog.warn("fallback get_window_handle() unavailable: %s", e)
else:
    _IMM32 = None
    _win32_gui_get_window_handle = None
if _IMM32:
    _ImmGetContext = _IMM32.ImmGetContext
    _ImmGetContext.argtypes = [HWND]
    _ImmGetContext.restype = HIMC
    _ImmAssociateContext = _IMM32.ImmAssociateContext
    _ImmAssociateContext.argtypes = [HWND, HIMC]
    _ImmAssociateContext.restype = HIMC
    _ImmReleaseContext = _IMM32.ImmReleaseContext
    _ImmReleaseContext.argtypes = [HWND, HIMC]
    _ImmReleaseContext.restype = wintypes.BOOL
else:
    _ImmGetContext = None
    _ImmAssociateContext = None
    _ImmReleaseContext = None
WIN32_CAN_TOGGLE_IME = WIN32_PLATFORM and _ImmGetContext is not None
if WIN32_PLATFORM and not WIN32_CAN_TOGGLE_IME:
    winimelog.warn("IME toggle disabled: imm32.dll not available or failed to load")

Gtk = gi_import("Gtk")
Gdk = gi_import("Gdk")
GdkPixbuf = gi_import("GdkPixbuf")
GObject = gi_import("GObject")
Gio = gi_import("Gio")

WINDOW_ICON = envbool("XPRA_WINDOW_ICON", True)
WINDOW_XPRA_MENU = envbool("XPRA_WINDOW_XPRA_MENU", True)
WINDOW_MENU = envbool("XPRA_WINDOW_MENU", True)


class ClientWindow(GTKClientWindowBase):
    """
    GTK3 version of the ClientWindow class
    """

    def init_window(self, metadata: typedict) -> None:
        super().init_window(metadata)
        self.menu_helper = None
        self.header_bar_image = None
        self._win32_ime_context = None
        self._win32_ime_hwnd = None
        if WIN32_CAN_TOGGLE_IME:
            self.connect("realize", self._win32_on_realize)
        if self.can_use_header_bar(metadata):
            self.add_header_bar()

    def _icon_size(self) -> int:
        tb = self.get_titlebar()
        try:
            h = tb.get_preferred_size()[-1] - 8
        except Exception:
            h = 24
        return min(128, max(h, 24))

    def set_icon(self, pixbuf: GdkPixbuf.Pixbuf) -> None:
        super().set_icon(pixbuf)
        hbi = self.header_bar_image
        if hbi and WINDOW_ICON:
            h = self._icon_size()
            pixbuf = pixbuf.scale_simple(h, h, GdkPixbuf.InterpType.HYPER)
            hbi.set_from_pixbuf(pixbuf)

    def can_use_header_bar(self, metadata: typedict) -> bool:
        if self.is_OR() or not self.get_decorated() or OSX:
            return False
        hbl = (self.headerbar or "").lower().strip()
        if hbl in FALSE_OPTIONS:
            return False
        if hbl == "force":
            return True
        # we can't enable it if there are size-constraints:
        sc = metadata.dictget("size-constraints")
        if sc is None:
            return True
        tsc = typedict(sc)
        maxs = tsc.intpair("maximum-size")
        if maxs:
            return False
        mins = tsc.intpair("minimum-size")
        if mins and mins != (0, 0):
            return False
        if tsc.intpair("increment", (0, 0)) != (0, 0):
            return False
        return True

    def add_header_bar(self) -> None:
        metalog("add_header_bar()")
        hb = Gtk.HeaderBar()
        hb.set_has_subtitle(False)
        hb.set_show_close_button(True)
        hb.props.title = self.get_title()
        if WINDOW_MENU:
            # the icon 'open-menu-symbolic' will be replaced with the window icon
            # when we receive it
            icon = Gio.ThemedIcon(name="preferences-system-windows")
            self.header_bar_image = Gtk.Image.new_from_gicon(icon, Gtk.IconSize.BUTTON)
            button = Gtk.Button()
            button.add(self.header_bar_image)
            button.connect("clicked", self.show_window_menu)
            hb.pack_start(button)
        elif WINDOW_ICON:
            # just the icon, no menu:
            pixbuf = get_icon_pixbuf("transparent.png")
            self.header_bar_image = scaled_image(pixbuf, self._icon_size())
            hb.pack_start(self.header_bar_image)
        if WINDOW_XPRA_MENU:
            icon = Gio.ThemedIcon(name="open-menu-symbolic")
            image = Gtk.Image.new_from_gicon(icon, Gtk.IconSize.BUTTON)
            button = Gtk.Button()
            button.add(image)
            button.connect("clicked", self.show_xpra_menu)
            hb.pack_end(button)
        self.set_titlebar(hb)

    def show_xpra_menu(self, *_args) -> None:
        mh = self._client.get_menu_helper()
        if mh:
            mh.build()
            mh.popup(0, 0)

    def show_window_menu(self, *_args) -> None:
        if not self.menu_helper:
            from xpra.client.gtk3.window_menu import WindowMenuHelper
            self.menu_helper = WindowMenuHelper(self._client, self)
            self.menu_helper.build()
        self.menu_helper.popup(0, 0)

    def get_backing_class(self) -> type:
        from xpra.client.gtk3.cairo_backing import CairoBacking
        return CairoBacking

    def xget_u32_property(self, target, name: str):
        if HAS_X11_BINDINGS:
            return GTKClientWindowBase.xget_u32_property(self, target, name)
        # pure Gdk lookup:
        try:
            name_atom = Gdk.Atom.intern(name, False)
            type_atom = Gdk.Atom.intern("CARDINAL", False)
            prop = Gdk.property_get(target, name_atom, type_atom, 0, 9999, False)
            if not prop or len(prop) != 3 or len(prop[2]) != 1:
                return None
            metalog("xget_u32_property(%s, %s)=%s", target, name, prop[2][0])
            return prop[2][0]
        except Exception as e:
            metalog.error("xget_u32_property error on %s / %s: %s", target, name, e)

    def get_drawing_area_geometry(self) -> tuple[int, int, int, int]:
        gdkwindow = self.drawing_area.get_window()
        if gdkwindow:
            x, y = gdkwindow.get_origin()[1:]
        else:
            x, y = self.get_position()
        w, h = self.get_size()
        return x, y, w, h

    def apply_geometry_hints(self, hints: typedict) -> None:
        """ we convert the hints as a dict into a gdk.Geometry + gdk.WindowHints """
        wh = Gdk.WindowHints
        name_to_hint = {
            "max_width": wh.MAX_SIZE,
            "max_height": wh.MAX_SIZE,
            "min_width": wh.MIN_SIZE,
            "min_height": wh.MIN_SIZE,
            "base_width": wh.BASE_SIZE,
            "base_height": wh.BASE_SIZE,
            "width_inc": wh.RESIZE_INC,
            "height_inc": wh.RESIZE_INC,
            "min_aspect_ratio": wh.ASPECT,
            "max_aspect_ratio": wh.ASPECT,
        }
        # these fields can be copied directly to the gdk.Geometry as ints:
        INT_FIELDS: list[str] = [
            "min_width", "min_height",
            "max_width", "max_height",
            "base_width", "base_height",
            "width_inc", "height_inc",
        ]
        ASPECT_FIELDS: dict[str, str] = {
            "min_aspect_ratio": "min_aspect",
            "max_aspect_ratio": "max_aspect",
        }
        thints = typedict(hints)
        if self.drawing_area:
            # apply min size to the drawing_area:
            # (for CSD mode, ie: headerbar)
            minw = thints.intget("min_width", 0)
            minh = thints.intget("min_height", 0)
            self.drawing_area.set_size_request(minw, minh)

        geom = Gdk.Geometry()
        mask = 0
        for k, v in hints.items():
            k = bytestostr(k)
            if k in INT_FIELDS:
                setattr(geom, k, v)
                mask |= int(name_to_hint.get(k, 0))
            elif k in ASPECT_FIELDS:
                field = ASPECT_FIELDS[k]
                setattr(geom, field, float(v))
                mask |= int(name_to_hint.get(k, 0))
        gdk_hints = Gdk.WindowHints(mask)
        geomlog("apply_geometry_hints(%s) geometry=%s, hints=%s", hints, geom, gdk_hints)
        self.set_geometry_hints(self.drawing_area, geom, gdk_hints)

    def do_x11_focus_in_event(self, event) -> None:
        super().do_x11_focus_in_event(event)
        self._win32_detach_ime()

    def do_x11_focus_out_event(self, event) -> None:
        super().do_x11_focus_out_event(event)
        self._win32_restore_ime()

    def _focus_change(self, *args) -> None:
        super()._focus_change(*args)
        if not WIN32_CAN_TOGGLE_IME:
            return
        htf = self.has_toplevel_focus()
        winimelog.info("focus change: has_toplevel-focus=%s, stored_himc=%#x, stored_hwnd=%#x",
                       htf, int(self._win32_ime_context or 0), int(self._win32_ime_hwnd or 0))
        if htf:
            self._win32_detach_ime()
        else:
            self._win32_restore_ime()

    def _win32_on_realize(self, *_args) -> None:
        if not WIN32_CAN_TOGGLE_IME:
            return
        hwnd = self._win32_get_hwnd()
        winimelog.info("realize: hwnd=%#x, has_toplevel_focus=%s", int(hwnd or 0), self.has_toplevel_focus())
        if self.has_toplevel_focus():
            self._win32_detach_ime()

    def _win32_detach_ime(self) -> None:
        if not WIN32_CAN_TOGGLE_IME:
            return
        if self._win32_ime_context:
            winimelog.info("skip detaching IME: context already saved (hwnd=%#x)", self._win32_ime_hwnd or 0)
            return
        hwnd = self._win32_get_hwnd()
        if not hwnd:
            winimelog.warn("skip detaching IME: no hwnd")
            return
        previous = _ImmAssociateContext(HWND(hwnd), NULL_HIMC)
        if not previous:
            winimelog.warn("ImmAssociateContext() returned NULL for hwnd=%#x", hwnd)
            return
        winimelog.info("detaching IME from hwnd=%#x (prev_himc=%#x)", hwnd, int(previous or 0))
        self._win32_ime_context = previous
        self._win32_ime_hwnd = hwnd

    def _win32_restore_ime(self) -> None:
        if not WIN32_CAN_TOGGLE_IME:
            return
        himc = self._win32_ime_context
        if not himc:
            winimelog.info("skip restoring IME: no stored context")
            return
        hwnd = self._win32_get_hwnd() or self._win32_ime_hwnd
        self._win32_ime_context = None
        self._win32_ime_hwnd = None
        if not hwnd:
            winimelog.warn("cannot restore IME context: missing hwnd")
            return
        winimelog.info("restoring IME to hwnd=%#x (himc=%#x)", hwnd, int(himc or 0))
        hwnd_obj = HWND(hwnd)
        previous = _ImmAssociateContext(hwnd_obj, himc)
        winimelog.info("restore results: prev_himc=%#x", int(previous or 0))

    def _win32_get_hwnd(self) -> int | None:
        """Return the native HWND for this Gtk window if available."""
        if not WIN32_CAN_TOGGLE_IME:
            return None
        window = self.get_window()
        if not window:
            winimelog.info("get_window() returned None while fetching hwnd")
            return None
        get_handle = getattr(window, "get_handle", None)
        # First try the Gdk method when present:
        if not callable(get_handle):
            winimelog.warn("get_handle() missing on gdk window")
        else:
            try:
                handle = get_handle()
            except Exception as exc:
                winimelog.warn("get_handle() failed: %s", exc)
            else:
                if handle:
                    return handle
                winimelog.info("get_handle() returned NULL/0")
        # Fallback to ctypes-based GDK Win32 helper if available:
        if _win32_gui_get_window_handle:
            try:
                handle = _win32_gui_get_window_handle(window)
                if handle:
                    winimelog.info("fallback get_window_handle() returned %#x", handle)
                    return handle
                winimelog.warn("fallback get_window_handle() returned 0")
            except Exception as exc:
                winimelog.warn("fallback get_window_handle() failed: %s", exc)
        else:
            winimelog.warn("no fallback win32 get_window_handle available")
        return None

    def can_maximize(self) -> bool:
        hints = self.geometry_hints
        if not hints:
            return True
        maxw = hints.intget("max_width", 32768)
        maxh = hints.intget("max_height", 32768)
        if maxw > 32000 and maxh > 32000:
            return True
        geom = self.get_drawing_area_geometry()
        dw, dh = geom[2], geom[3]
        return dw < maxw and dh < maxh

    def draw_widget(self, widget, context) -> bool:
        paintlog("draw_widget(%s, %s)", widget, context)
        if not self.get_mapped():
            return False
        backing = self._backing
        if not backing:
            return False
        context.save()
        self.paint_backing_offset_border(backing, context)
        self.clip_to_backing(backing, context)
        backing.cairo_draw(context)
        context.restore()
        self.cairo_paint_border(context, None)
        if not self._client.server_ok():
            self.paint_spinner(context)
        return True


GObject.type_register(ClientWindow)
