#!/usr/bin/env python3
"""Disposable native clients for the desktop pointer's manual visual smoke test."""
import argparse
import json
from pathlib import Path
import gi

parser = argparse.ArgumentParser()
parser.add_argument("--role", choices=("left", "right", "user", "layer"), required=True)
parser.add_argument("--result", type=Path, required=True)
args = parser.parse_args()
gi.require_version("Gtk", "4.0")
if args.role == "layer":
    gi.require_version("Gtk4LayerShell", "1.0")
    from gi.repository import Gtk4LayerShell
from gi.repository import Gtk, Gio


class Fixture(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="dev.codex.Desktop" + args.role.title(),
                         flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.connect("activate", self.open_window)

    def open_window(self, app):
        window = Gtk.ApplicationWindow(application=app, title="Desktop test · " + args.role)
        window.set_default_size(520 if args.role != "user" else 280, 430 if args.role != "user" else 160)
        if args.role == "layer":
            Gtk4LayerShell.init_for_window(window)
            Gtk4LayerShell.set_namespace(window, "bgui-desktop-test-panel")
            Gtk4LayerShell.set_layer(window, Gtk4LayerShell.Layer.TOP)
            for edge in (Gtk4LayerShell.Edge.TOP, Gtk4LayerShell.Edge.LEFT, Gtk4LayerShell.Edge.RIGHT):
                Gtk4LayerShell.set_anchor(window, edge, True)
            Gtk4LayerShell.set_keyboard_mode(window, Gtk4LayerShell.KeyboardMode.NONE)
            window.set_default_size(0, 60)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20,
                      margin_top=24, margin_bottom=24, margin_start=32, margin_end=32)
        window.set_child(box)
        heading = Gtk.Label(label="Рабочий стол · " + args.role, xalign=0)
        heading.add_css_class("title-1")
        box.append(heading)
        if args.role == "user":
            box.append(Gtk.Entry(placeholder_text="Фокус пользователя остаётся здесь"))
        elif args.role == "layer":
            button = Gtk.Button(label="Панель · проверить нажатие")
            button.connect("clicked", lambda _: args.result.write_text(json.dumps({"layer_clicked": True})))
            box.append(button)
        else:
            entry = Gtk.Entry(placeholder_text="Ввод независимой мышью")
            box.append(entry)
            button = Gtk.Button(label="Проверить ввод")
            box.append(button)
            result = Gtk.Label(label="Ожидание", xalign=0)
            box.append(result)
            def save(_):
                data = {"role": args.role, "text": entry.get_text(), "clicked": True}
                args.result.write_text(json.dumps(data, ensure_ascii=False))
                result.set_text("Готово: " + entry.get_text())
            button.connect("clicked", save)
            menu = Gtk.MenuButton(label="Открыть всплывающее меню")
            popover = Gtk.Popover()
            popup_button = Gtk.Button(label="Проверить popup")
            def popup_clicked(_):
                args.result.with_suffix(".popup.json").write_text('{"popup_clicked":true}')
                popover.popdown()
            popup_button.connect("clicked", popup_clicked)
            popover.set_child(popup_button)
            menu.set_popover(popover)
            box.append(menu)
        window.present()


Fixture().run([])
