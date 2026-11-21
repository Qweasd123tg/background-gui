# SPDX-License-Identifier: GPL-3.0-or-later
"""Protocol/lifecycle tests with an owned fake niri socket; no desktop required."""
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bgui
import host_background as host


def png(width, height):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\0" + b"\xff" * width * 4) * height)) + chunk(b"IEND", b""))


class FakeNiri:
    def __init__(self, directory):
        self.path = directory / "niri.sock"
        try:
            self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        except PermissionError:
            if os.environ.get("CI"):
                raise
            self.server = None
        if self.server is not None:
            self.server.bind(str(self.path))
            self.server.listen()
            self.server.settimeout(.1)
        else:
            self.path.touch()
        self.done = threading.Event()
        self.requests = []
        self.events = []
        self.parallel_supported = True
        self.patched = True
        self.malformed = None
        self.after_input = None
        self.info = {"width": 240, "height": 160, "scale": 2.0, "geometry_x": 8, "geometry_y": 8}
        self.windows = [
            {"id": 4, "pid": 1001, "app_id": "kitty", "title": "Agent", "workspace_id": 2,
             "is_focused": False, "layout": {"window_size": [112, 72]}},
            {"id": 5, "pid": 1002, "app_id": "editor", "title": "User", "is_focused": True},
        ]
        if self.server is not None:
            self.thread = threading.Thread(target=self.serve, daemon=True)
            self.thread.start()

    def reply(self, request):
        self.requests.append(request)
        if request == "Windows":
            return {"Ok": {"Windows": self.windows}}
        name, value = next(iter(request.items()))
        if not self.patched:
            return {"Err": f"unknown variant `{name}`"}
        if name == "AgentWindowBatch" and not self.parallel_supported:
            return {"Err": "unknown request variant"}
        if value["window_id"] == host.PROBE_ID:
            suffix = " on an output" if name == "AgentScreenshot" else ""
            return {"Err": f"no window with id {host.PROBE_ID}{suffix}"}
        if name == "AgentScreenshot":
            Path(value["path"]).write_bytes(png(240, 160))
            return {"Ok": {"AgentScreenshot": self.info}}
        if name in ("AgentInput", "AgentWindowBatch"):
            self.events.extend(value["events"])
            if self.after_input:
                self.after_input(value["events"])
            return {"Ok": "Handled"}
        raise AssertionError(request)

    def serve(self):
        while not self.done.is_set():
            try:
                connection, _ = self.server.accept()
            except socket.timeout:
                continue
            with connection, connection.makefile("rb") as stream:
                line = stream.readline()
                if not line:
                    continue
                request = json.loads(line)
                result = self.reply(request)
                raw = self.malformed if self.malformed is not None else json.dumps(result).encode() + b"\n"
                connection.sendall(raw)

    def close(self):
        self.done.set()
        if self.server is not None:
            self.thread.join(2)
            self.server.close()


class MemoryConnection:
    """Exercise the wire codec even where the executor forbids Unix sockets."""
    def __init__(self, compositor):
        self.compositor = compositor

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def settimeout(self, _):
        pass

    def connect(self, path):
        if path != str(self.compositor.path):
            raise OSError("Wrong socket")

    def getsockopt(self, *_):
        return struct.pack("3i", os.getpid(), os.getuid(), os.getgid())

    def sendall(self, data):
        self.request = json.loads(data)

    def shutdown(self, _):
        pass

    def makefile(self, _):
        reply = self.compositor.reply(self.request)
        return io.BytesIO(self.compositor.malformed if self.compositor.malformed is not None
                          else json.dumps(reply).encode() + b"\n")


class HostBackgroundTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="bgui-test-")
        self.path = Path(self.temporary.name)
        self.runtime = self.path / "runtime"
        self.runtime.mkdir()
        self.fake = FakeNiri(self.runtime)
        self.transports = contextlib.ExitStack()
        if self.fake.server is None:
            (self.runtime / "wayland-0").touch()
            self.transports.enter_context(mock.patch.object(host.socket, "socket", side_effect=lambda *_: MemoryConnection(self.fake)))
            self.transports.enter_context(mock.patch.object(host, "socket_identity",
                side_effect=lambda p: [Path(p).stat().st_dev, Path(p).stat().st_ino]))
            self.transports.enter_context(mock.patch.object(host, "peer_identity", return_value=[1234, "100", "test-boot"]))
            self.wayland = None
        else:
            self.wayland = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.wayland.bind(str(self.runtime / "wayland-0"))
        self.env = mock.patch.dict(os.environ, {
            "XDG_STATE_HOME": str(self.path / "state"), "XDG_RUNTIME_DIR": str(self.runtime),
            "WAYLAND_DISPLAY": "wayland-0", "NIRI_SOCKET": str(self.fake.path),
            "CODEX_BACKGROUND_GUI_SESSION": "", "WAYLAND_SOCKET": "",
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.transports.close()
        self.fake.close()
        if self.wayland is not None:
            self.wayland.close()
        self.temporary.cleanup()

    def start(self, window=4):
        args = bgui.parser().parse_args(["start", "--backend", "host-background"] +
                                       (["--window", str(window)] if window else []))
        result = bgui.start(args)
        self.session = result["session"]
        return result

    def action(self, *arguments):
        return bgui.operate(bgui.parser().parse_args([arguments[0], self.session, *arguments[1:]]))

    def test_stale_login_socket_recovers_only_matching_managed_peer(self):
        stale = self.runtime / "niri.old.sock"
        manager = (f"XDG_RUNTIME_DIR={self.runtime}\nWAYLAND_DISPLAY=wayland-0\n"
                   f"NIRI_SOCKET={self.fake.path}\nDBUS_SESSION_BUS_ADDRESS=unix:path=test\n"
                   "PRIVATE_TEST_SECRET=must-not-be-copied\n")
        pid = 1234 if self.fake.server is None else os.getpid()
        replies = [subprocess.CompletedProcess([], 0, manager, ""),
                   subprocess.CompletedProcess([], 0, f"{pid}\n0\n", "")]
        with mock.patch.dict(os.environ, NIRI_SOCKET=str(stale)), mock.patch.object(host.subprocess, "run", side_effect=replies):
            client, env = host.host_endpoint()
        self.assertEqual(client.path, str(self.fake.path))
        self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"], "unix:path=test")
        self.assertNotIn("PRIVATE_TEST_SECRET", env)
        self.assertNotIn("PRIVATE_TEST_SECRET", os.environ)
        replies = [subprocess.CompletedProcess([], 0, manager, ""),
                   subprocess.CompletedProcess([], 0, "999999999\n0\n", "")]
        with mock.patch.dict(os.environ, NIRI_SOCKET=str(stale)), mock.patch.object(host.subprocess, "run", side_effect=replies):
            with self.assertRaisesRegex(host.HostError, "active niri service"):
                host.host_endpoint()

    def test_full_window_input_and_detach_contract(self):
        self.assertTrue(host.doctor()["ready"])
        self.start()
        screenshot = self.action("screenshot")
        self.assertEqual(screenshot["coordinate_space"], "window-image-pixels")
        self.assertEqual(Path(screenshot["path"]).stat().st_mode & 0o777, 0o600)
        self.action("click", "48", "28")
        self.assertEqual(self.fake.events[:3], [
            {"Motion": {"x": 20.0, "y": 10.0}},
            {"Button": {"button": 272, "pressed": True}},
            {"Button": {"button": 272, "pressed": False}},
        ])
        self.action("scroll", "--vertical", "80")
        self.action("type", "Привет 🙂")
        self.action("key", "ctrl+a", "Return")
        self.assertTrue(self.action("status")["running"])
        with mock.patch.object(bgui, "terminate_group", side_effect=AssertionError("host process signaled")):
            self.assertTrue(self.action("stop")["detached"])
            self.assertTrue(self.action("stop")["detached"])
        self.assertEqual(self.fake.events[-2:], ["PointerLeave", "KeyboardLeave"])
        self.assertFalse(self.action("status")["running"])
        with self.assertRaisesRegex(host.HostError, "stopped"):
            self.action("type", "should not reach desktop")
        probe = {"AgentWindowBatch": {"window_id": host.PROBE_ID, "color": 0, "events": []}}
        self.assertTrue(all(r == "Windows" or r == probe or next(iter(r)) in ("AgentInput", "AgentScreenshot")
                            for r in self.fake.requests))

    def test_stock_niri_refused_without_input_or_screenshot(self):
        self.fake.patched = False
        self.assertFalse(host.doctor()["ready"])
        with self.assertRaisesRegex(host.HostError, "patch"):
            self.start()
        self.assertEqual(self.fake.events, [])
        self.assertTrue(all(r["AgentInput"]["window_id"] == host.PROBE_ID for r in self.fake.requests))

    def test_no_target_launch_then_select_without_focus_action(self):
        self.assertIsNone(self.start(window=None)["window"])
        self.assertTrue(self.action("status")["running"])
        with self.assertRaisesRegex(host.HostError, "No target"):
            self.action("type", "text")
        selected = self.action("focus", "4")
        self.assertFalse(selected["host_focus_changed"])
        self.action("type", "okay")

    def test_host_launch_environment_and_detach_do_not_signal_apps(self):
        self.start(window=None)
        args = bgui.parser().parse_args(["launch", self.session])
        args.argv = ["example-app"]
        with mock.patch.object(bgui, "launch_command", return_value={"launched": True}) as launch:
            self.assertTrue(bgui.operate(args)["launched"])
        env = launch.call_args.args[-1]
        self.assertEqual(env["NIRI_AGENT_LAUNCH"], "1")
        self.assertEqual(env["WAYLAND_DISPLAY"], str(self.runtime / "wayland-0"))
        self.assertEqual(env["CODEX_BACKGROUND_GUI_SESSION"], self.session)
        self.assertNotIn("XDG_ACTIVATION_TOKEN", env)

    def test_requires_current_screenshot_for_pointer_and_new_target(self):
        self.start()
        with self.assertRaisesRegex(host.HostError, "screenshot"):
            self.action("click", "48", "28")
        self.action("screenshot")
        self.action("focus", "4")
        with self.assertRaisesRegex(host.HostError, "screenshot"):
            self.action("move", "48", "28")
        self.assertEqual(self.fake.events, [])

    def test_focused_target_and_same_process_refused(self):
        for same_process in (False, True):
            with self.subTest(same_process=same_process):
                self.fake.windows[0]["is_focused"] = not same_process
                self.fake.windows[1]["pid"] = 1001 if same_process else 1002
                with self.assertRaisesRegex(host.HostError, "user is focused"):
                    self.start()
        self.assertEqual(self.fake.events, [])

    def test_user_focus_after_attach_blocks_input(self):
        self.start()
        self.fake.windows[1]["pid"] = 1001
        with self.assertRaisesRegex(host.HostError, "user is focused"):
            self.action("type", "blocked")
        self.assertEqual(self.fake.events, [])

    def test_closed_or_reused_window_refused(self):
        self.start()
        self.fake.windows[0]["pid"] = 4000
        with self.assertRaisesRegex(host.HostError, "identity changed"):
            self.action("key", "Return")
        self.fake.windows.pop(0)
        with self.assertRaisesRegex(host.HostError, "closed"):
            self.action("screenshot")

    def test_resized_window_refused(self):
        self.start()
        self.action("screenshot")
        self.fake.windows[0]["layout"]["window_size"] = [220, 70]
        with self.assertRaisesRegex(host.HostError, "resized"):
            self.action("click", "48", "28")

    def test_invalid_coordinates_scroll_keys_and_text_send_nothing(self):
        self.start()
        self.action("screenshot")
        for args in (("click", "-1", "20"), ("move", "240", "20"),
                     ("scroll", "--vertical", "nan"), ("scroll", "--vertical", "1"),
                     ("key", "ctrl+unsupported"), ("type", "x" * 4097),
                     ("drag", "48", "28", "60", "40", "--duration", "nan")):
            with self.subTest(args=args[:2]), self.assertRaises(host.HostError):
                self.action(*args)
        self.assertEqual(self.fake.events, [])

    def test_unicode_method_selection_and_override(self):
        self.start()
        self.action("type", "Привет")
        self.assertEqual(self.fake.events[-1]["Text"]["unicode_input"], "Keymap")
        self.fake.windows[0]["app_id"] = "chromium"
        self.action("focus", "4")
        self.action("type", "Привет")
        self.assertEqual(self.fake.events[-1]["Text"]["unicode_input"], "CtrlShiftU")
        self.action("type", "hello", "--unicode-input", "Keymap")
        self.assertEqual(self.fake.events[-1]["Text"]["unicode_input"], "Keymap")

    def test_unicode_auto_matches_app_components_without_codex_false_positive(self):
        self.start()
        cases = {
            "dev.codex.BackgroundGuiHostAgent": "Keymap",
            "org.example.knowledge": "Keymap",
            "org.gnome.TextEditor": "Keymap",
            "org.chromium.Chromium": "CtrlShiftU",
            "google-chrome": "CtrlShiftU",
            "microsoft-edge": "CtrlShiftU",
            "com.visualstudio.code": "CtrlShiftU",
            "code-oss": "CtrlShiftU",
            "com.vscodium.codium": "CtrlShiftU",
            "com.discordapp.Discord": "CtrlShiftU",
        }
        for app_id, expected in cases.items():
            with self.subTest(app_id=app_id):
                self.fake.windows[0]["app_id"] = app_id
                self.action("focus", "4")
                self.action("type", "Привет")
                self.assertEqual(self.fake.events[-1]["Text"]["unicode_input"], expected)

    def test_v1_accepts_exclusive_mode_and_refuses_parallel_input(self):
        self.fake.parallel_supported = False
        self.assertFalse(host.doctor()["parallel_windows"])
        args = bgui.parser().parse_args(["start", "--backend", "host-background", "--window", "4", "--parallel"])
        with self.assertRaisesRegex(host.HostError, "requires.*v2"):
            bgui.start(args)
        self.assertFalse(self.fake.events)
        self.start()

    def test_parallel_targets_keep_distinct_leases_and_colors(self):
        self.fake.windows.append(dict(self.fake.windows[0], id=6, pid=1003, app_id="second"))
        def attach(window):
            return bgui.start(bgui.parser().parse_args(
                ["start", "--backend", "host-background", "--parallel", "--window", str(window)]))
        first, second = attach(4), attach(6)
        self.assertNotEqual(first["color"], second["color"])
        with self.assertRaisesRegex(host.HostError, "owns this"):
            attach(4)
        with self.assertRaisesRegex(host.HostError, "already attached"):
            self.start()
        self.session = second["session"]
        with self.assertRaisesRegex(host.HostError, "pinned"):
            self.action("focus", "4")
        self.session = first["session"]
        self.action("stop")
        self.session = second["session"]
        self.action("type", "hello")
        batch = self.fake.requests[-1]["AgentWindowBatch"]
        self.assertEqual(batch["window_id"], 6)
        self.assertEqual(batch["color"], 1)
        third = attach(4)
        self.assertEqual(third["color"], first["color"])
        self.assertEqual(len(host.stop_current(bgui)["sessions"]), 2)
        self.start()

    def test_border_resize_during_capture_publishes_only_stable_geometry(self):
        self.start()
        original = self.fake.reply
        calls = []
        def resize_once(request):
            result = original(request)
            if isinstance(request, dict) and "AgentScreenshot" in request:
                calls.append(request)
                if len(calls) == 1:
                    self.fake.windows[0]["layout"]["window_size"] = [108, 68]
            return result
        self.fake.reply = resize_once
        self.action("screenshot")
        self.action("click", "10", "10")
        self.assertEqual(len(calls), 2)

    def test_inspected_click_refuses_changed_image_and_title(self):
        self.start()
        image = self.action("screenshot")["path"]
        self.action("click", "10", "10", "--seen", image)
        events = self.fake.events[:]
        self.fake.windows[0]["title"] = "another conversation"
        with self.assertRaisesRegex(host.HostError, "title changed"):
            self.action("click", "10", "10", "--seen", image)
        self.fake.windows[0]["title"] = "Agent"
        with mock.patch(__name__ + ".png", return_value=Path(image).read_bytes() + b"changed"):
            with self.assertRaisesRegex(host.HostError, "contents changed"):
                self.action("click", "10", "10", "--seen", image)
        self.assertEqual(self.fake.events, events)

    def test_single_compositor_attachment_and_lease_release(self):
        self.start()
        previous = self.session
        with self.assertRaisesRegex(host.HostError, "already attached"):
            self.start()
        self.session = previous
        self.action("stop")
        self.assertNotEqual(self.start()["session"], previous)

    def test_stop_host_hotkey_target(self):
        self.start()
        self.assertTrue(host.stop_current(bgui)["detached"])
        self.assertTrue(host.stop_current(bgui)["detached"])
        with self.assertRaisesRegex(host.HostError, "stopped"):
            self.action("type", "blocked")

    def test_launch_and_exec_shared_runner(self):
        self.start(window=None)
        args = bgui.parser().parse_args(["exec", self.session])
        args.argv = [sys.executable, "-c", "import os, time; print(os.environ['NIRI_AGENT_LAUNCH']); time.sleep(.2)"]
        if self.fake.server is None:
            log = self.path / "exec.log"
            log.write_text("1\n")
            process = mock.Mock()
            process.wait.return_value = 0
            with mock.patch.object(bgui, "spawn_owned", return_value=(process, {"log": str(log)})):
                result = bgui.operate(args)
        else:
            result = bgui.operate(args)
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["output"].strip(), "1")

    def test_socket_and_peer_replacement_refused(self):
        self.start()
        path = bgui.session_dir(self.session)
        state = bgui.read_state(path)
        state["socket_identity"][1] += 1
        bgui.write_state(path, state)
        with self.assertRaisesRegex(host.HostError, "socket changed"):
            self.action("type", "blocked")
        state["socket_identity"][1] -= 1
        state["peer_identity"][0] += 1
        bgui.write_state(path, state)
        # Avoid causing the fixture server to read EOF as JSON on this rejected connection.
        with mock.patch.object(host, "peer_identity", return_value=list(state["peer_identity"])):
            state["peer_identity"][0] += 1
            bgui.write_state(path, state)
            with self.assertRaisesRegex(host.HostError, "process changed"):
                self.action("type", "blocked")

    def test_invalid_screenshot_metadata_and_existing_path(self):
        self.start()
        self.fake.info["scale"] = 0
        with self.assertRaisesRegex(host.HostError, "metadata"):
            self.action("screenshot")
        state = bgui.read_state(bgui.session_dir(self.session))
        self.assertNotIn("capture", state)
        destination = self.path / "existing.png"
        destination.write_bytes(b"keep")
        with self.assertRaisesRegex(host.HostError, "already exist"):
            self.action("screenshot", "--path", str(destination))
        self.assertEqual(destination.read_bytes(), b"keep")

    def test_drag_interpolates_and_releases_on_stop_request(self):
        self.start()
        self.action("screenshot")
        self.action("drag", "48", "28", "88", "68", "--duration", ".1")
        self.assertEqual(self.fake.events[-1], {"Button": {"button": 272, "pressed": False}})
        self.assertEqual(self.fake.events[-2], {"Motion": {"x": 40.0, "y": 30.0}})
        def cancel(events):
            if any(isinstance(e, dict) and e.get("Button", {}).get("pressed") for e in events):
                (bgui.session_dir(self.session) / "stop.requested").touch()
        self.fake.after_input = cancel
        with self.assertRaisesRegex(host.HostError, "stopped"):
            self.action("drag", "48", "28", "88", "68", "--duration", ".1")
        self.assertEqual(self.fake.events[-1], {"Button": {"button": 272, "pressed": False}})

    def test_malformed_ipc_and_nested_environment_refused(self):
        self.fake.malformed = b"{}\n"
        self.assertFalse(host.doctor()["ready"])
        self.fake.malformed = None
        with mock.patch.dict(os.environ, {"CODEX_BACKGROUND_GUI_SESSION": "private-session"}):
            self.assertFalse(host.doctor()["ready"])

    def test_cli_process_smoke(self):
        if self.fake.server is None:
            self.skipTest("Executor forbids Unix sockets; real-process IPC runs in CI")
        script = str(Path(bgui.__file__))
        def cli(*args):
            result = subprocess.run([sys.executable, script, *args], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads(result.stdout)
        session = cli("start", "--backend", "host-background", "--window", "4")["session"]
        cli("screenshot", session)
        cli("click", session, "48", "28")
        cli("key", session, "ctrl+a")
        cli("type", session, "CLI 🙂")
        cli("stop", session)

    def test_cli_main_smoke(self):
        def cli(*args):
            output = io.StringIO()
            with mock.patch.object(sys, "argv", [str(Path(bgui.__file__)), *args]), contextlib.redirect_stdout(output):
                self.assertEqual(bgui.main(), 0, output.getvalue())
            return json.loads(output.getvalue())
        session = cli("start", "--backend", "host-background", "--window", "4")["session"]
        cli("screenshot", session)
        cli("click", session, "48", "28")
        cli("type", session, "test")
        cli("stop", session)


class LegacyDispatchTests(unittest.TestCase):
    def test_default_backend_and_legacy_state_stay_isolated(self):
        self.assertEqual(bgui.parser().parse_args(["start"]).backend, "isolated")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            state = {"id": "old-session", "status": "running", "processes": []}
            @contextlib.contextmanager
            def locked(_):
                yield path, state
            with mock.patch.object(bgui, "locked_session", locked), \
                 mock.patch.object(bgui, "isolated_env", return_value={}) as isolated, \
                 mock.patch.object(bgui, "niri_json", return_value=[{"id": 1}]):
                self.assertEqual(bgui.operate(bgui.parser().parse_args(["windows", "old-session"])), [{"id": 1}])
                isolated.assert_called_once_with(state)


if __name__ == "__main__":
    unittest.main()
