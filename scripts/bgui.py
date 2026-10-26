#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Control an owned headless Weston -> niri session, without host input fallback."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
import uuid

ASSETS = Path(__file__).resolve().parent.parent / "assets"
VIEWER_APP_ID = "dev.codex.BackgroundGuiViewer"
RUNTIME_TOOLS = ("weston", "niri", "wtype", "dbus-daemon", "dbus-update-activation-environment")
BUILD_TOOLS = ("wayland-scanner", "cc", "pkg-config")
SESSION_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{5,60}\Z")
DROP_ENV = (
    "DISPLAY", "WAYLAND_DISPLAY", "WAYLAND_SOCKET", "NIRI_SOCKET", "NIRI_CONFIG",
    "XDG_ACTIVATION_TOKEN", "DESKTOP_STARTUP_ID", "SESSION_MANAGER",
    "DBUS_SESSION_BUS_ADDRESS", "DBUS_SESSION_BUS_PID", "DBUS_STARTER_ADDRESS",
    "DBUS_STARTER_BUS_TYPE",
)


class GuiError(Exception):
    pass


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def session_name(value):
    name = value.strip()
    if not name or len(name) > 160 or not value.isprintable():
        raise GuiError("Name must be a nonempty single-line label of at most 160 characters")
    return name


def root_dir():
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "codex-background-gui"


def private_dir(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.stat().st_uid != os.getuid():
        raise GuiError(f"Expected an owned directory: {path}")
    path.chmod(0o700)
    return path


def session_dir(session):
    if not SESSION_PATTERN.fullmatch(session):
        raise GuiError("Use the session ID returned by start")
    path = root_dir() / "sessions" / session
    if not path.is_dir() or path.is_symlink() or path.stat().st_uid != os.getuid():
        raise GuiError(f"Session does not exist or is not owned: {session}")
    return path


def write_state(path, state):
    temporary = path / "state.json.tmp"
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    temporary.chmod(0o600)
    temporary.replace(path / "state.json")


def read_state(path):
    try:
        state = json.loads((path / "state.json").read_text())
    except (OSError, ValueError) as exc:
        raise GuiError(f"Cannot read session state: {path}") from exc
    if state.get("id") != path.name:
        raise GuiError("Session identity is invalid")
    return state


@contextlib.contextmanager
def locked_session(session):
    path = session_dir(session)
    with (path / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield path, read_state(path)


def process_info(pid):
    try:
        fields = (Path("/proc") / str(pid) / "stat").read_text().rsplit(") ", 1)[1].split()
        return {"state": fields[0], "pgid": int(fields[2]), "sid": int(fields[3]), "start": int(fields[19])}
    except (OSError, ValueError, IndexError):
        return None


def process_alive(record):
    info = process_info(record["pid"])
    return bool(info and info["start"] == record["start"] and info["state"] != "Z")


def group_owned_members(record, session):
    """Allow orphaned launcher descendants only when every group member is tagged."""
    info = process_info(record["pid"])
    if info and info["start"] != record["start"]:
        return None
    if info and info["state"] != "Z":
        if info["pgid"] != record["pid"] or info["sid"] != record["pid"]:
            return None
        return [record["pid"]]
    members = []
    marker = f"CODEX_BACKGROUND_GUI_SESSION={session}".encode()
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        candidate = process_info(int(proc.name))
        if not candidate or candidate["state"] == "Z" or candidate["pgid"] != record["pid"]:
            continue
        if candidate["sid"] != record["pid"] or candidate["start"] < record["start"]:
            return None
        try:
            if marker not in (proc / "environ").read_bytes().split(b"\0"):
                return None
        except OSError:
            return None
        members.append(int(proc.name))
    return members


def terminate_group(record, session, grace=2):
    members = group_owned_members(record, session)
    if members is None:
        return "ownership-mismatch"
    if not members:
        return "already-stopped"
    try:
        os.killpg(record["pid"], signal.SIGTERM)
    except ProcessLookupError:
        return "stopped"
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        members = group_owned_members(record, session)
        if members is None:
            return "ownership-mismatch"
        if not members:
            return "stopped"
        time.sleep(.05)
    # The launcher may already have exited while its children ignore SIGTERM.
    # Verify the remaining owned group, independently of the launcher's wait().
    members = group_owned_members(record, session)
    if members is None:
        return "ownership-mismatch"
    if members:
        try:
            os.killpg(record["pid"], signal.SIGKILL)
        except ProcessLookupError:
            return "stopped"
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            members = group_owned_members(record, session)
            if members is None:
                return "ownership-mismatch"
            if not members:
                return "stopped"
            time.sleep(.05)
        return "still-running"
    return "stopped"


def spawn_owned(path, state, argv, env, label, cwd=None):
    log = path / f"{label}.log"
    with log.open("ab") as stream:
        proc = subprocess.Popen(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                                stdout=stream, stderr=stream, start_new_session=True)
    info = process_info(proc.pid)
    if info is None:
        raise GuiError(f"Could not record {label}; inspect {log}")
    record = {"pid": proc.pid, "start": info["start"], "label": label, "argv": argv, "log": str(log)}
    state["processes"].append(record)
    write_state(path, state)
    return proc, record


def checked(argv, env=None, timeout=15, cwd=None):
    try:
        result = subprocess.run(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise GuiError(f"Command timed out after {timeout}s: {argv[0]}") from exc
    except OSError as exc:
        raise GuiError(f"Cannot run {argv[0]}: {exc}") from exc
    if result.returncode:
        raise GuiError(f"{argv[0]} exited {result.returncode}: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def base_env(state):
    env = os.environ.copy()
    for key in DROP_ENV:
        env.pop(key, None)
    env.update({
        "XDG_RUNTIME_DIR": state["runtime"],
        "DBUS_SESSION_BUS_ADDRESS": state["bus_address"],
        "CODEX_BACKGROUND_GUI_SESSION": state["id"],
        "LIBGL_ALWAYS_SOFTWARE": "1", "GDK_BACKEND": "wayland", "QT_QPA_PLATFORM": "wayland",
        "SDL_VIDEODRIVER": "wayland", "MOZ_ENABLE_WAYLAND": "1",
        "XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "niri", "XDG_SESSION_DESKTOP": "niri",
    })
    return env


def isolated_env(state):
    if state.get("status") != "running":
        raise GuiError("Session is not running; create a new one with start")
    env = base_env(state)
    inner = state.get("env", {})
    if not inner.get("WAYLAND_DISPLAY") or not inner.get("NIRI_SOCKET"):
        raise GuiError("Session has no isolated Wayland/IPC endpoints")
    runtime = Path(state["runtime"])
    for key in ("WAYLAND_DISPLAY", "NIRI_SOCKET"):
        socket = Path(inner[key])
        if not socket.is_absolute():
            socket = runtime / socket
        if socket.parent != runtime or not socket.is_socket():
            raise GuiError(f"Missing or invalid isolated endpoint: {key}")
    for label in ("dbus", "weston", "niri"):
        records = [p for p in state["processes"] if p["label"] == label]
        if not records or not process_alive(records[0]):
            raise GuiError(f"Session component is no longer running: {label}")
    env.update(inner)
    return env


def niri_json(state, *args):
    return json.loads(checked(["niri", "msg", "-j", *args], env=isolated_env(state)))


def wait_until(predicate, processes=(), timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        for proc, record in processes:
            if proc.poll() is not None:
                raise GuiError(f"{record['label']} exited during startup; inspect {record['log']}")
        time.sleep(.05)
    raise GuiError("Startup timed out; inspect session logs")


def viewer_ui_dependencies():
    probe = """import gi, json
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw
gtk = [Gtk.get_major_version(), Gtk.get_minor_version(), Gtk.get_micro_version()]
adw = [Adw.get_major_version(), Adw.get_minor_version(), Adw.get_micro_version()]
print(json.dumps({'ready': adw >= [1, 7, 0], 'gtk': gtk, 'libadwaita': adw}))
"""
    try:
        result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return json.loads(result.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return {"ready": False, "error": "PyGObject with GTK 4 and libadwaita 1.7 or newer is required"}


def observer_niri_binary():
    directory = Path.home() / ".cache/codex-background-gui/niri-observer"
    binary = directory / "niri"
    try:
        metadata = json.loads((directory / "build.json").read_text())
        if (directory.is_symlink() or binary.is_symlink() or directory.stat().st_uid != os.getuid()
            or not os.access(binary, os.X_OK) or metadata.get("capability") != "clipboard-free-v1"
            or hashlib.sha256(binary.read_bytes()).hexdigest() != metadata.get("binary_sha256")):
            return None
        return binary
    except (OSError, ValueError):
        return None


def dependencies():
    tools = {name: shutil.which(name) for name in (*RUNTIME_TOOLS, *BUILD_TOOLS)}
    package = False
    if tools["pkg-config"]:
        package = subprocess.run(["pkg-config", "--exists", "wayland-client"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    observer_niri = observer_niri_binary()
    viewer_ui = viewer_ui_dependencies()
    return {"ready": all(tools.values()) and package, "tools": tools, "wayland_client_devel": package,
            "viewer_capture_ready": bool(observer_niri),
            "viewer_capture_tools": {"private_niri": str(observer_niri) if observer_niri else None},
            "viewer_ui_ready": viewer_ui["ready"], "viewer_ui": viewer_ui}


def pointer_binary():
    source, protocol = ASSETS / "pointer.c", ASSETS / "virtual-pointer.xml"
    digest = hashlib.sha256(source.read_bytes() + protocol.read_bytes()).hexdigest()[:20]
    cache_root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "codex-background-gui"
    build = private_dir(cache_root / digest)
    binary = build / "pointer"
    with (build / ".build.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if binary.is_file() and os.access(binary, os.X_OK):
            return binary
        for tool in BUILD_TOOLS:
            if not shutil.which(tool):
                raise GuiError(f"Missing build dependency: {tool}; run doctor")
        checked(["wayland-scanner", "client-header", str(protocol), str(build / "virtual-pointer-client-protocol.h")])
        checked(["wayland-scanner", "private-code", str(protocol), str(build / "virtual-pointer-protocol.c")])
        flags = checked(["pkg-config", "--cflags", "--libs", "wayland-client"]).split()
        temporary = build / "pointer.tmp"
        checked(["cc", "-O2", "-Wall", "-Wextra", "-I", str(build), "-o", str(temporary),
                 str(source), str(build / "virtual-pointer-protocol.c"), *flags], timeout=30)
        temporary.replace(binary)
    return binary


def stop_owned(path, state):
    import observer_hub
    observer_cleanup = observer_hub.unwatch(state)
    results = []
    for record in reversed(state["processes"]):
        results.append({"label": record["label"], "status": terminate_group(record, state["id"])})
    state["status"] = "stopped" if all(r["status"] in ("stopped", "already-stopped") for r in results) else "stop-incomplete"
    state["stopped_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    state["cleanup"] = results
    if state["status"] == "stopped":
        runtime = Path(state["runtime"])
        if runtime.exists():
            if runtime.is_symlink() or not runtime.name.startswith("cbg-") or runtime.stat().st_uid != os.getuid():
                raise GuiError("Refusing to remove unexpected runtime directory")
            shutil.rmtree(runtime)
    write_state(path, state)
    return {"session": state["id"], "status": state["status"], "processes": results,
            "artifacts": str(path), "observer_cleanup": observer_cleanup}


def start(args):
    label = session_name(args.name) if args.name is not None else None
    for name in RUNTIME_TOOLS:
        if not shutil.which(name):
            raise GuiError(f"Missing dependency: {name}; run doctor")
    if not 320 <= args.width <= 7680 or not 240 <= args.height <= 4320:
        raise GuiError("Dimensions must be within 320..7680 by 240..4320")
    session = dt.datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:10]
    path = private_dir(root_dir() / "sessions" / session)
    private_dir(path / "screenshots")
    runtime_parent = Path(os.environ.get("XDG_RUNTIME_DIR", tempfile.gettempdir()))
    if not runtime_parent.is_dir() or len(str(runtime_parent)) > 50:
        runtime_parent = Path(tempfile.gettempdir())
    runtime = Path(tempfile.mkdtemp(prefix="cbg-", dir=runtime_parent))
    state = {"id": session, "status": "starting", "runtime": str(runtime),
             "bus_address": "unix:path=" + str(runtime / "bus"), "processes": [],
             "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
             "width": args.width, "height": args.height,
             "name": label}
    observer_niri = observer_niri_binary()
    if observer_niri:
        state["niri_binary"] = str(observer_niri)
        state["window_capture"] = "clipboard-free-v1"
    write_state(path, state)
    config = path / "niri.kdl"
    config.write_text(f'''output "winit" {{ mode "{args.width}x{args.height}"; }}
layout {{ default-column-width {{ proportion 1.0; }}; }}
window-rule {{
    focus-ring {{ off; }}
    border {{ off; }}
    geometry-corner-radius 10
    clip-to-geometry true
}}
animations {{ off; }}
hotkey-overlay {{ skip-at-startup; }}
workspace "main"
''')
    env = base_env(state)
    processes = []
    try:
        checked(["niri", "validate", "--config", str(config)], env=env)
        pair = spawn_owned(path, state, ["dbus-daemon", "--session", "--nofork", "--nopidfile",
                                       "--nosyslog", "--address=" + state["bus_address"]], env, "dbus")
        processes.append(pair)
        wait_until(lambda: (runtime / "bus").is_socket(), processes)
        pair = spawn_owned(path, state, ["weston", "--backend=headless", "--renderer=pixman", "--fake-seat", "--shell=kiosk-shell.so",
                                       f"--width={args.width}", f"--height={args.height}", "--socket=outer",
                                       "--idle-time=0", "--no-config", "--log=" + str(path / "weston.log")], env, "weston")
        processes.append(pair)
        wait_until(lambda: (runtime / "outer").is_socket(), processes)
        compositor_env = env | {"WAYLAND_DISPLAY": "outer"}
        if state.get("window_capture") == "clipboard-free-v1":
            compositor_env["CODEX_BACKGROUND_GUI_NO_SCREENSHOT_CLIPBOARD"] = "1"
        pair = spawn_owned(path, state, [state.get("niri_binary", "niri"), "--config", str(config)], compositor_env, "niri")
        processes.append(pair)

        def find_inner():
            sockets = list(runtime.glob("niri*.sock"))
            displays = [p for p in runtime.glob("wayland-*") if p.is_socket()]
            if len(sockets) != 1 or len(displays) != 1:
                return None
            candidate = env | {"WAYLAND_DISPLAY": displays[0].name, "NIRI_SOCKET": str(sockets[0])}
            try:
                outputs = json.loads(checked(["niri", "msg", "-j", "outputs"], env=candidate, timeout=2))
            except (GuiError, ValueError):
                return None
            if not outputs:
                return None
            logical = [output.get("logical") for output in outputs.values() if output.get("logical")]
            if len(logical) != 1 or logical[0]["width"] != args.width or logical[0]["height"] != args.height:
                return None
            return {"WAYLAND_DISPLAY": displays[0].name, "NIRI_SOCKET": str(sockets[0])}, outputs

        endpoints, outputs = wait_until(find_inner, processes)
        state["env"] = endpoints
        state["status"] = "running"
        write_state(path, state)
        inner = isolated_env(state)
        checked(["dbus-update-activation-environment", "WAYLAND_DISPLAY", "NIRI_SOCKET", "XDG_RUNTIME_DIR",
                 "GDK_BACKEND", "QT_QPA_PLATFORM", "XDG_CURRENT_DESKTOP", "CODEX_BACKGROUND_GUI_SESSION"], env=inner)
        output = {"session": session, "name": state.get("name"), "session_dir": str(path), "status": "running", "outputs": outputs,
                  "env": {k: inner[k] for k in ("XDG_RUNTIME_DIR", "WAYLAND_DISPLAY", "NIRI_SOCKET", "DBUS_SESSION_BUS_ADDRESS")}}
        return output
    except BaseException:
        stop_owned(path, state)
        raise


def screen_extent(state):
    outputs = niri_json(state, "outputs")
    logical = [o["logical"] for o in outputs.values() if o.get("logical")]
    if len(logical) != 1 or logical[0]["x"] != 0 or logical[0]["y"] != 0:
        raise GuiError("Pointer helper expects one isolated output at 0,0")
    return logical[0]["width"], logical[0]["height"]


def png_size(path):
    try:
        data = path.read_bytes()
        if len(data) > 24 and data[:8] == b"\x89PNG\r\n\x1a\n" and data[-12:] == b"\0\0\0\0IEND\xaeB`\x82":
            return struct.unpack(">II", data[16:24])
    except OSError:
        pass
    return None


def command_argv(args):
    argv = args.argv
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        raise GuiError("Provide a command after --")
    return argv


def key_argv(keys):
    modifiers = {"ctrl": "ctrl", "control": "ctrl", "alt": "alt", "shift": "shift", "super": "super", "meta": "super"}
    argv = ["wtype"]
    for chord in keys:
        parts = chord.split("+")
        if not parts[-1] or any(part.lower() not in modifiers for part in parts[:-1]):
            raise GuiError("Use keysyms or modifier chords, e.g. Return, Tab, ctrl+a, ctrl+shift+s")
        held = [modifiers[p.lower()] for p in parts[:-1]]
        for mod in held:
            argv.extend(["-M", mod])
        argv.extend(["-k", parts[-1]])
        for mod in reversed(held):
            argv.extend(["-m", mod])
    return argv


def viewer_host_env(state):
    runtime_value = os.environ.get("XDG_RUNTIME_DIR")
    display = os.environ.get("WAYLAND_DISPLAY")
    if not runtime_value or not display or os.environ.get("WAYLAND_SOCKET"):
        raise GuiError("watch needs the caller's host XDG_RUNTIME_DIR and WAYLAND_DISPLAY")
    runtime = Path(runtime_value)
    socket = Path(display)
    if not socket.is_absolute():
        socket = runtime / socket
    if not runtime.is_absolute() or not runtime.is_dir() or not socket.is_socket():
        raise GuiError("watch cannot identify a live host Wayland display")
    if (os.environ.get("CODEX_BACKGROUND_GUI_SESSION") or
        runtime.resolve() == Path(state["runtime"]).resolve() or
        runtime.name.startswith("cbg-") or socket.parent.name.startswith("cbg-")):
        raise GuiError("watch must be called from the host environment, outside every private GUI session")
    env = os.environ.copy()
    for key in ("DISPLAY", "WAYLAND_SOCKET", "NIRI_SOCKET", "NIRI_CONFIG", "XDG_ACTIVATION_TOKEN", "DESKTOP_STARTUP_ID", "GTK_THEME"):
        env.pop(key, None)
    env.update(CODEX_BACKGROUND_GUI_SESSION=state["id"], GDK_BACKEND="wayland", GSK_RENDERER="cairo")
    return env


def watch(path, state):
    import observer_hub
    isolated_env(state)
    if not viewer_ui_dependencies()["ready"]:
        raise GuiError("watch requires PyGObject with GTK 4 and libadwaita 1.7 or newer; inspect doctor")
    env = viewer_host_env(state)
    result = observer_hub.watch(path, state, env)
    # Retire this session's old per-session observers only after the hub is ready.
    for record in reversed(state["processes"]):
        if record["label"].startswith("viewer-") and process_alive(record):
            terminate_group(record, state["id"])
    return result


def unwatch(state):
    import observer_hub
    hubs = observer_hub.unwatch(state)
    results = [{"pid": record["pid"], "status": terminate_group(record, state["id"])}
               for record in reversed(state["processes"]) if record["label"].startswith("viewer-")]
    return {"session": state["id"], "viewers": results, "shared_observers": hubs,
            "closed": not hubs["errors"] and all(item["status"] in ("stopped", "already-stopped") for item in results)}


def operate(args):
    with locked_session(args.session) as (path, state):
        if args.command == "stop":
            return stop_owned(path, state)
        if args.command == "status":
            components = [{"label": p["label"], "pid": p["pid"], "alive": process_alive(p), "log": p["log"]}
                          for p in state["processes"]]
            return {"session": state["id"], "name": state.get("name"), "status": state["status"], "session_dir": str(path),
                    "running": state["status"] == "running" and all(
                        p["alive"] for p in components if p["label"] in ("dbus", "weston", "niri")),
                    "processes": components}
        if args.command == "rename":
            state["name"] = session_name(args.name)
            write_state(path, state)
            return {"session": state["id"], "name": state["name"]}
        if args.command == "watch":
            return watch(path, state)
        if args.command == "unwatch":
            return unwatch(state)
        env = isolated_env(state)
        if args.command in ("run", "exec"):
            argv = command_argv(args)
            if args.cwd is not None and not Path(args.cwd).is_dir():
                raise GuiError("--cwd must be an existing directory")
            if args.command == "run":
                label = "app-" + uuid.uuid4().hex[:10]
                proc, record = spawn_owned(path, state, argv, env, label, args.cwd)
                time.sleep(.1)
                return {"session": state["id"], "pid": proc.pid, "running": proc.poll() is None,
                        "returncode": proc.poll(), "log": record["log"]}
            proc, record = spawn_owned(path, state, argv, env, "exec-" + uuid.uuid4().hex[:10], args.cwd)
            try:
                returncode = proc.wait(timeout=args.timeout)
            except subprocess.TimeoutExpired:
                cleanup = terminate_group(record, state["id"])
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
                raise GuiError(f"exec timed out after {args.timeout}s; cleanup={cleanup}; log: {record['log']}")
            with Path(record["log"]).open("rb") as log:
                output = log.read(1024 * 1024 + 1)
            return {"returncode": returncode, "log": record["log"],
                    "output": output[:1024 * 1024].decode(errors="replace"),
                    "output_truncated": len(output) > 1024 * 1024}
        if args.command == "windows":
            return niri_json(state, "windows")
        if args.command == "focus":
            checked(["niri", "msg", "action", "focus-window", "--id", str(args.window)], env)
            return niri_json(state, "focused-window")
        if args.command == "screenshot":
            image = Path(args.path) if args.path else path / "screenshots" / (uuid.uuid4().hex + ".png")
            if not image.is_absolute():
                raise GuiError("--path must be absolute")
            if image.exists():
                raise GuiError("Screenshot path already exists; use a new path")
            image.parent.mkdir(parents=True, exist_ok=True)
            argv = ["niri", "msg", "action"]
            if args.window is not None:
                argv.extend(["screenshot-window", "--id", str(args.window)])
            else:
                argv.extend(["screenshot-screen", "--show-pointer", "false"])
            checked([*argv, "--path", str(image)], env)
            size = wait_until(lambda: png_size(image), timeout=8)
            return {"session": state["id"], "path": str(image), "width": size[0], "height": size[1],
                    "coordinate_space": "window" if args.window is not None else "screen", "window": args.window}
        if args.command in ("move", "click"):
            width, height = screen_extent(state)
            if not 0 <= args.x < width or not 0 <= args.y < height:
                raise GuiError(f"Coordinates must be within 0..{width-1}, 0..{height-1}")
            argv = [str(pointer_binary()), args.command, str(args.x), str(args.y), str(width), str(height)]
            if args.command == "click":
                argv.append(str({"left": 0x110, "right": 0x111, "middle": 0x112}[args.button]))
            checked(argv, env)
            return {"session": state["id"], "action": args.command, "x": args.x, "y": args.y}
        if args.command == "scroll":
            if not all(math.isfinite(x) and abs(x) <= 1000000 for x in (args.vertical, args.horizontal)):
                raise GuiError("Scroll values must be finite and within +/-1000000 pixels")
            checked([str(pointer_binary()), "scroll", str(args.vertical), str(args.horizontal)], env)
            return {"session": state["id"], "vertical": args.vertical, "horizontal": args.horizontal}
        if args.command == "type":
            checked(["wtype", "--", args.text], env)
            return {"session": state["id"], "typed_characters": len(args.text)}
        if args.command == "key":
            checked(key_argv(args.keys), env)
            return {"session": state["id"], "keys": args.keys}
    raise GuiError("Unknown command")


def parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Check installed runtime and pointer build dependencies")
    start_p = commands.add_parser("start", help="Create a unique invisible desktop")
    start_p.add_argument("--width", type=int, default=1280)
    start_p.add_argument("--height", type=int, default=800)
    start_p.add_argument("--name", help="Human-readable project or task label")
    for name in ("run", "exec"):
        sub = commands.add_parser(name, help="Launch argv in isolated desktop" + (" and wait" if name == "exec" else ""))
        sub.add_argument("session")
        sub.add_argument("--cwd")
        if name == "exec":
            sub.add_argument("--timeout", type=float, default=30)
    for name in ("windows", "status", "stop", "watch", "unwatch"):
        commands.add_parser(name).add_argument("session")
    rename = commands.add_parser("rename", help="Rename a session without restarting it")
    rename.add_argument("session"); rename.add_argument("name")
    focus = commands.add_parser("focus"); focus.add_argument("session"); focus.add_argument("window", type=int)
    screenshot = commands.add_parser("screenshot")
    screenshot.add_argument("session"); screenshot.add_argument("--window", type=int); screenshot.add_argument("--path")
    for name in ("move", "click"):
        sub = commands.add_parser(name); sub.add_argument("session"); sub.add_argument("x", type=int); sub.add_argument("y", type=int)
        if name == "click":
            sub.add_argument("--button", choices=("left", "right", "middle"), default="left")
    scroll = commands.add_parser("scroll"); scroll.add_argument("session")
    scroll.add_argument("--vertical", type=float, default=0); scroll.add_argument("--horizontal", type=float, default=0)
    typing = commands.add_parser("type"); typing.add_argument("session"); typing.add_argument("text")
    key = commands.add_parser("key"); key.add_argument("session"); key.add_argument("keys", nargs="+")
    return parser


def main():
    arguments = sys.argv[1:]
    app_argv = None
    if arguments and arguments[0] in ("run", "exec") and "--" in arguments:
        split = arguments.index("--")
        app_argv = arguments[split + 1:]
        arguments = arguments[:split]
    cli = parser()
    args = cli.parse_args(arguments)
    if args.command in ("run", "exec"):
        if app_argv is None:
            cli.error("run and exec require -- before the application command")
        args.argv = app_argv
    try:
        if args.command == "doctor":
            report = dependencies(); emit(report); return 0 if report["ready"] else 1
        if args.command == "start":
            emit(start(args)); return 0
        if args.command == "exec" and (not math.isfinite(args.timeout) or args.timeout <= 0 or args.timeout > 3600):
            raise GuiError("--timeout must be within 0..3600 seconds")
        result = operate(args)
        emit(result)
        if args.command == "exec" and result["returncode"]:
            return 1
        if args.command == "run" and result["returncode"] not in (None, 0):
            return 1
        if args.command == "stop" and result["status"] != "stopped":
            return 1
        if args.command == "unwatch" and not result["closed"]:
            return 1
        return 0
    except (GuiError, OSError, ValueError) as exc:
        emit({"error": str(exc)}); return 1
    except KeyboardInterrupt:
        emit({"error": "Interrupted"}); return 130


if __name__ == "__main__":
    raise SystemExit(main())
