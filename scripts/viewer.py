#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""One read-only native observer for windows from registered background GUI sessions."""
from __future__ import annotations

import argparse
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

# GI may initialize GTK while importing it; keep this override observer-local.
os.environ.pop("GTK_THEME", None)
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("GLibUnix", "2.0")
gi.require_version("Pango", "1.0")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, GLibUnix, Graphene, Gsk, Gtk, Pango

import bgui
import observer_hub
from window_stream import WindowStream

APP_ID = "dev.codex.BackgroundGuiViewer"
STALE_SECONDS = 3.0
IMAGE_CORNER_RADIUS = 14.0
FPS_OPTIONS = (1, 2, 5, 10, 15)
MAX_THUMBNAILS = 64
DEFAULT_SETTINGS = {"fps": 5, "fit_mode": "fit", "cursor": False,
                    "thumbnail_size": "normal", "rounding": 14}
OLD_SESSION_MESSAGE = "Для просмотра отдельных окон нужен новый сеанс"


def clean_text(value, fallback):
    return " ".join(value.split())[:160] if isinstance(value, str) and value.strip() else fallback


def atomic_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.chmod(0o600)
    temporary.replace(path)


def load_settings(hub_dir):
    try:
        saved = json.loads((hub_dir / "settings.json").read_text())
    except (OSError, ValueError):
        saved = {}
    result = dict(DEFAULT_SETTINGS)
    if not isinstance(saved, dict):
        return result
    if type(saved.get("fps")) is int and saved["fps"] in FPS_OPTIONS:
        result["fps"] = saved["fps"]
    if saved.get("fit_mode") in ("fit", "actual"):
        result["fit_mode"] = saved["fit_mode"]
    if type(saved.get("cursor")) is bool:
        result["cursor"] = saved["cursor"]
    if saved.get("thumbnail_size") in ("compact", "normal"):
        result["thumbnail_size"] = saved["thumbnail_size"]
    rounding = saved.get("rounding")
    if type(rounding) in (float, int) and math.isfinite(rounding) and 0 <= rounding <= 20:
        result["rounding"] = round(rounding)
    return result


def thumbnail_dimensions(settings):
    return (128, 80) if settings["thumbnail_size"] == "compact" else (176, 110)


def poll_windows(hub_dir):
    """Bound each private IPC request; a broken session must not block the UI."""
    subscriptions = observer_hub.read_subscriptions(hub_dir)
    items, errors = [], {}
    for session in subscriptions:
        try:
            state = bgui.read_state(bgui.session_dir(session))
            env = bgui.isolated_env(state)
            windows = json.loads(bgui.checked(["niri", "msg", "-j", "windows"], env=env, timeout=2))
            for window in sorted(windows, key=lambda window: window["id"]):
                window_id = window["id"]
                items.append({"key": f"{session}/{window_id}", "session": session, "window_id": window_id,
                              "title": clean_text(window.get("title"), clean_text(window.get("app_id"), "Окно приложения")),
                              "session_name": clean_text(state.get("name"), "Сеанс " + session[-6:]),
                              "app_id": window.get("app_id"), "available": True,
                              "capture_supported": state.get("window_capture") == "clipboard-free-v1", "state": state})
        except (OSError, ValueError, bgui.GuiError) as exc:
            errors[session] = str(exc)
    return {"subscriptions": list(subscriptions), "items": items, "errors": errors}


def frame_token(frame):
    return frame.sequence, frame.timestamp


def thumbnail_texture(frame, size):
    if frame.format.upper() != "PNG":
        raise ValueError("Unsupported window frame format")
    factor = min(size[0] / frame.width, size[1] / frame.height)
    width, height = max(1, round(frame.width * factor)), max(1, round(frame.height * factor))
    loader = GdkPixbuf.PixbufLoader.new_with_type("png")
    loader.set_size(width, height)
    loader.write(frame.data)
    loader.close()
    pixbuf = loader.get_pixbuf()
    channels, stride = pixbuf.get_n_channels(), pixbuf.get_rowstride()
    pixels = pixbuf.get_pixels()
    required = stride * pixbuf.get_height()
    if len(pixels) < required:
        pixels += bytes(required - len(pixels))
    fmt = Gdk.MemoryFormat.R8G8B8A8 if channels == 4 else Gdk.MemoryFormat.R8G8B8
    return Gdk.MemoryTexture.new(pixbuf.get_width(), pixbuf.get_height(), fmt, GLib.Bytes.new(pixels), stride)


class SelectedStream:
    """Owned only by the stream worker; no capture lifecycle runs on GTK."""
    def __init__(self):
        self.lock = threading.RLock()
        self.stream = None
        self.spec = None
        self.token = None
        self.error = None
        self.last_thumb_at = 0

    def close(self):
        with self.lock:
            if self.stream is not None:
                self.stream.stop()
                self.stream = None
            self.spec = None
            self.token = None

    def update(self, item, settings, stopped):
        with self.lock:
            return self._update(item, settings, stopped)

    def _update(self, item, settings, stopped):
        key = item["key"] if item else None
        spec = (key, settings["fps"], settings["cursor"], bool(item and item.get("available")),
                bool(item and item.get("capture_supported")))
        result = {"key": key, "spec": spec, "frame": None, "thumbnail": None, "error": None, "closed": False, "active": False}
        if stopped.is_set():
            self.close()
            return result
        if spec != self.spec:
            self.close()
            self.spec = spec
            self.error = None
            if item and item.get("available") and item.get("capture_supported"):
                try:
                    self.stream = WindowStream(item["state"], item["window_id"], fps=settings["fps"], cursor=settings["cursor"]).start()
                except Exception as exc:
                    self.error = str(exc)
            elif item:
                self.error = OLD_SESSION_MESSAGE if not item.get("capture_supported") else item.get("error", "Сеанс недоступен")
        if self.stream is None:
            result["error"] = self.error
            return result
        try:
            snapshot = self.stream.latest()
            result.update(error=snapshot.error, closed=snapshot.closed)
            result["active"] = not snapshot.closed
            frame = snapshot.frame
            if frame is not None and frame_token(frame) != self.token:
                if frame.format.upper() != "PNG":
                    raise ValueError("Unsupported window frame format")
                texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(frame.data))
                result["frame"] = {"texture": texture, "timestamp": frame.timestamp, "sequence": frame.sequence,
                                   "width": texture.get_width(), "height": texture.get_height()}
                self.token = frame_token(frame)
                if time.monotonic() - self.last_thumb_at >= .8:
                    result["thumbnail"] = {"key": key, "texture": thumbnail_texture(frame, thumbnail_dimensions(settings)),
                                           "timestamp": frame.timestamp}
                    self.last_thumb_at = time.monotonic()
        except Exception as exc:
            result["error"] = str(exc)
        return result


def sample_thumbnail(item, settings, stopped):
    stream = None
    try:
        stream = WindowStream(item["state"], item["window_id"], fps=1, cursor=settings["cursor"]).start()
        deadline = time.monotonic() + 3
        while not stopped.is_set() and time.monotonic() < deadline:
            snapshot = stream.latest()
            if snapshot.frame is not None:
                frame = snapshot.frame
                return {"key": item["key"], "texture": thumbnail_texture(frame, thumbnail_dimensions(settings)),
                        "timestamp": frame.timestamp}
            if snapshot.closed or snapshot.error:
                return {"key": item["key"], "error": snapshot.error or "Окно недоступно"}
            stopped.wait(.05)
        return {"key": item["key"], "error": "Нет обновлений"}
    except Exception as exc:
        return {"key": item["key"], "error": str(exc)}
    finally:
        if stream is not None:
            stream.stop()


class FrameCanvas(Gtk.Widget):
    """Fit a texture or draw one texture pixel per backing-surface pixel."""
    __gtype_name__ = "CodexBackgroundFrameCanvas"

    def __init__(self):
        super().__init__()
        self.texture = None
        self.mode = "fit"
        self.radius = IMAGE_CORNER_RADIUS
        self._surface = None
        self._scale_handler = None
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_focusable(False)
        self.set_accessible_role(Gtk.AccessibleRole.IMG)
        self.update_property([Gtk.AccessibleProperty.LABEL], ["Изображение приложения агента"])
        self.connect("notify::scale-factor", self._scale_changed)
        self.connect("realize", self._surface_realized)
        self.connect("unrealize", self._surface_unrealized)

    def _scale_changed(self, *_):
        self.queue_resize()
        self.queue_draw()

    def _surface_realized(self, *_):
        native = self.get_native()
        self._surface = native.get_surface() if native else None
        if self._surface and self._surface.find_property("scale"):
            self._scale_handler = self._surface.connect("notify::scale", self._scale_changed)
        self._scale_changed()

    def _surface_unrealized(self, *_):
        if self._surface and self._scale_handler is not None:
            self._surface.disconnect(self._scale_handler)
        self._surface = None
        self._scale_handler = None

    def display_scale(self):
        if self._surface and hasattr(self._surface, "get_scale"):
            scale = self._surface.get_scale()
            if math.isfinite(scale) and scale > 0:
                return scale
        return max(1, self.get_scale_factor())

    def set_frame(self, texture):
        old_size = self.pixel_size()
        self.texture = texture
        if old_size != self.pixel_size():
            self.queue_resize()
        self.queue_draw()

    def pixel_size(self):
        return (self.texture.get_width(), self.texture.get_height()) if self.texture else (0, 0)

    def set_mode(self, mode):
        if mode != self.mode:
            self.mode = mode
            self.queue_resize()
            self.queue_draw()

    def set_radius(self, radius):
        self.radius = max(0, min(20, float(radius)))
        self.queue_draw()

    def do_measure(self, orientation, for_size):
        if self.mode == "actual" and self.texture:
            pixels = self.pixel_size()[0 if orientation == Gtk.Orientation.HORIZONTAL else 1]
            size = math.ceil(pixels / self.display_scale())
            return size, size, -1, -1
        return 0, 640 if orientation == Gtk.Orientation.HORIZONTAL else 400, -1, -1

    def image_rect(self):
        if not self.texture:
            return None
        pixels_w, pixels_h = self.pixel_size()
        allocated_w, allocated_h = self.get_width(), self.get_height()
        if self.mode == "actual":
            factor = 1 / self.display_scale()
        else:
            factor = min(allocated_w / pixels_w, allocated_h / pixels_h)
        width, height = pixels_w * factor, pixels_h * factor
        x, y = max(0, (allocated_w - width) / 2), max(0, (allocated_h - height) / 2)
        if self.mode == "actual":
            scale = self.display_scale()
            x, y = round(x * scale) / scale, round(y * scale) / scale
        return x, y, width, height

    def do_snapshot(self, snapshot):
        rect = self.image_rect()
        if rect and rect[2] > 0 and rect[3] > 0:
            bounds = Graphene.Rect()
            bounds.init(*rect)
            radius = min(self.radius, rect[2] / 2, rect[3] / 2)
            rounded = Gsk.RoundedRect()
            rounded.init_from_rect(bounds, radius)
            snapshot.push_rounded_clip(rounded)
            snapshot.append_texture(self.texture, bounds)
            snapshot.pop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hub-dir", type=Path, required=True)
    parser.add_argument("--ready", type=Path, required=True)
    args = parser.parse_args()
    hub_dir = args.hub_dir.expanduser().resolve()
    hub_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    settings = load_settings(hub_dir)
    stopped = threading.Event()
    workers = ThreadPoolExecutor(max_workers=3, thread_name_prefix="observer")
    selected_stream = SelectedStream()
    app = Adw.Application(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
    ui = {"items": OrderedDict(), "buttons": {}, "thumbnails": OrderedDict(), "thumb_times": {},
          "selected": None, "selected_since": time.monotonic(), "subscriptions": [], "metadata_errors": {},
          "metadata_busy": False, "stream_busy": False, "thumbnail_busy": False, "thumbnail_key": None,
          "frames": 0, "last_frame_at": None, "sequence": None, "error": None, "closed": False,
          "last_thumb_job": 0, "style": None, "last_status_write": 0,
          "selected_stream_running": False, "settings_save_source": None, "rounding_css": None,
          "status_write_error": None}

    def current_item():
        return ui["items"].get(ui["selected"])

    def persist_settings():
        try:
            atomic_json(hub_dir / "settings.json", settings)
        except (OSError, ValueError) as exc:
            print(f"Observer settings could not be saved: {exc}", file=sys.stderr, flush=True)

    def apply_settings():
        if "canvas" not in ui:
            return
        ui["canvas"].set_mode(settings["fit_mode"])
        ui["canvas"].set_radius(settings["rounding"])
        policy = Gtk.PolicyType.NEVER if settings["fit_mode"] == "fit" else Gtk.PolicyType.AUTOMATIC
        ui["scroller"].set_policy(policy, policy)
        radius = settings["rounding"]
        if ui["rounding_css"] != radius:
            ui["style"].load_from_string(f".codex-frame-viewport {{ border-radius: {radius}px; }} "
                                        f".codex-window-thumb {{ border-radius: {min(radius, 10)}px; }}")
            ui["rounding_css"] = radius
        width, height = thumbnail_dimensions(settings)
        ui["thumbnail_scroll"].set_min_content_height(height + 64)
        for widgets in ui["buttons"].values():
            widgets["image_stack"].set_size_request(width, height)
            widgets["title"].set_max_width_chars(18 if settings["thumbnail_size"] == "compact" else 25)
            widgets["session"].set_max_width_chars(18 if settings["thumbnail_size"] == "compact" else 25)

    def set_setting(name, value):
        if settings.get(name) == value:
            return
        settings[name] = value
        if ui["settings_save_source"]:
            GLib.source_remove(ui["settings_save_source"])
        def save():
            persist_settings()
            ui["settings_save_source"] = None
            return GLib.SOURCE_REMOVE
        ui["settings_save_source"] = GLib.timeout_add(300, save)
        if name == "thumbnail_size":
            ui["thumb_times"].clear()
        apply_settings()
        publish_status()

    def select_window(key):
        if key not in ui["items"]:
            return
        if ui["selected"] != key:
            ui["selected"] = key
            ui["selected_since"] = time.monotonic()
            ui["last_frame_at"] = None
            ui["sequence"] = None
            ui["error"] = None
            ui["closed"] = False
            ui["canvas"].set_frame(None)
            ui["scroller"].get_hadjustment().set_value(0)
            ui["scroller"].get_vadjustment().set_value(0)
        for item_key, widgets in ui["buttons"].items():
            widgets["button"].set_active(item_key == key)
        publish_status()

    def put_thumbnail(result):
        key = result.get("key")
        if key not in ui["items"]:
            return
        ui["thumb_times"][key] = time.monotonic()
        widgets = ui["buttons"].get(key)
        if result.get("texture") is not None and widgets:
            texture = result["texture"]
            ui["thumbnails"][key] = {"texture": texture, "timestamp": result["timestamp"]}
            ui["thumbnails"].move_to_end(key)
            widgets["picture"].set_paintable(texture)
            widgets["image_stack"].set_visible_child_name("image")
            while len(ui["thumbnails"]) > MAX_THUMBNAILS:
                evicted, _ = ui["thumbnails"].popitem(last=False)
                previous = ui["buttons"].get(evicted)
                if previous:
                    previous["picture"].set_paintable(None)
                    previous["image_stack"].set_visible_child_name("placeholder")

    def sync_buttons():
        for key in list(ui["buttons"]):
            if key not in ui["items"]:
                ui["thumbnail_box"].remove(ui["buttons"].pop(key)["button"])
                ui["thumbnails"].pop(key, None)
                ui["thumb_times"].pop(key, None)
        previous = None
        for key, item in ui["items"].items():
            if key not in ui["buttons"]:
                button = Gtk.ToggleButton()
                button.add_css_class("flat")
                body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
                picture = Gtk.Picture()
                picture.set_can_shrink(True)
                picture.set_content_fit(Gtk.ContentFit.CONTAIN)
                picture.add_css_class("codex-window-thumb")
                picture.set_overflow(Gtk.Overflow.HIDDEN)
                image_stack = Gtk.Stack()
                image_stack.add_named(picture, "image")
                placeholder = Gtk.Image.new_from_icon_name("application-x-executable-symbolic")
                placeholder.set_pixel_size(32)
                placeholder.add_css_class("dim-label")
                image_stack.add_named(placeholder, "placeholder")
                image_stack.set_visible_child_name("placeholder")
                title = Gtk.Label(xalign=0)
                title.set_ellipsize(Pango.EllipsizeMode.END)
                session_label = Gtk.Label(xalign=0)
                session_label.set_ellipsize(Pango.EllipsizeMode.END)
                session_label.add_css_class("dim-label")
                session_label.add_css_class("caption")
                hint = Gtk.Label(label="Нужен новый сеанс", xalign=0)
                hint.add_css_class("dim-label")
                hint.add_css_class("caption")
                body.append(image_stack)
                body.append(title)
                body.append(session_label)
                body.append(hint)
                button.set_child(body)
                button.connect("clicked", lambda _button, selected_key=key: select_window(selected_key))
                ui["thumbnail_box"].append(button)
                ui["buttons"][key] = {"button": button, "picture": picture, "image_stack": image_stack,
                                      "title": title, "session": session_label, "hint": hint}
            widgets = ui["buttons"][key]
            widgets["title"].set_text(item["title"])
            widgets["session"].set_text(item["session_name"])
            widgets["hint"].set_visible(not item.get("capture_supported"))
            widgets["button"].set_tooltip_text(item["title"] + "\n" + item["session_name"])
            widgets["button"].update_property([Gtk.AccessibleProperty.LABEL],
                                                [item["title"] + ", " + item["session_name"]])
            widgets["button"].set_active(key == ui["selected"])
            widgets["picture"].set_opacity(1 if item.get("available") and item.get("capture_supported") else .45)
            ui["thumbnail_box"].reorder_child_after(widgets["button"], previous)
            previous = widgets["button"]
        ui["thumbnail_scroll"].set_visible(bool(ui["items"]))
        apply_settings()

    def publish_status():
        if "window" not in ui:
            return
        item = current_item()
        age = max(0, time.monotonic() - ui["last_frame_at"]) if ui["last_frame_at"] is not None else None
        message = ""
        if item is None:
            state, status_text = ("error", "Нет обновлений") if ui["metadata_errors"] else ("empty", "Нет открытых окон")
            message = "Не удалось получить список окон" if ui["metadata_errors"] else "Окна запущенных приложений появятся здесь"
        elif not item.get("capture_supported"):
            state, status_text, message = "unavailable", "Нужен новый сеанс", OLD_SESSION_MESSAGE
        elif not item.get("available"):
            state, status_text = "stale", "Нет обновлений"
            message = f"Нет обновлений · последний кадр {int(age)} с назад" if age is not None else "Сеанс недоступен"
        elif age is None:
            if ui["error"] or ui["closed"] or time.monotonic() - ui["selected_since"] > 4:
                state, status_text, message = "stale", "Нет обновлений", "Изображение окна недоступно"
            else:
                state, status_text, message = "loading", "Ожидание изображения", "Ожидание изображения окна…"
        elif age >= STALE_SECONDS or (ui["error"] and age >= 1.5):
            state, status_text = "stale", "Нет обновлений"
            message = f"Нет обновлений · последний кадр {int(age)} с назад"
        else:
            state, status_text = "connected", "Подключено"
        ui["canvas"].set_opacity(.30 if state in ("stale", "unavailable") else 1)
        ui["overlay_label"].set_text(message)
        ui["overlay_box"].set_visible(bool(message))
        subtitle = item["title"] if item else status_text
        ui["title"].set_subtitle(subtitle)
        ui["title"].set_tooltip_text((item["title"] + "\n" + item["session_name"]) if item else status_text)
        ui["window"].set_title((item["title"] + " — Codex") if item else "Codex")
        ui["connection"].set_text(status_text)
        ui["selected_info"].set_text((item["session_name"] + "\n" + item["session"]) if item else "Сеанс не выбран")
        for action in (ui["hide_session_action"], ui["stop_session_action"]):
            action.set_enabled(item is not None)
        if time.monotonic() - ui["last_status_write"] < 1:
            return
        ui["last_status_write"] = time.monotonic()
        width, height = ui["canvas"].pixel_size()
        compact_items = []
        for key, metadata in ui["items"].items():
            thumb = ui["thumbnails"].get(key)
            compact_items.append({name: metadata.get(name) for name in
                                  ("key", "session", "window_id", "title", "session_name", "available", "capture_supported")}
                                 | {"thumbnail": bool(thumb), "thumbnail_at": thumb["timestamp"] if thumb else None})
        payload = {
            "status": "running", "updated_at": time.time(), "style": "adwaita", "dark": app.get_style_manager().get_dark(),
            "subscriptions": ui["subscriptions"], "items": compact_items, "selected": ui["selected"],
            "settings": settings, "frames": ui["frames"], "sequence": ui["sequence"], "width": width, "height": height,
            "menu_open": ui["menu_button"].get_popover().get_visible(),
            "ui_state": state, "status_text": status_text, "error": ui["error"], "session_errors": ui["metadata_errors"],
            "frame_age_seconds": round(age, 3) if age is not None else None,
            "image_rect": ui["canvas"].image_rect(), "display_scale_factor": ui["canvas"].display_scale(),
            "canvas_logical_size": [ui["canvas"].get_width(), ui["canvas"].get_height()],
            "scroll_offset": [ui["scroller"].get_hadjustment().get_value(), ui["scroller"].get_vadjustment().get_value()],
            "streams": {"selected": ui["selected"] if ui["selected_stream_running"] else None,
                        "thumbnail": ui["thumbnail_key"], "active_count": int(ui["selected_stream_running"]) + int(ui["thumbnail_busy"]),
                        "limit": 2, "thumbnail_cache_limit": MAX_THUMBNAILS},
        }
        try:
            atomic_json(hub_dir / "status.json", payload)
            ui["status_write_error"] = None
        except (OSError, ValueError) as exc:
            error = str(exc)
            if ui["status_write_error"] != error:
                print(f"Observer status could not be saved: {error}", file=sys.stderr, flush=True)
            ui["status_write_error"] = error

    def metadata_done(future):
        try:
            result = future.result()
        except Exception as exc:
            result = {"subscriptions": ui["subscriptions"], "items": [], "errors": {"registry": str(exc)}}
        def apply():
            ui["metadata_busy"] = False
            if stopped.is_set():
                return GLib.SOURCE_REMOVE
            items = OrderedDict((item["key"], item) for item in result["items"])
            for key, old in ui["items"].items():
                if old["session"] in result["subscriptions"] and old["session"] in result["errors"]:
                    items[key] = {**old, "available": False, "error": result["errors"][old["session"]]}
            ui.update(items=items, subscriptions=result["subscriptions"], metadata_errors=result["errors"])
            sync_buttons()
            if ui["selected"] not in items:
                ui["selected"] = None
                ui["last_frame_at"] = None
                ui["canvas"].set_frame(None)
                if items:
                    select_window(next(iter(items)))
            publish_status()
            return GLib.SOURCE_REMOVE
        if not stopped.is_set():
            GLib.idle_add(apply)

    def metadata_tick():
        if stopped.is_set():
            return GLib.SOURCE_REMOVE
        if not ui["metadata_busy"]:
            ui["metadata_busy"] = True
            workers.submit(poll_windows, hub_dir).add_done_callback(metadata_done)
        return GLib.SOURCE_CONTINUE

    def stream_done(future):
        try:
            result = future.result()
        except Exception as exc:
            result = {"key": ui["selected"], "spec": None, "error": str(exc), "frame": None}
        def apply():
            ui["stream_busy"] = False
            if stopped.is_set():
                return GLib.SOURCE_REMOVE
            item = current_item()
            expected = (ui["selected"], settings["fps"], settings["cursor"], bool(item and item.get("available")),
                        bool(item and item.get("capture_supported")))
            if result.get("spec") != expected:
                return GLib.SOURCE_REMOVE
            ui["error"] = result.get("error")
            ui["closed"] = result.get("closed", False)
            ui["selected_stream_running"] = result.get("active", False)
            frame = result.get("frame")
            if frame:
                ui["canvas"].set_frame(frame["texture"])
                ui["last_frame_at"] = frame["timestamp"]
                ui["sequence"] = frame["sequence"]
                ui["frames"] += 1
            if result.get("thumbnail"):
                put_thumbnail(result["thumbnail"])
            return GLib.SOURCE_REMOVE
        if not stopped.is_set():
            GLib.idle_add(apply)

    def stream_tick():
        if stopped.is_set():
            return GLib.SOURCE_REMOVE
        if not ui["stream_busy"]:
            ui["stream_busy"] = True
            item = current_item()
            workers.submit(selected_stream.update, dict(item) if item else None, dict(settings), stopped).add_done_callback(stream_done)
        return GLib.SOURCE_CONTINUE

    def thumbnail_tick():
        if stopped.is_set():
            return GLib.SOURCE_REMOVE
        if ui["thumbnail_busy"] or time.monotonic() - ui["last_thumb_job"] < .8:
            return GLib.SOURCE_CONTINUE
        candidates = [item for key, item in ui["items"].items() if key != ui["selected"] and
                      item.get("available") and item.get("capture_supported")]
        if not candidates:
            return GLib.SOURCE_CONTINUE
        item = min(candidates, key=lambda item: ui["thumb_times"].get(item["key"], 0))
        ui["thumbnail_busy"] = True
        ui["thumbnail_key"] = item["key"]
        ui["last_thumb_job"] = time.monotonic()
        future = workers.submit(sample_thumbnail, dict(item), dict(settings), stopped)
        def done(future):
            try:
                result = future.result()
            except Exception as exc:
                result = {"key": item["key"], "error": str(exc)}
            def apply():
                ui["thumbnail_busy"] = False
                ui["thumbnail_key"] = None
                if not stopped.is_set():
                    put_thumbnail(result)
                return GLib.SOURCE_REMOVE
            if not stopped.is_set():
                GLib.idle_add(apply)
        future.add_done_callback(done)
        return GLib.SOURCE_CONTINUE

    def heartbeat():
        if stopped.is_set():
            return GLib.SOURCE_REMOVE
        publish_status()
        return GLib.SOURCE_CONTINUE

    def quit_app(*_):
        try:
            if ui["settings_save_source"]:
                GLib.source_remove(ui["settings_save_source"])
                ui["settings_save_source"] = None
            persist_settings()
        finally:
            stopped.set()
            app.quit()
        return GLib.SOURCE_REMOVE

    def session_action(command):
        item = current_item()
        if item:
            try:
                with (hub_dir / "actions.log").open("ab") as log:
                    subprocess.Popen([sys.executable, str(Path(bgui.__file__).resolve()), command, item["session"]],
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            except OSError as exc:
                ui["error"] = str(exc)

    def activate(app):
        if "window" in ui:
            return
        app.get_style_manager().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        window = Adw.ApplicationWindow(application=app, title="Codex")
        window.set_default_size(1120, 820)
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        title = Adw.WindowTitle(title="Codex", subtitle="Нет открытых окон")
        header.set_title_widget(title)
        toolbar.add_top_bar(header)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        body.set_margin_top(6)
        body.set_margin_start(12)
        body.set_margin_end(12)
        body.set_margin_bottom(12)
        thumbnail_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        thumbnail_scroll = Gtk.ScrolledWindow()
        thumbnail_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        thumbnail_scroll.set_child(thumbnail_box)
        thumbnail_scroll.set_propagate_natural_height(True)
        thumbnail_scroll.set_max_content_height(190)
        body.append(thumbnail_scroll)
        canvas = FrameCanvas()
        scroller = Gtk.ScrolledWindow()
        scroller.add_css_class("codex-frame-viewport")
        scroller.set_overflow(Gtk.Overflow.HIDDEN)
        scroller.set_child(canvas)
        scroller.set_hexpand(True)
        scroller.set_vexpand(True)
        style = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(window.get_display(), style, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        overlay = Gtk.Overlay()
        overlay.add_css_class("background")
        overlay.set_child(scroller)
        overlay.set_vexpand(True)
        overlay_label = Gtk.Label(label="Окна запущенных приложений появятся здесь")
        overlay_label.set_wrap(True)
        overlay_label.set_max_width_chars(52)
        overlay_label.add_css_class("heading")
        overlay_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        overlay_box.add_css_class("osd")
        overlay_box.set_halign(Gtk.Align.CENTER)
        overlay_box.set_valign(Gtk.Align.CENTER)
        overlay_box.set_can_target(False)
        for side in ("top", "bottom", "start", "end"):
            getattr(overlay_label, "set_margin_" + side)(16)
        overlay_box.append(overlay_label)
        overlay.add_overlay(overlay_box)
        body.append(overlay)
        toolbar.set_content(body)
        window.set_content(toolbar)

        mode_action = Gio.SimpleAction.new_stateful("view-mode", GLib.VariantType.new("s"), GLib.Variant("s", settings["fit_mode"]))
        modes = Adw.ToggleGroup()
        for mode, label, description in (("fit", "Вписать", "Вписать изображение в окно"),
                                          ("actual", "1:1", "Масштаб один к одному")):
            toggle = Adw.Toggle(name=mode, label=label)
            toggle.set_description(description)
            toggle.set_tooltip(description + (" · Ctrl+0" if mode == "fit" else " · Ctrl+1"))
            modes.add(toggle)
        modes.set_active_name(settings["fit_mode"])
        def change_mode(action, value):
            mode = value.get_string()
            if mode not in ("fit", "actual"):
                return
            action.set_state(value)
            set_setting("fit_mode", mode)
            if modes.get_active_name() != mode:
                modes.set_active_name(mode)
        mode_action.connect("change-state", change_mode)
        mode_action.connect("activate", lambda action, value: action.change_state(value))
        window.add_action(mode_action)
        modes.connect("notify::active-name", lambda group, *_: mode_action.change_state(GLib.Variant("s", group.get_active_name()))
                      if group.get_active_name() in ("fit", "actual") and group.get_active_name() != settings["fit_mode"] else None)
        header.pack_start(modes)
        hide_action = Gio.SimpleAction.new("hide-view", None)
        hide_action.connect("activate", quit_app)
        window.add_action(hide_action)
        hide_session_action = Gio.SimpleAction.new("hide-session", None)
        hide_session_action.connect("activate", lambda *_: session_action("unwatch"))
        window.add_action(hide_session_action)
        stop_session_action = Gio.SimpleAction.new("stop-session", None)
        stop_session_action.connect("activate", lambda *_: session_action("stop"))
        window.add_action(stop_session_action)
        app.set_accels_for_action("win.hide-view", ["<Primary>w"])
        app.set_accels_for_action("win.view-mode('fit')", ["<Primary>0"])
        app.set_accels_for_action("win.view-mode('actual')", ["<Primary>1"])
        menu_button = Gtk.MenuButton()
        menu_button.set_primary(True)
        menu_button.set_icon_name("open-menu-symbolic")
        menu_button.set_tooltip_text("Настройки просмотра")
        menu_button.update_property([Gtk.AccessibleProperty.LABEL], ["Настройки просмотра"])
        menu_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for side in ("top", "bottom", "start", "end"):
            getattr(menu_content, "set_margin_" + side)(12)
        connection = Gtk.Label(label="Нет открытых окон", xalign=0)
        connection.add_css_class("dim-label")
        menu_content.append(connection)
        def setting_row(label, widget):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
            caption = Gtk.Label(label=label, xalign=0)
            caption.set_hexpand(True)
            row.append(caption)
            widget.update_property([Gtk.AccessibleProperty.LABEL], [label])
            row.append(widget)
            menu_content.append(row)
        fps = Gtk.DropDown.new_from_strings([str(value) for value in FPS_OPTIONS])
        fps.set_selected(FPS_OPTIONS.index(settings["fps"]))
        fps.connect("notify::selected", lambda widget, *_: set_setting("fps", FPS_OPTIONS[widget.get_selected()]))
        setting_row("Кадров в секунду", fps)
        cursor = Gtk.Switch()
        cursor.set_valign(Gtk.Align.CENTER)
        cursor.set_active(settings["cursor"])
        cursor.connect("notify::active", lambda widget, *_: set_setting("cursor", widget.get_active()))
        setting_row("Показывать курсор", cursor)
        sizes = Gtk.DropDown.new_from_strings(["Компактные", "Обычные"])
        sizes.set_selected(0 if settings["thumbnail_size"] == "compact" else 1)
        sizes.connect("notify::selected", lambda widget, *_: set_setting("thumbnail_size", "compact" if widget.get_selected() == 0 else "normal"))
        setting_row("Миниатюры", sizes)
        rounding = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 20, 1)
        rounding.set_size_request(130, -1)
        rounding.set_value(settings["rounding"])
        rounding.set_draw_value(True)
        rounding.set_digits(0)
        rounding.connect("value-changed", lambda widget: set_setting("rounding", round(widget.get_value())))
        setting_row("Скругление", rounding)
        menu_content.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        selected_info = Gtk.Label(xalign=0)
        selected_info.add_css_class("dim-label")
        selected_info.add_css_class("caption")
        selected_info.set_wrap(True)
        selected_info.set_max_width_chars(44)
        menu_content.append(selected_info)
        for label, action_name, destructive in (("Скрыть просмотр", "hide-view", False),
                                                ("Скрыть сеанс", "hide-session", False),
                                                ("Завершить сеанс", "stop-session", True)):
            button = Gtk.Button(label=label)
            button.set_action_name("win." + action_name)
            button.add_css_class("destructive-action" if destructive else "flat")
            menu_content.append(button)
        popover = Gtk.Popover()
        popover.set_child(menu_content)
        menu_button.set_popover(popover)
        header.pack_end(menu_button)
        ui.update(window=window, title=title, canvas=canvas, scroller=scroller, style=style,
                  thumbnail_box=thumbnail_box, thumbnail_scroll=thumbnail_scroll,
                  overlay_box=overlay_box, overlay_label=overlay_label, connection=connection,
                  selected_info=selected_info, hide_session_action=hide_session_action, stop_session_action=stop_session_action,
                  menu_button=menu_button)
        apply_settings()
        def mapped(*_):
            atomic_json(args.ready, {"app_id": APP_ID, "hub_dir": str(hub_dir)})
            publish_status()
        window.connect("map", mapped)
        window.present()
        GLib.timeout_add(1000, metadata_tick)
        GLib.timeout_add(50, stream_tick)
        GLib.timeout_add(250, thumbnail_tick)
        GLib.timeout_add(250, heartbeat)
        metadata_tick()

    app.connect("activate", activate)
    GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, quit_app)
    GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, quit_app)
    try:
        app.run([])
    finally:
        stopped.set()
        close_future = workers.submit(selected_stream.close)
        workers.shutdown(wait=True, cancel_futures=False)
        close_future.result()
        persist_settings()
        try:
            atomic_json(hub_dir / "status.json", {"status": "viewer-closed", "updated_at": time.time(),
                                                  "selected": ui["selected"], "settings": settings, "frames": ui["frames"]})
        except (OSError, ValueError) as exc:
            print(f"Observer status could not be saved: {exc}", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
