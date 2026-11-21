"""Run on a private Xvfb display and a private AT-SPI registry. See the command below.

dbus-run-session -- xvfb-run -a sh -c 'export AT_SPI_BUS_ADDRESS="$DBUS_SESSION_BUS_ADDRESS" GDK_BACKEND=x11 GIO_USE_VFS=local GTK_A11Y=atspi; /usr/libexec/at-spi2-registryd & reg=$!; trap "kill $reg" EXIT; python3 tests/native_widget_focus.py'
"""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest import mock

import gi

TITLE = "Widget focus fixture"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def fixture(path):
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk
    win = Gtk.Window(title=TITLE)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    left, right = Gtk.Entry(), Gtk.Entry()
    button = Gtk.Button(label="Count")
    clicks = []
    def record(*_):
        path.write_text(json.dumps([left.get_text(), right.get_text(), len(clicks),
                                    win.get_focus() is right]))
    def clicked(*_):
        clicks.append(True)
        record()
    button.connect("clicked", clicked)
    for widget in (left, right):
        widget.connect("changed", record)
    for widget in (left, right, button):
        box.pack_start(widget, False, False, 0)
    win.add(box)
    win.show_all()
    win.present()
    left.grab_focus()
    Gtk.main()


def test():
    if not os.environ.get("XAUTHORITY", "").startswith("/tmp/xvfb-run."):
        raise RuntimeError("Use a private xvfb-run display")
    import bgui_widgets as widgets
    from bgui_widgets import actions, window_root
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "result.json"
        proc = subprocess.Popen([sys.executable, __file__, str(path)])
        try:
            def lookup():
                root = window_root(Atspi.get_desktop(0), proc.pid, TITLE)
                return next(item for item in actions(root, proc.pid, Atspi)
                            if item[0]["name"] == "Count" and item[0]["action"] == "click")
            for _ in range(60):
                try:
                    lookup()
                    break
                except (ValueError, StopIteration):
                    time.sleep(.1)
            else:
                raise RuntimeError("Missing fixture button")
            def keys(*args):
                subprocess.run(["xdotool", *args], check=True)
            ids = subprocess.check_output(["xdotool", "search", "--all", "--pid", str(proc.pid), "--name", TITLE], text=True).split()
            assert len(ids) == 1
            keys("windowfocus", "--sync", ids[0])
            # Exercise both CLI paths. Only niri discovery/feedback are simulated in Xvfb.
            client = mock.Mock()
            client.peer = [123, "fixture"]
            client.windows.return_value = [{"id": 7, "pid": proc.pid, "title": TITLE, "app_id": "fixture"}]
            client.request.return_value = "Handled"
            def cli(command, *extra):
                output = io.StringIO()
                with mock.patch.object(widgets.host, "host_endpoint", return_value=(client, {})), \
                     mock.patch.object(sys, "argv", ["widgets", command, "--window", "7", *extra]), \
                     redirect_stdout(output):
                    assert widgets.main() == 0
                return json.loads(output.getvalue())
            action = next(a for a in cli("inspect")["actions"] if a["name"] == "Count" and a["action"] == "click")
            keys("type", "left")
            keys("key", "Tab")
            keys("type", "before")
            result = cli("invoke", "--token", action["token"])
            assert result["indicator"] and result["invoked"] == action
            client.request.assert_called_once()
            time.sleep(.3)
            keys("type", "after")
            time.sleep(.2)
            result = json.loads(path.read_text())
            assert result == ["left", "beforeafter", 1, True], result
            print("User switched fields; agent invoked Count once; typing continued in the second field.")
        finally:
            proc.terminate()
            proc.wait(timeout=5)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        fixture(Path(sys.argv[1]))
    else:
        test()
