"""Desktop adapter contract: independent leases, monitor coordinates, no global fallback."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bgui
import host_background as host
import host_desktop as desktop
import test_host_background as fixtures


class DesktopTests(unittest.TestCase):
    def setUp(self):
        fixtures.HostBackgroundTests.setUp(self)
        self.active_outputs = {"LEFT": {"name": "LEFT", "current_mode": 0,
            "logical": {"x": -120, "y": 35, "width": 120, "height": 80, "scale": 2.0,
                        "transform": "Normal"}}}
        self.desktop_info = {"width": 240, "height": 160, "scale": 2.0,
                             "logical_x": -120, "logical_y": 35}
        self.stops = 0
        self.fail_input = False
        self.supported = True
        old_reply = self.fake.reply

        def reply(request):
            if request == "Outputs":
                return {"Ok": {"Outputs": self.active_outputs}}
            if request == "AgentDesktopStop":
                self.stops += 1
                return {"Ok": "Handled"}
            if isinstance(request, dict) and next(iter(request)).startswith("AgentDesktop"):
                name, value = next(iter(request.items()))
                if not self.supported:
                    return {"Err": "unknown variant"}
                if name == "AgentDesktopScreenshot":
                    if not value["output"]:
                        return {"Err": "no output named "}
                    Path(value["path"]).write_bytes(fixtures.png(240, 160))
                    return {"Ok": {"AgentDesktopScreenshot": self.desktop_info}}
                if name == "AgentDesktopInput":
                    self.fake.events.extend(value["events"])
                    return {"Err": "user is using this client"} if self.fail_input else {"Ok": "Handled"}
            return old_reply(request)
        self.fake.reply = reply

    tearDown = fixtures.HostBackgroundTests.tearDown
    action = fixtures.HostBackgroundTests.action

    def start(self):
        result = bgui.start(bgui.parser().parse_args(["start", "--backend", "host-desktop"]))
        self.session = result["session"]
        return result

    def test_monitor_pixels_map_to_negative_global_coordinates_and_detach(self):
        self.assertTrue(desktop.doctor()["ready"])
        self.start()
        self.action("screenshot", "--output", "LEFT")
        self.action("click", "100", "50")
        self.assertIn({"Motion": {"x": -70.0, "y": 60.0}}, self.fake.events)
        self.action("type", "Привет ☃")
        self.action("key", "ctrl+a", "Return")
        self.action("scroll", "--vertical", "120")
        stopped = self.action("stop")
        self.assertEqual(stopped["backend"], "host-desktop")
        self.assertIsNone(stopped["input_cleanup_error"])
        self.assertEqual(self.stops, 1)
        self.assertTrue(stopped["detached"])

    def test_refuses_changed_monitor_geometry_before_input(self):
        self.start()
        self.action("screenshot")
        self.active_outputs["LEFT"]["logical"]["scale"] = 1.5
        before = list(self.fake.events)
        with self.assertRaisesRegex(host.HostError, "layout changed"):
            self.action("click", "50", "50")
        self.assertEqual(self.fake.events, before)

    def test_monitor_metadata_and_bounds_are_checked(self):
        self.start()
        self.desktop_info["logical_x"] = 0
        with self.assertRaisesRegex(host.HostError, "origin/scale"):
            self.action("screenshot")
        self.desktop_info["logical_x"] = -120
        self.action("screenshot")
        before = list(self.fake.events)
        with self.assertRaises(host.HostError):
            self.action("click", "240", "0")
        self.assertEqual(self.fake.events, before)

    def test_screenshot_required_and_output_choice_explicit(self):
        self.start()
        with self.assertRaisesRegex(host.HostError, "screenshot"):
            self.action("click", "20", "20")
        self.active_outputs["RIGHT"] = dict(self.active_outputs["LEFT"], name="RIGHT")
        with self.assertRaisesRegex(host.HostError, "Choose --output"):
            self.action("screenshot")

    def test_failed_batch_is_not_retried_and_releases_pressed_input(self):
        self.start()
        self.action("screenshot")
        self.fail_input = True
        before = len(self.fake.events)
        with self.assertRaisesRegex(host.HostError, "user is using"):
            self.action("click", "20", "20")
        self.assertEqual(len(self.fake.events) - before, 3)
        self.assertEqual(self.stops, 1)

    def test_window_and_desktop_backends_share_one_compositor_lease(self):
        self.start()
        with self.assertRaisesRegex(host.HostError, "already attached"):
            bgui.start(bgui.parser().parse_args(["start", "--backend", "host-background"]))
        self.action("stop")
        result = bgui.start(bgui.parser().parse_args(["start", "--backend", "host-background"]))
        self.assertEqual(result["backend"], "host-background")

    def test_drag_releases_its_button_and_preserves_global_origin(self):
        self.start()
        self.action("screenshot")
        self.action("drag", "20", "20", "180", "100", "--duration", "0.1")
        self.assertEqual(self.fake.events[-1], {"Button": {"button": 272, "pressed": False}})
        self.assertIn({"Motion": {"x": -30.0, "y": 85.0}}, self.fake.events)

    def test_old_compositor_refused_without_cursor_or_input(self):
        self.supported = False
        self.assertFalse(desktop.doctor()["ready"])
        with self.assertRaises(host.HostError):
            self.start()
        self.assertFalse(self.fake.events)

    def test_hotkey_stops_desktop_attachment(self):
        self.start()
        result = host.stop_current(bgui)
        self.assertEqual(result["backend"], "host-desktop")
        self.assertEqual(self.stops, 1)
