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
            # Ordinary bgui commands route to the selected widget even with real
            # focus in the other entry in this very window.
            import host_background as host
            import bgui as core
            win = client.windows.return_value[0]
            win.update(is_focused=True, layout={"window_size": [400, 200]})
            state = {"id": "shared", "target": host.pin(win), "host_env": {}, "color": 1,
                     "capture": {"window_id": 7, "window_size": [400, 200], "width": 800,
                                 "height": 600, "scale": 1, "geometry_x": 0, "geometry_y": 0}}
            def normal(*argv):
                return host.input_action(core.parser().parse_args([argv[0], "shared", *argv[1:]]),
                                         Path(tmp), state, client)
            root = window_root(Atspi.get_desktop(0), proc.pid, TITLE)
            # Firefox with a CSD shadow reports its root and link in surface
            # coordinates. Exercise the two directions using the observed inset.
            from types import SimpleNamespace
            frame, link = mock.Mock(), mock.Mock()
            frame.path, link.path = "/window", "/new"
            frame.app.bus_name = link.app.bus_name = ":test"
            frame.get_extents.return_value = SimpleNamespace(x=20, y=20, width=702, height=903)
            link.get_extents.return_value = SimpleNamespace(x=50, y=130, width=23, height=16)
            frame.get_accessible_at_point.return_value = link
            link.get_accessible_at_point.return_value = None
            link.get_parent.return_value = frame
            link.get_role.return_value = Atspi.Role.LINK
            link.get_state_set.return_value.contains.side_effect = lambda flag: flag == Atspi.StateType.SENSITIVE
            with mock.patch.object(widgets, "window_root", return_value=frame):
                _, bounds = widgets.inspect_click(state, win, {"x": 41, "y": 118})
            frame.get_accessible_at_point.assert_called_once_with(61, 138, Atspi.CoordType.WINDOW)
            assert bounds == [30, 110, 23, 16], bounds
            def descendants(node):
                yield node
                for child in widgets.children(node):
                    yield from descendants(child)
            controls = list(descendants(root))
            first = next(n for n in controls if n.get_role() == Atspi.Role.TEXT and Atspi.Text.get_text(n, 0, -1) == "left")
            def center(n):
                b = n.get_extents(Atspi.CoordType.WINDOW)
                return [str(b.x+b.width-3), str(b.y+b.height//2)]
            assert normal("click", *center(first))["shared_action"] == "select-entry"
            assert normal("type", "Фокус ✓")["shared_action"] == "insert"
            count = next(n for n in controls if n.get_name() == "Count")
            assert normal("click", *center(count))["shared_action"] == "invoke"
            keys("type", "--", "-middle")
            assert json.loads(path.read_text()) == ["leftФокус ✓", "before-middle", 1, True]
            client.request.reset_mock()
            result = cli("invoke", "--token", action["token"])
            assert result["indicator"] and result["invoked"] == action
            client.request.assert_called_once()
            time.sleep(.3)
            keys("type", "after")
            time.sleep(.2)
            result = json.loads(path.read_text())
            assert result == ["leftФокус ✓", "before-middleafter", 2, True], result
            print("User switched fields; agent invoked Count once; typing continued in the second field.")
        finally:
            proc.terminate()
            proc.wait(timeout=5)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        fixture(Path(sys.argv[1]))
    else:
        test()
