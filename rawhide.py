#!/usr/bin/env python3
"""
Rawhide - Image Viewer for Debian Linux
Supports NEF (Nikon RAW), JPG, and PNG files
"""

import os
import sys
import threading
import gc

# If running with the system Python (e.g. `python3 rawhide.py` directly),
# the venv packages won't be on sys.path. Re-exec with the venv Python if
# it exists so rawpy/Pillow/numpy are available.
_VENV_PYTHON = "/usr/local/share/rawhide/venv/bin/python3"
if (
    os.path.isfile(_VENV_PYTHON)
    and os.path.realpath(sys.executable) != os.path.realpath(_VENV_PYTHON)
):
    os.execv(_VENV_PYTHON, [_VENV_PYTHON] + sys.argv)

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, GdkPixbuf, GLib, Gio

RAWPY_IMPORT_ERROR = None
try:
    import rawpy
    RAWPY_AVAILABLE = True
except Exception as _e:
    RAWPY_AVAILABLE = False
    RAWPY_IMPORT_ERROR = str(_e)
    print(f"Warning: rawpy not available — {_e}")

try:
    from PIL import Image
    import numpy as np
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    print("Warning: Pillow/numpy not available.")


SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".nef", ".nrw", ".raw"}
RAW_EXTENSIONS = {".nef", ".nrw", ".raw"}

APP_NAME = "Rawhide"
APP_VERSION = "1.0.0"


def pil_image_to_pixbuf(pil_img):
    """Convert a PIL Image to a GdkPixbuf."""
    if pil_img.mode not in ("RGB", "RGBA"):
        pil_img = pil_img.convert("RGB")
    has_alpha = pil_img.mode == "RGBA"
    data = pil_img.tobytes()
    w, h = pil_img.size
    return GdkPixbuf.Pixbuf.new_from_data(
        data,
        GdkPixbuf.Colorspace.RGB,
        has_alpha,
        8,
        w,
        h,
        w * (4 if has_alpha else 3),
    )


def load_image_file(path):
    """Load any supported image file, return PIL Image."""
    ext = os.path.splitext(path)[1].lower()
    if ext in RAW_EXTENSIONS and not RAWPY_AVAILABLE:
        raise RuntimeError(f"rawpy is required to open RAW files but is not available: {RAWPY_IMPORT_ERROR}")
    if ext in RAW_EXTENSIONS:
        with rawpy.imread(path) as raw:
            rgb = raw.postprocess(
                use_camera_wb=True,
                half_size=False,
                no_auto_bright=False,
                output_bps=8,
            )
        return Image.fromarray(rgb)
    elif PIL_AVAILABLE:
        return Image.open(path).copy()
    else:
        raise RuntimeError("No image loading backend available.")


def load_exif_data(path):
    """Return EXIF data as an ordered list of (field, value, is_header) tuples.
    Works for JPG, PNG, and NEF (PIL reads TIFF-based metadata from NEF
    without needing to fully decode the RAW image)."""
    from PIL import ExifTags

    # Friendly names for the tags we care about, in display order
    WANTED = {
        # Camera
        "Make":              "Make",
        "Model":             "Model",
        "LensModel":         "Lens",
        "Software":          "Software",
        # Capture settings
        "DateTime":          "Date / Time",
        "ExposureTime":      "Exposure",
        "FNumber":           "Aperture",
        "ISOSpeedRatings":   "ISO",
        "FocalLength":       "Focal Length",
        "FocalLengthIn35mmFilm": "Focal (35 mm equiv)",
        "ExposureBiasValue": "Exp. Bias",
        "ExposureMode":      "Exp. Mode",
        "ExposureProgram":   "Exp. Program",
        "MeteringMode":      "Metering",
        "WhiteBalance":      "White Balance",
        "Flash":             "Flash",
        "SceneCaptureType":  "Scene Type",
        # Image
        "ImageWidth":        "Width",
        "ImageLength":       "Height",
        "Orientation":       "Orientation",
        "ColorSpace":        "Color Space",
        "BitsPerSample":     "Bit Depth",
        # GPS
        "GPSInfo":           "GPS",
    }

    # Human-readable value maps for integer-coded fields
    EXPOSURE_PROGRAMS = {0:"Not defined",1:"Manual",2:"Auto",3:"Aperture priority",
                         4:"Shutter priority",5:"Creative",6:"Action",7:"Portrait",8:"Landscape"}
    METERING_MODES    = {0:"Unknown",1:"Average",2:"Center-weighted",3:"Spot",
                         4:"Multi-spot",5:"Multi-segment",6:"Partial"}
    WHITE_BALANCE     = {0:"Auto",1:"Manual"}
    EXPOSURE_MODES    = {0:"Auto",1:"Manual",2:"Auto bracket"}
    ORIENTATIONS      = {1:"Normal",2:"Flipped H",3:"Rotated 180°",4:"Flipped V",
                         5:"Transposed",6:"Rotated 90° CW",7:"Transverse",8:"Rotated 90° CCW"}
    COLOR_SPACES      = {1:"sRGB",65535:"Uncalibrated"}
    SCENE_TYPES       = {0:"Standard",1:"Landscape",2:"Portrait",3:"Night scene"}

    rows = []  # (field, value, is_header)

    def section(title):
        rows.append((title, "", True))

    def row(label, val):
        rows.append((label, str(val), False))

    # ---- File info (always available) ----
    section("File")
    row("Name", os.path.basename(path))
    try:
        size = os.path.getsize(path)
        row("File Size", f"{size / 1_048_576:.2f} MB" if size >= 1_048_576 else f"{size / 1024:.1f} KB")
    except OSError:
        pass

    # ---- EXIF via PIL ----
    try:
        # PIL can open NEF just for metadata (TIFF-based), even without rawpy
        img = Image.open(path)
        w, h = img.size
        row("Dimensions", f"{w} × {h} px")

        exif = img.getexif()
        if not exif:
            return rows

        # Build a name→value dict for wanted tags
        tag_name_map = {v: k for k, v in ExifTags.TAGS.items()}  # name → id
        found = {}
        for tag_id, value in exif.items():
            tag_name = ExifTags.TAGS.get(tag_id)
            if tag_name and tag_name in WANTED:
                found[tag_name] = value

        # Also check IFD sub-tables (e.g. Exif IFD, GPS IFD)
        try:
            ifd = exif.get_ifd(0x8769)  # ExifIFD
            for tag_id, value in ifd.items():
                tag_name = ExifTags.TAGS.get(tag_id)
                if tag_name and tag_name in WANTED:
                    found[tag_name] = value
        except Exception:
            pass
        try:
            gps_ifd = exif.get_ifd(0x8825)  # GPS IFD
            if gps_ifd:
                found["GPSInfo"] = gps_ifd
        except Exception:
            pass

        if not found:
            return rows

        # ---- Camera section ----
        cam_keys = ["Make", "Model", "LensModel", "Software"]
        if any(k in found for k in cam_keys):
            section("Camera")
            for k in cam_keys:
                if k in found:
                    row(WANTED[k], str(found[k]).strip())

        # ---- Capture section ----
        capture_keys = ["DateTime","ExposureTime","FNumber","ISOSpeedRatings",
                        "FocalLength","FocalLengthIn35mmFilm","ExposureBiasValue",
                        "ExposureMode","ExposureProgram","MeteringMode",
                        "WhiteBalance","Flash","SceneCaptureType"]
        if any(k in found for k in capture_keys):
            section("Capture")
            for k in capture_keys:
                if k not in found:
                    continue
                v = found[k]
                label = WANTED[k]
                if k == "ExposureTime":
                    try:
                        f = float(v)
                        row(label, f"1/{int(1/f)}s" if f < 1 else f"{f}s")
                    except Exception:
                        row(label, str(v))
                elif k == "FNumber":
                    try:
                        row(label, f"f/{float(v):.1f}")
                    except Exception:
                        row(label, str(v))
                elif k in ("FocalLength", "FocalLengthIn35mmFilm"):
                    try:
                        row(label, f"{float(v):.0f} mm")
                    except Exception:
                        row(label, str(v))
                elif k == "ExposureBiasValue":
                    try:
                        row(label, f"{float(v):+.1f} EV")
                    except Exception:
                        row(label, str(v))
                elif k == "ExposureProgram":
                    row(label, EXPOSURE_PROGRAMS.get(int(v), str(v)))
                elif k == "MeteringMode":
                    row(label, METERING_MODES.get(int(v), str(v)))
                elif k == "WhiteBalance":
                    row(label, WHITE_BALANCE.get(int(v), str(v)))
                elif k == "ExposureMode":
                    row(label, EXPOSURE_MODES.get(int(v), str(v)))
                elif k == "Flash":
                    row(label, "On" if int(v) & 0x1 else "Off")
                elif k == "SceneCaptureType":
                    row(label, SCENE_TYPES.get(int(v), str(v)))
                else:
                    row(label, str(v))

        # ---- Image section ----
        img_keys = ["Orientation","ColorSpace","BitsPerSample"]
        if any(k in found for k in img_keys):
            section("Image")
            for k in img_keys:
                if k not in found:
                    continue
                v = found[k]
                label = WANTED[k]
                if k == "Orientation":
                    row(label, ORIENTATIONS.get(int(v), str(v)))
                elif k == "ColorSpace":
                    row(label, COLOR_SPACES.get(int(v), str(v)))
                else:
                    row(label, str(v))

        # ---- GPS section ----
        if "GPSInfo" in found:
            gps = found["GPSInfo"]
            if isinstance(gps, dict) and gps:
                section("GPS")
                try:
                    gps_tags = ExifTags.GPSTAGS
                    lat_ref = gps.get(1, "")
                    lat     = gps.get(2)
                    lon_ref = gps.get(3, "")
                    lon     = gps.get(4)
                    if lat and lon:
                        def dms(t):
                            return float(t[0]) + float(t[1])/60 + float(t[2])/3600
                        la = dms(lat) * (-1 if lat_ref == "S" else 1)
                        lo = dms(lon) * (-1 if lon_ref == "W" else 1)
                        row("Latitude",  f"{la:.6f}°")
                        row("Longitude", f"{lo:.6f}°")
                    alt = gps.get(6)
                    if alt is not None:
                        row("Altitude", f"{float(alt):.1f} m")
                except Exception:
                    pass
    except Exception:
        pass

    return rows


def _load_nef_thumbnail(path, max_size):
    """Fast NEF thumbnail using the camera-embedded JPEG (milliseconds vs. seconds).
    Falls back to half-size postprocess if no embedded thumb is available."""
    import io
    with rawpy.imread(path) as raw:
        try:
            thumb = raw.extract_thumb()
            if thumb.format == rawpy.ThumbFormat.JPEG:
                img = Image.open(io.BytesIO(thumb.data))
                img.load()
            elif thumb.format == rawpy.ThumbFormat.BITMAP:
                img = Image.fromarray(thumb.data)
            else:
                raise ValueError("Unknown thumb format")
        except rawpy.LibRawNoThumbnailError:
            # No embedded thumbnail — half-size decode as fallback
            rgb = raw.postprocess(use_camera_wb=True, half_size=True, output_bps=8)
            img = Image.fromarray(rgb)
    img.thumbnail((max_size, max_size), Image.LANCZOS)
    return img


def get_folder_images(folder):
    """Return sorted list of supported image paths in a folder."""
    try:
        entries = os.listdir(folder)
    except PermissionError:
        return []
    images = []
    for name in entries:
        if os.path.splitext(name)[1].lower() in SUPPORTED_EXTENSIONS:
            images.append(os.path.join(folder, name))
    images.sort(key=lambda p: p.lower())
    return images


class ThumbnailLoader:
    """Background loader for sidebar thumbnails."""
    THUMB_SIZE = 96

    def __init__(self, on_ready):
        self._on_ready = on_ready  # callback(path, pixbuf)
        self._queue = []
        self._lock = threading.Lock()
        self._thread = None
        self._stop = False

    def enqueue(self, paths):
        with self._lock:
            self._queue = list(paths)
            self._stop = False
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._worker, daemon=True)
            self._thread.start()

    def cancel(self):
        with self._lock:
            self._stop = True
            self._queue.clear()

    def _worker(self):
        while True:
            with self._lock:
                if self._stop or not self._queue:
                    return
                path = self._queue.pop(0)
            try:
                ext = os.path.splitext(path)[1].lower()
                if ext in RAW_EXTENSIONS and RAWPY_AVAILABLE:
                    img = _load_nef_thumbnail(path, self.THUMB_SIZE)
                else:
                    img = load_image_file(path)
                    img.thumbnail((self.THUMB_SIZE, self.THUMB_SIZE), Image.LANCZOS)
                pixbuf = pil_image_to_pixbuf(img)
                GLib.idle_add(self._on_ready, path, pixbuf)
            except Exception:
                pass


class ImageViewer(Gtk.ApplicationWindow):

    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME)
        self.set_default_size(1200, 800)
        self.set_icon_name("image-viewer")

        # State
        self._current_path = None
        self._current_pil = None
        self._folder_images = []
        self._folder_index = -1
        self._zoom = 1.0
        self._fit_mode = True
        self._drag_start = None
        self._scroll_origin = None
        self._fullscreen = False
        self._loading = False

        self._thumb_loader = ThumbnailLoader(self._on_thumb_ready)
        self._thumb_path_to_row = {}

        self._build_ui()
        self._connect_signals()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        # Overall layout: header bar + content pane
        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        header.set_title(APP_NAME)
        self.set_titlebar(header)

        # Toolbar buttons in header
        btn_open = Gtk.Button.new_from_icon_name("document-open-symbolic", Gtk.IconSize.BUTTON)
        btn_open.set_tooltip_text("Open file (Ctrl+O)")
        btn_open.connect("clicked", self._on_open_clicked)
        header.pack_start(btn_open)

        self._btn_prev = Gtk.Button.new_from_icon_name("go-previous-symbolic", Gtk.IconSize.BUTTON)
        self._btn_prev.set_tooltip_text("Previous image (←)")
        self._btn_prev.connect("clicked", lambda *_: self._navigate(-1))
        header.pack_start(self._btn_prev)

        self._btn_next = Gtk.Button.new_from_icon_name("go-next-symbolic", Gtk.IconSize.BUTTON)
        self._btn_next.set_tooltip_text("Next image (→)")
        self._btn_next.connect("clicked", lambda *_: self._navigate(1))
        header.pack_start(self._btn_next)

        # Zoom controls
        btn_zoom_out = Gtk.Button.new_from_icon_name("zoom-out-symbolic", Gtk.IconSize.BUTTON)
        btn_zoom_out.set_tooltip_text("Zoom out (-)")
        btn_zoom_out.connect("clicked", lambda *_: self._adjust_zoom(1 / 1.25))
        header.pack_end(btn_zoom_out)

        btn_zoom_in = Gtk.Button.new_from_icon_name("zoom-in-symbolic", Gtk.IconSize.BUTTON)
        btn_zoom_in.set_tooltip_text("Zoom in (+)")
        btn_zoom_in.connect("clicked", lambda *_: self._adjust_zoom(1.25))
        header.pack_end(btn_zoom_in)

        btn_zoom_fit = Gtk.Button.new_from_icon_name("zoom-fit-best-symbolic", Gtk.IconSize.BUTTON)
        btn_zoom_fit.set_tooltip_text("Fit to window (F)")
        btn_zoom_fit.connect("clicked", lambda *_: self._zoom_fit())
        header.pack_end(btn_zoom_fit)

        btn_zoom_100 = Gtk.Button.new_from_icon_name("zoom-original-symbolic", Gtk.IconSize.BUTTON)
        btn_zoom_100.set_tooltip_text("Actual size (1)")
        btn_zoom_100.connect("clicked", lambda *_: self._zoom_actual())
        header.pack_end(btn_zoom_100)

        btn_fs = Gtk.Button.new_from_icon_name("view-fullscreen-symbolic", Gtk.IconSize.BUTTON)
        btn_fs.set_tooltip_text("Fullscreen (F11)")
        btn_fs.connect("clicked", lambda *_: self._toggle_fullscreen())
        header.pack_end(btn_fs)

        btn_exif = Gtk.ToggleButton()
        btn_exif.set_image(Gtk.Image.new_from_icon_name("dialog-information-symbolic", Gtk.IconSize.BUTTON))
        btn_exif.set_tooltip_text("Toggle EXIF panel (E)")
        btn_exif.set_active(True)
        btn_exif.connect("toggled", self._on_exif_toggle)
        self._btn_exif = btn_exif
        header.pack_end(btn_exif)

        # Main horizontal pane: sidebar | image area
        self._paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.add(self._paned)

        # --- Sidebar ---
        sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sidebar_label = Gtk.Label(label="Files")
        sidebar_label.set_halign(Gtk.Align.START)
        sidebar_label.get_style_context().add_class("caption")

        scroll_side = Gtk.ScrolledWindow()
        scroll_side.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll_side.set_min_content_width(170)

        # ListStore: path (str), thumb (Pixbuf), name (str)
        self._store = Gtk.ListStore(str, GdkPixbuf.Pixbuf, str)

        # TreeView virtualizes rows — only visible rows are rendered,
        # so scrolling stays fast even with hundreds of files.
        self._tree_view = Gtk.TreeView(model=self._store)
        self._tree_view.set_headers_visible(False)
        self._tree_view.set_activate_on_single_click(True)

        col = Gtk.TreeViewColumn()
        pix_cell = Gtk.CellRendererPixbuf()
        pix_cell.set_property("xpad", 3)
        pix_cell.set_property("ypad", 3)
        txt_cell = Gtk.CellRendererText()
        txt_cell.set_property("ellipsize", 3)   # PANGO_ELLIPSIZE_END
        txt_cell.set_property("width-chars", 12)
        col.pack_start(pix_cell, False)
        col.add_attribute(pix_cell, "pixbuf", 1)
        col.pack_start(txt_cell, True)
        col.add_attribute(txt_cell, "text", 2)
        self._tree_view.append_column(col)
        self._tree_view.connect("row-activated", self._on_thumb_activated)

        scroll_side.add(self._tree_view)
        sidebar_box.pack_start(sidebar_label, False, False, 4)
        sidebar_box.pack_start(scroll_side, True, True, 0)
        self._paned.pack1(sidebar_box, False, False)
        self._paned.set_position(170)

        # --- Image area ---
        image_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # Scrolled window holding the image
        self._scroll = Gtk.ScrolledWindow()
        self._scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)

        # EventBox captures mouse events on the image
        self._event_box = Gtk.EventBox()
        self._event_box.add_events(
            Gdk.EventMask.SCROLL_MASK
            | Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.SMOOTH_SCROLL_MASK
        )
        self._event_box.connect("scroll-event", self._on_scroll)
        self._event_box.connect("button-press-event", self._on_button_press)
        self._event_box.connect("button-release-event", self._on_button_release)
        self._event_box.connect("motion-notify-event", self._on_motion)

        self._image_widget = Gtk.Image()
        self._image_widget.set_halign(Gtk.Align.CENTER)
        self._image_widget.set_valign(Gtk.Align.CENTER)
        self._event_box.add(self._image_widget)
        self._scroll.add(self._event_box)

        # Status bar
        self._statusbar = Gtk.Label(label="Open an image to get started  (Ctrl+O)")
        self._statusbar.set_halign(Gtk.Align.START)
        self._statusbar.set_margin_start(8)
        self._statusbar.set_margin_bottom(4)
        self._statusbar.set_ellipsize(3)  # PANGO_ELLIPSIZE_END

        # Loading spinner overlay
        overlay = Gtk.Overlay()
        overlay.add(self._scroll)

        spinner_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        spinner_box.set_halign(Gtk.Align.CENTER)
        spinner_box.set_valign(Gtk.Align.CENTER)
        self._spinner = Gtk.Spinner()
        self._spinner.set_size_request(48, 48)
        self._spinner_label = Gtk.Label(label="Loading…")
        spinner_box.pack_start(self._spinner, False, False, 0)
        spinner_box.pack_start(self._spinner_label, False, False, 0)
        self._spinner_box = spinner_box
        overlay.add_overlay(spinner_box)

        image_box.pack_start(overlay, True, True, 0)
        image_box.pack_start(Gtk.Separator(), False, False, 0)
        image_box.pack_start(self._statusbar, False, False, 0)

        # --- EXIF panel (right side) ---
        exif_outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        exif_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        exif_title = Gtk.Label(label="EXIF")
        exif_title.set_halign(Gtk.Align.START)
        exif_title.set_margin_start(6)
        exif_title.set_margin_top(4)
        exif_title.set_margin_bottom(4)
        exif_header.pack_start(exif_title, True, True, 0)
        exif_outer.pack_start(exif_header, False, False, 0)
        exif_outer.pack_start(Gtk.Separator(), False, False, 0)

        scroll_exif = Gtk.ScrolledWindow()
        scroll_exif.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll_exif.set_min_content_width(200)

        # Store: field (str), value (str), is_header (bool for bold)
        self._exif_store = Gtk.ListStore(str, str, bool)
        self._exif_view = Gtk.TreeView(model=self._exif_store)
        self._exif_view.set_headers_visible(False)
        self._exif_view.set_can_focus(False)

        # Field column
        field_cell = Gtk.CellRendererText()
        field_cell.set_property("xpad", 6)
        field_cell.set_property("ypad", 2)
        field_col = Gtk.TreeViewColumn("Field", field_cell, text=0, weight_set=2)
        field_col.add_attribute(field_cell, "weight", 2)
        self._exif_view.append_column(field_col)

        # Value column
        val_cell = Gtk.CellRendererText()
        val_cell.set_property("xpad", 6)
        val_cell.set_property("ypad", 2)
        val_cell.set_property("ellipsize", 3)  # PANGO_ELLIPSIZE_END
        val_col = Gtk.TreeViewColumn("Value", val_cell, text=1)
        val_col.set_expand(True)
        self._exif_view.append_column(val_col)

        scroll_exif.add(self._exif_view)
        exif_outer.pack_start(scroll_exif, True, True, 0)

        # Right pane splits image | EXIF
        self._right_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self._right_paned.pack1(image_box, True, True)
        self._right_paned.pack2(exif_outer, False, False)

        self._paned.pack2(self._right_paned, True, True)

        # Welcome screen placeholder
        self._show_placeholder()
        self.show_all()
        self._spinner_box.hide()

    def _show_placeholder(self):
        placeholder = Gtk.Image.new_from_icon_name("image-x-generic-symbolic", Gtk.IconSize.DIALOG)
        self._image_widget.set_from_icon_name("image-x-generic-symbolic", Gtk.IconSize.DIALOG)

    # ------------------------------------------------------------------
    # Signal connections
    # ------------------------------------------------------------------

    def _connect_signals(self):
        self.connect("key-press-event", self._on_key_press)
        self.connect("configure-event", self._on_configure)
        # Drag-and-drop
        self.drag_dest_set(Gtk.DestDefaults.ALL, [], Gdk.DragAction.COPY)
        self.drag_dest_add_uri_targets()
        self.connect("drag-data-received", self._on_drag_drop)

    # ------------------------------------------------------------------
    # File opening
    # ------------------------------------------------------------------

    def _on_open_clicked(self, *_):
        dialog = Gtk.FileChooserDialog(
            title="Open Image",
            parent=self,
            action=Gtk.FileChooserAction.OPEN,
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OPEN, Gtk.ResponseType.OK,
        )
        dialog.set_select_multiple(False)

        flt_all = Gtk.FileFilter()
        flt_all.set_name("All supported images")
        for ext in SUPPORTED_EXTENSIONS:
            flt_all.add_pattern(f"*{ext}")
            flt_all.add_pattern(f"*{ext.upper()}")
        dialog.add_filter(flt_all)

        flt_raw = Gtk.FileFilter()
        flt_raw.set_name("RAW images (NEF, NRW)")
        for ext in (".nef", ".nrw", ".raw"):
            flt_raw.add_pattern(f"*{ext}")
            flt_raw.add_pattern(f"*{ext.upper()}")
        dialog.add_filter(flt_raw)

        flt_jpg = Gtk.FileFilter()
        flt_jpg.set_name("JPEG images")
        flt_jpg.add_pattern("*.jpg")
        flt_jpg.add_pattern("*.jpeg")
        flt_jpg.add_pattern("*.JPG")
        flt_jpg.add_pattern("*.JPEG")
        dialog.add_filter(flt_jpg)

        flt_png = Gtk.FileFilter()
        flt_png.set_name("PNG images")
        flt_png.add_pattern("*.png")
        flt_png.add_pattern("*.PNG")
        dialog.add_filter(flt_png)

        response = dialog.run()
        path = dialog.get_filename() if response == Gtk.ResponseType.OK else None
        dialog.destroy()

        if path:
            self.open_file(path)

    def _on_drag_drop(self, widget, ctx, x, y, data, info, time):
        uris = data.get_uris()
        if uris:
            path = uris[0]
            if path.startswith("file://"):
                path = path[7:]
            path = path.rstrip()
            self.open_file(path)

    def open_file(self, path):
        if self._loading:
            return
        path = os.path.abspath(path)
        ext = os.path.splitext(path)[1].lower()
        if ext not in SUPPORTED_EXTENSIONS:
            self._show_error(f"Unsupported file type: {ext}\n\nSupported formats: NEF, NRW, JPG, PNG")
            return
        if ext in RAW_EXTENSIONS and not RAWPY_AVAILABLE:
            msg = (
                "rawpy is required to open NEF/RAW files but could not be loaded.\n\n"
                f"Error: {RAWPY_IMPORT_ERROR}\n\n"
                "Re-run the installer to repair the Python environment:\n"
                "  sudo ./install.sh"
            )
            self._show_error(msg)
            return

        folder = os.path.dirname(path)
        self._folder_images = get_folder_images(folder)
        try:
            self._folder_index = self._folder_images.index(path)
        except ValueError:
            self._folder_index = 0

        self._load_image_async(path)
        self._populate_sidebar(self._folder_images, path)

    def _populate_sidebar(self, images, selected_path):
        self._store.clear()
        self._thumb_path_to_row.clear()
        placeholder = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8,
                                            ThumbnailLoader.THUMB_SIZE,
                                            ThumbnailLoader.THUMB_SIZE)
        placeholder.fill(0x555555FF)

        for path in images:
            name = os.path.basename(path)
            if len(name) > 14:
                name = name[:11] + "…"
            row_iter = self._store.append([path, placeholder, name])
            self._thumb_path_to_row[path] = Gtk.TreeRowReference(
                self._store, self._store.get_path(row_iter)
            )

        self._thumb_loader.cancel()
        self._thumb_loader.enqueue(images)
        self._sidebar_select(selected_path)

    def _on_thumb_ready(self, path, pixbuf):
        ref = self._thumb_path_to_row.get(path)
        if ref and ref.valid():
            tree_path = ref.get_path()
            it = self._store.get_iter(tree_path)
            self._store.set_value(it, 1, pixbuf)
        return False  # remove from idle

    def _on_thumb_activated(self, tree_view, tree_path, column=None):
        it = self._store.get_iter(tree_path)
        path = self._store.get_value(it, 0)
        if path != self._current_path:
            try:
                self._folder_index = self._folder_images.index(path)
            except ValueError:
                pass
            self._load_image_async(path)

    def _sidebar_select(self, path):
        ref = self._thumb_path_to_row.get(path)
        if ref and ref.valid():
            tree_path = ref.get_path()
            self._tree_view.get_selection().select_path(tree_path)
            self._tree_view.scroll_to_cell(tree_path, None, True, 0.5, 0.0)

    # ------------------------------------------------------------------
    # Image loading (async)
    # ------------------------------------------------------------------

    def _load_image_async(self, path):
        self._loading = True
        self._spinner.start()
        self._spinner_box.show()
        self._update_nav_buttons()

        t = threading.Thread(target=self._load_worker, args=(path,), daemon=True)
        t.start()

    def _load_worker(self, path):
        try:
            img = load_image_file(path)
            exif = load_exif_data(path)
            GLib.idle_add(self._on_image_loaded, path, img, exif, None)
        except Exception as e:
            GLib.idle_add(self._on_image_loaded, path, None, [], str(e))

    def _on_image_loaded(self, path, img, exif, error):
        self._loading = False
        self._spinner.stop()
        self._spinner_box.hide()

        if error:
            self._show_error(f"Failed to load {os.path.basename(path)}:\n{error}")
            return

        self._current_path = path
        self._current_pil = img
        self._zoom = 1.0
        self._fit_mode = True
        self._display_image()
        self._update_title()
        self._update_status()
        self._update_nav_buttons()
        self._sidebar_select(path)
        self._populate_exif(exif)
        gc.collect()
        return False

    # ------------------------------------------------------------------
    # Image display & zoom
    # ------------------------------------------------------------------

    def _display_image(self):
        if self._current_pil is None:
            return
        if self._fit_mode:
            self._render_fit()
        else:
            self._render_zoom()

    def _render_fit(self):
        alloc = self._scroll.get_allocation()
        avail_w = max(alloc.width - 20, 100)
        avail_h = max(alloc.height - 20, 100)
        img_w, img_h = self._current_pil.size
        scale = min(avail_w / img_w, avail_h / img_h, 1.0)
        self._zoom = scale
        self._render_at_zoom(scale)

    def _render_zoom(self):
        self._render_at_zoom(self._zoom)

    def _render_at_zoom(self, scale):
        img = self._current_pil
        w = max(1, int(img.width * scale))
        h = max(1, int(img.height * scale))
        if scale < 1.0:
            resample = Image.LANCZOS
        else:
            resample = Image.NEAREST
        resized = img.resize((w, h), resample)
        pixbuf = pil_image_to_pixbuf(resized)
        self._image_widget.set_from_pixbuf(pixbuf)

    def _adjust_zoom(self, factor):
        self._fit_mode = False
        self._zoom = max(0.05, min(self._zoom * factor, 20.0))
        self._render_zoom()
        self._update_status()

    def _zoom_fit(self):
        self._fit_mode = True
        self._render_fit()
        self._update_status()

    def _zoom_actual(self):
        self._fit_mode = False
        self._zoom = 1.0
        self._render_zoom()
        self._update_status()

    def _on_configure(self, widget, event):
        if self._fit_mode and self._current_pil:
            self._render_fit()
        return False

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _navigate(self, delta):
        if not self._folder_images:
            return
        new_idx = self._folder_index + delta
        if 0 <= new_idx < len(self._folder_images):
            self._folder_index = new_idx
            self._load_image_async(self._folder_images[self._folder_index])

    def _update_nav_buttons(self):
        self._btn_prev.set_sensitive(self._folder_index > 0)
        self._btn_next.set_sensitive(
            len(self._folder_images) > 0
            and self._folder_index < len(self._folder_images) - 1
        )

    # ------------------------------------------------------------------
    # Mouse / scroll events
    # ------------------------------------------------------------------

    def _on_scroll(self, widget, event):
        if event.state & Gdk.ModifierType.CONTROL_MASK:
            if event.direction == Gdk.ScrollDirection.UP or (
                event.direction == Gdk.ScrollDirection.SMOOTH and event.delta_y < 0
            ):
                self._adjust_zoom(1.1)
            elif event.direction == Gdk.ScrollDirection.DOWN or (
                event.direction == Gdk.ScrollDirection.SMOOTH and event.delta_y > 0
            ):
                self._adjust_zoom(1 / 1.1)
            return True
        return False

    def _on_button_press(self, widget, event):
        if event.button == 1:
            self._drag_start = (event.x_root, event.y_root)
            hadj = self._scroll.get_hadjustment()
            vadj = self._scroll.get_vadjustment()
            self._scroll_origin = (hadj.get_value(), vadj.get_value())
            widget.get_window().set_cursor(
                Gdk.Cursor.new_from_name(widget.get_display(), "grabbing")
            )
        return False

    def _on_button_release(self, widget, event):
        if event.button == 1:
            self._drag_start = None
            self._scroll_origin = None
            widget.get_window().set_cursor(None)
        return False

    def _on_motion(self, widget, event):
        if self._drag_start and self._scroll_origin:
            dx = self._drag_start[0] - event.x_root
            dy = self._drag_start[1] - event.y_root
            hadj = self._scroll.get_hadjustment()
            vadj = self._scroll.get_vadjustment()
            hadj.set_value(self._scroll_origin[0] + dx)
            vadj.set_value(self._scroll_origin[1] + dy)
        return False

    # ------------------------------------------------------------------
    # Keyboard shortcuts
    # ------------------------------------------------------------------

    def _on_key_press(self, widget, event):
        key = event.keyval
        ctrl = event.state & Gdk.ModifierType.CONTROL_MASK

        if ctrl and key == Gdk.KEY_o:
            self._on_open_clicked()
        elif key in (Gdk.KEY_Right, Gdk.KEY_n):
            self._navigate(1)
        elif key in (Gdk.KEY_Left, Gdk.KEY_p):
            self._navigate(-1)
        elif key == Gdk.KEY_plus or key == Gdk.KEY_equal:
            self._adjust_zoom(1.25)
        elif key == Gdk.KEY_minus:
            self._adjust_zoom(1 / 1.25)
        elif key == Gdk.KEY_0 or key == Gdk.KEY_1:
            self._zoom_actual()
        elif key == Gdk.KEY_f or key == Gdk.KEY_F:
            self._zoom_fit()
        elif key == Gdk.KEY_F11:
            self._toggle_fullscreen()
        elif key == Gdk.KEY_Escape and self._fullscreen:
            self._toggle_fullscreen()
        elif key == Gdk.KEY_e or key == Gdk.KEY_E:
            self._btn_exif.set_active(not self._btn_exif.get_active())
        elif key == Gdk.KEY_q and ctrl:
            self.get_application().quit()
        return False

    # ------------------------------------------------------------------
    # Fullscreen
    # ------------------------------------------------------------------

    def _toggle_fullscreen(self):
        if self._fullscreen:
            self.unfullscreen()
            self._fullscreen = False
        else:
            self.fullscreen()
            self._fullscreen = True

    # ------------------------------------------------------------------
    # EXIF panel
    # ------------------------------------------------------------------

    def _populate_exif(self, rows):
        """Fill the EXIF TreeView. rows = [(field, value, is_header), ...]"""
        self._exif_store.clear()
        BOLD   = 700  # Pango.Weight.BOLD
        NORMAL = 400  # Pango.Weight.NORMAL
        for field, value, is_header in rows:
            self._exif_store.append([field, value, BOLD if is_header else NORMAL])

    def _on_exif_toggle(self, btn):
        _, exif_widget = self._right_paned.get_children()
        if btn.get_active():
            exif_widget.show()
        else:
            exif_widget.hide()

    # ------------------------------------------------------------------
    # UI helpers
    # ------------------------------------------------------------------

    def _update_title(self):
        if self._current_path:
            name = os.path.basename(self._current_path)
            n = self._folder_index + 1
            total = len(self._folder_images)
            self.set_title(f"{name} — {APP_NAME}")
            if hasattr(self, "_headerbar"):
                pass  # titlebar already set via set_titlebar

    def _update_status(self):
        if self._current_pil is None:
            self._statusbar.set_text("Open an image to get started  (Ctrl+O)")
            return
        name = os.path.basename(self._current_path)
        w, h = self._current_pil.size
        n = self._folder_index + 1
        total = len(self._folder_images)
        zoom_pct = int(self._zoom * 100)
        mode = "fit" if self._fit_mode else f"{zoom_pct}%"
        self._statusbar.set_text(
            f"{name}   {w}×{h}px   Zoom: {mode}   [{n}/{total}]"
        )

    def _show_error(self, msg):
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK,
            text="Error",
        )
        dialog.format_secondary_text(msg)
        dialog.run()
        dialog.destroy()


class RawhideApp(Gtk.Application):

    def __init__(self):
        super().__init__(
            application_id="org.rawhide.imageviewer",
            flags=Gio.ApplicationFlags.HANDLES_OPEN,
        )
        self._window = None

    def do_activate(self):
        if not self._window:
            self._window = ImageViewer(self)
            self._window.present()

    def do_open(self, files, n_files, hint):
        self.do_activate()
        if files:
            path = files[0].get_path()
            if path:
                self._window.open_file(path)

    def do_startup(self):
        Gtk.Application.do_startup(self)
        self._setup_actions()

    def _setup_actions(self):
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Ctrl>Q"])


def main():
    app = RawhideApp()
    sys.exit(app.run(sys.argv))


if __name__ == "__main__":
    main()
