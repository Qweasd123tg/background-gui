# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise login startup without contacting the real session manager."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


LAUNCHER = Path(__file__).resolve().parents[1] / "examples/niri-agent-session"


class SessionLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="bgui-login-test-")
        self.root = Path(self.temporary.name)
        self.install = self.root / "install"
        (self.install / "bin").mkdir(parents=True)
        self.binary = self.install / "bin/niri"
        self.binary.write_text('#!/bin/sh\nprintf "niri %s\\n" "$*" >> "$BGUI_TEST_LOG"\n')
        self.binary.chmod(0o755)
        (self.install / "build.json").write_text(json.dumps({
            "binary_sha256": hashlib.sha256(self.binary.read_bytes()).hexdigest(),
            "integration_commit": "test-fixture",
        }))
        self.commands = self.root / "commands"
        self.commands.mkdir()
        self.log = self.root / "commands.log"
        systemctl = self.commands / "systemctl"
        systemctl.write_text('''#!/bin/sh
printf 'systemctl %s\n' "$*" >> "$BGUI_TEST_LOG"
case "$*" in
  "--user --quiet is-active "*)
    [ "${BGUI_TEST_ACTIVE:-}" = "${4:-}" ] && exit 0
    exit 3 ;;
  "--user reset-failed "*)
    echo 'Unit niri-agent.service not loaded.' >&2
    exit 1 ;;
  "--user --wait start "*) exit "${BGUI_TEST_START_STATUS:-0}" ;;
  *) exit 0 ;;
esac
''')
        systemctl.chmod(0o755)
        dbus = self.commands / "dbus-update-activation-environment"
        dbus.write_text('#!/bin/sh\nprintf "dbus %s\\n" "$*" >> "$BGUI_TEST_LOG"\n')
        dbus.chmod(0o755)
        self.env = os.environ | {
            "HOME": str(self.root),
            "PATH": str(self.commands) + os.pathsep + os.environ["PATH"],
            "BGUI_NIRI_INSTALL_DIR": str(self.install),
            "BGUI_NIRI_CONFIG": str(self.root / "agent-input.kdl"),
            "BGUI_NIRI_SYSTEMD_UNIT": "niri-agent.service",
            "BGUI_TEST_LOG": str(self.log),
            "BGUI_TEST_ACTIVE": "",
            "BGUI_TEST_START_STATUS": "0",
        }

    def tearDown(self):
        self.temporary.cleanup()

    def run_launcher(self):
        result = subprocess.run(["bash", str(LAUNCHER), "--login"], env=self.env,
                                text=True, capture_output=True, timeout=10)
        commands = self.log.read_text() if self.log.exists() else ""
        return result, commands

    def test_first_login_starts_unit_after_reset_of_unloaded_unit_fails(self):
        result, commands = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("systemctl --user reset-failed niri-agent.service", commands)
        self.assertIn("systemctl --user --wait start niri-agent.service", commands)
        self.assertIn("niri-shutdown.target", commands)

    def test_default_installation_uses_current_version_symlink(self):
        self.env.pop("BGUI_NIRI_INSTALL_DIR")
        current = self.root / ".local/lib/background-gui-host/current"
        current.parent.mkdir(parents=True)
        current.symlink_to(self.install, target_is_directory=True)
        result, commands = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("--wait start niri-agent.service", commands)

    def test_actual_start_failure_is_reported(self):
        self.env["BGUI_TEST_START_STATUS"] = "9"
        result, commands = self.run_launcher()
        self.assertEqual(result.returncode, 9, result.stdout + result.stderr)
        self.assertIn("systemctl --user --wait start niri-agent.service", commands)
        self.assertIn("niri-shutdown.target", commands)

    def test_running_desktop_is_not_replaced(self):
        self.env["BGUI_TEST_ACTIVE"] = "niri.service"
        result, commands = self.run_launcher()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Already running: niri.service", result.stderr)
        self.assertNotIn("import-environment", commands)
        self.assertNotIn("--wait start", commands)
        self.assertNotIn("niri-shutdown.target", commands)

    def test_invalid_binary_digest_prevents_start(self):
        self.binary.write_text(self.binary.read_text() + "# changed\n")
        result, commands = self.run_launcher()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("digest mismatch", result.stderr)
        self.assertNotIn("import-environment", commands)
        self.assertNotIn("--wait start", commands)

    def test_prebuilt_binary_without_optional_manifest_can_start(self):
        (self.install / "build.json").unlink()
        result, commands = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("systemctl --user --wait start niri-agent.service", commands)


if __name__ == "__main__":
    unittest.main()
