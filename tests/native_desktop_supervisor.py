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
args = parser.parse_args()
if not os.environ.get("CODEX_BACKGROUND_GUI_SESSION"):
    parser.error("Run inside an owned isolated bgui session")
project = Path(__file__).resolve().parents[1]
helper = project / "scripts/bgui.py"
args.artifacts.mkdir(parents=True, exist_ok=False, mode=0o700)
runtime = Path(tempfile.mkdtemp(prefix="bgui-desktop-test-", dir=os.environ["XDG_RUNTIME_DIR"]))
processes, logs = [], []
session, client_env = None, None


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
    for role in ("left", "right", "user", "layer"):
        env = client_env | {"LD_PRELOAD": "libgtk4-layer-shell.so"} if role == "layer" else client_env
        launch([sys.executable, str(project / "tests/desktop_fixture.py"), "--role", role,
                "--result", str(args.artifacts / (role + ".json"))], env, role)
        time.sleep(.3)
    for _ in range(60):
        windows = cli("doctor", "--backend", "host-background")["windows"]
        if len(windows) >= 3: break
        time.sleep(.25)
    else: raise RuntimeError("Fixture windows did not appear")
    attachment = cli("start", "--backend", "host-desktop", "--name", "Глобальный курсор · проверка")
    session = attachment["session"]
    selected = {k: client_env[k] for k in ("XDG_RUNTIME_DIR", "WAYLAND_DISPLAY", "NIRI_SOCKET",
                                          "DBUS_SESSION_BUS_ADDRESS", "XDG_STATE_HOME")}
    (args.artifacts / "test-env.json").write_text(json.dumps(selected, indent=2))
    capture = cli("screenshot", session, "--output", "winit", "--path", str(args.artifacts / "initial.png"))
    ready = {"session": session, "windows_before": windows, "capture": capture,
             "private_runtime": str(runtime), "compositor_pid": compositor.pid}
    (args.artifacts / "ready.json").write_text(json.dumps(ready, ensure_ascii=False, indent=2))
    for _ in range(3000):
        if (args.artifacts / "stop").exists(): break
        if compositor.poll() is not None: raise RuntimeError("Nested niri exited during verification")
        time.sleep(.2)
    else: raise RuntimeError("Bounded ten-minute test lifetime expired")
except BaseException as exc:
    (args.artifacts / "error.txt").write_text(str(exc))
    raise
finally:
    if session:
        try:
            (args.artifacts / "detach.json").write_text(json.dumps(cli("stop", session), indent=2))
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
