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
        from PIL import ImageOps
        img = Image.open(path).copy()
        return ImageOps.exif_transpose(img)
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
                from PIL import ImageOps
                img = Image.open(io.BytesIO(thumb.data))
                img.load()
                img = ImageOps.exif_transpose(img)
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
        self._fs_loaded_dirs = set()

        # Edit state — changes are cached here until "Save As"
        self._edit_brightness = 0    # -100 … +100
        self._crop_rect = None        # (x1, y1, x2, y2) in original image coords
        self._crop_mode = False
        self._crop_drag = None        # active drag descriptor
        self._display_pixbuf = None   # currently rendered pixbuf
        self._img_draw_x = 0          # image origin inside DrawingArea
        self._img_draw_y = 0

        self._build_ui()
        self._connect_signals()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        # ── Header bar ───────────────────────────────────────────────
        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        header.set_title(APP_NAME)
        self.set_titlebar(header)

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

        btn_fullscreen = Gtk.Button.new_from_icon_name("view-fullscreen-symbolic", Gtk.IconSize.BUTTON)
        btn_fullscreen.set_tooltip_text("Fullscreen (F11)")
        btn_fullscreen.connect("clicked", lambda *_: self._toggle_fullscreen())
        header.pack_end(btn_fullscreen)

        btn_exif = Gtk.ToggleButton()
        btn_exif.set_image(Gtk.Image.new_from_icon_name("dialog-information-symbolic", Gtk.IconSize.BUTTON))
        btn_exif.set_tooltip_text("Toggle EXIF panel (E)")
        btn_exif.set_active(True)
        btn_exif.connect("toggled", self._on_exif_toggle)
        self._btn_exif = btn_exif
        header.pack_end(btn_exif)

        # ── Root paned: filesystem tree (left) | rest (right) ────────
        self._paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.add(self._paned)

        # ── LEFT: Filesystem tree ─────────────────────────────────────
        fs_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        fs_label = Gtk.Label(label="Files")
        fs_label.set_halign(Gtk.Align.START)
        fs_label.set_margin_start(6)
        fs_label.set_margin_top(4)
        fs_label.set_margin_bottom(2)

        scroll_fs = Gtk.ScrolledWindow()
        scroll_fs.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll_fs.set_min_content_width(200)

        # TreeStore columns: display_name, full_path, is_dir
        self._fs_store = Gtk.TreeStore(str, str, bool)
        self._fs_tree = Gtk.TreeView(model=self._fs_store)
        self._fs_tree.set_headers_visible(False)
        self._fs_tree.set_activate_on_single_click(True)

        fs_col = Gtk.TreeViewColumn()
        fs_icon_cell = Gtk.CellRendererPixbuf()
        fs_icon_cell.set_property("xpad", 2)
        fs_text_cell = Gtk.CellRendererText()
        fs_text_cell.set_property("ellipsize", 3)
        fs_col.pack_start(fs_icon_cell, False)
        fs_col.set_cell_data_func(fs_icon_cell, self._fs_icon_func)
        fs_col.pack_start(fs_text_cell, True)
        fs_col.add_attribute(fs_text_cell, "text", 0)
        self._fs_tree.append_column(fs_col)

        self._fs_tree.connect("row-activated", self._on_fs_activated)
        self._fs_tree.connect("test-expand-row", self._on_fs_expand)

        scroll_fs.add(self._fs_tree)
        fs_box.pack_start(fs_label, False, False, 0)
        fs_box.pack_start(Gtk.Separator(), False, False, 0)
        fs_box.pack_start(scroll_fs, True, True, 0)
        self._paned.pack1(fs_box, False, False)
        self._paned.set_position(220)

        # Populate filesystem tree roots
        self._fs_populate_roots()

        # ── RIGHT: vertical box = [content paned] + [thumb strip] ────
        right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # ── Content paned: image view | EXIF panel ───────────────────
        self._content_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)

        # Image area
        image_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        self._scroll = Gtk.ScrolledWindow()
        self._scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)

        # DrawingArea renders both the image and the crop overlay in one pass
        self._draw_area = Gtk.DrawingArea()
        self._draw_area.add_events(
            Gdk.EventMask.SCROLL_MASK
            | Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
            | Gdk.EventMask.SMOOTH_SCROLL_MASK
        )
        self._draw_area.connect("draw", self._on_draw)
        self._draw_area.connect("scroll-event", self._on_scroll)
        self._draw_area.connect("button-press-event", self._on_button_press)
        self._draw_area.connect("button-release-event", self._on_button_release)
        self._draw_area.connect("motion-notify-event", self._on_motion)
        self._scroll.add(self._draw_area)

        self._statusbar = Gtk.Label(label="Open an image to get started  (Ctrl+O)")
        self._statusbar.set_halign(Gtk.Align.START)
        self._statusbar.set_margin_start(8)
        self._statusbar.set_margin_bottom(4)
        self._statusbar.set_ellipsize(3)

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

        # ── Edit toolbar ─────────────────────────────────────────────
        edit_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        edit_bar.set_margin_start(6)
        edit_bar.set_margin_end(6)
        edit_bar.set_margin_top(3)
        edit_bar.set_margin_bottom(3)

        self._btn_crop = Gtk.ToggleButton(label="✂  Crop")
        self._btn_crop.set_tooltip_text("Toggle crop tool (C)")
        self._btn_crop.connect("toggled", self._on_crop_toggled)
        edit_bar.pack_start(self._btn_crop, False, False, 0)

        btn_reset = Gtk.Button(label="↺  Reset")
        btn_reset.set_tooltip_text("Reset all edits")
        btn_reset.connect("clicked", lambda *_: self._reset_edits())
        edit_bar.pack_start(btn_reset, False, False, 0)

        edit_bar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 4)

        edit_bar.pack_start(Gtk.Label(label="☀  Brightness:"), False, False, 0)
        self._brightness_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, -100, 100, 1)
        self._brightness_scale.set_value(0)
        self._brightness_scale.set_draw_value(True)
        self._brightness_scale.set_size_request(180, -1)
        self._brightness_scale.connect("value-changed", self._on_brightness_changed)
        edit_bar.pack_start(self._brightness_scale, False, False, 0)

        edit_bar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 4)

        btn_save_as = Gtk.Button(label="💾  Save As…")
        btn_save_as.set_tooltip_text("Save edited image to a new file (Ctrl+Shift+S)")
        btn_save_as.connect("clicked", lambda *_: self._on_save_as())
        edit_bar.pack_end(btn_save_as, False, False, 0)

        image_box.pack_start(edit_bar, False, False, 0)
        image_box.pack_start(Gtk.Separator(), False, False, 0)
        image_box.pack_start(self._statusbar, False, False, 0)

        # EXIF panel
        exif_outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        exif_title = Gtk.Label(label="EXIF")
        exif_title.set_halign(Gtk.Align.START)
        exif_title.set_margin_start(6)
        exif_title.set_margin_top(4)
        exif_title.set_margin_bottom(4)
        exif_outer.pack_start(exif_title, False, False, 0)
        exif_outer.pack_start(Gtk.Separator(), False, False, 0)

        scroll_exif = Gtk.ScrolledWindow()
        scroll_exif.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll_exif.set_min_content_width(420)

        self._exif_store = Gtk.ListStore(str, str, bool)
        self._exif_view = Gtk.TreeView(model=self._exif_store)
        self._exif_view.set_headers_visible(False)
        self._exif_view.set_can_focus(False)
        field_cell = Gtk.CellRendererText()
        field_cell.set_property("xpad", 6)
        field_cell.set_property("ypad", 2)
        field_col = Gtk.TreeViewColumn("Field", field_cell, text=0, weight_set=2)
        field_col.add_attribute(field_cell, "weight", 2)
        self._exif_view.append_column(field_col)
        val_cell = Gtk.CellRendererText()
        val_cell.set_property("xpad", 6)
        val_cell.set_property("ypad", 2)
        val_cell.set_property("ellipsize", 3)
        val_col = Gtk.TreeViewColumn("Value", val_cell, text=1)
        val_col.set_expand(True)
        self._exif_view.append_column(val_col)
        scroll_exif.add(self._exif_view)
        exif_outer.pack_start(scroll_exif, True, True, 0)

        self._content_paned.pack1(image_box, True, True)
        self._content_paned.pack2(exif_outer, False, False)
        right_box.pack_start(self._content_paned, True, True, 0)

        # ── BOTTOM: Thumbnail strip ───────────────────────────────────
        right_box.pack_start(Gtk.Separator(), False, False, 0)

        thumb_strip_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        thumb_label = Gtk.Label(label="Preview")
        thumb_label.set_halign(Gtk.Align.START)
        thumb_label.set_margin_start(6)
        thumb_label.set_margin_top(2)
        thumb_strip_box.pack_start(thumb_label, False, False, 0)

        scroll_thumb = Gtk.ScrolledWindow()
        scroll_thumb.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)

        # ListStore: path (str), thumb (Pixbuf), name (str)
        self._store = Gtk.ListStore(str, GdkPixbuf.Pixbuf, str)
        self._thumb_view = Gtk.IconView(model=self._store)
        self._thumb_view.set_pixbuf_column(1)
        self._thumb_view.set_text_column(2)
        self._thumb_view.set_item_width(ThumbnailLoader.THUMB_SIZE + 10)
        self._thumb_view.set_row_spacing(2)
        self._thumb_view.set_column_spacing(2)
        self._thumb_view.set_columns(9999)   # force single row, scroll horizontally
        self._thumb_view.set_selection_mode(Gtk.SelectionMode.SINGLE)
        # Fix height to exactly one row so the strip never grows vertically
        self._thumb_view.set_size_request(-1, ThumbnailLoader.THUMB_SIZE + 36)
        self._thumb_view.connect("item-activated", self._on_thumb_activated)

        scroll_thumb.add(self._thumb_view)
        thumb_strip_box.pack_start(scroll_thumb, False, False, 0)
        right_box.pack_start(thumb_strip_box, False, False, 0)

        self._paned.pack2(right_box, True, True)

        # Welcome screen placeholder
        self._show_placeholder()
        self.show_all()
        self._spinner_box.hide()

    def _show_placeholder(self):
        self._display_pixbuf = None
        self._draw_area.queue_draw()

    # ------------------------------------------------------------------
    # Filesystem tree
    # ------------------------------------------------------------------

    def _fs_populate_roots(self):
        """Seed the tree with the home directory, expanded one level."""
        home = os.path.expanduser("~")
        name = os.path.basename(home) or "Home"
        it = self._fs_store.append(None, [name, home, True])
        self._fs_store.append(it, ["", "", False])   # dummy child
        # Pre-load and expand one level so the tree is useful immediately
        self._fs_load_dir(it, home)
        GLib.idle_add(self._fs_tree.expand_row, self._fs_store.get_path(it), False)

    def _fs_icon_func(self, col, cell, model, it, data):
        is_dir = model.get_value(it, 2)
        name   = model.get_value(it, 1)
        ext    = os.path.splitext(name)[1].lower()
        if is_dir:
            icon = "folder-symbolic"
        elif ext in SUPPORTED_EXTENSIONS:
            icon = "image-x-generic-symbolic"
        else:
            icon = "text-x-generic-symbolic"
        cell.set_property("icon-name", icon)

    def _fs_load_dir(self, parent_iter, dir_path):
        """Populate children of dir_path under parent_iter."""
        if dir_path in self._fs_loaded_dirs:
            return
        self._fs_loaded_dirs.add(dir_path)

        # Remove dummy placeholder
        child = self._fs_store.iter_children(parent_iter)
        while child:
            next_child = self._fs_store.iter_next(child)
            self._fs_store.remove(child)
            child = next_child

        try:
            entries = sorted(os.scandir(dir_path), key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
        except OSError:
            return

        for entry in entries:
            if entry.name.startswith("."):
                continue
            if entry.is_dir(follow_symlinks=False):
                it = self._fs_store.append(parent_iter, [entry.name, entry.path, True])
                self._fs_store.append(it, ["", "", False])   # dummy child
            elif os.path.splitext(entry.name)[1].lower() in SUPPORTED_EXTENSIONS:
                self._fs_store.append(parent_iter, [entry.name, entry.path, False])

    def _on_fs_expand(self, tree_view, parent_iter, tree_path):
        dir_path = self._fs_store.get_value(parent_iter, 1)
        self._fs_load_dir(parent_iter, dir_path)
        return False   # allow the expansion

    def _on_fs_activated(self, tree_view, tree_path, col):
        it = self._fs_store.get_iter(tree_path)
        path   = self._fs_store.get_value(it, 1)
        is_dir = self._fs_store.get_value(it, 2)
        if is_dir:
            if tree_view.row_expanded(tree_path):
                tree_view.collapse_row(tree_path)
            else:
                tree_view.expand_row(tree_path, False)
        else:
            self.open_file(path)

    def _fs_reveal_path(self, file_path):
        """Select and scroll to file_path in the filesystem tree.
        Expands and lazy-loads ancestor directories as needed."""
        folder = os.path.dirname(file_path)

        def _walk(parent_iter):
            child = self._fs_store.iter_children(parent_iter)
            while child:
                child_path  = self._fs_store.get_value(child, 1)
                child_is_dir = self._fs_store.get_value(child, 2)
                if child_path == file_path:
                    tp = self._fs_store.get_path(child)
                    self._fs_tree.get_selection().select_path(tp)
                    self._fs_tree.scroll_to_cell(tp, None, True, 0.5, 0.0)
                    return True
                if child_is_dir and folder.startswith(child_path + os.sep):
                    # This dir is an ancestor — expand and recurse
                    self._fs_load_dir(child, child_path)
                    tp = self._fs_store.get_path(child)
                    self._fs_tree.expand_row(tp, False)
                    return _walk(child)
                child = self._fs_store.iter_next(child)
            return False

        # Try from each root; if the file is in a directory that wasn't
        # loaded yet under an existing root, load and expand as we go.
        root = self._fs_store.get_iter_first()
        while root:
            root_path = self._fs_store.get_value(root, 1)
            if file_path.startswith(root_path + os.sep) or os.path.dirname(file_path) == root_path:
                self._fs_load_dir(root, root_path)
                self._fs_tree.expand_row(self._fs_store.get_path(root), False)
                _walk(root)
                break
            root = self._fs_store.iter_next(root)

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

    def _on_thumb_activated(self, icon_view, tree_path):
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
            self._thumb_view.select_path(tree_path)
            self._thumb_view.scroll_to_path(tree_path, False, 0.5, 0.5)

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
        # Reset edits for the new image
        self._edit_brightness = 0
        self._brightness_scale.set_value(0)
        self._crop_rect = None
        self._crop_drag = None
        self._display_image()
        self._update_title()
        self._update_status()
        self._update_nav_buttons()
        self._sidebar_select(path)
        self._populate_exif(exif)
        self._fs_reveal_path(path)
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
        img = self._get_brightness_image()  # apply brightness, not crop (crop is drawn as overlay)
        w = max(1, int(img.width * scale))
        h = max(1, int(img.height * scale))
        resample = Image.LANCZOS if scale < 1.0 else Image.NEAREST
        resized = img.resize((w, h), resample)
        self._display_pixbuf = pil_image_to_pixbuf(resized)
        self._draw_area.set_size_request(w, h)
        self._draw_area.queue_draw()

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
            if self._crop_mode and self._display_pixbuf:
                self._crop_press(event.x, event.y)
            else:
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
            if self._crop_mode:
                self._crop_drag = None
                self._draw_area.queue_draw()
            else:
                self._drag_start = None
                self._scroll_origin = None
                widget.get_window().set_cursor(None)
        return False

    def _on_motion(self, widget, event):
        if self._crop_mode and self._crop_drag:
            self._crop_move(event.x, event.y)
        elif self._drag_start and self._scroll_origin:
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
        elif key == Gdk.KEY_c or key == Gdk.KEY_C:
            self._btn_crop.set_active(not self._btn_crop.get_active())
        elif ctrl and (event.state & Gdk.ModifierType.SHIFT_MASK) and key == Gdk.KEY_S:
            self._on_save_as()
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
    # Image rendering (DrawingArea)
    # ------------------------------------------------------------------

    def _on_draw(self, da, cr):
        from gi.repository import Gdk as _Gdk
        alloc = da.get_allocation()
        dw, dh = alloc.width, alloc.height

        cr.set_source_rgb(0.15, 0.15, 0.15)
        cr.paint()

        if self._display_pixbuf is None:
            # Placeholder icon
            cr.set_source_rgb(0.4, 0.4, 0.4)
            cr.set_font_size(14)
            msg = "Open an image to get started  (Ctrl+O)"
            ext = cr.text_extents(msg)
            cr.move_to((dw - ext.width) / 2, dh / 2)
            cr.show_text(msg)
            return

        pw = self._display_pixbuf.get_width()
        ph = self._display_pixbuf.get_height()
        self._img_draw_x = max(0, (dw - pw) // 2)
        self._img_draw_y = max(0, (dh - ph) // 2)

        _Gdk.cairo_set_source_pixbuf(cr, self._display_pixbuf,
                                     self._img_draw_x, self._img_draw_y)
        cr.paint()

        if self._crop_mode:
            self._draw_crop_overlay(cr, pw, ph)

    # ------------------------------------------------------------------
    # Non-destructive editing
    # ------------------------------------------------------------------

    def _get_brightness_image(self):
        """Return _current_pil with brightness applied (no crop)."""
        img = self._current_pil
        if img is None:
            return None
        if self._edit_brightness != 0:
            from PIL import ImageEnhance
            factor = max(0.0, 1.0 + self._edit_brightness / 100.0)
            img = ImageEnhance.Brightness(img).enhance(factor)
        return img

    def _get_edited_image(self):
        """Return the fully edited image (brightness + crop) for Save As."""
        img = self._get_brightness_image()
        if img is None:
            return None
        if self._crop_rect:
            x1, y1, x2, y2 = self._crop_rect
            left   = max(0, int(round(min(x1, x2))))
            top    = max(0, int(round(min(y1, y2))))
            right  = min(img.width,  int(round(max(x1, x2))))
            bottom = min(img.height, int(round(max(y1, y2))))
            if right > left and bottom > top:
                img = img.crop((left, top, right, bottom))
        return img

    def _on_brightness_changed(self, scale):
        self._edit_brightness = int(scale.get_value())
        if self._current_pil:
            self._display_image()

    def _reset_edits(self):
        self._edit_brightness = 0
        self._brightness_scale.set_value(0)
        self._crop_rect = None
        self._crop_drag = None
        self._btn_crop.set_active(False)
        if self._current_pil:
            self._display_image()

    # ------------------------------------------------------------------
    # Crop tool
    # ------------------------------------------------------------------

    # Handle layout (index → which coords it controls):
    #   0(TL) 1(TC) 2(TR)
    #   3(ML)       4(MR)
    #   5(BL) 6(BC) 7(BR)
    _HANDLE_AXES = [
        ('x1', 'y1'), (None, 'y1'), ('x2', 'y1'),
        ('x1',  None),              ('x2',  None),
        ('x1', 'y2'), (None, 'y2'), ('x2', 'y2'),
    ]
    _HANDLE_CURSORS = [
        "nw-resize", "n-resize", "ne-resize",
        "w-resize",              "e-resize",
        "sw-resize", "s-resize", "se-resize",
    ]

    def _on_crop_toggled(self, btn):
        self._crop_mode = btn.get_active()
        if not self._crop_mode:
            self._crop_drag = None
        if self._display_pixbuf:
            self._draw_area.queue_draw()

    def _img_to_screen(self, ix, iy):
        return (self._img_draw_x + ix * self._zoom,
                self._img_draw_y + iy * self._zoom)

    def _screen_to_img(self, sx, sy):
        return ((sx - self._img_draw_x) / self._zoom,
                (sy - self._img_draw_y) / self._zoom)

    def _get_handles_screen(self):
        """Return list of 8 (sx, sy) handle positions in screen coords."""
        if not self._crop_rect:
            return []
        x1, y1, x2, y2 = self._crop_rect
        lx, rx = sorted([x1, x2])
        ty, by = sorted([y1, y2])
        cx, cy = (lx + rx) / 2, (ty + by) / 2
        pts = [(lx, ty), (cx, ty), (rx, ty),
               (lx, cy),           (rx, cy),
               (lx, by), (cx, by), (rx, by)]
        return [self._img_to_screen(ix, iy) for ix, iy in pts]

    def _handle_at(self, sx, sy, radius=8):
        for i, (hx, hy) in enumerate(self._get_handles_screen()):
            if abs(sx - hx) <= radius and abs(sy - hy) <= radius:
                return i
        return None

    def _inside_crop(self, sx, sy):
        if not self._crop_rect:
            return False
        x1, y1, x2, y2 = self._crop_rect
        lx, rx = sorted([x1 * self._zoom + self._img_draw_x,
                          x2 * self._zoom + self._img_draw_x])
        ty, by = sorted([y1 * self._zoom + self._img_draw_y,
                          y2 * self._zoom + self._img_draw_y])
        return lx <= sx <= rx and ty <= sy <= by

    def _crop_press(self, sx, sy):
        ix, iy = self._screen_to_img(sx, sy)
        h = self._handle_at(sx, sy)
        if h is not None:
            self._crop_drag = {
                'type': 'handle', 'idx': h,
                'orig': self._crop_rect,
                'start_ix': ix, 'start_iy': iy,
            }
        elif self._inside_crop(sx, sy):
            x1, y1, x2, y2 = self._crop_rect
            self._crop_drag = {
                'type': 'move',
                'orig': self._crop_rect,
                'start_ix': ix, 'start_iy': iy,
            }
        else:
            # Start a new selection
            self._crop_drag = {
                'type': 'new',
                'start_ix': ix, 'start_iy': iy,
            }
            self._crop_rect = (ix, iy, ix, iy)
        self._draw_area.queue_draw()

    def _crop_move(self, sx, sy):
        if not self._crop_drag:
            return
        ix, iy = self._screen_to_img(sx, sy)
        iw = self._current_pil.width
        ih = self._current_pil.height
        d = self._crop_drag

        if d['type'] == 'new':
            self._crop_rect = (d['start_ix'], d['start_iy'], ix, iy)

        elif d['type'] == 'handle':
            ox1, oy1, ox2, oy2 = d['orig']
            dx = ix - d['start_ix']
            dy = iy - d['start_iy']
            x_axis, y_axis = self._HANDLE_AXES[d['idx']]
            nx1, ny1, nx2, ny2 = ox1, oy1, ox2, oy2
            if x_axis == 'x1':
                nx1 = max(0, min(ox1 + dx, iw))
            elif x_axis == 'x2':
                nx2 = max(0, min(ox2 + dx, iw))
            if y_axis == 'y1':
                ny1 = max(0, min(oy1 + dy, ih))
            elif y_axis == 'y2':
                ny2 = max(0, min(oy2 + dy, ih))
            self._crop_rect = (nx1, ny1, nx2, ny2)

        elif d['type'] == 'move':
            ox1, oy1, ox2, oy2 = d['orig']
            dx = ix - d['start_ix']
            dy = iy - d['start_iy']
            w = ox2 - ox1
            h = oy2 - oy1
            nx1 = max(0, min(ox1 + dx, iw - w))
            ny1 = max(0, min(oy1 + dy, ih - h))
            self._crop_rect = (nx1, ny1, nx1 + w, ny1 + h)

        self._draw_area.queue_draw()

    def _draw_crop_overlay(self, cr, img_screen_w, img_screen_h):
        ix = self._img_draw_x
        iy = self._img_draw_y

        if self._crop_rect:
            x1, y1, x2, y2 = self._crop_rect
            lx, rx = sorted([x1, x2])
            ty, by = sorted([y1, y2])
            sx1 = ix + lx * self._zoom
            sy1 = iy + ty * self._zoom
            sx2 = ix + rx * self._zoom
            sy2 = iy + by * self._zoom

            # Darken outside crop
            cr.set_source_rgba(0, 0, 0, 0.55)
            cr.rectangle(ix, iy, img_screen_w, sy1 - iy)
            cr.fill()
            cr.rectangle(ix, sy2, img_screen_w, iy + img_screen_h - sy2)
            cr.fill()
            cr.rectangle(ix, sy1, sx1 - ix, sy2 - sy1)
            cr.fill()
            cr.rectangle(sx2, sy1, ix + img_screen_w - sx2, sy2 - sy1)
            cr.fill()

            # Crop border
            cr.set_source_rgb(1, 1, 1)
            cr.set_line_width(1.5)
            cr.rectangle(sx1, sy1, sx2 - sx1, sy2 - sy1)
            cr.stroke()

            # Rule-of-thirds grid
            cr.set_source_rgba(1, 1, 1, 0.35)
            cr.set_line_width(0.7)
            cw, ch = sx2 - sx1, sy2 - sy1
            for t in (1/3, 2/3):
                cr.move_to(sx1 + cw * t, sy1); cr.line_to(sx1 + cw * t, sy2); cr.stroke()
                cr.move_to(sx1, sy1 + ch * t); cr.line_to(sx2, sy1 + ch * t); cr.stroke()

            # Handles
            for hx, hy in self._get_handles_screen():
                cr.set_source_rgb(1, 1, 1)
                cr.rectangle(hx - 5, hy - 5, 10, 10)
                cr.fill()
                cr.set_source_rgb(0.25, 0.25, 0.25)
                cr.set_line_width(1)
                cr.rectangle(hx - 5, hy - 5, 10, 10)
                cr.stroke()
        else:
            # No rect yet — show a faint "draw here" hint
            cr.set_source_rgba(1, 1, 1, 0.25)
            cr.set_line_width(1)
            cr.set_dash([6, 4], 0)
            cr.rectangle(ix + 2, iy + 2, img_screen_w - 4, img_screen_h - 4)
            cr.stroke()
            cr.set_dash([], 0)

    # ------------------------------------------------------------------
    # Save As
    # ------------------------------------------------------------------

    def _on_save_as(self):
        if self._current_pil is None:
            return
        dialog = Gtk.FileChooserDialog(
            title="Save Edited Image As",
            parent=self,
            action=Gtk.FileChooserAction.SAVE,
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_SAVE,   Gtk.ResponseType.OK,
        )
        dialog.set_do_overwrite_confirmation(True)

        # Suggest a name derived from the original but different
        orig_name = os.path.basename(self._current_path)
        base, _ext = os.path.splitext(orig_name)
        dialog.set_current_folder(os.path.dirname(self._current_path))
        dialog.set_current_name(f"{base}_edited.jpg")

        flt = Gtk.FileFilter()
        flt.set_name("JPEG / PNG")
        flt.add_pattern("*.jpg"); flt.add_pattern("*.jpeg"); flt.add_pattern("*.png")
        dialog.add_filter(flt)

        response = dialog.run()
        save_path = dialog.get_filename() if response == Gtk.ResponseType.OK else None
        dialog.destroy()

        if not save_path:
            return

        # Safety: refuse to overwrite the original file
        if os.path.realpath(save_path) == os.path.realpath(self._current_path):
            self._show_error("Cannot overwrite the original file.\nPlease choose a different filename.")
            return

        ext = os.path.splitext(save_path)[1].lower()
        if ext not in (".jpg", ".jpeg", ".png"):
            save_path += ".jpg"
            ext = ".jpg"

        try:
            edited = self._get_edited_image()
            if ext in (".jpg", ".jpeg"):
                if edited.mode in ("RGBA", "P"):
                    edited = edited.convert("RGB")
                edited.save(save_path, "JPEG", quality=95)
            else:
                edited.save(save_path, "PNG")
        except Exception as e:
            self._show_error(f"Failed to save:\n{e}")
            return

        # Confirm to user
        dialog2 = Gtk.MessageDialog(
            transient_for=self, flags=0,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text="Image saved",
        )
        dialog2.format_secondary_text(save_path)
        dialog2.run()
        dialog2.destroy()

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
        _, exif_widget = self._content_paned.get_children()
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
