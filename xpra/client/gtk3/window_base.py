# This file is part of Xpra.
# Copyright (C) 2011 Serviware (Arthur Huillet, <ahuillet@serviware.com>)
# Copyright (C) 2010 Antoine Martin <antoine@xpra.org>
# Copyright (C) 2008 Nathaniel Smith <njs@pobox.com>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import math
import os.path
from time import monotonic
from urllib.parse import unquote
from typing import Optional, Any
from collections.abc import Callable, Sequence

from cairo import (  # pylint: disable=no-name-in-module
    RectangleInt, Region,  # @UnresolvedImport
    OPERATOR_OVER, LINE_CAP_ROUND,  # @UnresolvedImport
)
from xpra.os_util import gi_import, WIN32, OSX, POSIX
from xpra.util.system import is_Wayland, is_X11
from xpra.util.objects import typedict
from xpra.util.str_fn import csv, bytestostr
from xpra.util.env import envint, envbool, first_time, ignorewarnings, IgnoreWarningsContext
from xpra.gtk.gobject import no_arg_signal, one_arg_signal
from xpra.gtk.util import ds_inited, get_default_root_window, GRAB_STATUS_STRING
from xpra.gtk.window import set_visual
from xpra.gtk.pixbuf import get_pixbuf_from_data
from xpra.gtk.keymap import KEY_TRANSLATIONS
from xpra.common import (
    MoveResize, force_size_constraint, noop,
    MOVERESIZE_DIRECTION_STRING, SOURCE_INDICATION_STRING, WORKSPACE_UNSET,
    WORKSPACE_ALL, WORKSPACE_NAMES,
)
from xpra.keyboard.common import KeyEvent
from xpra.client.gui.window_base import ClientWindowBase
from xpra.platform.gui import (
    set_fullscreen_monitors, set_shaded,
    add_window_hooks, remove_window_hooks,
    pointer_grab, pointer_ungrab,
)
from xpra.log import Logger

GLib = gi_import("GLib")
Gtk = gi_import("Gtk")
Gdk = gi_import("Gdk")
Gio = gi_import("Gio")

focuslog = Logger("focus", "grab")
workspacelog = Logger("workspace")
log = Logger("window")
keylog = Logger("keyboard")
keyeventlog = Logger("keyboard", "events")
zenlog = Logger("keyboard", "zenkaku")
iconlog = Logger("icon")
metalog = Logger("metadata")
statelog = Logger("state")
eventslog = Logger("events")
shapelog = Logger("shape")
mouselog = Logger("mouse")
geomlog = Logger("geometry")
grablog = Logger("grab")
draglog = Logger("dragndrop")
alphalog = Logger("alpha")

CAN_SET_WORKSPACE = False
HAS_X11_BINDINGS = False

prop_get, prop_set, prop_del = None, None, None
NotifyInferior = None
X11Window = X11Core = None

WIN32_WORKSPACE = WIN32 and envbool("XPRA_WIN32_WORKSPACE", False)
WIN32_POPUP_TRIM_CACHE: dict[str, int] = {}


def use_x11_bindings() -> bool:
    if not POSIX or OSX:
        return False
    if not is_X11() or is_Wayland():
        return False
    if envbool("XPRA_USE_X11_BINDINGS", False):
        return True
    try:
        from xpra.x11.bindings.xwayland_info import isxwayland
    except ImportError:
        log("no xwayland bindings", exc_info=True)
        return False
    return not isxwayland()


if use_x11_bindings():
    try:
        from xpra.gtk.error import xlog, verify_sync
        from xpra.x11.gtk.prop import prop_get, prop_set, prop_del
        from xpra.x11.bindings.window import constants, X11WindowBindings, SHAPE_KIND
        from xpra.x11.bindings.core import X11CoreBindings, set_context_check
        from xpra.x11.bindings.send_wm import send_wm_workspace

        X11Window = X11WindowBindings()
        X11Core = X11CoreBindings()
    except ImportError as x11e:
        log("x11 bindings", exc_info=True)
        # gtk util should have already logged a detailed warning
        log("cannot import the X11 bindings:")
        log(" %s", x11e)
    except RuntimeError as e:
        log("x11", exc_info=True)
        log.error(f"Error loading X11 bindings: {e}")
    else:
        set_context_check(verify_sync)
        NotifyInferior = constants["NotifyInferior"]
        HAS_X11_BINDINGS = True

        SubstructureNotifyMask = constants["SubstructureNotifyMask"]
        SubstructureRedirectMask = constants["SubstructureRedirectMask"]

        def can_set_workspace() -> bool:
            SET_WORKSPACE = envbool("XPRA_SET_WORKSPACE", True)
            if not SET_WORKSPACE:
                return False
            try:
                # in theory this is not a proper check, meh - that will do
                root_xid = X11Window.get_root_xid()
                supported = prop_get(root_xid, "_NET_SUPPORTED", ["atom"], ignore_errors=True) or ()
                return "_NET_WM_DESKTOP" in supported
            except Exception as we:
                workspacelog("x11 workspace bindings error", exc_info=True)
                workspacelog.error("Error: failed to setup workspace hooks:")
                workspacelog.estr(we)

        CAN_SET_WORKSPACE = can_set_workspace()
elif WIN32 and WIN32_WORKSPACE:
    from _ctypes import COMError
    try:
        from pyvda.pyvda import get_virtual_desktops
    except (OSError, ImportError, COMError, NotImplementedError) as e:
        workspacelog(f"no workspace support: {e}")
        WIN32_WORKSPACE = 0
    else:
        CAN_SET_WORKSPACE = len(get_virtual_desktops()) > 0

AWT_DIALOG_WORKAROUND = envbool("XPRA_AWT_DIALOG_WORKAROUND", WIN32)
BREAK_MOVERESIZE = os.environ.get("XPRA_BREAK_MOVERESIZE", "Escape").split(",")
MOVERESIZE_GUESS_BUTTON = envbool("XPRA_MOVERESIZE_GUESS_BUTTON", True)
MOVERESIZE_X11 = envbool("XPRA_MOVERESIZE_X11", POSIX)
MOVERESIZE_GDK = envbool("XPRA_MOVERESIZE_GDK", True)
CURSOR_IDLE_TIMEOUT = envint("XPRA_CURSOR_IDLE_TIMEOUT", 6)
DISPLAY_HAS_SCREEN_INDEX = POSIX and os.environ.get("DISPLAY", "").split(":")[-1].find(".") >= 0
DRAGNDROP = envbool("XPRA_DRAGNDROP", True)
CLAMP_WINDOW_TO_SCREEN = envbool("XPRA_CLAMP_WINDOW_TO_SCREEN", True)
FOCUS_RECHECK_DELAY = envint("XPRA_FOCUS_RECHECK_DELAY", 0)
REPAINT_MAXIMIZED = envint("XPRA_REPAINT_MAXIMIZED", 0)
REFRESH_MAXIMIZED = envbool("XPRA_REFRESH_MAXIMIZED", True)
UNICODE_KEYNAMES = envbool("XPRA_UNICODE_KEYNAMES", False)
SMOOTH_SCROLL = envbool("XPRA_SMOOTH_SCROLL", True)
POLL_WORKSPACE = envbool("XPRA_POLL_WORKSPACE", CAN_SET_WORKSPACE)
ICONIFY_LATENCY = envint("XPRA_ICONIFY_LATENCY", 150)
SMOOTH_SCROLL_NORM = envint("XPRA_SMOOTH_SCROLL_NORM", 50 if OSX else 100)
OVERLAY_TIMEOUT_MS = envint("XPRA_OVERLAY_TIMEOUT", 2500)
OVERLAY_MIN_FLUSHES = max(1, envint("XPRA_OVERLAY_MIN_FLUSHES", 2))
OVERLAY_MIN_FLUSHES_SMALL = max(1, envint("XPRA_OVERLAY_MIN_FLUSHES_SMALL", 1))
OVERLAY_MIN_DURATION_MS = max(0, envint("XPRA_OVERLAY_MIN_DURATION", 750))
OVERLAY_LOG = envbool("XPRA_OVERLAY_DEBUG", False)

WINDOW_OVERFLOW_TOP = envbool("XPRA_WINDOW_OVERFLOW_TOP", False)
AWT_RECENTER = envbool("XPRA_AWT_RECENTER", True)
UNDECORATED_TRANSIENT_IS_OR = envint("XPRA_UNDECORATED_TRANSIENT_IS_OR", 1)
XSHAPE = envbool("XPRA_XSHAPE", True)
bit_to_rectangles: Optional[callable] = None
try:
    from xpra.codecs.argb import argb

    bit_to_rectangles = argb.bit_to_rectangles
except (ImportError, AttributeError):
    pass
LAZY_SHAPE = envbool("XPRA_LAZY_SHAPE", not callable(bit_to_rectangles))

AUTOGRAB_MODES = os.environ.get("XPRA_AUTOGRAB_MODES", "shadow,desktop,monitors").split(",")
AUTOGRAB_WITH_FOCUS = envbool("XPRA_AUTOGRAB_WITH_FOCUS", False)
AUTOGRAB_WITH_POINTER = envbool("XPRA_AUTOGRAB_WITH_POINTER", True)


def parse_padding_colors(colors_str: str) -> tuple[int, int, int]:
    padding_colors = 0, 0, 0
    if colors_str:
        try:
            padding_colors = tuple(float(x.strip()) for x in colors_str.split(","))
            assert len(padding_colors) == 3, "you must specify 3 components"
        except Exception as e:
            log.warn("Warning: invalid padding colors specified,")
            log.warn(" %s", e)
            log.warn(" using black")
            padding_colors = 0, 0, 0
    log("parse_padding_colors(%s)=%s", colors_str, padding_colors)
    return padding_colors


def norm_scroll(value: float):
    if SMOOTH_SCROLL_NORM == 100:
        return value
    smoothed = math.pow(abs(value), SMOOTH_SCROLL_NORM / 100)
    return math.copysign(smoothed, value)


PADDING_COLORS = parse_padding_colors(os.environ.get("XPRA_PADDING_COLORS", ""))

# window types we map to POPUP rather than TOPLEVEL
POPUP_TYPE_HINTS: set[str] = {
    # "DIALOG",
    # "MENU",
    # "TOOLBAR",
    # "SPLASH",
    # "UTILITY",
    # "DOCK",
    # "DESKTOP",
    "DROPDOWN_MENU",
    "POPUP_MENU",
    # "TOOLTIP",
    # "NOTIFICATION",
    # "COMBO",
    # "DND",
}
# window types for which we skip window decorations (title bar)
UNDECORATED_TYPE_HINTS: set[str] = {
    # "DIALOG",
    "MENU",
    # "TOOLBAR",
    "SPLASH",
    "SPLASHSCREEN",
    "UTILITY",
    "DOCK",
    "DESKTOP",
    "DROPDOWN_MENU",
    "POPUP_MENU",
    "TOOLTIP",
    "NOTIFICATION",
    "COMBO",
    "DND",
}
GDK_MOVERESIZE_MAP = {int(d): we for d, we in {
    MoveResize.SIZE_TOPLEFT: Gdk.WindowEdge.NORTH_WEST,
    MoveResize.SIZE_TOP: Gdk.WindowEdge.NORTH,
    MoveResize.SIZE_TOPRIGHT: Gdk.WindowEdge.NORTH_EAST,
    MoveResize.SIZE_RIGHT: Gdk.WindowEdge.EAST,
    MoveResize.SIZE_BOTTOMRIGHT: Gdk.WindowEdge.SOUTH_EAST,
    MoveResize.SIZE_BOTTOM: Gdk.WindowEdge.SOUTH,
    MoveResize.SIZE_BOTTOMLEFT: Gdk.WindowEdge.SOUTH_WEST,
    MoveResize.SIZE_LEFT: Gdk.WindowEdge.WEST,
    # MOVERESIZE_SIZE_KEYBOARD,
}.items()}

GDK_SCROLL_MAP = {
    Gdk.ScrollDirection.UP: 4,
    Gdk.ScrollDirection.DOWN: 5,
    Gdk.ScrollDirection.LEFT: 6,
    Gdk.ScrollDirection.RIGHT: 7,
}

BUTTON_MASK: dict[int, int] = {
    Gdk.ModifierType.BUTTON1_MASK: 1,
    Gdk.ModifierType.BUTTON2_MASK: 2,
    Gdk.ModifierType.BUTTON3_MASK: 3,
    Gdk.ModifierType.BUTTON4_MASK: 4,
    Gdk.ModifierType.BUTTON5_MASK: 5,
}

WINDOW_EVENT_MASK: Gdk.EventMask = Gdk.EventMask.STRUCTURE_MASK
WINDOW_EVENT_MASK |= Gdk.EventMask.KEY_PRESS_MASK | Gdk.EventMask.KEY_RELEASE_MASK
WINDOW_EVENT_MASK |= Gdk.EventMask.POINTER_MOTION_MASK
WINDOW_EVENT_MASK |= Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
WINDOW_EVENT_MASK |= Gdk.EventMask.PROPERTY_CHANGE_MASK | Gdk.EventMask.SCROLL_MASK

GRAB_EVENT_MASK: Gdk.EventMask = Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
GRAB_EVENT_MASK |= Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.POINTER_MOTION_HINT_MASK
GRAB_EVENT_MASK |= Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK

wth = Gdk.WindowTypeHint
ALL_WINDOW_TYPES: Sequence[Gdk.WindowTypeHint] = (
    wth.NORMAL,
    wth.DIALOG,
    wth.MENU,
    wth.TOOLBAR,
    wth.SPLASHSCREEN,
    wth.UTILITY,
    wth.DOCK,
    wth.DESKTOP,
    wth.DROPDOWN_MENU,
    wth.POPUP_MENU,
    wth.TOOLTIP,
    wth.NOTIFICATION,
    wth.COMBO,
    wth.DND,
)
del wth
WINDOW_NAME_TO_HINT: dict[str, Gdk.WindowTypeHint] = {
    wth.value_name.replace("GDK_WINDOW_TYPE_HINT_", ""): wth for wth in ALL_WINDOW_TYPES
}


def get_follow_window_types() -> Sequence[Gdk.WindowTypeHint]:
    types_strs: list[str] = os.environ.get(
        "XPRA_FOLLOW_WINDOW_TYPES",
        "DIALOG,MENU,TOOLBAR,DROPDOWN_MENU,POPUP_MENU,TOOLTIP,COMBO,DND"
    ).upper().split(",")
    if "*" in types_strs or "ALL" in types_strs:
        return ALL_WINDOW_TYPES
    types: list[Gdk.WindowTypeHint] = []
    for v in types_strs:
        try:
            hint = WINDOW_NAME_TO_HINT[v]
            types.append(hint)
        except KeyError:
            log.warn(f"Warning: invalid follow window type specified {v!r}")
            continue
    return tuple(types)


FOLLOW_WINDOW_TYPES = get_follow_window_types()


def wn(w) -> str:
    return WORKSPACE_NAMES.get(w, str(w))


def add_border_rectangles(rectangles: Sequence[tuple[int, int, int, int]],
                          ww: int, wh: int, border_size: int) -> Sequence[tuple[int, int, int, int]]:
    from xpra.util.rectangle import add_rectangle, rectangle
    # convert to rectangle objects:
    rects = list(rectangle(*rect) for rect in rectangles)
    # add border rectangles:
    bsize = border_size
    for x, y, w, h in (
            (0, 0, ww, bsize),  # top
            (0, wh - bsize, ww, bsize),  # bottom
            (ww - bsize, bsize, bsize, wh-bsize*2),  # right
            (0, bsize, bsize, wh-bsize*2),  # left
    ):
        if w > 0 and h > 0:
            add_rectangle(rects, rectangle(x, y, w, h))
    # convert rectangles back to tuples:
    return tuple((rect.x, rect.y, rect.width, rect.height) for rect in rects)


# noinspection PyTestUnpassedFixture
def is_awt(metadata) -> bool:
    wm_class = metadata.strtupleget("class-instance")
    return wm_class and len(wm_class) == 2 and wm_class[0].startswith("sun-awt-X11")


def _button_resolve(button: int) -> int:
    if WIN32 and button in (4, 5):
        # On Windows "X" buttons (the extra buttons sometimes found on the
        # side of the mouse) are numbered 4 and 5, as there is a different
        # API for scroll events. Convert them into the X11 convention of 8
        # and 9.
        return button + 4
    return button


def noop_destroy() -> None:
    log.warn("Warning: window destroy called twice!")


def _event_buttons(event) -> list[int]:
    return [button for mask, button in BUTTON_MASK.items() if event.state & mask]


class GTKClientWindowBase(ClientWindowBase, Gtk.Window):
    __gsignals__ = {
        "state-updated": no_arg_signal,
        "x11-focus-out-event": one_arg_signal,
        "x11-focus-in-event": one_arg_signal,
    }

    # maximum size of the actual window:
    MAX_VIEWPORT_DIMS = 16 * 1024, 16 * 1024
    # maximum size of the backing pixel buffer:
    MAX_BACKING_DIMS = 16 * 1024, 16 * 1024

    def init_window(self, metadata):
        self.init_max_window_size()
        if self._is_popup(metadata):
            window_type = Gtk.WindowType.POPUP
        else:
            window_type = Gtk.WindowType.TOPLEVEL
        self.on_realize_cb = {}
        Gtk.Window.__init__(self, type=window_type)
        self.set_app_paintable(True)
        # Ensure overlay attributes always exist, even when subclasses replace init_drawing_area
        self.overlay_container = None
        self.loading_overlay = None
        self._overlay_visible = False
        self._first_frame_received = False
        self._frame_count = 0
        self._overlay_timeout = 0
        # PATCH: Track painted regions for smart overlay removal
        # Use a list of rectangles to track which areas have been painted
        self._painted_regions = []  # List of (x, y, w, h) tuples
        self._coverage_grid: set[tuple[int, int]] = set()
        self._coverage_check_timer = 0
        self._completed_flushes = 0
        self._waiting_for_flush_completion = False
        self._saw_flush_value = False
        self._overlay_last_status = ""
        self._overlay_created_time = monotonic()
        self.init_drawing_area()
        self.set_decorated(self._is_decorated(metadata))
        self._window_state = {}
        self._resize_counter = 0
        self._can_set_workspace = CAN_SET_WORKSPACE
        self._current_frame_extents = None
        self._monitor = None
        self._frozen: bool = False
        self._focus_latest = None
        self._ondeiconify: list[Callable] = []
        self._follow = None
        self._follow_handler = 0
        self._follow_position = None
        self._follow_configure = None
        self.recheck_focus_timer: int = 0
        self.window_state_timer: int = 0
        self.send_iconify_timer: int = 0
        self.remove_pointer_overlay_timer: int = 0
        self.show_pointer_overlay_timer: int = 0
        self.moveresize_timer: int = 0
        self.moveresize_event = None
        self.workspace_timer = 0
        # PATCH: Preserve the initial server position for later comparisons
        self._original_server_position = self._pos
        # PATCH: Keep the original metadata for set_initial_position()
        # self._metadata is cleared in init_window() and only updated after setup_window()
        # Copy to avoid reference side effects
        if isinstance(metadata, typedict):
            self._initial_metadata = typedict(dict(metadata))
        else:
            self._initial_metadata = typedict(metadata)
        self._win32_shadow_disabled = False
        self._win32_shadow_trim = 0
        self._win32_shadow_region_applied = False
        self._win32_shadow_detect_attempts = 0
        self._win32_shadow_retry_source = 0
        # add platform hooks
        self.connect_after("realize", self.on_realize)
        self.connect("unrealize", self.on_unrealize)
        self.connect("map-event", self.on_map_event)
        self.connect("enter-notify-event", self.on_enter_notify_event)
        self.connect("leave-notify-event", self.on_leave_notify_event)
        self.connect("key-press-event", self.handle_key_press_event)
        self.connect("key-release-event", self.handle_key_release_event)
        self.add_events(self.get_window_event_mask())
        if DRAGNDROP and not self._client.readonly:
            self.init_dragndrop()
        self.init_workspace()
        self.remove_event_receiver = noop
        self.init_focus()
        ClientWindowBase.init_window(self, metadata)

    def init_drawing_area(self) -> None:
        widget = Gtk.DrawingArea()
        widget.set_app_paintable(True)
        widget.set_size_request(*self._size)
        widget.show()
        self.drawing_area = widget
        self.init_widget_events(widget)

        # PATCH: Use a Gtk.Overlay to hide the initial black flash with a light mint background (#EEF9F3)
        self.overlay_container = Gtk.Overlay()
        self.overlay_container.add(widget)

        # Create the overlay widget with color #EEF9F3
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
        self._frame_count = 0  # Number of frames received
        self._overlay_created_time = monotonic()

        # Timeout fallback: remove the overlay after 2.5s if no frame arrives
        self._overlay_timeout = GLib.timeout_add(OVERLAY_TIMEOUT_MS, self._remove_loading_overlay_fallback)

        self.add(self.overlay_container)
        self._reset_overlay_tracking()

    def repaint(self, x: int, y: int, w: int, h: int) -> None:
        if OSX:
            self.queue_draw_area(x, y, w, h)
            return
        widget = self.drawing_area
        # log("repaint%s widget=%s", (x, y, w, h), widget)
        if widget:
            widget.queue_draw_area(x, y, w, h)
    
    def _remove_loading_overlay(self) -> None:
        """
        PATCH: Remove the mint loading overlay.
        Called when sufficient coverage is achieved or when the fallback timeout fires.
        """
        if not self._overlay_visible:
            return  # Already removed
        
        self._overlay_visible = False
        self._first_frame_received = True
        
        # Cancel the timeout if it is still scheduled
        if self._overlay_timeout:
            GLib.source_remove(self._overlay_timeout)
            self._overlay_timeout = 0
        
        # Cancel coverage check timer if running
        if self._coverage_check_timer:
            GLib.source_remove(self._coverage_check_timer)
            self._coverage_check_timer = 0

        self._reset_overlay_tracking()

        # Hide the overlay widget
        if self.loading_overlay:
            self.loading_overlay.hide()
            # Could remove it entirely from the container if needed
            # self.overlay_container.remove(self.loading_overlay)
            # self.loading_overlay = None
        if WIN32 and self.is_OR() and self._is_wine_popup_menu():
            self._queue_win32_shadow_retry(0)

    def _reset_overlay_tracking(self) -> None:
        """
        PATCH: Reset overlay coverage bookkeeping so we only hide the overlay after a full paint.
        """
        self._painted_regions = []
        self._coverage_grid.clear()
        self._completed_flushes = 0
        self._waiting_for_flush_completion = False
        self._saw_flush_value = False
        self._overlay_last_status = ""
        self._frame_count = 0
        self._overlay_created_time = monotonic()

    def _overlay_reset_measurements(self) -> None:
        """
        PATCH: Reset overlay timers and measurements when geometry or content changes.
        """
        if not self._overlay_visible:
            return
        if self._coverage_check_timer:
            GLib.source_remove(self._coverage_check_timer)
            self._coverage_check_timer = 0
        self._reset_overlay_tracking()

    def _overlay_log(self, message: str, *args) -> None:
        """
        PATCH: Optional debug logging for overlay decisions.
        """
        if OVERLAY_LOG:
            log.info("overlay[%s] " + message, self.wid, *args)

    def _is_small_overlay_window(self) -> bool:
        ww, wh = self._size
        return (ww < 300 and wh < 300) or self.is_OR()
    
    def _remove_loading_overlay_fallback(self) -> bool:
        """
        PATCH: Fallback timeout to remove the overlay after 2.5s.
        Triggered when:
        - The network is slow and no frame arrived
        - A bug prevents any frames from being sent
        Returns False to avoid repeating the timeout.
        """
        if not self._overlay_visible:
            self._overlay_timeout = 0
            return False
        is_small_popup = self._is_small_overlay_window()
        required_flushes = OVERLAY_MIN_FLUSHES_SMALL if is_small_popup else OVERLAY_MIN_FLUSHES
        elapsed = (monotonic() - self._overlay_created_time) * 1000
        if elapsed < OVERLAY_MIN_DURATION_MS:
            wait_ms = OVERLAY_MIN_DURATION_MS - int(elapsed)
            self._overlay_log("timeout reached but minimum duration %dms not met (elapsed=%dms)",
                              OVERLAY_MIN_DURATION_MS, int(elapsed))
            self._overlay_timeout = GLib.timeout_add(max(OVERLAY_TIMEOUT_MS, wait_ms),
                                                     self._remove_loading_overlay_fallback)
            return False
        if self._frame_count == 0:
            self._overlay_log("timeout but no frames decoded yet; keeping overlay")
            self._overlay_timeout = GLib.timeout_add(OVERLAY_TIMEOUT_MS, self._remove_loading_overlay_fallback)
            return False
        if self._saw_flush_value and self._completed_flushes < required_flushes:
            self._overlay_log("timeout but only %d/%d flushes complete; keeping overlay",
                              self._completed_flushes, required_flushes)
            self._overlay_timeout = GLib.timeout_add(OVERLAY_TIMEOUT_MS, self._remove_loading_overlay_fallback)
            return False
        self._overlay_log("timeout removing overlay after %d frames and %d flushes",
                          self._frame_count, self._completed_flushes)
        self._remove_loading_overlay()
        self._overlay_timeout = 0
        return False  # Do not repeat
    
    def draw_region(self, x: int, y: int, width: int, height: int,
                    coding: str, img_data, rowstride: int,
                    options: typedict, callbacks):
        """
        PATCH: Override draw_region to track painted regions and remove overlay when coverage is sufficient.
        Uses smart coverage tracking instead of black pixel detection.
        """
        # Track painted regions for overlay removal (ignore "void" frames)
        if self._overlay_visible and coding != "void":
            flush_value = -1
            if hasattr(options, "intget"):
                flush_value = options.intget("flush", -1)
            elif isinstance(options, dict):
                flush_value = int(options.get("flush", -1))
            if flush_value >= 0:
                self._saw_flush_value = True
                if flush_value == 0:
                    if self._waiting_for_flush_completion:
                        self._waiting_for_flush_completion = False
                    self._completed_flushes += 1
                    if OVERLAY_LOG:
                        self._overlay_log("flush #%d complete after %d frames, coverage=%.1f%%",
                                          self._completed_flushes, self._frame_count,
                                          self._calculate_coverage() * 100.0)
                else:
                    self._waiting_for_flush_completion = True
            else:
                self._waiting_for_flush_completion = False
            self._frame_count += 1
            # Add this region to painted regions
            self._add_painted_region(x, y, width, height)
            
            # Add callback to check coverage after paint completes
            if callbacks:
                # Wrap existing callbacks to check coverage after paint
                original_callbacks = list(callbacks)
                def check_coverage_after_paint(success, message=""):
                    # Call original callbacks first
                    for cb in original_callbacks:
                        try:
                            cb(success, message)
                        except Exception:
                            pass
                    # Then check coverage after paint completes successfully
                    if success:
                        GLib.idle_add(self._check_coverage_and_remove_overlay)
                
                # Replace callbacks with wrapped version (safe modification)
                if isinstance(callbacks, list):
                    callbacks.clear()
                    callbacks.append(check_coverage_after_paint)
                else:
                    # If callbacks is not a list, create a new list
                    # This shouldn't happen, but be safe
                    callbacks = [check_coverage_after_paint]
            else:
                # No callbacks, check coverage directly after a short delay
                if not self._coverage_check_timer:
                    self._coverage_check_timer = GLib.timeout_add(50, self._check_coverage_and_remove_overlay)
        
        # Call the base implementation from ClientWindowBase
        return super().draw_region(x, y, width, height, coding, img_data, rowstride, options, callbacks)
    
    def _add_painted_region(self, x: int, y: int, w: int, h: int) -> None:
        """
        PATCH: Add a painted region to the tracking list.
        Merges overlapping regions to reduce memory usage.
        """
        if w <= 0 or h <= 0:
            return
        
        new_region = (x, y, w, h)
        
        # Simple merge: if new region overlaps significantly with existing ones, merge them
        # For efficiency, we use a simple grid-based approach for large windows
        ww, wh = self._size
        if ww > 0 and wh > 0:
            # For very large windows, use a grid to track coverage
            if ww * wh > 1000000:  # > 1MP
                # Use 16x16 grid (256 cells) to track coverage
                grid_size = 16
                cell_w = (ww + grid_size - 1) // grid_size
                cell_h = (wh + grid_size - 1) // grid_size
                # Mark cells covered by this region
                coverage_grid = self._coverage_grid
                start_cell_x = max(0, x // cell_w)
                end_cell_x = min(grid_size - 1, (x + w - 1) // cell_w)
                start_cell_y = max(0, y // cell_h)
                end_cell_y = min(grid_size - 1, (y + h - 1) // cell_h)

                for cy in range(start_cell_y, end_cell_y + 1):
                    for cx in range(start_cell_x, end_cell_x + 1):
                        coverage_grid.add((cx, cy))
            else:
                # For smaller windows, track exact regions
                # Simple approach: just add the region (could be optimized with region merging)
                self._painted_regions.append(new_region)
    
    def _check_coverage_and_remove_overlay(self) -> bool:
        """
        PATCH: Check if enough of the window has been painted to remove the overlay.
        Returns True if timer should continue, False to stop.
        """
        if not self._overlay_visible:
            self._coverage_check_timer = 0
            return False
        
        ww, wh = self._size
        if ww <= 0 or wh <= 0:
            self._coverage_check_timer = 0
            return False
        
        total_area = ww * wh
        if total_area == 0:
            self._coverage_check_timer = 0
            return False
        
        # Check coverage
        coverage = self._calculate_coverage()
        
        # Detect small popups (IME candidate, tooltip, etc) - remove quickly
        is_small_popup = self._is_small_overlay_window()
        coverage_threshold = 0.95 if is_small_popup else 0.985
        flush_ready = (not self._saw_flush_value) or (not self._waiting_for_flush_completion)
        required_flushes = OVERLAY_MIN_FLUSHES_SMALL if is_small_popup else OVERLAY_MIN_FLUSHES
        flush_count_ready = (not self._saw_flush_value) or (self._completed_flushes >= required_flushes)
        if coverage >= coverage_threshold and not self._first_frame_received:
            elapsed = (monotonic() - self._overlay_created_time) * 1000
            if elapsed < OVERLAY_MIN_DURATION_MS:
                if OVERLAY_LOG and self._overlay_last_status != "waiting-min-duration":
                    self._overlay_last_status = "waiting-min-duration"
                    self._overlay_log(
                        "coverage %.1f%% met but waiting minimum duration (%d/%d ms)",
                        coverage * 100.0, int(elapsed), OVERLAY_MIN_DURATION_MS)
                return True
            if flush_ready and flush_count_ready:
                self._first_frame_received = True
                delay = 100 if is_small_popup else 150
                self._overlay_log(
                    "coverage %.1f%% reached with %d flushes after %d frames, scheduling overlay removal in %dms",
                    coverage * 100.0, self._completed_flushes, self._frame_count, delay)
                self._overlay_last_status = ""
                GLib.timeout_add(delay, self._remove_loading_overlay_delayed)
                self._coverage_check_timer = 0
                return False
            if OVERLAY_LOG:
                if self._waiting_for_flush_completion and self._saw_flush_value:
                    reason = "waiting for final damage flush"
                elif not flush_count_ready and self._saw_flush_value:
                    reason = f"waiting for {required_flushes} flushes (have {self._completed_flushes})"
                elif self._overlay_last_status == "waiting-min-duration":
                    reason = "waiting minimum duration"
                else:
                    reason = "waiting for implicit flush confirmation"
                if reason != self._overlay_last_status:
                    self._overlay_last_status = reason
                    self._overlay_log(
                        "%s (coverage=%.1f%%, frames=%d, flushes=%d, saw_flush=%s)",
                        reason, coverage * 100.0, self._frame_count,
                        self._completed_flushes, self._saw_flush_value)

        # Continue checking
        return True
    
    def _calculate_coverage(self) -> float:
        """
        PATCH: Calculate the percentage of window area that has been painted.
        Returns a value between 0.0 and 1.0.
        """
        ww, wh = self._size
        if ww <= 0 or wh <= 0:
            return 0.0
        
        total_area = ww * wh
        
        # Use grid-based approach for large windows
        if self._coverage_grid:
            grid_size = 16
            covered_cells = len(self._coverage_grid)
            total_cells = grid_size * grid_size
            return covered_cells / total_cells if total_cells > 0 else 0.0
        
        # For smaller windows, calculate union of painted regions
        if not self._painted_regions:
            return 0.0
        
        # Simple approximation: sum of all region areas (may overcount overlaps)
        # For better accuracy, we could use a proper region union algorithm
        painted_area = 0
        for x, y, w, h in self._painted_regions:
            # Clamp to window bounds
            x1 = max(0, min(ww, x))
            y1 = max(0, min(wh, y))
            x2 = max(0, min(ww, x + w))
            y2 = max(0, min(wh, y + h))
            painted_area += (x2 - x1) * (y2 - y1)
        
        # Cap at total area (to handle overlaps)
        painted_area = min(painted_area, total_area)
        return painted_area / total_area if total_area > 0 else 0.0
    
    def _check_black_percentage(self, img_data, width: int, height: int, 
                                 coding: str, rowstride: int) -> float:
        """
        PATCH: Estimate the percentage of black pixels in the frame.
        Sample a subset of pixels for speed.
        Returns: black pixel percentage (0-100).
        """
        try:
            # Only inspect RGB/RGBA formats
            if coding not in ("rgb24", "rgb32", "rgba", "rgbx"):
                return 0  # Skip non-RGB formats
            
            if not img_data or width <= 0 or height <= 0:
                return 100  # Empty data counts as black
            
            # Determine bytes per pixel
            if coding in ("rgb24",):
                bpp = 3
            else:  # rgb32, rgba, rgbx
                bpp = 4
            
            # Sample a 10x10 grid (~100 pixels) for speed
            sample_size = 10
            step_x = max(1, width // sample_size)
            step_y = max(1, height // sample_size)
            
            black_count = 0
            total_count = 0
            
            # Handle bytes / bytearray / memoryview input
            if isinstance(img_data, (bytes, bytearray, memoryview)):
                data = img_data if isinstance(img_data, (bytes, bytearray)) else bytes(img_data)
                
                for y in range(0, height, step_y):
                    for x in range(0, width, step_x):
                        offset = y * rowstride + x * bpp
                        if offset + bpp <= len(data):
                            r = data[offset]
                            g = data[offset + 1]
                            b = data[offset + 2]
                            
                            # Pixel is black if R, G, and B are all < 30
                            if r < 30 and g < 30 and b < 30:
                                black_count += 1
                            total_count += 1
            
            if total_count == 0:
                return 100
            
            return (black_count / total_count) * 100
            
        except Exception as e:
            # On error, treat as non-black so the overlay can be removed
            log("Error checking black percentage: %s", e)
            return 0
    
    def _remove_loading_overlay_delayed(self) -> bool:
        """
        PATCH: Remove the overlay after a 250ms delay.
        Called when the frame is no longer mostly black (< 30% black pixels).
        Ensures GTK has painted before hiding the overlay.
        """
        if self._overlay_visible:
            self._remove_loading_overlay()
        return False  # Do not repeat

    def get_window_event_mask(self) -> Gdk.EventMask:
        mask = WINDOW_EVENT_MASK
        if self._client.wheel_smooth:
            mask |= Gdk.EventMask.SMOOTH_SCROLL_MASK
        return mask

    def init_widget_events(self, widget) -> None:
        widget.add_events(self.get_window_event_mask())

        def motion(_w, event) -> bool:
            self._do_motion_notify_event(event)
            return True

        widget.connect("motion-notify-event", motion)

        def press(_w, event) -> bool:
            self._do_button_press_event(event)
            return True

        widget.connect("button-press-event", press)

        def release(_w, event) -> bool:
            self._do_button_release_event(event)
            return True

        widget.connect("button-release-event", release)

        def scroll(_w, event) -> bool:
            self._do_scroll_event(event)
            return True

        def configure_event(_w, event) -> bool:
            geomlog("widget configure_event: new size=%ix%i", event.width, event.height)
            return True

        widget.connect("configure-event", configure_event)
        widget.connect("scroll-event", scroll)
        widget.connect("draw", self.draw_widget)

    def draw_widget(self, widget, context) -> bool:
        raise NotImplementedError()

    def get_drawing_area_geometry(self) -> tuple[int, int, int, int]:
        raise NotImplementedError()

    ######################################################################
    # drag and drop:
    def init_dragndrop(self) -> None:
        targets = [
            Gtk.TargetEntry.new("text/uri-list", 0, 80),
        ]
        flags = Gtk.DestDefaults.MOTION | Gtk.DestDefaults.HIGHLIGHT
        actions = Gdk.DragAction.COPY  # | Gdk.ACTION_LINK
        self.drag_dest_set(flags, targets, actions)
        self.connect('drag_drop', self.drag_drop_cb)
        self.connect('drag_motion', self.drag_motion_cb)
        self.connect('drag_data_received', self.drag_got_data_cb)

    def drag_drop_cb(self, widget, context, x: int, y: int, time: int) -> None:
        targets = list(x.name() for x in context.list_targets())
        draglog("drag_drop_cb%s targets=%s", (widget, context, x, y, time), targets)
        if not targets:
            # this happens on macOS, but we can still get the data...
            draglog("Warning: no targets provided, continuing anyway")
        elif "text/uri-list" not in targets:
            draglog("Warning: cannot handle targets:")
            draglog(" %s", csv(targets))
            return
        atom = Gdk.Atom.intern("text/uri-list", False)
        widget.drag_get_data(context, atom, time)

    def drag_motion_cb(self, wid: int, context, x: int, y: int, time: int):
        draglog("drag_motion_cb%s", (wid, context, x, y, time))
        Gdk.drag_status(context, Gdk.DragAction.COPY, time)
        return True  # accept this data

    def drag_got_data_cb(self, wid: int, context, x: int, y: int, selection, info, time: int) -> None:
        draglog("drag_got_data_cb%s", (wid, context, x, y, selection, info, time))
        targets = list(x.name() for x in context.list_targets())
        actions = context.get_actions()

        def xid(w) -> int:
            # TODO: use a generic window handle function
            # this only used for debugging for now
            if w and POSIX:
                return w.get_xid()
            return 0

        dest_window = xid(context.get_dest_window())
        source_window = xid(context.get_source_window())
        suggested_action = context.get_suggested_action()
        draglog("drag_got_data_cb context: source_window=%#x, dest_window=%#x",
                source_window, dest_window)
        draglog("drag_got_data_cb context: suggested_action=%s, actions=%s, targets=%s",
                suggested_action, actions, targets)
        dtype = selection.get_data_type()
        fmt = selection.get_format()
        length = selection.get_length()
        target = selection.get_target()
        text = selection.get_text()
        uris = selection.get_uris()
        draglog("drag_got_data_cb selection: data type=%s, format=%s, length=%s, target=%s, text=%s, uris=%s",
                dtype, fmt, length, target, text, uris)
        if not uris:
            return
        filelist = []
        for uri in uris:
            if not uri:
                continue
            if not uri.startswith("file://"):
                draglog.warn("Warning: cannot handle drag-n-drop URI '%s'", uri)
                continue
            filename = unquote(uri[len("file://"):].rstrip("\n\r"))
            if WIN32:
                filename = filename.lstrip("/")
            abspath = os.path.abspath(filename)
            if not os.path.isfile(abspath):
                draglog.warn("Warning: '%s' is not a file", abspath)
                continue
            filelist.append(abspath)
        draglog("drag_got_data_cb: will try to upload: %s", csv(filelist))
        pending = set(filelist)

        def file_done(filename: str) -> None:
            if not pending:
                return
            try:
                pending.remove(filename)
            except KeyError:
                pass
            # when all the files have been loaded / failed,
            # finish the drag and drop context so the source knows we're done with them:
            if not pending:
                context.finish(True, False, time)

        # we may want to only process a limited number of files "at the same time":
        for filename in filelist:
            self.drag_process_file(filename, file_done)

    def drag_process_file(self, filename: str, file_done_cb: Callable) -> None:
        def got_file_info(gfile, result, arg=None):
            draglog("got_file_info(%s, %s, %s)", gfile, result, arg)
            file_info = gfile.query_info_finish(result)
            basename = gfile.get_basename()
            ctype = file_info.get_content_type()
            size = file_info.get_size()
            draglog("file_info(%s)=%s ctype=%s, size=%s", filename, file_info, ctype, size)

            def got_file_data(gfile, result, user_data=None) -> None:
                _, data, entity = gfile.load_contents_finish(result)
                filesize = len(data)
                draglog("got_file_data(%s, %s, %s) entity=%s", gfile, result, user_data, entity)
                file_done_cb(filename)
                openit = self._client.remote_open_files
                draglog.info("sending file %s (%i bytes)", basename, filesize)
                self._client.send_file(filename, "", data, filesize=filesize, openit=openit)

            cancellable = None
            user_data = (filename, True)
            gfile.load_contents_async(cancellable, got_file_data, user_data)

        try:
            gfile = Gio.File.new_for_path(path=filename)
            # basename = gf.get_basename()
            FILE_QUERY_INFO_NONE = 0
            G_PRIORITY_DEFAULT = 0
            cancellable = None
            gfile.query_info_async("standard::*", FILE_QUERY_INFO_NONE, G_PRIORITY_DEFAULT,
                                   cancellable, got_file_info, None)
        except Exception as e:
            draglog("file upload for %s:", filename, exc_info=True)
            draglog.error("Error: cannot upload '%s':", filename)
            draglog.estr(e)
            del e
            file_done_cb(filename)

    ######################################################################
    # focus:
    def init_focus(self) -> None:
        self.when_realized("init-focus", self.do_init_focus)

    def do_init_focus(self) -> None:
        # hook up the X11 gdk event notifications,
        # so we can get focus-out when grabs are active:
        if is_X11():
            try:
                from xpra.x11.gtk.bindings import add_event_receiver, remove_event_receiver
            except ImportError as e:
                log("do_init_focus()", exc_info=True)
                if not ds_inited():
                    log.warn("Warning: missing Gdk X11 bindings:")
                    log.warn(" %s", e)
                    log.warn(" you may experience window focus issues")
            else:
                grablog("adding event receiver so we can get FocusIn and FocusOut events whilst grabbing the keyboard")
                xid = self.get_window().get_xid()
                add_event_receiver(xid, self)

                def remove_hook() -> None:
                    remove_event_receiver(xid, self)
                self.remove_event_receiver = remove_hook

        # other platforms should bet getting regular focus events instead:

        def focus_in(_window, event) -> None:
            focuslog("focus-in-event for wid=%s", self.wid)
            self.do_x11_focus_in_event(event)

        def focus_out(_window, event) -> None:
            focuslog("focus-out-event for wid=%s", self.wid)
            self.do_x11_focus_out_event(event)

        self.connect("focus-in-event", focus_in)
        self.connect("focus-out-event", focus_out)
        if not self._override_redirect:
            self.connect("notify::has-toplevel-focus", self._focus_change)

        def grab_broken(win, event) -> None:
            grablog("grab_broken%s", (win, event))
            self._client._window_with_grab = None

        self.connect("grab-broken-event", grab_broken)

    def _focus_change(self, *args) -> None:
        assert not self._override_redirect
        htf = self.has_toplevel_focus()
        focuslog("%s focus_change%s has-toplevel-focus=%s, _been_mapped=%s", self, args, htf, self._been_mapped)
        if self._been_mapped:
            self._focus_latest = htf
            self.schedule_recheck_focus()

    def recheck_focus(self) -> None:
        self.recheck_focus_timer = 0
        self.send_latest_focus()

    def send_latest_focus(self) -> None:
        focused = self._client._focused
        focuslog("send_latest_focus() wid=%i, focused=%s, latest=%s", self.wid, focused, self._focus_latest)
        if self._focus_latest:
            self._focus()
        else:
            self._unfocus()

    def on_enter_notify_event(self, window, event) -> None:
        focuslog("on_enter_notify_event(%s, %s)", window, event)
        if AUTOGRAB_WITH_POINTER:
            self.may_autograb()

    def on_leave_notify_event(self, window, event) -> None:
        info = {}
        for attr in ("detail", "focus", "mode", "subwindow", "type", "window"):
            info[attr] = getattr(event, attr, None)
        focuslog("on_leave_notify_event(%s, %s) crossing event fields: %s", window, event, info)
        if AUTOGRAB_WITH_POINTER and (event.subwindow or event.detail == Gdk.NotifyType.NONLINEAR_VIRTUAL):
            self.keyboard_ungrab()

    def may_autograb(self) -> bool:
        server_mode = self._client._remote_server_mode
        autograb = AUTOGRAB_MODES and any(x == "*" or server_mode.find(x) >= 0 for x in AUTOGRAB_MODES)
        focuslog("may_autograb() server-mode=%s, autograb(%s)=%s", server_mode, AUTOGRAB_MODES, autograb)
        if autograb:
            self.keyboard_grab()
        return autograb

    def _focus(self) -> bool:
        change = super()._focus()
        if change and AUTOGRAB_WITH_FOCUS:
            self.may_autograb()
        return change

    def _unfocus(self) -> bool:
        client = self._client
        client.window_ungrab()
        if client.pointer_grabbed and client.pointer_grabbed == self.wid:
            # we lost focus, assume we also lost the grab:
            client.pointer_grabbed = None
        changed = super()._unfocus()
        if changed and AUTOGRAB_WITH_FOCUS:
            self.keyboard_ungrab()
        return changed

    def cancel_focus_timer(self) -> None:
        rft = self.recheck_focus_timer
        if rft:
            self.recheck_focus_timer = 0
            GLib.source_remove(rft)

    def schedule_recheck_focus(self) -> None:
        if self._override_redirect:
            # never send focus events for OR windows
            return
        # we receive pairs of FocusOut + FocusIn following a keyboard grab,
        # so we recheck the focus status via this timer to skip unnecessary churn
        if FOCUS_RECHECK_DELAY < 0:
            self.recheck_focus()
        elif self.recheck_focus_timer == 0:
            focuslog(f"will recheck focus in {FOCUS_RECHECK_DELAY}ms")
            self.recheck_focus_timer = GLib.timeout_add(FOCUS_RECHECK_DELAY, self.recheck_focus)

    def do_x11_focus_out_event(self, event) -> None:
        focuslog("do_x11_focus_out_event(%s)", event)
        if NotifyInferior is not None:
            detail = getattr(event, "detail", None)
            if detail == NotifyInferior:
                focuslog("dropped NotifyInferior focus event")
                return
        self._focus_latest = False
        self.schedule_recheck_focus()

    def do_x11_focus_in_event(self, event) -> None:
        focuslog("do_x11_focus_in_event(%s) been_mapped=%s", event, self._been_mapped)
        if self._been_mapped:
            self._focus_latest = True
            self.schedule_recheck_focus()

    def init_max_window_size(self) -> None:
        """ used by GL windows to enforce a hard limit on window sizes """
        saved_mws = self.max_window_size

        def clamp_to(maxw: int, maxh: int):
            # don't bother if the new limit is greater than 16k:
            if maxw >= 16 * 1024 and maxh >= 16 * 1024:
                return
            # only take into account the current max-window-size if non-zero:
            mww, mwh = self.max_window_size
            if mww > 0:
                maxw = min(mww, maxw)
            if mwh > 0:
                maxh = min(mwh, maxh)
            self.max_window_size = maxw, maxh

        # viewport is easy, measured in window pixels:
        clamp_to(*self.MAX_VIEWPORT_DIMS)
        # backing dimensions are harder,
        # we have to take scaling into account (if any):
        clamp_to(*self.sp(*self.MAX_BACKING_DIMS))
        if self.max_window_size != saved_mws:
            log("init_max_window_size(..) max-window-size changed from %s to %s",
                saved_mws, self.max_window_size)
            log(" because of max viewport dims %s and max backing dims %s",
                self.MAX_VIEWPORT_DIMS, self.MAX_BACKING_DIMS)

    def _is_popup(self, metadata) -> bool:
        # decide if the window type is POPUP or NORMAL
        if self._override_redirect:
            return True
        if UNDECORATED_TRANSIENT_IS_OR > 0:
            transient_for = metadata.intget("transient-for", -1)
            decorations = metadata.intget("decorations", 0)
            # noinspection PyChainedComparisons
            if transient_for > 0 and decorations <= 0:
                if UNDECORATED_TRANSIENT_IS_OR > 1:
                    metalog("forcing POPUP type for window transient-for=%s", transient_for)
                    return True
                if metadata.get("skip-taskbar") and is_awt(metadata):
                    metalog("forcing POPUP type for Java AWT skip-taskbar window, transient-for=%s", transient_for)
                    return True
        window_types = metadata.strtupleget("window-type")
        popup_types = tuple(POPUP_TYPE_HINTS.intersection(window_types))
        metalog("popup_types(%s)=%s", window_types, popup_types)
        if popup_types:
            metalog("forcing POPUP window type for %s", popup_types)
            return True
        return False

    def _is_decorated(self, metadata: typedict) -> bool:
        # decide if the window type is POPUP or NORMAL
        # (show window decorations or not)
        if self._override_redirect:
            return False
        return metadata.boolget("decorations", True)

    def _is_wine_popup_menu(self) -> bool:
        """
        Detect Wine popup menus/submenus using multiple heuristics.
        Wine's X11 driver does not translate window styles correctly:
        - WS_EX_TOOLWINDOW should become _NET_WM_WINDOW_TYPE_DROPDOWN_MENU
        - but often ends up as _NET_WM_WINDOW_TYPE_NORMAL

        Heuristics:
        1. override-redirect=True (required)
        2. Either has transient-for or other menu traits:
           - small size (menus usually < 500x500)
           - no decorations
           - empty window title
           - class name contains "menu" / "popup" / "dropdown"
           - menu-like window type in metadata (even if not DIALOG)
        """
        if not self.is_OR():
            return False
        
        # Heuristic 1: override-redirect=True is mandatory
        if not self._override_redirect:
            return False
        
        # Heuristic 2: look for a transient window
        def find(attr: str):
            return self._client.find_window(self._metadata, attr)
        
        follow_window = find("transient-for") or find("parent")
        has_transient = follow_window is not None
        
        # Transient means it is definitely a popup menu
        if has_transient:
            geomlog("WINE POPUP: detected via transient-for (wid=%s, follow_window=%s)", self.wid, follow_window)
            return True
        
        # Without a transient, check other popup characteristics
        # Heuristic 3: small size (menus usually < 500x500)
        w, h = self._size
        is_small = w < 500 and h < 500
        
        # Heuristic 4: no decorations
        has_decorations = self._metadata.intget("decorations", 1) > 0
        is_undecorated = not has_decorations
        
        # Heuristic 5: empty window title
        title = self._metadata.strget("title", "")
        has_empty_title = not title or title.strip() == ""
        
        # Heuristic 6: window class hints include menu/popup/dropdown
        wm_class = self._metadata.strtupleget("class-instance", (None, None), 2, 2)
        class_name = ""
        if wm_class and len(wm_class) >= 2:
            class_name = (wm_class[0] or "").lower() + " " + (wm_class[1] or "").lower()
        has_menu_in_class = "menu" in class_name or "popup" in class_name or "dropdown" in class_name
        
        # Heuristic 7: menu-like window type in metadata
        window_types = self._metadata.strtupleget("window-type", ())
        has_menu_type = any(t in ("MENU", "DROPDOWN_MENU", "POPUP_MENU", "DIALOG") for t in window_types)
        
        # Combine heuristics:
        # Consider it a popup if at least two of: small size, undecorated, empty title, menu in class, menu type
        criteria_count = sum([
            is_small,
            is_undecorated,
            has_empty_title,
            has_menu_in_class,
            has_menu_type
        ])
        
        is_likely_popup = criteria_count >= 2
        
        if is_likely_popup:
            geomlog("WINE POPUP: detected via heuristics (wid=%s, criteria_count=%d: small=%s, undecorated=%s, empty_title=%s, menu_class=%s, menu_type=%s)",
                   self.wid, criteria_count, is_small, is_undecorated, has_empty_title, has_menu_in_class, has_menu_type)
            return True
        
        geomlog("WINE POPUP: NOT detected (wid=%s, OR=%s, has_transient=%s, criteria_count=%d)", 
               self.wid, self.is_OR(), has_transient, criteria_count)
        return False

    def set_decorated(self, decorated: bool) -> None:
        was_decorated = self.get_decorated()
        if self._fullscreen and was_decorated and not decorated:
            # fullscreen windows aren't decorated anyway!
            # calling set_decorated(False) would cause it to get unmapped! (why?)
            pass
        else:
            Gtk.Window.set_decorated(self, decorated)
        if WIN32:
            # workaround for new window offsets:
            # keep the window contents where they were and adjust the frame
            # this generates a configure event which ensures the server has the correct window position
            wfs = self._client.get_window_frame_sizes()
            if wfs and decorated and not was_decorated:
                geomlog("set_decorated(%s) re-adjusting window location using %s", decorated, wfs)
                normal = wfs.get("normal")
                fixed = wfs.get("fixed")
                if normal and fixed:
                    nx, ny = normal
                    fx, fy = fixed
                    x, y = self.get_position()
                    Gtk.Window.move(self, max(0, x - nx + fx), max(0, y - ny + fy))

    def setup_window(self, *args) -> None:
        log("setup_window%s OR=%s", args, self._override_redirect)
        self.set_alpha()

        self.connect("property-notify-event", self.property_changed)
        self.connect("window-state-event", self.window_state_updated)

        # this will create the backing:
        ClientWindowBase.setup_window(self, *args)

        # try to honour the initial position
        geomlog("setup_window() position=%s, set_initial_position=%s, OR=%s, decorated=%s",
                self._pos, self._set_initial_position, self.is_OR(), self.get_decorated())
        # honour "set-initial-position"
        if self._set_initial_position or self.is_OR():
            self.set_initial_position(self._requested_position or self._pos)
        self.set_default_size(*self._size)

    def set_initial_position(self, pos) -> None:
        x, y = self.adjusted_position(*pos)
        w, h = self._size
        if self.is_OR():
            # PATCH: Skip offset calculation for Wine popup menus
            # Wine apps create popup menus with override-redirect=True and the server already provides the right position
            # Use helper heuristics to detect them
            
            # PATCH: Use the initial metadata snapshot saved in init_window() because self._metadata may not be updated yet
            initial_metadata = getattr(self, '_initial_metadata', self._metadata)
            
            # Temporarily swap metadata so _is_wine_popup_menu() sees the full dataset
            original_metadata = self._metadata
            self._metadata = initial_metadata
            try:
                is_wine_popup = self._is_wine_popup_menu()
            finally:
                # Restore metadata
                self._metadata = original_metadata
            
            # Fallback: if detection failed, check simpler hints
            if not is_wine_popup:
                # Check hints from the initial metadata
                window_types = initial_metadata.strtupleget("window-type", ())
                has_dialog_type = "DIALOG" in window_types
                title = initial_metadata.strget("title", "")
                has_empty_title = not title or title.strip() == ""
                # If we have a DIALOG type or empty title, this may be a popup menu
                # Skip the offset for now and re-check in set_metadata()
                if has_dialog_type or has_empty_title:
                    geomlog("WINE POPUP set_initial_position: fallback detection (wid=%s, DIALOG=%s, empty_title=%s)", 
                           self.wid, has_dialog_type, has_empty_title)
                    geomlog("WINE POPUP set_initial_position: skipping offset temporarily, will recheck in set_metadata (wid=%s)", self.wid)
                    geomlog("WINE POPUP set_initial_position: using position as-is: (%s, %s) (wid=%s)", x, y, self.wid)
                    self.window_offset = None
                    is_wine_popup = True  # Mark to skip the logic below
            
            if is_wine_popup:
                # Wine popup menu: keep the server-provided position, no offset needed
                geomlog("WINE POPUP set_initial_position: skipping offset calculation (wid=%s)", self.wid)
                geomlog("WINE POPUP set_initial_position: using Wine-provided position as-is: (%s, %s) (wid=%s)", x, y, self.wid)
                self.window_offset = None
                # PATCH: Skip frame offset adjustment for Wine popup menus (even if decorated=True)
                # This must run in the OR branch because OR windows never reach the decorated branch below
                geomlog("WINE POPUP setup_window: skipping frame offset adjustment (wid=%s, pos=%s)", self.wid, (x, y))
            else:
                # Original logic for other OR windows (tooltips, DND, etc.)
                if self._client._current_screen_sizes:
                    self.window_offset = self.calculate_window_offset(x, y, w, h)
                    geomlog("OR offsets=%s (calculated)", self.window_offset)
                    if self.window_offset:
                        x += self.window_offset[0]
                        y += self.window_offset[1]
                        geomlog("   Position adjusted to: (%s, %s)", x, y)
                else:
                    self.window_offset = None
                    geomlog("OR offsets=None (no screen sizes)")
        elif self.get_decorated():
            # try to adjust for window frame size if we can figure it out:
            # Note: we cannot just call self.get_window_frame_size() here because
            # the window is not realized yet, and it may take a while for the window manager
            # to set the frame-extents property anyway
            # PATCH: Skip frame offset adjustment for Wine popup menus; the server already provides absolute positions
            is_wine_popup = False
            if self.is_OR():
                is_wine_popup = self._is_wine_popup_menu()
            
            if not is_wine_popup:
                wfs = self._client.get_window_frame_sizes()
                if wfs:
                    geomlog("setup_window() window frame sizes=%s", wfs)
                    v = wfs.get("offset")
                    if v:
                        dx, dy = v
                        x = max(32 - w, x - dx)
                        y = max(32 - h, y - dy)
                        self._pos = x, y
                        geomlog("setup_window() adjusted initial position=%s", self._pos)
            else:
                geomlog("WINE POPUP setup_window: skipping frame offset adjustment (wid=%s, pos=%s)", self.wid, (x, y))
        self.move(x, y)

    def finalize_window(self) -> None:
        if not self.is_tray():
            self.setup_following()

    def set_metadata(self, metadata: typedict):
        # Call parent method first to update metadata
        super().set_metadata(metadata)
        
        # PATCH: Re-check position for Wine popup menus after metadata is updated.
        # Issue: set_initial_position() runs before metadata has transient-for,
        # so has_transient=False and offset handling may be wrong.
        # Re-evaluate once metadata is available.
        
        if self.is_OR() and self._pos:
            # Only check when we already have a position and use the helper to detect Wine popups
            is_wine_popup = self._is_wine_popup_menu()
            
            if is_wine_popup:
                # Wine popup detected after metadata refresh: ensure position matches the server (no offsets)
                geomlog("WINE POPUP set_metadata: detected (wid=%s)", self.wid)
                
                # Current position
                current_x, current_y = self._pos
                
                # Original server position saved in init_window
                original_x, original_y = getattr(self, '_original_server_position', self._pos)
                
                # Or use requested-position from metadata if present
                requested_pos = self._metadata.intpair("requested-position", None)
                if requested_pos:
                    original_x, original_y = requested_pos
                
                # Check if the window is visible to avoid jumps
                is_mapped = self.get_mapped()
                is_visible = False
                if self.get_realized():
                    gdkwin = self.get_window()
                    if gdkwin:
                        is_visible = gdkwin.is_visible()
                
                # Compare current position with the original server position
                position_changed = (current_x != original_x) or (current_y != original_y)
                
                # If an offset was applied, revert it
                if self.window_offset:
                    # Revert the offset to use the Wine-provided position
                    corrected_x = current_x - self.window_offset[0]
                    corrected_y = current_y - self.window_offset[1]
                    
                    # Only revert when the window is not visible to avoid jumps
                    if not is_visible and not is_mapped:
                        geomlog("WINE POPUP set_metadata: reverting offset (wid=%s)", self.wid)
                        geomlog("WINE POPUP set_metadata: current position (with offset): (%s, %s) (wid=%s)", current_x, current_y, self.wid)
                        geomlog("WINE POPUP set_metadata: reverting offset: %s (wid=%s)", self.window_offset, self.wid)
                        geomlog("WINE POPUP set_metadata: correct position (no offset): (%s, %s) (wid=%s)", corrected_x, corrected_y, self.wid)
                        
                        # Clear the offset and move back to the right position
                        self.window_offset = None
                        self.move(corrected_x, corrected_y)
                        self._pos = (corrected_x, corrected_y)
                        geomlog("WINE POPUP set_metadata: position corrected to: (%s, %s) (wid=%s)", corrected_x, corrected_y, self.wid)
                    else:
                        # Window already visible: avoid moving to prevent jumps, just clear the offset
                        geomlog("WINE POPUP set_metadata: window already visible, skipping move to avoid jump (wid=%s)", self.wid)
                        geomlog("WINE POPUP set_metadata: current position (with offset): (%s, %s) (wid=%s)", current_x, current_y, self.wid)
                        geomlog("WINE POPUP set_metadata: would revert to (no offset): (%s, %s) (wid=%s)", corrected_x, corrected_y, self.wid)
                        geomlog("WINE POPUP set_metadata: keeping current position, offset cleared (wid=%s)", self.wid)
                        # Clear offset only
                        self.window_offset = None
                elif position_changed:
                    # No offset but position changed because of other logic; revert to the server position
                    if not is_visible and not is_mapped:
                        geomlog("WINE POPUP set_metadata: position was changed, reverting to original (wid=%s)", self.wid)
                        geomlog("WINE POPUP set_metadata: current position: (%s, %s) (wid=%s)", current_x, current_y, self.wid)
                        geomlog("WINE POPUP set_metadata: original server position: (%s, %s) (wid=%s)", original_x, original_y, self.wid)
                        
                        # Move back to the original server position
                        self.move(original_x, original_y)
                        self._pos = (original_x, original_y)
                        self.window_offset = None
                        geomlog("WINE POPUP set_metadata: position corrected to original: (%s, %s) (wid=%s)", original_x, original_y, self.wid)
                    else:
                        # Window already visible: avoid moving to prevent jumps
                        geomlog("WINE POPUP set_metadata: position changed but window already visible (wid=%s)", self.wid)
                        geomlog("WINE POPUP set_metadata: current position: (%s, %s), original: (%s, %s) (wid=%s)", 
                               current_x, current_y, original_x, original_y, self.wid)
                        geomlog("WINE POPUP set_metadata: keeping current position to avoid jump (wid=%s)", self.wid)
                else:
                    # Position already matches the server, nothing to do
                    geomlog("WINE POPUP set_metadata: position already correct (wid=%s)", self.wid)
                    geomlog("WINE POPUP set_metadata: position: (%s, %s) (wid=%s)", current_x, current_y, self.wid)

    def setup_following(self) -> None:
        # find a parent window we should follow when it moves:
        def find(attr: str):
            return self._client.find_window(self._metadata, attr)

        follow = find("transient-for") or find("parent")
        log("setup_following() follow=%s", follow)
        if not follow or not isinstance(follow, Gtk.Window):
            return
        type_hint = self.get_type_hint()
        log("setup_following() type_hint=%s, FOLLOW_WINDOW_TYPES=%s", type_hint, FOLLOW_WINDOW_TYPES)
        
        # PATCH: Skip following for Wine popup menus
        # Wine popups already use absolute coordinates; following the parent would reposition them incorrectly
        # Use helper heuristics to detect them
        
        is_wine_popup = self._is_wine_popup_menu()
        
        if is_wine_popup:
            geomlog("WINE POPUP setup_following: skipping following (wid=%s, follow=%s)", self.wid, follow)
            return
        
        if not self._override_redirect and type_hint not in FOLLOW_WINDOW_TYPES:
            return

        def follow_configure_event(window, event) -> bool:
            follow = self._follow
            rp = self._follow_position
            log("follow_configure_event(%s, %s) follow=%s, relative position=%s",
                window, event, follow, rp)
            if not follow or not rp:
                return False
            fpos = getattr(follow, "_pos", None)
            log("follow_configure_event: %s moved to %s", follow, fpos)
            if not fpos:
                return False
            fx, fy = fpos
            rx, ry = rp
            x, y = self.get_position()
            newx, newy = fx + follow.sx(rx), fy + follow.sy(ry)
            log("follow_configure_event: new position from %s: %s", self._pos, (newx, newy))
            if newx != x or newy != y:
                # don't update the relative position on the next configure event,
                # since we're generating it
                self._follow_configure = monotonic(), (newx, newy)
                self.move(newx, newy)
            return True

        self.cancel_follow_handler()
        self._follow = follow

        def follow_unmapped(window) -> None:
            log("follow_unmapped(%s)", window)
            self._follow = None
            self.cancel_follow_handler()

        follow.connect("unmap", follow_unmapped)
        self._follow_handler = follow.connect_after("configure-event", follow_configure_event)
        log("setup_following() following %s", follow)

    def cancel_follow_handler(self) -> None:
        f = self._follow
        fh = self._follow_handler
        if f and fh:
            f.disconnect(fh)
            self._follow_handler = 0
            self._follow = None

    def new_backing(self, bw: int, bh: int):
        b = ClientWindowBase.new_backing(self, bw, bh)
        # call via idle_add so that the backing has time to be realized too:
        self.when_realized("cursor", GLib.idle_add, self._backing.set_cursor_data, self.cursor_data)
        return b

    def set_cursor_data(self, cursor_data) -> None:
        self.cursor_data = cursor_data
        b = self._backing
        if b:
            self.when_realized("cursor", b.set_cursor_data, cursor_data)

    def adjusted_position(self, ox, oy) -> tuple[int, int]:
        if AWT_RECENTER and is_awt(self._metadata):
            ss = self._client._current_screen_sizes
            if ss and len(ss) == 1:
                screen0 = ss[0]
                monitors = screen0[5]
                if monitors and len(monitors) > 1:
                    monitor = monitors[0]
                    mw = monitor[3]
                    mh = monitor[4]
                    w, h = self._size
                    # adjust for window centering on monitor instead of screen java
                    screen = self.get_screen()
                    sw = screen.get_width()
                    sh = screen.get_height()
                    # re-center on first monitor if the window is within
                    # tolerance of the center of the screen:
                    tolerance = 10
                    # center of the window:
                    cx = ox + w // 2
                    cy = oy + h // 2
                    if abs(sw // 2 - cx) <= tolerance:
                        x = mw // 2 - w // 2
                    else:
                        x = ox
                    if abs(sh // 2 - cy) <= tolerance:
                        y = mh // 2 - h // 2
                    else:
                        y = oy
                    geomlog("adjusted_position(%i, %i)=%i, %i", ox, oy, x, y)
                    return x, y
        return ox, oy

    def calculate_window_offset(self, wx: int, wy: int, ww: int, wh: int) -> tuple[int, int] | None:
        ss = self._client._current_screen_sizes
        if not ss:
            return None
        if len(ss) != 1:
            geomlog("cannot handle more than one screen for OR offset")
            return None
        screen0 = ss[0]
        monitors = screen0[5]
        if not monitors:
            geomlog("screen %s lacks monitors information: %s", screen0)
            return None
        try:
            from xpra.util.rectangle import rectangle
        except ImportError as e:
            geomlog("cannot calculate offset: %s", e)
            return None
        wrect = rectangle(wx, wy, ww, wh)
        rects = [wrect]
        pixels_in_monitor = {}
        for i, monitor in enumerate(monitors):
            plug_name, x, y, w, h = monitor[:5]
            new_rects = []
            for rect in rects:
                new_rects += rect.subtract(x, y, w, h)
            geomlog("after removing areas visible on %s from %s: %s", plug_name, rects, new_rects)
            rects = new_rects
            if not rects:
                # the whole window is visible
                return None
            # keep track of how many pixels would be on this monitor:
            inter = wrect.intersection(x, y, w, h)
            if inter:
                pixels_in_monitor[inter.width * inter.height] = i
        # if we're here, then some of the window would land on an area
        # not show on any monitors
        # choose the monitor that had most of the pixels and make it fit:
        geomlog("pixels in monitor=%s", pixels_in_monitor)
        if not pixels_in_monitor:
            i = 0
        else:
            best = max(pixels_in_monitor.keys())
            i = pixels_in_monitor[best]
        monitor = monitors[i]
        plug_name, x, y, w, h = monitor[:5]
        geomlog("calculating OR offset for monitor %i: %s", i, plug_name)
        if ww > w or wh >= h:
            geomlog("window %ix%i is bigger than the monitor %i: %s %ix%i, not adjusting it",
                    ww, wh, i, plug_name, w, h)
            return None
        dx = 0
        dy = 0
        if wx < x:
            dx = x - wx
        elif wx + ww > x + w:
            dx = (x + w) - (wx + ww)
        if wy < y:
            dy = y - wy
        elif wy + wh > y + h:
            dy = (y + h) - (wy + wh)
        assert dx != 0 or dy != 0
        geomlog("calculate_window_offset%s=%s", (wx, wy, ww, wh), (dx, dy))
        return dx, dy

    def when_realized(self, identifier: str, callback: Callable, *args) -> None:
        if self.get_realized():
            callback(*args)
        else:
            self.on_realize_cb[identifier] = callback, args

    def on_realize(self, widget) -> None:
        eventslog("on_realize(%s) gdk window=%s", widget, self.get_window())
        add_window_hooks(self)
        cb = self.on_realize_cb
        self.on_realize_cb = {}
        for callback, args in cb.values():
            with eventslog.trap_error(f"Error on realize callback {callback} for window {self.wid}"):
                callback(*args)
        if HAS_X11_BINDINGS:
            # request frame extents if the window manager supports it
            self._client.request_frame_extents(self)
            if self.watcher_pid:
                log("using watcher pid=%i for wid=%i", self.watcher_pid, self.wid)
                self.do_set_x11_property("_NET_WM_PID", "u32", self.watcher_pid)
        if self.group_leader:
            self.get_window().set_group(self.group_leader)
        if WIN32:
            self._maybe_disable_win32_shadow()
        
        # PATCH: Force position for Wine popup menus after realize
        # GTK may reposition the window during realize
        if self.is_OR():
            is_wine_popup = self._is_wine_popup_menu()
            if is_wine_popup and self._pos:
                # Current position from GTK
                current_pos = self.get_position()
                saved_pos = self._pos
                if current_pos != saved_pos:
                    geomlog("WINE POPUP on_realize: GTK repositioned window, forcing correct position (wid=%s)", self.wid)
                    geomlog("WINE POPUP on_realize: current=%s, target=%s (wid=%s)", current_pos, saved_pos, self.wid)
                    self.move(saved_pos[0], saved_pos[1])
                    self._pos = saved_pos
                    # Verify after move
                    verify_pos = self.get_position()
                    geomlog("WINE POPUP on_realize: position forced (wid=%s, target=%s, actual=%s)", 
                           self.wid, saved_pos, verify_pos)
                else:
                    geomlog("WINE POPUP on_realize: position already correct (wid=%s, pos=%s)", self.wid, saved_pos)

    def on_unrealize(self, widget) -> None:
        eventslog("on_unrealize(%s)", widget)
        if WIN32 and self.is_OR():
            self._mark_win32_shadow_dirty(True)
        self.cancel_follow_handler()
        remove_window_hooks(self)

    def on_map_event(self, widget, _event) -> bool:
        if WIN32 and self.is_OR() and self._is_wine_popup_menu():
            if self._win32_shadow_trim:
                geomlog("Win32 popup remapped, forcing shadow re-detect (wid=%s)", self.wid)
            self._mark_win32_shadow_dirty(True)
            self._queue_win32_shadow_retry(0)
        return False

    def set_alpha(self) -> None:
        # try to enable alpha on this window if needed,
        # and if the backing class can support it:
        bc = self.get_backing_class()
        alphalog("set_alpha() has_alpha=%s, %s.HAS_ALPHA=%s, realized=%s",
                 self._has_alpha, bc, bc.HAS_ALPHA, self.get_realized())
        # by default, only RGB (no transparency):
        # rgb_formats = tuple(BACKING_CLASS.RGB_MODES)
        self._client_properties["encodings.rgb_formats"] = ["RGB", "RGBX"]
        # only set the visual if we need to enable alpha:
        # (breaks the headerbar otherwise!)
        if not self.get_realized() and self._has_alpha:
            if set_visual(self, True):
                if self._has_alpha:
                    self._client_properties["encodings.rgb_formats"] = ["RGBA", "RGB", "RGBX"]
                self._window_alpha = self._has_alpha
            else:
                alphalog("failed to set RGBA visual")
                self._has_alpha = False
                self._client_properties["encoding.transparency"] = False
        if not self._has_alpha or not bc.HAS_ALPHA:
            self._client_properties["encoding.transparency"] = False

    def freeze(self) -> None:
        # the OpenGL subclasses override this method to also free their GL context
        self._frozen = True
        self.iconify()

    def unfreeze(self) -> None:
        if not self._frozen or not self._iconified:
            return
        log("unfreeze() wid=%i, frozen=%s, iconified=%s", self.wid, self._frozen, self._iconified)
        if not self._frozen or not self._iconified:
            # has been deiconified already
            return
        self._frozen = False
        self.deiconify()

    def deiconify(self) -> None:
        functions = tuple(self._ondeiconify)
        self._ondeiconify = []
        for function in functions:
            try:
                function()
            except Exception as e:
                log("deiconify()", exc_info=True)
                log.error(f"Error calling {function} on {self} during deiconification:")
                log.estr(e)
        Gtk.Window.deiconify(self)

    def window_state_updated(self, widget, event) -> None:
        statelog("%s.window_state_updated(%s, %s) changed_mask=%s, new_window_state=%s",
                 self, widget, repr(event), event.changed_mask, event.new_window_state)
        state_updates: dict[str, bool] = {}
        for flag in ("fullscreen", "above", "below", "sticky", "iconified", "maximized", "focused"):
            wstate = getattr(Gdk.WindowState, flag.upper())  # ie: Gdk.WindowState.FULLSCREEN
            if event.changed_mask & wstate:
                state_updates[flag] = bool(event.new_window_state & wstate)
        self.update_window_state(state_updates)

    def update_window_state(self, state_updates: dict[str, bool]):
        if self._client.readonly:
            log("update_window_state(%s) ignored in readonly mode", state_updates)
            return
        if state_updates.get("maximized") is False or state_updates.get("fullscreen") is False:
            # if we unfullscreen or unmaximize, re-calculate offsets if we have any:
            w, h = self._backing.render_size
            ww, wh = self.get_size()
            log("update_window_state(%s) unmax or unfullscreen", state_updates)
            log("window_offset=%s, backing render_size=%s, window size=%s",
                self.window_offset, (w, h), (ww, wh))
            if self._backing.offsets != (0, 0, 0, 0):
                self.center_backing(w, h)
                self.repaint(0, 0, ww, wh)
        # decide if this is really an update by comparing with our local state vars:
        # (could just be a notification of a state change we already know about)
        actual_updates: dict[str, bool] = {}
        for state, value in state_updates.items():
            var = "_" + state.replace("-", "_")  # ie: "skip-pager" -> "_skip_pager"
            cur = getattr(self, var)  # ie: self._maximized
            if cur != value:
                setattr(self, var, value)  # ie: self._maximized = True
                actual_updates[state] = value
                statelog("%s=%s (was %s)", var, value, cur)
        server_updates: dict[str, bool] = {k: v for k, v in actual_updates.items()
                                           if k in self._client.server_window_states}
        # iconification is handled a bit differently...
        iconified = server_updates.pop("iconified", None)
        if iconified is not None:
            statelog("iconified=%s", iconified)
            # handle iconification as map events:
            if iconified:
                # usually means it is unmapped
                self._unfocus()
                if not self._override_redirect and not self.send_iconify_timer:
                    # tell server, but wait a bit to try to prevent races:
                    self.schedule_send_iconify()
            else:
                self.cancel_send_iconifiy_timer()
                self._frozen = False
                self.process_map_event()
        statelog("window_state_updated(..) state updates: %s, actual updates: %s, server updates: %s",
                 state_updates, actual_updates, server_updates)
        if "maximized" in state_updates:
            if REPAINT_MAXIMIZED > 0:
                def repaint_maximized():
                    if not self._backing:
                        return
                    ww, wh = self.get_size()
                    self.repaint(0, 0, ww, wh)

                GLib.timeout_add(REPAINT_MAXIMIZED, repaint_maximized)
            if REFRESH_MAXIMIZED:
                self._client.send_refresh(self.wid)

        self._window_state.update(server_updates)
        self.emit("state-updated")
        # if we have state updates, send them back to the server using a configure window packet:
        if self._window_state and not self.window_state_timer:
            self.window_state_timer = GLib.timeout_add(25, self.send_updated_window_state)

    def send_updated_window_state(self) -> None:
        statelog(f"sending configure event with window state={self._window_state}")
        self.window_state_timer = 0
        if self._window_state and self.get_window():
            self.send_configure_event(True)

    def cancel_window_state_timer(self) -> None:
        wst = self.window_state_timer
        if wst:
            self.window_state_timer = 0
            GLib.source_remove(wst)

    def schedule_send_iconify(self) -> None:
        # calculate a good delay to prevent races causing minimize/unminimize loops:
        if self._client.readonly:
            return
        delay = ICONIFY_LATENCY
        if delay > 0:
            spl = tuple(self._client.server_ping_latency)
            if spl:
                worst = max(x[1] for x in self._client.server_ping_latency)
                delay += int(1000 * worst)
                delay = min(1000, delay)
        statelog("telling server about iconification with %sms delay", delay)
        self.send_iconify_timer = GLib.timeout_add(delay, self.send_iconify)

    def send_iconify(self) -> None:
        self.send_iconify_timer = 0
        if self._iconified:
            self.send("unmap-window", self.wid, True, self._window_state)
            # we have sent the window-state already:
            self._window_state = {}
            self.cancel_window_state_timer()

    def cancel_send_iconifiy_timer(self) -> None:
        sit = self.send_iconify_timer
        if sit:
            self.send_iconify_timer = 0
            GLib.source_remove(sit)

    def set_command(self, command) -> None:
        self.set_x11_property("WM_COMMAND", "latin1", command)

    def set_x11_property(self, prop_name: str, dtype: str, value) -> None:
        if not HAS_X11_BINDINGS:
            return
        self.when_realized("x11-prop-%s" % prop_name, self.do_set_x11_property, prop_name, dtype, value)

    def do_set_x11_property(self, prop_name: str, dtype: str, value) -> None:
        xid = self.get_window().get_xid()
        metalog(f"do_set_x11_property({prop_name!r}, {dtype!r}, {value!r}) xid={xid}")
        if dtype == "latin1":
            value = bytestostr(value)
        if isinstance(value, (list, tuple)) and not isinstance(dtype, (list, tuple)):
            dtype = (dtype,)
        if not dtype and not value:
            # remove prop
            prop_del(xid, prop_name)
        else:
            prop_set(xid, prop_name, dtype, value)

    def set_class_instance(self, wmclass_name, wmclass_class) -> None:
        if not self.get_realized():
            # Warning: window managers may ignore the icons we try to set
            # if the wm_class value is set and matches something somewhere undocumented
            # (if the default is used, you cannot override the window icon)
            ignorewarnings(self.set_wmclass, wmclass_name, wmclass_class)
        elif HAS_X11_BINDINGS:
            xid = self.get_window().get_xid()
            with xlog:
                X11Window.setClassHint(xid, wmclass_class, wmclass_name)
                log("XSetClassHint(%s, %s) done", wmclass_class, wmclass_name)

    def set_shape(self, shape) -> None:
        shapelog("set_shape(%s)", shape)
        if not shape:
            return

        def do_set_shape() -> None:
            window = self.get_window()
            if not window:
                return
            use_x11_shape = HAS_X11_BINDINGS and XSHAPE
            xid = window.get_xid() if use_x11_shape else None
            base_x, base_y = shape.get("x", 0), shape.get("y", 0)
            if use_x11_shape:
                shape_defs = tuple(SHAPE_KIND.items())
            else:
                shape_defs = tuple((None, name) for name in ("Bounding", "Clip", "Input"))
            for kind, name in shape_defs:
                rectangles = shape.get(f"{name}.rectangles")  # ie: Bounding.rectangles = [(0, 0, 150, 100)]
                if not rectangles:
                    continue
                x_off = base_x
                y_off = base_y
                # adjust for scaling:
                if self._xscale != 1 or self._yscale != 1:
                    x_off = self.sx(base_x)
                    y_off = self.sy(base_y)
                    rectangles = self.scale_shape_rectangles(name, rectangles)
                if name == "Bounding" and self.border.shown and self.border.size > 0:
                    ww, wh = self._size
                    rectangles = add_border_rectangles(rectangles, ww, wh, self.border.size)
                if use_x11_shape:
                    # too expensive to log with actual rectangles:
                    shapelog("XShapeCombineRectangles(%#x, %s, %i, %i, %i rects)",
                             xid, name, x_off, y_off, len(rectangles))
                    with xlog:
                        X11Window.XShapeCombineRectangles(xid, kind, x_off, y_off, rectangles)
                else:
                    self._apply_gdk_shape_region(window, name, rectangles, x_off, y_off)

        self.when_realized("shape", do_set_shape)

    def _apply_gdk_shape_region(self, window, name: str, rectangles, x_off: int, y_off: int) -> None:
        """Apply window shapes using GDK for platforms without XShape."""
        if WIN32 and name == "Bounding":
            applied = self._apply_win32_shape_region(window, rectangles, x_off, y_off)
            if applied:
                return
        region = None
        if rectangles:
            try:
                region = Region()
                for rx, ry, rw, rh in rectangles:
                    region.union(RectangleInt(rx, ry, rw, rh))
            except Exception as exc:  # pragma: no cover - defensive
                shapelog("failed to build region for %s: %s", name, exc)
                region = None
        combine = window.input_shape_combine_region if name.lower() == "input" else window.shape_combine_region
        combine(region, x_off, y_off)

    def _apply_win32_shape_region(self, gdk_window, rectangles, x_off: int, y_off: int) -> bool:
        """Use native Win32 region APIs to clip a shaped Wine popup."""
        try:
            from xpra.platform.win32.gui import get_window_handle  # pylint: disable=import-outside-toplevel
            from xpra.platform.win32.common import (  # pylint: disable=import-outside-toplevel
                CreateRectRgn, CombineRgn, SetWindowRgn, DeleteObject,
            )
            from xpra.platform.win32 import constants as win32con  # pylint: disable=import-outside-toplevel
        except Exception as exc:  # pragma: no cover - defensive
            shapelog("Win32 shape helpers unavailable: %s", exc)
            return False
        try:
            hwnd = get_window_handle(gdk_window)
        except Exception as exc:  # pragma: no cover - defensive
            shapelog("Win32 get_window_handle failed: %s", exc)
            return False
        if not hwnd:
            shapelog("Win32 get_window_handle returned 0")
            return False
        region_handle = None
        try:
            for rx, ry, rw, rh in rectangles:
                left = int(round(x_off + rx))
                top = int(round(y_off + ry))
                right = int(round(left + rw))
                bottom = int(round(top + rh))
                rect_region = CreateRectRgn(left, top, right, bottom)
                if not rect_region:
                    continue
                if not region_handle:
                    region_handle = rect_region
                else:
                    CombineRgn(region_handle, region_handle, rect_region, win32con.RGN_OR)
                    DeleteObject(rect_region)
        except Exception as exc:  # pragma: no cover - defensive
            shapelog("Win32 region construction failed: %s", exc)
            if region_handle:
                DeleteObject(region_handle)
            return False
        # if no rectangles, clear any existing region
        target_region = region_handle or None
        result = SetWindowRgn(hwnd, target_region, True)
        if not result and region_handle:
            DeleteObject(region_handle)
        shapelog("Win32 SetWindowRgn(hwnd=%#x, rects=%s) result=%s", hwnd, len(rectangles), bool(result))
        return bool(result)

    def _mark_win32_shadow_dirty(self, reset_trim: bool = False) -> None:
        """Flag the Win32 popup region so it gets recomputed / re-applied."""
        if reset_trim:
            self._win32_shadow_trim = 0
            self._win32_shadow_detect_attempts = 0
        self._win32_shadow_region_applied = False
        if self._win32_shadow_retry_source:
            GLib.source_remove(self._win32_shadow_retry_source)
            self._win32_shadow_retry_source = 0

    def _queue_win32_shadow_retry(self, delay_ms: int = 40) -> None:
        """Ensure we retry trimming soon after map/resume until success."""
        if not WIN32 or not self.is_OR():
            return
        metadata = getattr(self, "_initial_metadata", None) or self._metadata
        if not metadata.boolget("_client-popup-heuristics", False):
            return
        if self._win32_shadow_trim and self._win32_shadow_region_applied:
            return
        if self._win32_shadow_retry_source:
            return
        self._win32_shadow_retry_source = GLib.timeout_add(max(1, delay_ms), self._win32_shadow_retry_cb)

    def _win32_shadow_retry_cb(self) -> bool:
        self._win32_shadow_retry_source = 0
        if not WIN32 or not self.get_realized():
            return False
        self._maybe_trim_win32_shadow()
        return False

    def _maybe_disable_win32_shadow(self) -> None:
        """Disable DWM shadows on heuristically detected popup windows."""
        if self._win32_shadow_disabled:
            return
        metadata = getattr(self, "_initial_metadata", None) or self._metadata
        if not metadata.boolget("_client-popup-heuristics", False):
            return
        if not (self._override_redirect or metadata.boolget("override-redirect", False)):
            return
        try:
            from ctypes import byref, sizeof, c_int  # pylint: disable=import-outside-toplevel
            from xpra.platform.win32.gui import get_window_handle  # pylint: disable=import-outside-toplevel
            from xpra.platform.win32.common import (  # pylint: disable=import-outside-toplevel
                DwmSetWindowAttribute, DWMWA_NCRENDERING_POLICY, DWMNCRP_DISABLED,
            )
        except Exception as exc:  # pragma: no cover - defensive
            geomlog("Win32 shadow disable unavailable: %s", exc)
            return
        if not DwmSetWindowAttribute:
            return
        try:
            hwnd = get_window_handle(self)
        except Exception as exc:  # pragma: no cover
            geomlog("Win32 get_window_handle failed: %s", exc)
            return
        if not hwnd:
            return
        value = c_int(DWMNCRP_DISABLED)
        result = DwmSetWindowAttribute(hwnd, DWMWA_NCRENDERING_POLICY, byref(value), sizeof(value))
        if result != 0:
            geomlog("Failed to disable DWM shadow for hwnd=%#x wid=%s (result=%s)", hwnd, self.wid, result)
            return
        class_shadow_removed = self._clear_win32_class_shadow(hwnd)
        geomlog("Win32 DWM shadow disabled for hwnd=%#x wid=%s%s",
                hwnd, self.wid, ", class shadow cleared" if class_shadow_removed else "")
        self._win32_shadow_disabled = True

    @staticmethod
    def _clear_win32_class_shadow(hwnd: int) -> bool:
        try:
            from xpra.platform.win32.common import (  # pylint: disable=import-outside-toplevel
                GetClassLongW, SetClassLongW, GCL_STYLE, CS_DROPSHADOW,
            )
        except Exception as exc:  # pragma: no cover
            geomlog("Win32 class shadow helpers unavailable: %s", exc)
            return False
        try:
            style = GetClassLongW(hwnd, GCL_STYLE)
            if style & CS_DROPSHADOW:
                SetClassLongW(hwnd, GCL_STYLE, style & ~CS_DROPSHADOW)
                return True
        except Exception as exc:  # pragma: no cover
            geomlog("Failed to clear Win32 class shadow for hwnd=%#x: %s", hwnd, exc)
        return False

    def _get_win32_trim_cache_key(self) -> Optional[str]:
        metadata = getattr(self, "_initial_metadata", None) or self._metadata
        if not metadata:
            return None
        wm_class = metadata.strtupleget("class-instance", (None, None), 2, 2)
        class_a = (wm_class[0] or "").strip().lower()
        class_b = (wm_class[1] or "").strip().lower()
        owner = metadata.strget("client-machine", "").strip().lower()
        key_parts = tuple(part for part in (class_a, class_b, owner) if part)
        if not key_parts:
            return None
        return "|".join(key_parts)

    def _get_cached_win32_trim(self) -> int:
        key = self._get_win32_trim_cache_key()
        if key:
            return WIN32_POPUP_TRIM_CACHE.get(key, 0)
        return 0

    def _store_win32_trim(self, trim: int) -> None:
        key = self._get_win32_trim_cache_key()
        if not key:
            return
        prev = WIN32_POPUP_TRIM_CACHE.get(key, 0)
        if trim > prev:
            WIN32_POPUP_TRIM_CACHE[key] = trim

    def _maybe_trim_win32_shadow(self) -> bool:
        """Detect solid shadow rows in the backing and clip them via SetWindowRgn."""
        metadata = getattr(self, "_initial_metadata", None) or self._metadata
        if not self._override_redirect or not metadata.boolget("_client-popup-heuristics", False):
            return False
        window = self.get_window()
        if not window:
            self._queue_win32_shadow_retry(40)
            return False
        cached_trim = self._get_cached_win32_trim()
        if self._win32_shadow_trim:
            if self._win32_shadow_region_applied:
                return False
            w, h = self._size
            trimmed_height = max(1, h - self._win32_shadow_trim)
            rectangles = [(0, 0, w, trimmed_height)]
            applied = self._apply_win32_shape_region(window, rectangles, 0, 0)
            self._win32_shadow_region_applied = applied
            if not applied:
                geomlog("Win32 popup shadow reapply failed for wid=%s", self.wid)
                self._queue_win32_shadow_retry(80)
            return False
        self._win32_shadow_detect_attempts += 1
        allow_fallback = self._win32_shadow_detect_attempts >= 4
        trim, fallback_used, boundary_ratio, residual_rows = self._detect_black_shadow_rows(allow_fallback)
        if trim <= 0:
            if cached_trim > 0:
                w, h = self._size
                self._win32_shadow_trim = min(cached_trim, max(1, h - 1))
                self._win32_shadow_region_applied = False
                geomlog("Win32 popup shadow using cached trim %spx (wid=%s)", self._win32_shadow_trim, self.wid)
                rectangles = [(0, 0, w, max(1, h - self._win32_shadow_trim))]
                applied = self._apply_win32_shape_region(window, rectangles, 0, 0)
                self._win32_shadow_region_applied = applied
                if applied:
                    self._store_win32_trim(self._win32_shadow_trim)
                else:
                    self._win32_shadow_trim = 0
                    self._queue_win32_shadow_retry(80)
                return False
            self._queue_win32_shadow_retry(60)
            return False
        self._win32_shadow_detect_attempts = 0
        w, h = self._size
        height = max(1, h)
        max_reasonable = min(80, max(24, height // 10))
        if trim > max_reasonable:
            geomlog("Win32 popup shadow detection trimmed %spx, clamping to %spx (wid=%s)",
                    trim, max_reasonable, self.wid)
            trim = max_reasonable
        if residual_rows > 0:
            extra = min(residual_rows, 4)
            geomlog("Win32 popup residual dark rows=%s, extending trim by %s (wid=%s)",
                    residual_rows, extra, self.wid)
            trim = min(height, trim + extra)
        boundary_ratio = max(0.0, min(1.0, boundary_ratio))
        if not fallback_used:
            extra_rows = 0
            if boundary_ratio >= 0.85:
                extra_rows = 2
            elif boundary_ratio >= 0.65:
                extra_rows = 1
            if extra_rows > 0:
                trim = min(height, trim + extra_rows)
                geomlog("Win32 popup shadow boundary still dark (ratio=%.3f), extending trim by %spx (wid=%s)",
                        boundary_ratio, extra_rows, self.wid)
        if fallback_used:
            margin = max(2, min(6, trim // 6))
        else:
            if boundary_ratio >= 0.8:
                margin = 0
            elif boundary_ratio >= 0.5:
                margin = 1
            else:
                margin = 2
        detected_trim = max(1, trim - margin)
        if cached_trim > 0 and detected_trim + 1 < cached_trim:
            detected_trim = min(cached_trim, height - 1)
            geomlog("Win32 popup shadow detection %spx < cached %spx, using cached value (wid=%s)",
                    max(1, trim - margin), cached_trim, self.wid)
        self._win32_shadow_trim = detected_trim
        w, h = self._size
        trimmed_height = max(1, h - self._win32_shadow_trim)
        rectangles = [(0, 0, w, trimmed_height)]
        applied = self._apply_win32_shape_region(window, rectangles, 0, 0)
        self._win32_shadow_region_applied = applied
        if applied:
            geomlog("Win32 popup shadow trimmed by %spx (wid=%s)", self._win32_shadow_trim, self.wid)
            self._store_win32_trim(self._win32_shadow_trim)
        else:
            geomlog("Failed to apply Win32 popup trim (wid=%s)", self.wid)
            self._win32_shadow_trim = 0
            self._queue_win32_shadow_retry(80)
        return False

    def _detect_black_shadow_rows(self, allow_fallback: bool = False) -> tuple[int, bool, float, int]:
        backing = getattr(self._backing, "_backing", None)
        if not backing:
            return 0, False, 0.0, 0
        try:
            backing.flush()
            data = memoryview(backing.get_data())
        except Exception as exc:
            shapelog("Win32 shadow detection failed: %s", exc)
            return 0, False, 0.0, 0
        height = backing.get_height()
        width = backing.get_width()
        stride = backing.get_stride()
        max_trim = min(96, height // 2)
        min_trim = 8
        threshold = 8
        ratio_limit = 0.985
        trim = 0
        boundary_ratio = 0.0
        for row in range(height - 1, max(height - max_trim, -1), -1):
            offset = row * stride
            row_bytes = data[offset: offset + width * 4]
            dark_ratio = self._dark_pixel_ratio(row_bytes, width, threshold)
            if dark_ratio < ratio_limit:
                boundary_ratio = dark_ratio
                break
            trim += 1
        if trim >= min_trim:
            residual = self._residual_dark_rows(data, width, stride, height, trim, threshold + 8, 0.9)
            return trim, False, boundary_ratio, residual
        if allow_fallback:
            fallback = min(64, max(12, height // 12))
            residual = self._residual_dark_rows(data, width, stride, height, fallback, threshold + 8, 0.9)
            return fallback, True, 0.0, residual
        return 0, False, 0.0, 0

    @staticmethod
    def _residual_dark_rows(data, width: int, stride: int, height: int,
                             trim: int, threshold: int, ratio_limit: float,
                             max_rows: int = 4) -> int:
        if trim <= 0 or height <= trim:
            return 0
        leftover = 0
        start_row = height - trim - 1
        for i in range(max_rows):
            row = start_row - i
            if row < 0:
                break
            offset = row * stride
            row_bytes = data[offset: offset + width * 4]
            dark_ratio = GTKClientWindowBase._dark_pixel_ratio(row_bytes, width, threshold)
            if dark_ratio >= ratio_limit:
                leftover += 1
            else:
                break
        return leftover

    @staticmethod
    def _dark_pixel_ratio(row_bytes, width: int, threshold: int) -> float:
        if width <= 0:
            return 0.0
        pixels = width
        dark = 0
        length = min(len(row_bytes), width * 4)
        for i in range(0, length, 4):
            b = row_bytes[i]
            g = row_bytes[i + 1]
            r = row_bytes[i + 2]
            if b <= threshold and g <= threshold and r <= threshold:
                dark += 1
        return dark / float(pixels)

    def lazy_scale_shape(self, rectangles) -> list:
        # scale the rectangles without a bitmap...
        # results aren't so good! (but better than nothing?)
        return [self.srect(*x) for x in rectangles]

    def scale_shape_rectangles(self, kind_name, rectangles):
        if LAZY_SHAPE or len(rectangles) < 2:
            return self.lazy_scale_shape(rectangles)
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            return self.lazy_scale_shape(rectangles)
        ww, wh = self._size
        sw, sh = self.cp(ww, wh)
        img = Image.new('1', (sw, sh), color=0)
        shapelog("drawing %s on bitmap(%s,%s)=%s", kind_name, sw, sh, img)
        d = ImageDraw.Draw(img)
        for x, y, w, h in rectangles:
            d.rectangle((x, y, x + w, y + h), fill=1)
        shapelog("drawing complete")
        img = img.resize((ww, wh), resample=Image.BICUBIC)
        shapelog("resized %s bitmap to window size %sx%s: %s", kind_name, ww, wh, img)
        # now convert back to rectangles...
        monodata = img.tobytes("raw", "1")
        shapelog("got %i bytes", len(monodata))
        # log.warn("monodata: %s (%i bytes) %ix%i", repr_ellipsized(monodata), len(monodata), ww, wh)
        assert callable(bit_to_rectangles)
        rectangles = bit_to_rectangles(monodata, ww, wh)
        shapelog("back to rectangles")
        return rectangles

    def set_bypass_compositor(self, v) -> None:
        if v not in (0, 1, 2):
            v = 0
        self.set_x11_property("_NET_WM_BYPASS_COMPOSITOR", "u32", v)

    def set_strut(self, strut: dict) -> None:
        if not HAS_X11_BINDINGS:
            return
        log("strut=%s", strut)
        d = typedict(strut)
        values: list[int] = []
        for x in ("left", "right", "top", "bottom"):
            v = d.intget(x, 0)
            # handle scaling:
            if x in ("left", "right"):
                v = self.sx(v)
            else:
                v = self.sy(v)
            values.append(v)
        has_partial = False
        for x in (
                "left_start_y", "left_end_y",
                "right_start_y", "right_end_y",
                "top_start_x", "top_end_x",
                "bottom_start_x", "bottom_end_x",
        ):
            if x in d:
                has_partial = True
            v = d.intget(x, 0)
            if x.find("_x"):
                v = self.sx(v)
            elif x.find("_y"):
                v = self.sy(v)
            values.append(v)
        log("setting strut=%s, has partial=%s", values, has_partial)
        if has_partial:
            self.set_x11_property("_NET_WM_STRUT_PARTIAL", "u32", values)
        self.set_x11_property("_NET_WM_STRUT", "u32", values[:4])

    def set_window_type(self, window_types) -> None:
        # PATCH: Save the position before set_type_hint for Wine popup menus
        # On Windows, GTK's set_type_hint() with DIALOG can trigger a reposition
        is_wine_popup = self._is_wine_popup_menu()
        saved_pos = None
        if is_wine_popup and self._pos:
            saved_pos = self._pos
            geomlog("WINE POPUP set_window_type: saving position before set_type_hint (wid=%s, pos=%s)", self.wid, saved_pos)
        
        hints = 0
        for window_type in window_types:
            # win32 workaround:
            if AWT_DIALOG_WORKAROUND and window_type == "DIALOG" and self._metadata.boolget("skip-taskbar"):
                wm_class = self._metadata.strtupleget("class-instance", (None, None), 2, 2)
                if wm_class and len(wm_class) == 2 and wm_class[0] and wm_class[0].startswith("sun-awt-X11"):
                    # replace "DIALOG" with "NORMAL":
                    if "NORMAL" in window_types:
                        continue
                    window_type = "NORMAL"
            hint = WINDOW_NAME_TO_HINT.get(window_type, None)
            if hint is not None:
                hints |= hint
            else:
                log("ignoring unknown window type hint: %s", window_type)
        log("set_window_type(%s) hints=%s", window_types, hints)
        if hints:
            self.set_type_hint(hints)
            # PATCH: Force the position after set_type_hint for Wine popup menus to avoid GTK repositioning
            if is_wine_popup and saved_pos:
                # Check if the position was altered
                current_pos = self.get_position() if self.get_realized() else None
                if current_pos and current_pos != saved_pos:
                    geomlog("WINE POPUP set_window_type: GTK repositioned after set_type_hint, restoring (wid=%s)", self.wid)
                    geomlog("WINE POPUP set_window_type: saved=%s, current=%s, restoring to=%s (wid=%s)", saved_pos, current_pos, saved_pos, self.wid)
                    self.move(saved_pos[0], saved_pos[1])
                    self._pos = saved_pos
                    geomlog("WINE POPUP set_window_type: position restored (wid=%s)", self.wid)
                elif not current_pos:
                    # Window not realized yet, force position on realize
                    geomlog("WINE POPUP set_window_type: will force position on realize (wid=%s, pos=%s)", self.wid, saved_pos)
                    def force_position_on_realize():
                        # Always force position on realize to prevent GTK repositioning
                        current = self.get_position() if self.get_realized() else None
                        geomlog("WINE POPUP set_window_type: forcing position on realize (wid=%s, saved_pos=%s, current_pos=%s)", 
                               self.wid, saved_pos, current)
                        self.move(saved_pos[0], saved_pos[1])
                        self._pos = saved_pos
                        # Verify after move
                        verify_pos = self.get_position() if self.get_realized() else None
                        geomlog("WINE POPUP set_window_type: position forced on realize (wid=%s, target=%s, actual=%s)", 
                               self.wid, saved_pos, verify_pos)
                    self.when_realized("wine-popup-position", force_position_on_realize)
                else:
                    geomlog("WINE POPUP set_window_type: position unchanged after set_type_hint (wid=%s, pos=%s)", self.wid, saved_pos)

    def set_modal(self, modal: bool) -> None:
        # setting the window as modal would prevent
        # all other windows we manage from receiving input
        # including other unrelated applications
        # what we want is "window-modal"
        # so we can turn this off using the "modal_windows" feature,
        # from the command line and the system tray:
        mw = self._client.modal_windows
        log("set_modal(%s) modal_windows=%s", modal, mw)
        Gtk.Window.set_modal(self, modal and mw)

    def set_fullscreen_monitors(self, fsm) -> None:
        # platform specific code:
        log("set_fullscreen_monitors(%s)", fsm)

        def do_set_fullscreen_monitors():
            set_fullscreen_monitors(self.get_window(), fsm)

        self.when_realized("fullscreen-monitors", do_set_fullscreen_monitors)

    def set_shaded(self, shaded: bool) -> None:
        # platform specific code:
        log("set_shaded(%s)", shaded)

        def do_set_shaded() -> None:
            set_shaded(self.get_window(), shaded)

        self.when_realized("shaded", do_set_shaded)

    def restack(self, other_window, above: int = 0) -> None:
        log("restack(%s, %s)", other_window, above)

        def do_restack() -> None:
            self.get_window().restack(other_window, above)

        self.when_realized("restack", do_restack)

    def set_fullscreen(self, fullscreen: bool) -> None:
        statelog("%s.set_fullscreen(%s)", self, fullscreen)

        def do_set_fullscreen() -> None:
            if fullscreen:
                # we may need to temporarily remove the max-window-size restrictions
                # to be able to honour the fullscreen request:
                w, h = self.max_window_size
                if w > 0 and h > 0:
                    self.set_size_constraints(self.size_constraints, (0, 0))
                self.fullscreen()
            else:
                self.unfullscreen()
                # re-apply size restrictions:
                w, h = self.max_window_size
                if w > 0 and h > 0:
                    self.set_size_constraints(self.size_constraints, self.max_window_size)

        self.when_realized("fullscreen", do_set_fullscreen)

    def set_opaque_region(self, rectangles=()):
        # gtk can only set a single region!
        # noinspection PyArgumentList
        r = Region()
        for rect in rectangles:
            # "opaque-region", aka "_NET_WM_OPAQUE_REGION" is meant to use unsigned values
            # but some applications use 0xffffffff, so we have to validate it:
            rvalues = tuple((int(v) if v < 2**32 else -1) for v in rect)
            rectint = RectangleInt(*self.srect(*rvalues))
            r.union(Region(rectint))

        def do_set_region():
            log("set_opaque_region(%s)", r)
            try:
                self.get_window().set_opaque_region(r)
            except KeyError as e:
                if first_time("region-KeyError"):
                    log.warn("Warning: cannot set opaque region %r", r)
                    log.warn(" a package may be missing")
                    log.warn(f" {e}")

        self.when_realized("set-opaque-region", do_set_region)

    def set_xid(self, xid: str | int) -> None:
        if xid.startswith("0x") and xid.endswith("L"):
            xid = xid[:-1]
        try:
            iid = int(xid, 16)
        except Exception as e:
            log("%s.set_xid(%s) error parsing/setting xid: %s", self, xid, e)
            return
        self.set_x11_property("XID", "u32", iid)

    def xget_u32_property(self, target, name: str) -> int:
        if prop_get:
            v = prop_get(target.get_xid(), name, "u32", ignore_errors=True)
            log("%s.xget_u32_property(%s, %s)=%s", self, target, name, v)
            if isinstance(v, int):
                return v
        return 0

    def property_changed(self, widget, event) -> None:
        atom = str(event.atom)
        statelog("property_changed(%s, %s) : %s", widget, event, atom)
        if atom == "_NET_WM_DESKTOP":
            if self._been_mapped and not self._override_redirect and self._can_set_workspace:
                self.do_workspace_changed(str(event))
            return
        # the remaining handlers need `prop_get`:
        if not prop_get:
            return
        xid = self.get_window().get_xid()
        if atom == "_NET_FRAME_EXTENTS":
            v = prop_get(xid, "_NET_FRAME_EXTENTS", ["u32"], ignore_errors=False)
            statelog("_NET_FRAME_EXTENTS: %s", v)
            if v:
                if v == self._current_frame_extents:
                    # unchanged
                    return
                if not self._been_mapped:
                    # map event will take care of sending it
                    return
                if self.is_OR() or self.is_tray():
                    # we can't do it: the server can't handle configure packets for OR windows!
                    return
                if not self._client.server_window_frame_extents:
                    # can't send cheap "skip-geometry" packets or frame-extents feature not supported:
                    return
                # tell server about new value:
                self._current_frame_extents = v
                statelog("sending configure event to update _NET_FRAME_EXTENTS to %s", v)
                self._window_state["frame"] = self.crect(*v)
                self.send_configure_event(True)
            return
        if atom == "XKLAVIER_STATE":
            # unused for now, but log it:
            xklavier_state = prop_get(xid, "XKLAVIER_STATE", ["integer"], ignore_errors=False)
            keylog("XKLAVIER_STATE=%s", [hex(x) for x in (xklavier_state or [])])
            return
        if atom == "_NET_WM_STATE":
            wm_state_atoms = prop_get(xid, "_NET_WM_STATE", ["atom"], ignore_errors=False)
            # code mostly duplicated from xpra/x11/gtk/window.py:
            WM_STATE_NAME = {
                "fullscreen": ("_NET_WM_STATE_FULLSCREEN",),
                "maximized": ("_NET_WM_STATE_MAXIMIZED_VERT", "_NET_WM_STATE_MAXIMIZED_HORZ"),
                "shaded": ("_NET_WM_STATE_SHADED",),
                "sticky": ("_NET_WM_STATE_STICKY",),
                "skip-pager": ("_NET_WM_STATE_SKIP_PAGER",),
                "skip-taskbar": ("_NET_WM_STATE_SKIP_TASKBAR",),
                "above": ("_NET_WM_STATE_ABOVE",),
                "below": ("_NET_WM_STATE_BELOW",),
                "focused": ("_NET_WM_STATE_FOCUSED",),
            }
            state_atoms = set(wm_state_atoms or [])
            state_updates = {}
            for state, atoms in WM_STATE_NAME.items():
                var = "_" + state.replace("-", "_")  # ie: "skip-pager" -> "_skip_pager"
                cur_state = getattr(self, var)
                wm_state_is_set = set(atoms).issubset(state_atoms)
                if wm_state_is_set and not cur_state:
                    state_updates[state] = True
                elif cur_state and not wm_state_is_set:
                    state_updates[state] = False
            log("_NET_WM_STATE=%s, state_updates=%s", wm_state_atoms, state_updates)
            if state_updates:
                self.update_window_state(state_updates)

    ######################################################################
    # workspace
    def init_workspace(self) -> None:
        if not self._can_set_workspace:
            return
        if not POLL_WORKSPACE:
            return
        self.when_realized("workspace", self.init_workspace_timer)

    def init_workspace_timer(self) -> None:
        value = [-1]

        def poll_workspace() -> bool:
            ws = self.get_window_workspace()
            workspacelog(f"poll_workspace() {ws=}")
            if value[0] != ws:
                value[0] = ws
                self.workspace_changed()
            return True

        self.workspace_timer = GLib.timeout_add(1000, poll_workspace)

    def cancel_workspace_timer(self) -> None:
        wt = self.workspace_timer
        if wt:
            self.workspace_timer = 0
            GLib.source_remove(wt)

    def workspace_changed(self) -> None:
        # on X11 clients, this fires from the root window property watcher
        ClientWindowBase.workspace_changed(self)
        if self._can_set_workspace:
            self.do_workspace_changed("desktop workspace changed")

    def do_workspace_changed(self, info: str) -> None:
        # call this method whenever something workspace related may have changed
        window_workspace = self.get_window_workspace()
        desktop_workspace = self.get_desktop_workspace()
        workspacelog("do_workspace_changed(%s) for window %i (window, desktop): from %s to %s",
                     info, self.wid,
                     (wn(self._window_workspace), wn(self._desktop_workspace)),
                     (wn(window_workspace), wn(desktop_workspace)))
        if self._window_workspace == window_workspace and self._desktop_workspace == desktop_workspace:
            # no change
            return
        suspend_resume = None
        if desktop_workspace < 0 or window_workspace is None:
            # maybe the property has been cleared? maybe the window is being scrubbed?
            workspacelog("not sure if the window is shown or not: %s vs %s, resuming to be safe",
                         wn(desktop_workspace), wn(window_workspace))
            suspend_resume = False
        elif window_workspace == WORKSPACE_UNSET:
            workspacelog("workspace unset: assume current")
            suspend_resume = False
        elif window_workspace == WORKSPACE_ALL:
            workspacelog("window is on all workspaces")
            suspend_resume = False
        elif desktop_workspace != window_workspace:
            workspacelog("window is on a different workspace, increasing its batch delay")
            workspacelog(" desktop: %s, window: %s", wn(desktop_workspace), wn(window_workspace))
            suspend_resume = True
        elif self._window_workspace != self._desktop_workspace:
            assert desktop_workspace == window_workspace
            workspacelog("window was on a different workspace, resetting its batch delay")
            workspacelog(" (was desktop: %s, window: %s, now both on %s)",
                         wn(self._window_workspace), wn(self._desktop_workspace), wn(desktop_workspace))
            suspend_resume = False
        self._window_workspace = window_workspace
        self._desktop_workspace = desktop_workspace
        client_properties = {}
        if window_workspace is not None:
            client_properties["workspace"] = window_workspace
        self.send_control_refresh(suspend_resume, client_properties)

    def send_control_refresh(self, suspend_resume, client_properties=None, refresh=False) -> None:
        statelog("send_control_refresh%s", (suspend_resume, client_properties, refresh))
        # we can tell the server using a "buffer-refresh" packet instead
        # and also take care of tweaking the batch config
        options = {"refresh-now": refresh}  # no need to refresh it
        self._client.control_refresh(self.wid, suspend_resume,
                                     refresh=refresh, options=options, client_properties=client_properties)

    def get_workspace_count(self) -> int:
        if not self._can_set_workspace:
            return 0
        if WIN32:
            if not WIN32_WORKSPACE:
                return 0
            from pyvda.pyvda import get_virtual_desktops
            return len(get_virtual_desktops())
        root = get_default_root_window()
        return self.xget_u32_property(root, "_NET_NUMBER_OF_DESKTOPS")

    def set_workspace(self, workspace) -> None:
        workspacelog("set_workspace(%s)", workspace)
        if not self._can_set_workspace:
            return
        if not self._been_mapped:
            # will be dealt with in the map event handler
            # which will look at the window metadata again
            workspacelog("workspace=%s will be set when the window is mapped", wn(workspace))
            return
        if workspace is not None:
            workspace = workspace & 0xffffffff
        desktop = self.get_desktop_workspace()
        ndesktops = self.get_workspace_count()
        current = self.get_window_workspace()
        workspacelog("set_workspace(%s) realized=%s", wn(workspace), self.get_realized())
        workspacelog(" current workspace=%s, detected=%s, desktop workspace=%s, ndesktops=%s",
                     wn(self._window_workspace), wn(current), wn(desktop), ndesktops)
        if not self._can_set_workspace or not ndesktops:
            return
        if workspace == desktop or workspace == WORKSPACE_ALL or desktop is None:
            # window is back in view
            self._client.control_refresh(self.wid, False, False)
        if (workspace < 0 or workspace >= ndesktops) and workspace not in (WORKSPACE_UNSET, WORKSPACE_ALL):
            # this should not happen, workspace is unsigned (CARDINAL)
            # and the server should have the same list of desktops that we have here
            workspacelog.warn("Warning: invalid workspace number: %s", wn(workspace))
            workspace = WORKSPACE_UNSET
        if workspace == WORKSPACE_UNSET:
            # we cannot unset via send_wm_workspace, so we have to choose one:
            workspace = self.get_desktop_workspace()
        if workspace in (None, WORKSPACE_UNSET):
            workspacelog.warn("workspace=%s (doing nothing)", wn(workspace))
            return
        # we will need the gdk window:
        if current == workspace:
            workspacelog("window workspace unchanged: %s", wn(workspace))
            return
        if WIN32:
            if not WIN32_WORKSPACE:
                return
            from xpra.platform.win32.gui import get_window_handle
            from pyvda.pyvda import AppView, VirtualDesktop
            hwnd = get_window_handle(self)
            if not hwnd:
                return
            vd = VirtualDesktop(number=workspace + 1)
            app_view = AppView(hwnd=hwnd)
            workspacelog(f"moving {app_view} to {vd}")
            app_view.move(vd)
            return
        if not HAS_X11_BINDINGS:
            return
        gdkwin = self.get_window()
        workspacelog("do_set_workspace: gdkwindow: %#x, mapped=%s, visible=%s",
                     gdkwin.get_xid(), self.get_mapped(), gdkwin.is_visible())
        with xlog:
            root_xid = X11Window.get_root_xid()
            send_wm_workspace(root_xid, gdkwin.get_xid(), workspace)

    def get_desktop_workspace(self) -> int:
        if WIN32:
            if not WIN32_WORKSPACE:
                return 0
            from pyvda.pyvda import VirtualDesktop
            return VirtualDesktop.current().number - 1

        window = self.get_window()
        if window:
            root = window.get_screen().get_root_window()
        else:
            # if we are called during init...
            # we don't have a window
            root = get_default_root_window()
        return self.do_get_workspace(root, "_NET_CURRENT_DESKTOP")

    def get_window_workspace(self) -> int:
        if WIN32:
            if not WIN32_WORKSPACE:
                return 0
            try:
                from xpra.platform.win32.gui import get_window_handle
                from pyvda.pyvda import AppView
            except ImportError as e:
                workspacelog(f"unable to query workspace: {e}")
                return 0
            hwnd = get_window_handle(self)
            if not hwnd:
                return 0
            try:
                return AppView(hwnd).desktop.number - 1
            except Exception:
                workspacelog("failed to query pyvda appview", exc_info=True)
                return 0
        return self.do_get_workspace(self.get_window(), "_NET_WM_DESKTOP", WORKSPACE_UNSET)

    def do_get_workspace(self, target, prop: str, default_value=0) -> int:
        if not self._can_set_workspace:
            workspacelog("do_get_workspace: not supported, returning %s", wn(default_value))
            return default_value  # OSX does not have workspaces
        if target is None:
            workspacelog("do_get_workspace: target is None, returning %s", wn(default_value))
            return default_value  # window is not realized yet
        value = self.xget_u32_property(target, prop)
        if value is not None:
            workspacelog("do_get_workspace %s=%s on window %i: %#x",
                         prop, wn(value), self.wid, target.get_xid())
            return value & 0xffffffff
        workspacelog("do_get_workspace %s unset on window %i: %#x, returning default value=%s",
                     prop, self.wid, target.get_xid(), wn(default_value))
        return default_value

    def keyboard_ungrab(self, *args) -> None:
        grablog("keyboard_ungrab%s", args)
        gdkwin = self.get_window()
        if gdkwin:
            d = gdkwin.get_display()
            if d:
                seat = d.get_default_seat()
                if seat:
                    seat.ungrab()
                    self._client.keyboard_grabbed = False

    def keyboard_grab(self, *args) -> None:
        grablog("keyboard_grab%s", args)
        gdkwin = self.get_window()
        r = Gdk.GrabStatus.FAILED
        seat = None
        if gdkwin:
            self.add_events(Gdk.EventMask.ALL_EVENTS_MASK)
            d = gdkwin.get_display()
            if d:
                seat = d.get_default_seat()
                if seat:
                    capabilities = Gdk.SeatCapabilities.KEYBOARD
                    owner_events = True
                    cursor = None
                    event = None
                    r = seat.grab(gdkwin, capabilities, owner_events, cursor, event, None, None)
                    grablog("%s.grab(..)=%s", seat, r)
        self._client.keyboard_grabbed = r == Gdk.GrabStatus.SUCCESS
        grablog("keyboard_grab%s %s.grab(..)=%s, keyboard_grabbed=%s",
                args, seat, GRAB_STATUS_STRING.get(r), self._client.keyboard_grabbed)

    def toggle_keyboard_grab(self) -> None:
        grabbed = self._client.keyboard_grabbed
        grablog("toggle_keyboard_grab() grabbed=%s", grabbed)
        if grabbed:
            self.keyboard_ungrab()
        else:
            self.keyboard_grab()

    def pointer_grab(self, *args) -> None:
        gdkwin = self.get_window()
        # try platform specific variant first:
        if pointer_grab(gdkwin):
            self._client.pointer_grabbed = self.wid
            grablog(f"{pointer_grab}({gdkwin}) success")
            return
        with IgnoreWarningsContext():
            r = Gdk.pointer_grab(gdkwin, True, GRAB_EVENT_MASK, gdkwin, None, Gdk.CURRENT_TIME)
        if r == Gdk.GrabStatus.SUCCESS:
            self._client.pointer_grabbed = self.wid
        grablog("pointer_grab%s Gdk.pointer_grab(%s, True)=%s, pointer_grabbed=%s",
                args, self.get_window(), GRAB_STATUS_STRING.get(r), self._client.pointer_grabbed)

    def pointer_ungrab(self, *args) -> None:
        gdkwin = self.get_window()
        if pointer_ungrab(gdkwin):
            self._client.pointer_grabbed = None
            grablog(f"{pointer_ungrab}({gdkwin}) success")
            return
        grablog("pointer_ungrab%s pointer_grabbed=%s",
                args, self._client.pointer_grabbed)
        self._client.pointer_grabbed = None
        gdkwin = self.get_window()
        if gdkwin:
            d = gdkwin.get_display()
            if d:
                d.pointer_ungrab(Gdk.CURRENT_TIME)

    def toggle_pointer_grab(self) -> None:
        pg = self._client.pointer_grabbed
        grablog("toggle_pointer_grab() pointer_grabbed=%s, our id=%s", pg, self.wid)
        if pg == self.wid:
            self.pointer_ungrab()
        else:
            self.pointer_grab()

    def toggle_fullscreen(self) -> None:
        geomlog("toggle_fullscreen()")
        if self._fullscreen:
            self.unfullscreen()
        else:
            self.fullscreen()

    ######################################################################
    # pointer overlay handling
    def cancel_remove_pointer_overlay_timer(self) -> None:
        rpot = self.remove_pointer_overlay_timer
        mouselog(f"cancel_remove_pointer_overlay_timer() timer={rpot}")
        if rpot:
            self.remove_pointer_overlay_timer = 0
            GLib.source_remove(rpot)

    def cancel_show_pointer_overlay_timer(self) -> None:
        rsot = self.show_pointer_overlay_timer
        mouselog(f"cancel_show_pointer_overlay_timer() timer={rsot}")
        if rsot:
            self.show_pointer_overlay_timer = 0
            GLib.source_remove(rsot)

    def show_pointer_overlay(self, pos) -> None:
        # schedule do_show_pointer_overlay if needed
        b = self._backing
        if not b:
            return
        prev = b.pointer_overlay
        if pos is None:
            if not prev:
                return
            value = None
        else:
            if prev and prev[:2] == pos[:2]:
                return
            # store both scaled and unscaled value:
            # (the opengl client uses the raw value)
            value = pos[:2] + self.sp(*pos[:2]) + pos[2:]
        mouselog("show_pointer_overlay(%s) previous value=%s, new value=%s", pos, prev, value)
        b.pointer_overlay = value
        if not self.show_pointer_overlay_timer:
            self.show_pointer_overlay_timer = GLib.timeout_add(10, self.do_show_pointer_overlay, prev)

    def do_show_pointer_overlay(self, prev) -> None:
        # queue a draw event at the previous and current position of the pointer
        # (so the backend will repaint / overlay the cursor image there)
        self.show_pointer_overlay_timer = 0
        b = self._backing
        if not b:
            return
        cursor_data = b.cursor_data

        def abs_coords(x, y, size) -> tuple[int, int, int, int]:
            if self.window_offset:
                x += self.window_offset[0]
                y += self.window_offset[1]
            w, h = size, size
            if cursor_data:
                w = cursor_data[3]
                h = cursor_data[4]
                xhot = cursor_data[5]
                yhot = cursor_data[6]
                x = x - xhot
                y = y - yhot
            return x, y, w, h

        value = b.pointer_overlay
        if value:
            # repaint the scale value (in window coordinates):
            x, y, w, h = abs_coords(*value[2:5])
            self.repaint(x, y, w, h)
            # clear it shortly after:
            self.schedule_remove_pointer_overlay()
        if prev:
            x, y, w, h = abs_coords(*prev[2:5])
            self.repaint(x, y, w, h)

    def schedule_remove_pointer_overlay(self, delay: int = CURSOR_IDLE_TIMEOUT * 1000) -> None:
        mouselog(f"schedule_remove_pointer_overlay({delay})")
        self.cancel_remove_pointer_overlay_timer()
        self.remove_pointer_overlay_timer = GLib.timeout_add(delay, self.remove_pointer_overlay)

    def remove_pointer_overlay(self) -> None:
        mouselog("remove_pointer_overlay()")
        self.remove_pointer_overlay_timer = 0
        self.show_pointer_overlay(None)

    def _do_button_press_event(self, event) -> None:
        # Gtk.Window.do_button_press_event(self, event)
        button = _button_resolve(event.button)
        self._button_action(button, event, True)

    def _do_button_release_event(self, event) -> None:
        # Gtk.Window.do_button_release_event(self, event)
        button = _button_resolve(event.button)
        self._button_action(button, event, False)

    ######################################################################
    # pointer motion

    def _do_motion_notify_event(self, event) -> None:
        # Gtk.Window.do_motion_notify_event(self, event)
        if self.moveresize_event:
            self.motion_moveresize(event)
        self.cancel_remove_pointer_overlay_timer()
        self.remove_pointer_overlay()
        ClientWindowBase._do_motion_notify_event(self, event)

    def motion_moveresize(self, event) -> None:
        x_root, y_root, direction, button, start_buttons, wx, wy, ww, wh = self.moveresize_event
        dirstr = MOVERESIZE_DIRECTION_STRING.get(direction, direction)
        buttons = _event_buttons(event)
        geomlog("motion_moveresize(%s) direction=%s, buttons=%s", event, dirstr, buttons)
        if start_buttons is None:
            # first time around, store the buttons
            start_buttons = buttons
            self.moveresize_event[4] = buttons
        if (button > 0 and button not in buttons) or (button == 0 and start_buttons != buttons):
            geomlog("%s for window button %i is no longer pressed (buttons=%s) cancelling moveresize",
                    dirstr, button, buttons)
            self.moveresize_event = None
            self.cancel_moveresize_timer()
        else:
            x = event.x_root
            y = event.y_root
            dx = x - x_root
            dy = y - y_root
            # clamp resizing using size hints,
            # or sane defaults: minimum of (1x1) and maximum of (2*15x2*25)
            minw = self.geometry_hints.get("min_width", 1)
            minh = self.geometry_hints.get("min_height", 1)
            maxw = self.geometry_hints.get("max_width", 2 ** 15)
            maxh = self.geometry_hints.get("max_height", 2 ** 15)
            geomlog("%s: min=%ix%i, max=%ix%i, window=%ix%i, delta=%ix%i",
                    dirstr, minw, minh, maxw, maxh, ww, wh, dx, dy)
            if direction in (MoveResize.SIZE_BOTTOMRIGHT, MoveResize.SIZE_BOTTOM, MoveResize.SIZE_BOTTOMLEFT):
                # height will be set to: wh+dy
                dy = max(minh - wh, dy)
                dy = min(maxh - wh, dy)
            elif direction in (MoveResize.SIZE_TOPRIGHT, MoveResize.SIZE_TOP, MoveResize.SIZE_TOPLEFT):
                # height will be set to: wh-dy
                dy = min(wh - minh, dy)
                dy = max(wh - maxh, dy)
            if direction in (MoveResize.SIZE_BOTTOMRIGHT, MoveResize.SIZE_RIGHT, MoveResize.SIZE_TOPRIGHT):
                # width will be set to: ww+dx
                dx = max(minw - ww, dx)
                dx = min(maxw - ww, dx)
            elif direction in (MoveResize.SIZE_BOTTOMLEFT, MoveResize.SIZE_LEFT, MoveResize.SIZE_TOPLEFT):
                # width will be set to: ww-dx
                dx = min(ww - minw, dx)
                dx = max(ww - maxw, dx)
            # calculate move + resize:
            if direction == MoveResize.MOVE:
                data = (wx + dx, wy + dy), None
            elif direction == MoveResize.SIZE_BOTTOMRIGHT:
                data = None, (ww + dx, wh + dy)
            elif direction == MoveResize.SIZE_BOTTOM:
                data = None, (ww, wh + dy)
            elif direction == MoveResize.SIZE_BOTTOMLEFT:
                data = (wx + dx, wy), (ww - dx, wh + dy)
            elif direction == MoveResize.SIZE_RIGHT:
                data = None, (ww + dx, wh)
            elif direction == MoveResize.SIZE_LEFT:
                data = (wx + dx, wy), (ww - dx, wh)
            elif direction == MoveResize.SIZE_TOPRIGHT:
                data = (wx, wy + dy), (ww + dx, wh - dy)
            elif direction == MoveResize.SIZE_TOP:
                data = (wx, wy + dy), (ww, wh - dy)
            elif direction == MoveResize.SIZE_TOPLEFT:
                data = (wx + dx, wy + dy), (ww - dx, wh - dy)
            else:
                # not handled yet!
                data = None
            geomlog("%s for window %ix%i: started at %s, now at %s, delta=%s, button=%s, buttons=%s, data=%s",
                    dirstr, ww, wh, (x_root, y_root), (x, y), (dx, dy), button, buttons, data)
            if data:
                # modifying the window is slower than moving the pointer,
                # do it via a timer to batch things together
                self.moveresize_data = data
                if self.moveresize_timer is None:
                    self.moveresize_timer = GLib.timeout_add(20, self.do_moveresize)

    def cancel_moveresize_timer(self) -> None:
        mrt = self.moveresize_timer
        if mrt:
            self.moveresize_timer = 0
            GLib.source_remove(mrt)

    def do_moveresize(self) -> None:
        self.moveresize_timer = 0
        mrd = self.moveresize_data
        geomlog("do_moveresize() data=%s", mrd)
        if not mrd:
            return
        move, resize = mrd
        x = y = w = h = 0
        if move:
            x, y = int(move[0]), int(move[1])
        if resize:
            w, h = int(resize[0]), int(resize[1])
            if self._client.readonly:
                # change size-constraints first,
                # so the resize can be honoured:
                sc = typedict(force_size_constraint(w, h))
                self._metadata.update(sc)
                self.set_metadata(sc)
        if move and resize:
            self.get_window().move_resize(x, y, w, h)
        elif move:
            self.get_window().move(x, y)
        elif resize:
            self.get_window().resize(w, h)

    def initiate_moveresize(self, x_root: int, y_root: int, direction: int, button: int,
                            source_indication: int) -> None:
        geomlog("initiate_moveresize%s",
                (
                    x_root, y_root, MOVERESIZE_DIRECTION_STRING.get(direction, direction),
                    button, SOURCE_INDICATION_STRING.get(source_indication, source_indication)
                ))
        # the values we get are bogus!
        # x, y = x_root, y_root
        # use the current position instead:
        # the values we get are bogus!
        # x, y = x_root, y_root
        # use the current position instead:
        with IgnoreWarningsContext():
            p = self.get_root_window().get_pointer()[-3:]
        x, y, mask = p
        if MOVERESIZE_GUESS_BUTTON and button <= 0 and direction not in (
                MoveResize.MOVE_KEYBOARD, MoveResize.SIZE_KEYBOARD, MoveResize.CANCEL,
        ):
            # button seems to be missing!
            for bmask, bval in BUTTON_MASK.items():
                if bmask & mask:
                    button = bval
                    log(f"guessed button {button=}")
                    break
        if MOVERESIZE_X11 and HAS_X11_BINDINGS:
            self.initiate_moveresize_x11(x, y, direction, button, source_indication)
            return
        if direction == MoveResize.CANCEL:
            self.moveresize_event = None
            self.moveresize_data = None
            self.cancel_moveresize_timer()
        elif MOVERESIZE_GDK:
            if direction in (MoveResize.MOVE, MoveResize.MOVE_KEYBOARD):
                self.begin_move_drag(button, x, y, 0)
            else:
                edge = GDK_MOVERESIZE_MAP.get(direction)
                geomlog("edge(%s)=%s", MOVERESIZE_DIRECTION_STRING.get(direction), edge)
                if direction is not None:
                    etime = Gtk.get_current_event_time()
                    self.begin_resize_drag(edge, button, x, y, etime)
        else:
            # handle it ourselves:
            # use window coordinates (which include decorations)
            wx, wy = self.get_window().get_root_origin()
            ww, wh = self.get_size()
            self.moveresize_event = [x_root, y_root, direction, button, None, wx, wy, ww, wh]

    def initiate_moveresize_x11(self, x_root: int, y_root: int, direction: int,
                                button: int, source_indication: int) -> None:
        statelog("initiate_moveresize_x11%s",
                 (x_root, y_root, MOVERESIZE_DIRECTION_STRING.get(direction, direction),
                  button, SOURCE_INDICATION_STRING.get(source_indication, source_indication)))
        event_mask = SubstructureNotifyMask | SubstructureRedirectMask
        assert HAS_X11_BINDINGS
        xwin = self.get_window().get_xid()
        with xlog:
            root_xid = X11Core.get_root_xid()
            X11Core.UngrabPointer()
            X11Window.sendClientMessage(root_xid, xwin, False, event_mask, "_NET_WM_MOVERESIZE",
                                        x_root, y_root, direction, button, source_indication)

    def apply_transient_for(self, wid: int) -> None:
        # Previously we skipped this to avoid GTK repositioning, but that leaves the popup without an owner.
        # Always set transient-for and restore the position if GTK moves the window.
        is_wine_popup = self._is_wine_popup_menu()
        if is_wine_popup:
            geomlog("WINE POPUP apply_transient_for: applying transient owner (wid=%s, target=%s)",
                    self.wid, wid)

        if wid == -1:
            def set_root_transient() -> None:
                # root is a gdk window, so we need to ensure we have one
                # backing our gtk window to be able to call set_transient_for on it
                log("%s.apply_transient_for(%s) gdkwindow=%s, mapped=%s",
                    self, wid, self.get_window(), self.get_mapped())
                self.get_window().set_transient_for(get_default_root_window())

            self.when_realized("transient-for-root", set_root_transient)
        else:
            # gtk window is easier:
            window = self._client._id_to_window.get(wid)
            log("%s.apply_transient_for(%s) window=%s", self, wid, window)
            if window and isinstance(window, Gtk.Window):
                # Save current position before setting transient so we can restore if GTK repositions the window
                saved_pos = self._pos if hasattr(self, '_pos') and self._pos else None
                self.set_transient_for(window)
                # If this is an OR window and we have a saved position, restore it (non-Wine popups handled here)
                if self.is_OR() and saved_pos:
                    # Check if GTK changed the position
                    current_pos = self.get_position()
                    if current_pos != saved_pos:
                        geomlog("WINE POPUP apply_transient_for: GTK repositioned OR window, restoring position (wid=%s)", self.wid)
                        geomlog("WINE POPUP apply_transient_for: saved=%s, current=%s, restoring to=%s (wid=%s)", saved_pos, current_pos, saved_pos, self.wid)
                        self.move(saved_pos[0], saved_pos[1])
                        self._pos = saved_pos

    def cairo_paint_border(self, context, clip_area=None) -> None:
        log("cairo_paint_border(%s, %s)", context, clip_area)
        b = self.border
        if b is None or not b.shown:
            return
        rw, rh = self.get_size()
        hsize = min(b.size, rw)
        vsize = min(b.size, rh)
        if rw <= hsize or rh <= vsize:
            rects = ((0, 0, rw, rh), )
        else:
            rects = (
                (0, 0, rw, vsize),                              # top
                (rw - hsize, vsize, hsize, rh - vsize * 2),     # right
                (0, rh - vsize, rw, vsize),                     # bottom
                (0, vsize, hsize, rh - vsize * 2),              # left
            )

        for x, y, w, h in rects:
            if w <= 0 or h <= 0:
                continue
            r = Gdk.Rectangle()
            r.x = x
            r.y = y
            r.width = w
            r.height = h
            rect = r
            if clip_area:
                rect = clip_area.intersect(r)
            if rect.width == 0 or rect.height == 0:
                continue
            context.save()
            context.rectangle(x, y, w, h)
            context.clip()
            context.set_source_rgba(b.red, b.green, b.blue, b.alpha)
            context.fill()
            context.paint()
            context.restore()

    def paint_spinner(self, context, area=None) -> None:
        log("%s.paint_spinner(%s, %s)", self, context, area)
        c = self._client
        if not c:
            return
        ww, wh = self.get_size()
        w = c.cx(ww)
        h = c.cy(wh)
        # add grey semi-opaque layer on top:
        context.set_operator(OPERATOR_OVER)
        context.set_source_rgba(0.2, 0.2, 0.2, 0.4)
        # we can't use the area as rectangle with:
        # context.rectangle(area)
        # because those would be unscaled dimensions
        # it is easier and safer to repaint the whole window:
        context.rectangle(0, 0, w, h)
        context.fill()
        # add spinner:
        dim = min(w / 3.0, h / 3.0, 100.0)
        context.set_line_width(dim / 10.0)
        context.set_line_cap(LINE_CAP_ROUND)
        context.translate(w / 2, h / 2)
        from xpra.client.gui.spinner import cv
        count = int(monotonic() * 4.0)
        for i in range(8):  # 8 lines
            context.set_source_rgba(0, 0, 0, cv.trs[count % 8][i])
            context.move_to(0.0, -dim / 4.0)
            context.line_to(0.0, -dim)
            context.rotate(math.pi / 4)
            context.stroke()

    def spinner(self, _ok) -> None:
        c = self._client
        if not self.can_have_spinner() or not c:
            return
        # with normal windows, we just queue a draw request
        # and let the expose event paint the spinner
        w, h = self.get_size()
        self.repaint(0, 0, w, h)

    def do_map_event(self, event) -> None:
        log("%s.do_map_event(%s) OR=%s", self, event, self._override_redirect)
        Gtk.Window.do_map_event(self, event)
        if not self._override_redirect:
            # we can get a map event for an iconified window on win32:
            if self._iconified:
                self.deiconify()
            self.process_map_event()
        # use the drawing area to enforce the minimum size:
        # (as this also honoured correctly with CSD,
        # whereas set_geometry_hints is not..)
        minw, minh = self.size_constraints.intpair("minimum-size", (0, 0))
        w, h = self.sp(minw, minh)
        geomlog("do_map_event %s.set_size_request%s", self.drawing_area, (minw, minh))
        self.drawing_area.set_size_request(w, h)

    def process_map_event(self) -> None:
        x, y, w, h = self.get_drawing_area_geometry()
        state = self._window_state
        props = self._client_properties
        self._client_properties = {}
        self._window_state = {}
        self.cancel_window_state_timer()
        workspace = self.get_window_workspace()
        if self._been_mapped:
            if workspace is None:
                # not set, so assume it is on the current workspace:
                workspace = self.get_desktop_workspace()
        else:
            self._been_mapped = True
            workspace = self._metadata.intget("workspace", WORKSPACE_UNSET)
            if workspace != WORKSPACE_UNSET:
                log("map event set workspace %s", wn(workspace))
                self.set_workspace(workspace)
        if self._window_workspace != workspace and workspace is not None:
            workspacelog("map event: been_mapped=%s, changed workspace from %s to %s",
                         self._been_mapped, wn(self._window_workspace), wn(workspace))
            self._window_workspace = workspace
        if workspace is not None:
            props["workspace"] = workspace
        if self._client.server_window_frame_extents and "frame" not in state:
            wfs = self.get_window_frame_size()
            if wfs and len(wfs) == 4:
                state["frame"] = self.crect(*wfs)
                self._current_frame_extents = wfs
        geomlog("map-window wid=%s, geometry=%s, client props=%s, state=%s", self.wid, (x, y, w, h), props, state)
        sx, sy, sw, sh = self.cx(x), self.cy(y), self.cx(w), self.cy(h)
        if self._backing is None:
            # we may have cleared the backing, so we must re-create one:
            self._set_backing_size(w, h)
        packet = ["map-window", self.wid, sx, sy, sw, sh, props, state]
        self.send(*packet)
        self._pos = (x, y)
        self._size = (w, h)
        self.update_relative_position()
        if not self._override_redirect:
            htf = self.has_toplevel_focus()
            focuslog("mapped: has-toplevel-focus=%s", htf)
            if htf:
                self._client.update_focus(self.wid, htf)

    def get_window_frame_size(self) -> dict[str, Any]:
        frame = self._client.get_frame_extents(self)
        if not frame:
            # default to global value we may have:
            wfs = self._client.get_window_frame_sizes()
            if wfs:
                frame = wfs.get("frame")
        return frame

    def monitor_changed(self, monitor) -> None:
        display = monitor.get_display()
        mid = -1
        for i in range(display.get_n_monitors()):
            m = display.get_monitor(i)
            if m == monitor:
                mid = i
                break
        geom = monitor.get_geometry()
        manufacturer = monitor.get_manufacturer()
        model = monitor.get_model()
        if manufacturer == "unknown":
            manufacturer = ""
        if model == "unknown":
            model = ""
        if manufacturer and model:
            plug_name = "%s %s" % (manufacturer, model)
        elif manufacturer:
            plug_name = manufacturer
        elif model:
            plug_name = model
        else:
            plug_name = "%i" % mid
        plug_name += " %ix%i at %i,%i" % (geom.width, geom.height, geom.x, geom.y)
        eventslog.info("window %i has been moved to monitor %i: %s", self.wid, mid, plug_name)

    def update_relative_position(self) -> None:
        x, y = self.get_position()
        log("update_relative_position() follow_configure=%s", self._follow_configure)
        fc = self._follow_configure
        if fc:
            event_time, event_pos = fc
            # until we see the event we caused by calling move(),
            # or if we timeout (for safety - some platforms may skip events?),
            # don't update the relative position
            if monotonic() - event_time > 0.1 or fc == event_pos:
                # next time we will allow the update:
                self._follow_configure = None
            return
        follow = self._follow
        if not follow:
            return
        # adjust our relative position:
        fpos = getattr(follow, "_pos", None)
        if not fpos:
            return
        fx, fy = fpos
        rel_pos = x - fx, y - fy
        self._follow_position = follow.cp(*rel_pos)
        log("update_relative_position() relative position of %s from %s is %s, follow position=%s",
            self._pos, fpos, rel_pos, self._follow_position)

    def may_send_client_properties(self) -> None:
        # if there are client properties the server should know about,
        # we currently have no other way to send them to the server:
        if self._client_properties:
            self.send_configure_event(True)

    def send_configure(self) -> None:
        self.send_configure_event()

    def do_configure_event(self, event) -> None:
        eventslog("%s.do_configure_event(%s) OR=%s, iconified=%s",
                  self, event, self._override_redirect, self._iconified)
        Gtk.Window.do_configure_event(self, event)
        
        # PATCH: Re-check and enforce position for Wine popup menus during configure events
        # GTK may trigger configure events with incorrect positions after realize
        if self.is_OR() and not self._iconified:
            is_wine_popup = self._is_wine_popup_menu()
            if is_wine_popup and self._pos:
                # Current position from GTK after the event
                current_pos = self.get_position() if self.get_realized() else None
                saved_pos = self._pos
                # If the current position differs, GTK has repositioned the window
                if current_pos and current_pos != saved_pos:
                    geomlog("WINE POPUP do_configure_event: GTK repositioned in configure event, correcting (wid=%s)", self.wid)
                    geomlog("WINE POPUP do_configure_event: current position=%s, target=%s (wid=%s)", 
                           current_pos, saved_pos, self.wid)
                    # Force the position back
                    self.move(saved_pos[0], saved_pos[1])
                    self._pos = saved_pos
                    # Verify after move
                    verify_pos = self.get_position() if self.get_realized() else None
                    geomlog("WINE POPUP do_configure_event: position corrected (wid=%s, target=%s, actual=%s)", 
                           self.wid, saved_pos, verify_pos)
        
        if self._override_redirect or self._iconified:
            # don't send configure packet for OR windows or iconified windows
            return
        x, y, w, h = self.get_drawing_area_geometry()
        w = max(1, w)
        h = max(1, h)
        ox, oy = self._pos
        dx, dy = x - ox, y - oy
        self._pos = (x, y)
        self.update_relative_position()
        gdkwin = self.get_window()
        screen = gdkwin.get_screen()
        display = screen.get_display()
        monitor = display.get_monitor_at_window(gdkwin)
        if monitor != self._monitor:
            if self._monitor is not None:
                self.monitor_changed(monitor)
            self._monitor = monitor
        geomlog("configure event: current size=%s, new size=%s, moved by=%s, backing=%s, iconified=%s",
                self._size, (w, h), (dx, dy), self._backing, self._iconified)
        self._size = (w, h)
        self._set_backing_size(w, h)
        self.send_configure_event()
        if self._backing and not self._iconified:
            geomlog("configure event: queueing redraw")
            self.repaint(0, 0, w, h)

    def send_configure_event(self, skip_geometry=False) -> None:
        assert skip_geometry or not self.is_OR()
        x, y, w, h = self.get_drawing_area_geometry()
        w = max(1, w)
        h = max(1, h)
        state = self._window_state
        props = self._client_properties
        self._client_properties = {}
        self._window_state = {}
        self.cancel_window_state_timer()
        if self._been_mapped:
            # if the window has been mapped already, the workspace should be set:
            workspace = self.get_window_workspace()
            if self._window_workspace != workspace and workspace is not None:
                workspacelog("send_configure_event: changed workspace from %s to %s",
                             wn(self._window_workspace), wn(workspace))
                self._window_workspace = workspace
                props["workspace"] = workspace
        sx, sy, sw, sh = self.cx(x), self.cy(y), self.cx(w), self.cy(h)
        packet = ["configure-window", self.wid, sx, sy, sw, sh, props, self._resize_counter, state, skip_geometry]
        pwid = self.wid
        if self.is_OR():
            pwid = -1
        packet.append(pwid)
        packet.append(self.get_mouse_position())
        packet.append(self._client.get_current_modifiers())
        geomlog("%s", packet)
        self.send(*packet)

    def _set_backing_size(self, ww: int, wh: int) -> None:
        b = self._backing
        bw = self.cx(ww)
        bh = self.cy(wh)
        if max(ww, wh) >= 32000 or min(ww, wh) < 0:
            raise ValueError("invalid window size %ix%i" % (ww, wh))
        if max(bw, bh) >= 32000:
            raise ValueError("invalid window backing size %ix%i" % (bw, bh))
        if b:
            prev_render_size = b.render_size
            b.init(ww, wh, bw, bh)
            if prev_render_size != b.render_size:
                self._client_properties["encoding.render-size"] = b.render_size
        else:
            self.new_backing(bw, bh)

    def resize(self, w: int, h: int, resize_counter: int = 0) -> None:
        self._overlay_reset_measurements()
        ww, wh = self.get_size()
        geomlog("resize(%s, %s, %s) current size=%s, fullscreen=%s, maximized=%s",
                w, h, resize_counter, (ww, wh), self._fullscreen, self._maximized)
        size_changed = (w, h) != self._size
        if WIN32 and self.is_OR() and size_changed:
            self._mark_win32_shadow_dirty(True)
            if self._is_wine_popup_menu():
                self._queue_win32_shadow_retry(0)
        self._resize_counter = resize_counter
        if (w, h) == (ww, wh):
            self._backing.offsets = 0, 0, 0, 0
            self.repaint(0, 0, w, h)
            return
        if not self._fullscreen and not self._maximized:
            Gtk.Window.resize(self, w, h)
            ww, wh = w, h
            self._backing.offsets = 0, 0, 0, 0
        else:
            self.center_backing(w, h)
        geomlog("backing offsets=%s, window offset=%s", self._backing.offsets, self.window_offset)
        self._set_backing_size(w, h)
        self.repaint(0, 0, ww, wh)
        self.may_send_client_properties()

    def center_backing(self, w, h) -> None:
        ww, wh = self.get_size()
        # align in the middle:
        dw = max(0, ww - w)
        dh = max(0, wh - h)
        ox = dw // 2
        oy = dh // 2
        geomlog("using window offset values %i,%i", ox, oy)
        # some backings use top,left values,
        # (opengl uses left and bottom since the viewport starts at the bottom)
        self._backing.offsets = ox, oy, ox + (dw & 0x1), oy + (dh & 0x1)
        geomlog("center_backing(%i, %i) window size=%ix%i, backing offsets=%s", w, h, ww, wh, self._backing.offsets)
        # adjust pointer coordinates:
        self.window_offset = ox, oy

    def paint_backing_offset_border(self, backing, context) -> None:
        w, h = self.get_size()
        left, top, right, bottom = backing.offsets
        if left != 0 or top != 0 or right != 0 or bottom != 0:
            context.save()
            context.set_source_rgb(*PADDING_COLORS)
            coords = (
                (0, 0, left, h),  # left hand side padding
                (0, 0, w, top),  # top padding
                (w - right, 0, right, h),  # RHS
                (0, h - bottom, w, bottom),  # bottom
            )
            geomlog("paint_backing_offset_border(%s, %s) offsets=%s, size=%s, rgb=%s, coords=%s",
                    backing, context, backing.offsets, (w, h), PADDING_COLORS, coords)
            for rx, ry, rw, rh in coords:
                if rw > 0 and rh > 0:
                    context.rectangle(rx, ry, rw, rh)
            context.fill()
            context.restore()

    def clip_to_backing(self, backing, context) -> None:
        w, h = self.get_size()
        left, top, right, bottom = backing.offsets
        clip_rect = (left, top, w - left - right, h - top - bottom)
        context.rectangle(*clip_rect)
        geomlog("clip_to_backing%s rectangle=%s", (backing, context), clip_rect)
        context.clip()

    def after_draw_refresh(self, success, message="") -> None:
        ClientWindowBase.after_draw_refresh(self, success, message)
        if not success or not WIN32:
            return
        if not self.is_OR() or not self._is_wine_popup_menu():
            return
        if self._win32_shadow_trim and self._win32_shadow_region_applied:
            return
        self._queue_win32_shadow_retry(0)

    def move_resize(self, x: int, y: int, w: int, h: int, resize_counter: int = 0) -> None:
        geomlog("window %i move_resize%s", self.wid, (x, y, w, h, resize_counter))
        self._overlay_reset_measurements()

        # PATCH: Skip moving Wine popup menus; only resize when necessary
        # Wine popup menus use absolute positions from the server, so avoid moving them
        is_wine_popup = self._is_wine_popup_menu() if self.is_OR() else False
        if is_wine_popup:
            geomlog("WINE POPUP move_resize: skipping move, only resize if needed (wid=%s)", self.wid)
            geomlog("WINE POPUP move_resize: requested position=(%s, %s), current=%s (wid=%s)", 
                   x, y, self._pos, self.wid)
            # Only resize if the size changes, keep the position
            w = max(1, w)
            h = max(1, h)
            if self._size != (w, h):
                geomlog("WINE POPUP move_resize: resizing to (%s, %s) (wid=%s)", w, h, self.wid)
                self.resize(w, h)
            else:
                geomlog("WINE POPUP move_resize: size unchanged, no action (wid=%s)", self.wid)
            return
        size_changed = (w, h) != self._size
        if WIN32 and self.is_OR() and size_changed:
            self._mark_win32_shadow_dirty(True)
            if self._is_wine_popup_menu():
                self._queue_win32_shadow_retry(0)
        x, y = self.adjusted_position(x, y)
        w = max(1, w)
        h = max(1, h)
        if self.window_offset:
            x += self.window_offset[0]
            y += self.window_offset[1]
            # TODO: check this doesn't move it off-screen!
        self._resize_counter = resize_counter
        wx, wy = self.get_drawing_area_geometry()[:2]
        if (wx, wy) == (x, y):
            # same location, just resize:
            if self._size == (w, h):
                geomlog("window unchanged")
            else:
                geomlog("unchanged position %ix%i, using resize(%i, %i)", x, y, w, h)
                self.resize(w, h)
            return
        # we have to move:
        if not self.get_realized():
            geomlog("window was not realized yet")
            self.realize()
        # adjust for window frame:
        window = self.get_window()
        ox, oy = window.get_origin()[-2:]
        rx, ry = window.get_root_origin()
        ax = x - (ox - rx)
        ay = y - (oy - ry)
        geomlog("window origin=%ix%i, root origin=%ix%i, actual position=%ix%i", ox, oy, rx, ry, ax, ay)
        # validate against edge of screen (ensure window is shown):
        if CLAMP_WINDOW_TO_SCREEN:
            mw, mh = self._client.get_root_size()
            if (ax + w) <= 0:
                ax = -w + 1
            elif ax >= mw:
                ax = mw - 1
            if not WINDOW_OVERFLOW_TOP and ay <= 0:
                ay = 0
            elif (ay + h) <= 0:
                ay = -y + 1
            elif ay >= mh:
                ay = mh - 1
            geomlog("validated window position for total screen area %ix%i : %ix%i", mw, mh, ax, ay)
        if self._size == (w, h):
            # just move:
            geomlog("window size unchanged: %ix%i, using move(%i, %i)", w, h, ax, ay)
            window.move(ax, ay)
            return
        # resize:
        self._size = (w, h)
        geomlog("%s.move_resize%s", window, (ax, ay, w, h))
        window.move_resize(ax, ay, w, h)
        # re-init the backing with the new size
        self._set_backing_size(w, h)
        self.repaint(0, 0, w, h)
        self.may_send_client_properties()

    def destroy(self) -> None:  # pylint: disable=method-hidden
        self.cancel_window_state_timer()
        self.cancel_send_iconifiy_timer()
        self.cancel_show_pointer_overlay_timer()
        self.cancel_remove_pointer_overlay_timer()
        self.cancel_focus_timer()
        self.cancel_moveresize_timer()
        self.cancel_follow_handler()
        self.cancel_workspace_timer()
        self.on_realize_cb = {}
        self.remove_event_receiver()
        ClientWindowBase.destroy(self)
        Gtk.Window.destroy(self)
        if self._client.has_focus(self.wid):
            self._unfocus()
        self.destroy = noop_destroy

    def do_unmap_event(self, event) -> None:
        self.cancel_follow_handler()
        eventslog("do_unmap_event(%s)", event)
        self._unfocus()
        if not self._override_redirect:
            self.send("unmap-window", self.wid, False)

    def do_delete_event(self, event) -> bool:
        # Gtk.Window.do_delete_event(self, event)
        eventslog("do_delete_event(%s)", event)
        self._client.window_close_event(self.wid)
        return True

    def get_mouse_position(self) -> tuple[int, int]:
        # this method is used on some platforms
        # to get the pointer position for events that don't include it
        # (ie: wheel events)
        x, y = self._client.get_raw_mouse_position()
        return self._offset_pointer(x, y)

    def _offset_pointer(self, x: int, y: int) -> tuple[int, int]:
        if self.window_offset:
            x -= self.window_offset[0]
            y -= self.window_offset[1]
        return self.cp(x, y)

    def _get_pointer(self, event) -> tuple[int, int]:
        return round(event.x_root), round(event.y_root)

    def _get_relative_pointer(self, event) -> tuple[int, int]:
        return round(event.x), round(event.y)

    def get_pointer_data(self, event) -> tuple[int, int, int, int]:
        x, y = self._get_pointer(event)
        rx, ry = self._get_relative_pointer(event)
        return self.adjusted_pointer_data(x, y, rx, ry)

    def adjusted_pointer_data(self, x: int, y: int, rx: int = 0, ry: int = 0) -> tuple[int, int, int, int]:
        # regular pointer coordinates are translated and scaled,
        # relative coordinates are scaled only:
        ox, oy = self._offset_pointer(x, y)
        cx, cy = self.cp(rx, ry)
        return ox, oy, cx, cy

    def _pointer_modifiers(self, event) -> tuple[tuple[int, int, int, int], list[str], list[int]]:
        pointer_data = self.get_pointer_data(event)
        # FIXME: state is used for both mods and buttons??
        modifiers = self._client.mask_to_names(event.state)
        buttons = _event_buttons(event)
        v = pointer_data, modifiers, buttons
        mouselog("pointer_modifiers(%s)=%s (x_root=%s, y_root=%s, window_offset=%s)",
                 event, v, event.x_root, event.y_root, self.window_offset)
        return v

    def parse_key_event(self, event, pressed: bool) -> KeyEvent:
        keyval = event.keyval
        keycode = event.hardware_keycode
        keyname = Gdk.keyval_name(keyval) or ""
        keyname = KEY_TRANSLATIONS.get((keyname, keyval, keycode), keyname)
        if keyname.startswith("U+") and not UNICODE_KEYNAMES:
            # workaround for MS Windows, try harder to find a valid key
            # see ticket #3417
            keymap = Gdk.Keymap.get_default()
            r = keymap.get_entries_for_keycode(event.hardware_keycode)
            if r[0]:
                for kc in r[2]:
                    keyname = Gdk.keyval_name(kc)
                    if not keyname.startswith("U+"):
                        break
        key_event = KeyEvent()
        key_event.modifiers = self._client.mask_to_names(event.state)
        key_event.keyname = keyname
        key_event.keyval = keyval or 0
        key_event.keycode = keycode
        key_event.group = event.group
        key_event.pressed = pressed
        key_event.string = ""
        try:
            codepoint = Gdk.keyval_to_unicode(keyval)
            key_event.string = chr(codepoint)
        except ValueError as e:
            keylog(f"failed to parse unicode string value of {event}", exc_info=True)
            try:
                key_event.string = event.string or ""
            except UnicodeDecodeError as ve:
                if first_time(f"key-{keycode}-{keyname}"):
                    keylog("parse_key_event(%s, %s)", event, pressed, exc_info=True)
                    keylog.warn("Warning: failed to parse string for key")
                    keylog.warn(f" {keyname=}, {keycode=}")
                    keylog.warn(f" {keyval=}, group={event.group}")
                    keylog.warn(" modifiers=%s", csv(key_event.modifiers))
                    keylog.warn(f" {e}")
                    keylog.warn(f" {ve}")
        keyeventlog("parse_key_event(%s, %s)=%s", event, pressed, key_event)
        return key_event

    def handle_key_press_event(self, _window, event) -> bool:
        key_event = self.parse_key_event(event, True)
        self._log_zenkaku_key(event, key_event, True)
        # On Windows, skip native zenkaku key events - they are handled by low-level keyboard hook
        # to avoid double processing and unstable IME toggle behavior
        if WIN32 and self._is_native_zenkaku_key(event, key_event):
            zenlog.info("skipping native zenkaku key press in GTK handler (handled by low-level hook)")
            return True  # Return True to indicate event was handled (by ignoring it)
        if self.moveresize_event and key_event.keyname in BREAK_MOVERESIZE:
            # cancel move resize if there is one:
            self.moveresize_event = None
            self.cancel_moveresize_timer()
            return False
        self._client.handle_key_action(self, key_event)
        return True

    def handle_key_release_event(self, _window, event) -> bool:
        key_event = self.parse_key_event(event, False)
        self._log_zenkaku_key(event, key_event, False)
        # On Windows, skip native zenkaku key events - they are handled by low-level keyboard hook
        # to avoid double processing and unstable IME toggle behavior
        if WIN32 and self._is_native_zenkaku_key(event, key_event):
            zenlog.info("skipping native zenkaku key release in GTK handler (handled by low-level hook)")
            return True  # Return True to indicate event was handled (by ignoring it)
        self._client.handle_key_action(self, key_event)
        return True

    def _is_native_zenkaku_key(self, event, key_event: KeyEvent) -> bool:
        """Check if this is a native Windows zenkaku key event that should be ignored.
        Native events have hardware_keycode matching VK_KANA (0x15), VK_OEM_F3 (0xF3), or VK_OEM_ENLW (0xF4).
        Synthesized events from the low-level hook will have different characteristics (keyval=0xFF2A).
        """
        if not WIN32:
            return False
        hwcode = getattr(event, "hardware_keycode", 0)
        keycode = getattr(key_event, "keycode", 0)
        keyval = getattr(event, "keyval", 0)
        keyname = (key_event.keyname or "").lower()
        # VK_KANA is 0x15, which is the native Windows virtual key code for zenkaku key
        VK_KANA = 0x15
        VK_OEM_F3 = 0xF3  # Some Japanese keyboards emit this for zenkaku key
        VK_OEM_ENLW = 0xF4  # Some Japanese keyboards emit this for zenkaku key
        ZENKAKU_KEYVAL = 0xFF2A  # Synthesized keyval from low-level hook
        VOID_SYMBOL = 0xFFFFFF  # GTK maps unmapped keys to this
        
        # Check hardware keycode - native zenkaku key codes
        if hwcode in (VK_KANA, VK_OEM_F3, VK_OEM_ENLW):
            # Only ignore if it's a native event (not synthesized)
            # Synthesized events have keyval 0xFF2A (ZENKAKU_KEYVAL)
            if keyval != ZENKAKU_KEYVAL:
                zenlog.info(
                    "detected native zenkaku key: hwcode=0x%x keycode=%s keyval=0x%x keyname=%s",
                    hwcode, keycode, keyval, keyname
                )
                return True
        
        # Check keycode in key_event - also check this in case hardware_keycode is not set correctly
        if keycode in (VK_KANA, VK_OEM_F3, VK_OEM_ENLW):
            # Only ignore if it's a native event (not synthesized)
            if keyval != ZENKAKU_KEYVAL:
                zenlog.info(
                    "detected native zenkaku key by keycode: hwcode=0x%x keycode=%s keyval=0x%x keyname=%s",
                    hwcode, keycode, keyval, keyname
                )
                return True
        
        # Check for VoidSymbol (0xFFFFFF) with zenkaku keycodes - GTK maps unmapped keys to this
        if keyval == VOID_SYMBOL and (hwcode in (VK_KANA, VK_OEM_F3, VK_OEM_ENLW) or 
                                      keycode in (VK_KANA, VK_OEM_F3, VK_OEM_ENLW)):
            zenlog.info(
                "detected native zenkaku key by VoidSymbol: hwcode=0x%x keycode=%s keyval=0x%x keyname=%s",
                hwcode, keycode, keyval, keyname
            )
            return True
        
        # Also check for zenkaku-related keynames that come from native Windows events
        if keyname in {"zenkaku_hankaku", "zenkaku", "hankaku", "kana"}:
            # Only ignore if it's a native event (not synthesized)
            if keyval != ZENKAKU_KEYVAL:
                return True
        return False

    def _log_zenkaku_key(self, event, key_event: KeyEvent, pressed: bool) -> None:
        """Log the Windows Zenkaku/Hankaku key so we can confirm it is delivered."""
        if not WIN32:
            return
        keyname = (key_event.keyname or "").lower()
        hwcode = getattr(event, "hardware_keycode", 0)
        keyval = getattr(event, "keyval", 0)
        if keyname in {
            "zenkaku_hankaku", "zenkaku", "hankaku",
            "kanji", "hiragana_katakana", "kana_switch",
        } or hwcode == 0x15:  # 0x15 is VK_KANA on Windows keyboards
            zenlog.info(
                "zenkaku key %s: keyname=%s keyval=%#x hwcode=%#x state=%#x group=%s string=%r",
                "down" if pressed else "up",
                key_event.keyname,
                keyval or 0,
                hwcode,
                getattr(event, "state", 0),
                getattr(event, "group", 0),
                key_event.string,
            )

    def _do_scroll_event(self, event) -> bool:
        if self._client.readonly:
            return True
        if event.direction == Gdk.ScrollDirection.SMOOTH:
            log("smooth scroll event: %s, raw delta: %s,%s", event, event.delta_x, event.delta_y)
            pointer = self.get_pointer_data(event)
            device_id = -1
            norm_x = norm_scroll(event.delta_x)
            norm_y = norm_scroll(event.delta_y)
            self._client.wheel_event(device_id, self.wid, norm_x, -norm_y, pointer)
            return True
        button_mapping = GDK_SCROLL_MAP.get(event.direction, -1)
        mouselog("do_scroll_event device=%s, direction=%s, button_mapping=%s",
                 self._device_info(event), event.direction, button_mapping)
        if button_mapping >= 0:
            self._button_action(button_mapping, event, True)
            self._button_action(button_mapping, event, False)
        return True

    def update_icon(self, img) -> None:
        self._current_icon = img
        has_alpha = img.mode == "RGBA"
        width, height = img.size
        rowstride = width * (3 + int(has_alpha))
        pixbuf = get_pixbuf_from_data(img.tobytes(), has_alpha, width, height, rowstride)
        iconlog("%s.set_icon(%s)", self, pixbuf)
        self.set_icon(pixbuf)
