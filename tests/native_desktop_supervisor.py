#!/usr/bin/env python3
"""Launch an AgentDesktop build inside an EXISTING private Wayland display, never --session.

Run with bgui run OUTER -- python3 tests/native_desktop_supervisor.py --binary ...
--artifacts /absolute/new-directory. Read ready.json, use the saved scoped env with bgui,
inspect screenshots before input, then touch ARTIFACTS/stop. No host seat input is sent.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

parser = argparse.ArgumentParser()
parser.add_argument("--binary", type=Path, required=True)
parser.add_argument("--artifacts", type=Path, required=True)
parser.add_argument("--scale", type=float, choices=(1.0, 1.5, 2.0), default=1.0)
parser.add_argument("--origin-x", type=int, default=0)
parser.add_argument("--origin-y", type=int, default=0)
parser.add_argument("--parallel-windows", action="store_true",
                    help="Exercise two pinned window agents with a Russian second keyboard layout")
args = parser.parse_args()
if not os.environ.get("CODEX_BACKGROUND_GUI_SESSION"):
    parser.error("Run inside an owned isolated bgui session")
project = Path(__file__).resolve().parents[1]
helper = project / "scripts/bgui.py"
args.artifacts.mkdir(parents=True, exist_ok=False, mode=0o700)
runtime = Path(tempfile.mkdtemp(prefix="bgui-desktop-test-", dir=os.environ["XDG_RUNTIME_DIR"]))
processes, logs = [], []
sessions, client_env = [], None


def launch(argv, env, name):
    log = (args.artifacts / (name + ".log")).open("wb")
    logs.append(log)
    proc = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
    processes.append(proc)
    return proc


def cli(*arguments):
    proc = subprocess.run([sys.executable, str(helper), *arguments], env=client_env,
                          text=True, capture_output=True, timeout=15)
    if proc.returncode:
        raise RuntimeError(proc.stdout + proc.stderr)
    return json.loads(proc.stdout)


try:
    outer_display = Path(os.environ["WAYLAND_DISPLAY"])
    if not outer_display.is_absolute():
        outer_display = Path(os.environ["XDG_RUNTIME_DIR"]) / outer_display
    config = args.artifacts / "niri.kdl"
    config.write_text(f'output "winit" {{ mode "1440x900"; scale {args.scale}; position x={args.origin_x} y={args.origin_y}; }}\n' + '''
prefer-no-csd
animations { off; }
hotkey-overlay { skip-at-startup; }
layout { gaps 16; focus-ring { off; }; border { off; }; }
window-rule { match app-id="dev.codex.DesktopLeft"; open-floating true; open-focused false;
    default-floating-position x=40 y=150 relative-to="top-left";
}
window-rule { match app-id="dev.codex.DesktopRight"; open-floating true; open-focused false;
    default-floating-position x=680 y=150 relative-to="top-left";
}
window-rule { match app-id="dev.codex.DesktopUser"; open-floating true; open-focused true;
    default-floating-position x=1100 y=700 relative-to="top-left";
}
''')
    if args.parallel_windows:
        config.write_text(config.read_text().replace("x=680 y=150", "x=970 y=150")
                          + '\ninput { keyboard { xkb { layout "us,ru"; }; }; }\n'
                          + 'window-rule { match app-id="dev.codex.DesktopAlternate"; open-floating true; '
                            'open-focused false; default-floating-position x=40 y=700 relative-to="top-left"; }\n')
    compositor_env = os.environ.copy()
    compositor_env.update(XDG_RUNTIME_DIR=str(runtime), WAYLAND_DISPLAY=str(outer_display),
                          XDG_STATE_HOME=str(args.artifacts / "state"))
    for key in ("NIRI_SOCKET", "WAYLAND_SOCKET", "NOTIFY_SOCKET", "NIRI_AGENT_LAUNCH"):
        compositor_env.pop(key, None)
    compositor = launch([str(args.binary), "--config", str(config)], compositor_env, "niri")
    for _ in range(60):
        sockets = list(runtime.glob("niri.*.sock"))
        if sockets: break
        if compositor.poll() is not None: raise RuntimeError("Nested niri exited; inspect niri.log")
        time.sleep(.25)
    else: raise RuntimeError("Nested niri IPC timeout")
    endpoint = sockets[0]
    display = endpoint.name.removeprefix("niri.").removesuffix(f".{compositor.pid}.sock")
    client_env = compositor_env | {"WAYLAND_DISPLAY": display, "NIRI_SOCKET": str(endpoint)}
    client_env.pop("CODEX_BACKGROUND_GUI_SESSION", None)
    roles = ("left", "right", "alternate", "user") if args.parallel_windows else ("left", "right", "user", "layer")
    if args.parallel_windows:
        # Separate accessibility registry for the two inputs into one Wayland client.
        # Both socket and services belong to this fixture.
        atspi_socket = runtime / "accessibility-bus"
        address = "unix:path=" + str(atspi_socket)
        launch(["dbus-daemon", "--session", "--nofork", "--address=" + address], client_env, "a11y-bus")
        for _ in range(100):
            if atspi_socket.is_socket(): break
            time.sleep(.02)
        client_env.update(AT_SPI_BUS_ADDRESS=address, GTK_A11Y="atspi")
        registry = shutil.which("at-spi2-registryd") or "/usr/libexec/at-spi2-registryd"
        launch([registry], client_env, "a11y-registry")
    for role in roles:
        env = client_env | {"LD_PRELOAD": "libgtk4-layer-shell.so"} if role == "layer" else client_env
        launch([sys.executable, str(project / "tests/desktop_fixture.py"), "--role", role,
                "--result", str(args.artifacts / (role + ".json"))], env, role)
        time.sleep(.3)
    for _ in range(60):
        windows = cli("doctor", "--backend", "host-background")["windows"]
        if len(windows) >= (4 if args.parallel_windows else 3): break
        time.sleep(.25)
    else: raise RuntimeError("Fixture windows did not appear")
    selected = {k: client_env[k] for k in ("XDG_RUNTIME_DIR", "WAYLAND_DISPLAY", "NIRI_SOCKET",
                                          "DBUS_SESSION_BUS_ADDRESS", "XDG_STATE_HOME")}
    (args.artifacts / "test-env.json").write_text(json.dumps(selected, indent=2))
    ready = {"windows_before": windows, "private_runtime": str(runtime), "compositor_pid": compositor.pid}
    if args.parallel_windows:
        # This is the fixture's nested compositor, never the host seat.
        subprocess.run([str(args.binary), "msg", "action", "switch-layout", "next"],
                       env=client_env, check=True, timeout=5)
        ready["agents"] = {}
        for role in ("left", "right"):
            window = next(w for w in windows if w["app_id"] == "dev.codex.Desktop" + role.title())
            agent = cli("start", "--backend", "host-background", "--window", str(window["id"]),
                        "--name", role, "--parallel")
            sessions.append(agent["session"])
            ready["agents"][role] = {k: agent[k] for k in ("session", "color")}
            cli("screenshot", agent["session"], "--path", str(args.artifacts / (role + "-initial.png")))
    else:
        attachment = cli("start", "--backend", "host-desktop", "--name", "Глобальный курсор · проверка")
        session = attachment["session"]
        sessions.append(session)
        capture = cli("screenshot", session, "--output", "winit", "--path", str(args.artifacts / "initial.png"))
        ready.update(session=session, capture=capture)
    (args.artifacts / "ready.json").write_text(json.dumps(ready, ensure_ascii=False, indent=2))
    if args.parallel_windows:
        from native_parallel_check import check_parallel
        def type_as_user(text):
            subprocess.run(["wtype", "--", text], env=client_env, check=True, timeout=15)
            # Disconnecting the virtual keyboard restores the configured layout
            # asynchronously. Never retype the string while waiting for the change.
            for _ in range(30):
                layout = json.loads(subprocess.check_output(
                    [str(args.binary), "msg", "--json", "keyboard-layouts"], env=client_env, timeout=5))
                if layout["current_idx"] == 1:
                    break
                time.sleep(.05)
            else:
                raise AssertionError(layout)
        def focus_as_user(window_id):
            subprocess.run([str(args.binary), "msg", "action", "focus-window", "--id", str(window_id)],
                           env=client_env, check=True, timeout=5)
        result = check_parallel(cli, args.artifacts, ready, type_as_user, focus_as_user)
        # Capture the whole nested output while both per-window cursors are still visible.
        desktop_frame = args.artifacts / "desktop.png"
        subprocess.run([str(args.binary), "msg", "action", "screenshot-screen", "--show-pointer", "false",
                        "--path", str(desktop_frame)], env=client_env, check=True, timeout=5)
        from PIL import Image
        for _ in range(50):
            if desktop_frame.is_file():
                try:
                    with Image.open(desktop_frame) as image:
                        image.verify()
                    break
                except OSError:
                    pass
            time.sleep(.1)
        else:
            raise RuntimeError("Nested compositor did not finish the desktop screenshot")
        # The first agent now shares its *focused* GTK window with the user's seat.
        first = ready["agents"]["left"]["session"]
        wid = next(w["id"] for w in windows if w["app_id"] == "dev.codex.DesktopLeft")
        geometry = json.loads((args.artifacts / "left.geometry.json").read_text())
        frame = cli("screenshot", first)
        def click_control(widget):
            x, y = geometry[widget]
            return cli("click", first, str(round(x*frame["scale"] + frame["geometry_x"])),
                       str(round(y*frame["scale"] + frame["geometry_y"])))
        # Select with native input, then let the real user take window focus
        # between the agent's click and its next type command.
        assert "shared_action" not in click_control("entry")
        focus_as_user(wid)
        subprocess.run(["wtype", "-k", "F6"], env=client_env, check=True, timeout=5)
        type_as_user("before")
        assert cli("type", first, " Я")["shared_action"] == "insert"
        assert click_control("entry")["shared_action"] == "select-entry"
        assert cli("type", first, " ✓")["shared_action"] == "insert"
        version = json.loads(subprocess.check_output([str(args.binary), "msg", "--json", "version"],
                                                     env=client_env, timeout=5))
        if version["compositor"].endswith("-v4"):
            from concurrent.futures import ThreadPoolExecutor
            from threading import Event
            from native_parallel_check import check_click_outline
            sys.path.insert(0, str(project / "scripts"))
            from host_background import NiriIPC
            bounds = json.loads((args.artifacts/"left.bounds.json").read_text())["button"]
            def capture_edges(filename):
                # Read the nested compositor directly so CLI startup cannot miss
                # the short effect. These requests neither move nor click a pointer.
                path = args.artifacts / filename
                ipc = NiriIPC(client_env["NIRI_SOCKET"])
                frame = ipc.request({"AgentScreenshot": {"window_id": wid, "path": str(path),
                                     "show_pointer": True}})["AgentScreenshot"]
                return check_click_outline(dict(frame, path=path), bounds, (145, 92, 255))
            started = Event()
            def observe_press():
                started.set()
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    edges = capture_edges("outline-on.png")
                    if min(edges) >= 12: return edges
                    time.sleep(.06)
                raise AssertionError(("button outline missing", edges, bounds))
            with ThreadPoolExecutor(max_workers=1) as sampler:
                pending = sampler.submit(observe_press)
                started.wait(timeout=2)
                assert click_control("button")["shared_action"] == "invoke"
                visible = pending.result()
            time.sleep(1.3)
            expired = capture_edges("outline-off.png")
            assert max(expired) == 0, expired
            result["click_outline"] = {"visible": visible, "expired": expired}
        else:
            assert click_control("button")["shared_action"] == "invoke"
        type_as_user("after")
        for _ in range(40):
            received = json.loads((args.artifacts / "left.shared.json").read_text())
            if received["user"] == "beforeafter": break
            time.sleep(.05)
        assert received["user"] == "beforeafter" and " Я" in received["agent"], received
        assert json.loads((args.artifacts / "left.json").read_text())["text"] == received["agent"]
        assert next(w["id"] for w in cli("windows", first) if w["is_focused"]) == wid
        result["shared_window"] = received
        cli("screenshot", first, "--show-pointer", "--path", str(args.artifacts / "shared-after.png"))
        if version["compositor"].endswith(("-v3", "-v4")):
            # Leave the arrow over a flat patch so the renderer can be compared
            # against the original Xcursor mask independently of the text caret.
            x, y = frame["width"]*3//4, frame["height"]*9//10
            cli("move", first, str(x), str(y))
            time.sleep(1)
            cli("screenshot", first, "--path", str(args.artifacts/"cursor-background.png"))
            sample = cli("screenshot", first, "--show-pointer", "--path", str(args.artifacts/"cursor-theme.png"))
            result["cursor_sample"] = {"x": x, "y": y, "scale": sample["scale"]}
        (args.artifacts / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        (args.artifacts / "stop").touch()
    for _ in range(3000):
        if (args.artifacts / "stop").exists(): break
        if compositor.poll() is not None: raise RuntimeError("Nested niri exited during verification")
        time.sleep(.2)
    else: raise RuntimeError("Bounded ten-minute test lifetime expired")
except BaseException as exc:
    (args.artifacts / "error.txt").write_text(str(exc))
    raise
finally:
    for session in sessions:
        try:
            label = f"detach-{session}.json" if args.parallel_windows else "detach.json"
            (args.artifacts / label).write_text(json.dumps(cli("stop", session), indent=2))
        except Exception as exc:
            (args.artifacts / "detach-error.txt").write_text(str(exc))
    for proc in reversed(processes):
        if proc.poll() is None: proc.terminate()
        try: proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
    for log in logs: log.close()
    shutil.rmtree(runtime)
    (args.artifacts / "stopped").touch()
