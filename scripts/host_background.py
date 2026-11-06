# SPDX-License-Identifier: GPL-3.0-or-later
"""Window-addressed input for lihaoze123/niri-computer-use's host compositor.

This adapter never activates a window or falls back to seat/global input.
The compositor patch, including its visible agent cursor, must already be running.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import struct
import tempfile
import time
import uuid

BACKEND = "host-background"
MAX_REPLY = 4 * 1024 * 1024
PROBE_ID = 2**64 - 1
BUTTONS = {"left": 272, "right": 273, "middle": 274}
MODIFIERS = {"ctrl": 29, "control": 29, "shift": 42, "alt": 56, "super": 125, "meta": 125}
# Named keys and US physical key positions. Literal text uses AgentEvent::Text.
KEYS = dict(zip("1234567890", range(2, 12)))
KEYS.update(dict(zip("qwertyuiop", range(16, 26))))
KEYS.update(dict(zip("asdfghjkl", range(30, 39))))
KEYS.update(dict(zip("zxcvbnm", range(44, 51))))
KEYS.update({"escape": 1, "minus": 12, "equal": 13, "backspace": 14, "tab": 15,
             "bracketleft": 26, "bracketright": 27, "return": 28, "enter": 28,
             "semicolon": 39, "apostrophe": 40, "grave": 41, "backslash": 43,
             "comma": 51, "period": 52, "slash": 53, "space": 57, "caps_lock": 58,
             "home": 102, "up": 103, "page_up": 104, "left": 105, "right": 106,
             "end": 107, "down": 108, "page_down": 109, "insert": 110, "delete": 111})
KEYS.update({f"f{i}": 58 + i for i in range(1, 11)})
KEYS.update(f11=87, f12=88)


class HostError(ValueError):
    pass


class ReplyError(HostError):
    pass


def socket_identity(path):
    info = Path(path).stat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise HostError("NIRI_SOCKET must be a live Unix socket owned by this user")
    return [info.st_dev, info.st_ino]


def peer_identity(connection):
    pid, uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    if uid != os.getuid():
        raise HostError("Host niri IPC peer is not owned by this user")
    start = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]
    return [pid, start, Path("/proc/sys/kernel/random/boot_id").read_text().strip()]


class NiriIPC:
    def __init__(self, path, identity=None, peer=None):
        self.path = str(path)
        self.identity = identity or socket_identity(path)
        self.peer = peer

    def request(self, request):
        if socket_identity(self.path) != self.identity:
            raise HostError("Host compositor socket changed; create a new session")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(5)
                connection.connect(self.path)
                peer = peer_identity(connection)
                if self.peer is not None and peer != self.peer:
                    raise HostError("Host compositor process changed; create a new session")
                self.peer = peer
                connection.sendall(json.dumps(request, allow_nan=False).encode() + b"\n")
                with connection.makefile("rb") as stream:
                    line = stream.readline(MAX_REPLY + 1)
            if len(line) > MAX_REPLY or not line.endswith(b"\n"):
                raise HostError("Invalid or oversized niri IPC reply")
            reply = json.loads(line)
        except (OSError, ValueError) as exc:
            raise HostError(f"Host niri IPC failed: {exc}; input was not retried") from exc
        if not isinstance(reply, dict) or len(reply) != 1:
            raise HostError("Invalid niri IPC reply")
        if "Err" in reply:
            raise ReplyError(str(reply["Err"]))
        if "Ok" not in reply:
            raise HostError("Missing niri IPC result")
        return reply["Ok"]

    def windows(self):
        result = self.request("Windows")
        if not isinstance(result, dict) or not isinstance(result.get("Windows"), list):
            raise HostError("Invalid niri window list")
        return result["Windows"]

    def probe(self):
        # The impossible window ID fails before either handler changes any state.
        # Requiring the exact errors distinguishes support from stock niri's parse error.
        for request, expected in (
            ({"AgentInput": {"window_id": PROBE_ID, "events": []}}, f"no window with id {PROBE_ID}"),
            ({"AgentScreenshot": {"window_id": PROBE_ID, "path": "/unused-bgui-probe.png",
                                  "show_pointer": False}}, f"no window with id {PROBE_ID} on an output"),
        ):
            try:
                self.request(request)
            except ReplyError as exc:
                if str(exc) == expected:
                    continue
                raise HostError("Host niri lacks the compatible AgentInput/AgentScreenshot patch; "
                                "see references/host-background.md. No global input fallback was used") from exc
            raise HostError("Unexpected niri capability probe result")


def host_endpoint():
    if os.environ.get("CODEX_BACKGROUND_GUI_SESSION") or os.environ.get("WAYLAND_SOCKET"):
        raise HostError("Run host-background from the normal host shell, outside a private session")
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", ""))
    display = os.environ.get("WAYLAND_DISPLAY")
    endpoint = Path(os.environ.get("NIRI_SOCKET", ""))
    if not runtime.is_absolute() or not endpoint.is_absolute() or not display:
        raise HostError("Host XDG_RUNTIME_DIR, WAYLAND_DISPLAY and an absolute NIRI_SOCKET are required")
    runtime = runtime.resolve()
    if runtime.name.startswith("cbg-") or runtime.stat().st_uid != os.getuid():
        raise HostError("Expected the user's host runtime directory")
    wayland = Path(display)
    if not wayland.is_absolute():
        wayland = runtime / wayland
    endpoint = endpoint.resolve()
    if endpoint.parent != runtime or wayland.resolve().parent != runtime:
        raise HostError("Host Wayland and niri sockets must belong to the host runtime directory")
    socket_identity(wayland)
    client = NiriIPC(endpoint)
    return client, {"XDG_RUNTIME_DIR": str(runtime), "WAYLAND_DISPLAY": str(wayland),
                    "NIRI_SOCKET": str(endpoint),
                    "DBUS_SESSION_BUS_ADDRESS": os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")}


def doctor():
    try:
        client, _ = host_endpoint()
        client.probe()
        return {"ready": True, "backend": BACKEND, "windows": client.windows(),
                "agent_cursor": "compositor overlay (visible when its window is visible)"}
    except (HostError, OSError) as exc:
        return {"ready": False, "backend": BACKEND, "error": str(exc)}


def target(client, window_id, pinned=None, for_input=False):
    windows = client.windows()
    window = next((w for w in windows if w.get("id") == window_id), None)
    if window is None:
        raise HostError("Target window closed; list windows and select a new target")
    if pinned and any(window.get(k) != pinned.get(k) for k in ("id", "pid", "app_id")):
        raise HostError("Target window identity changed; select it again")
    if for_input:
        focused = next((w for w in windows if w.get("is_focused")), None)
        if focused and (focused["id"] == window_id or
                        (window.get("pid") is not None and focused.get("pid") == window["pid"])):
            raise HostError("The user is focused on the target window or its process; "
                            "move focus away before background input. Use a separate browser profile")
    return window


def pin(window):
    return {k: window.get(k) for k in ("id", "pid", "app_id")}


@contextlib.contextmanager
def compositor_lock(core, identity):
    directory = core.private_dir(core.root_dir() / "host-attachments")
    lease = directory / ("-".join(map(str, identity)) + ".json")
    with lease.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield lease


def start(args, core):
    if args.window is not None and not 0 < args.window < 2**64:
        raise HostError("--window must be a positive uint64 from doctor --backend host-background")
    client, env = host_endpoint()
    client.probe()
    window = target(client, args.window, for_input=True) if args.window is not None else None
    name = core.session_name(args.name) if args.name is not None else (window or {}).get("title")
    with compositor_lock(core, client.identity) as lease:
        if lease.exists():
            previous = json.loads(lease.read_text())
            previous_path = core.session_dir(previous["session"])
            previous_state = core.read_state(previous_path)
            if previous_state.get("status") == "running":
                raise HostError(f"Host compositor already attached by {previous['session']}; stop it first")
        session = dt.datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:10]
        path = core.private_dir(core.root_dir() / "sessions" / session)
        core.private_dir(path / "screenshots")
        state = {"id": session, "backend": BACKEND, "name": name, "status": "running",
                 "created_at": dt.datetime.now(dt.timezone.utc).isoformat(), "processes": [],
                 "host_env": env, "socket_identity": client.identity, "peer_identity": client.peer,
                 "target": pin(window) if window else None}
        core.write_state(path, state)
        lease.write_text(json.dumps({"session": session}))
        lease.chmod(0o600)
    return {"session": session, "backend": BACKEND, "status": "running", "name": name,
            "window": window, "session_dir": str(path), "agent_cursor": "compositor overlay"}


def client_for(state):
    if state.get("status") != "running":
        raise HostError("Host attachment is stopped; create a new session")
    return NiriIPC(state["host_env"]["NIRI_SOCKET"], state["socket_identity"], state["peer_identity"])


def target_id(state):
    if not state.get("target"):
        raise HostError("No target selected; launch an app, list windows, then use focus SESSION ID")
    return state["target"]["id"]


def capture(args, path, state, client, core):
    window_id = target_id(state)
    if args.window is not None and args.window != window_id:
        raise HostError("Select the target with focus SESSION ID before capturing another window")
    window = target(client, window_id, state["target"])
    image = Path(args.path) if args.path else path / "screenshots" / (uuid.uuid4().hex + ".png")
    if not image.is_absolute() or image.exists() or image.is_symlink():
        raise HostError("Screenshot path must be absolute and must not already exist")
    image.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="capture-", dir=path / "screenshots") as temporary:
        pending = Path(temporary) / "frame.png"
        response = client.request({"AgentScreenshot": {"window_id": window_id,
                                  "path": str(pending), "show_pointer": False}})
        info = response.get("AgentScreenshot") if isinstance(response, dict) else None
        if not isinstance(info, dict):
            raise HostError("Invalid AgentScreenshot metadata")
        if (not all(type(info.get(k)) is int for k in ("width", "height", "geometry_x", "geometry_y"))
            or not isinstance(info.get("scale"), (int, float)) or isinstance(info["scale"], bool)
            or not math.isfinite(info["scale"]) or info["scale"] <= 0
            or core.png_size(pending) != (info["width"], info["height"])):
            raise HostError("Screenshot file or coordinate metadata is invalid")
        # Exclusive publication works across filesystems too. Never clobber an existing path.
        try:
            descriptor = os.open(image, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(pending.read_bytes())
        except FileExistsError as exc:
            raise HostError("Screenshot path already exists") from exc
    state["capture"] = dict(info, window_id=window_id,
                            window_size=(window.get("layout") or {}).get("window_size"))
    core.write_state(path, state)
    return {"session": state["id"], "backend": BACKEND, "path": str(image), "window": window_id,
            "coordinate_space": "window-image-pixels", **info}


def position(state, window, x, y):
    info = state.get("capture")
    if not info or info["window_id"] != window["id"]:
        raise HostError("Take screenshot SESSION before pointer input")
    if info["window_size"] != (window.get("layout") or {}).get("window_size"):
        raise HostError("Window resized; take a new screenshot before pointer input")
    if not all(math.isfinite(v) for v in (x, y)) or not (0 <= x < info["width"] and 0 <= y < info["height"]):
        raise HostError("Coordinates must be inside the last window screenshot")
    x, y = (x - info["geometry_x"]) / info["scale"], (y - info["geometry_y"]) / info["scale"]
    # Popups can extend beyond the toplevel geometry, including negative logical coordinates.
    return {"Motion": {"x": x, "y": y}}


def key_events(chords):
    events = []
    for chord in chords:
        parts = chord.lower().split("+")
        if parts[-1] not in KEYS or any(p not in MODIFIERS for p in parts[:-1]):
            raise HostError("Unsupported key; use Return, Tab, BackSpace, arrows, F1..F12 or ctrl+a. "
                            "Use type for literal Unicode text")
        held = list(dict.fromkeys(MODIFIERS[p] for p in parts[:-1]))
        codes = [*held, KEYS[parts[-1]]]
        events.extend({"Key": {"keycode": code, "pressed": True}} for code in codes)
        events.extend({"Key": {"keycode": code, "pressed": False}} for code in reversed(codes))
    return events


def send(client, state, events):
    if client.request({"AgentInput": {"window_id": target_id(state), "events": events}}) != "Handled":
        raise HostError("Unexpected AgentInput reply; input was not retried")


def input_action(args, path, state, client):
    window = target(client, target_id(state), state["target"], for_input=True)
    if (path / "stop.requested").exists():
        raise HostError("Stop requested")
    command = args.command
    if command in ("move", "click"):
        events = [position(state, window, args.x, args.y)]
        if command == "click":
            events.extend({"Button": {"button": BUTTONS[args.button], "pressed": p}} for p in (True, False))
        send(client, state, events)
    elif command == "scroll":
        if not all(math.isfinite(v) and abs(v) <= 1000000 for v in (args.vertical, args.horizontal)):
            raise HostError("Scroll values must be finite and within +/-1000000 logical pixels")
        if "pointer" not in state:
            raise HostError("Use move or click to position the agent pointer before scrolling")
        send(client, state, [position(state, window, *state["pointer"]),
                            {"Axis": {"vertical": args.vertical, "horizontal": args.horizontal}}])
    elif command == "type":
        if len(args.text) > 4096:
            raise HostError("Type at most 4096 characters per call")
        method = args.unicode_input
        if method == "auto":
            chromium_apps = {"chromium", "chrome", "edge", "msedge", "brave", "vivaldi", "electron",
                             "code", "vscode", "codium", "vscodium", "discord", "slack", "obsidian"}
            app_parts = re.split(r"[./_-]+", (window.get("app_id") or "").lower())
            method = "CtrlShiftU" if chromium_apps.intersection(app_parts) else "Keymap"
        send(client, state, [{"Text": {"text": args.text, "unicode_input": method}}])
    elif command == "key":
        send(client, state, key_events(args.keys))
    elif command == "drag":
        if not math.isfinite(args.duration) or not .1 <= args.duration <= 2:
            raise HostError("Drag duration must be within 0.1..2 seconds")
        begin = position(state, window, args.x, args.y)
        end = position(state, window, args.end_x, args.end_y)
        button = BUTTONS[args.button]
        try:
            send(client, state, [begin, {"Button": {"button": button, "pressed": True}}])
            steps = max(2, round(args.duration * 30))
            for i in range(1, steps + 1):
                time.sleep(args.duration / steps)
                if (path / "stop.requested").exists():
                    raise HostError("Drag stopped")
                current = target(client, window["id"], state["target"], for_input=True)
                position(state, current, args.end_x, args.end_y)
                point = {k: begin["Motion"][k] + (end["Motion"][k] - begin["Motion"][k]) * i / steps
                         for k in ("x", "y")}
                send(client, state, [{"Motion": point}])
        finally:
            # Release even after interruption or a failed press acknowledgement; never replay a press.
            send(client, state, [{"Button": {"button": button, "pressed": False}}])
    else:
        raise HostError(f"{command} is unavailable for {BACKEND}")
    if command in ("move", "click", "drag"):
        state["pointer"] = [args.end_x, args.end_y] if command == "drag" else [args.x, args.y]
    return {"session": state["id"], "backend": BACKEND, "action": command,
            "window": window["id"]}


def stop(path, state, core):
    error = None
    if state["status"] == "running" and state.get("target"):
        try:
            send(client_for(state), state, ["PointerLeave", "KeyboardLeave"])
        except (HostError, OSError) as exc:
            error = str(exc)
    state["status"] = "stopped"
    state["stopped_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    core.write_state(path, state)
    with compositor_lock(core, state["socket_identity"]) as lease:
        if lease.exists() and json.loads(lease.read_text()).get("session") == state["id"]:
            lease.unlink()
    # Host applications and the host compositor are never signaled by detach.
    return {"session": state["id"], "backend": BACKEND, "status": "stopped",
            "detached": True, "input_cleanup_error": error}


def stop_current(core):
    client, _ = host_endpoint()
    with compositor_lock(core, client.identity) as lease:
        session = json.loads(lease.read_text())["session"] if lease.exists() else None
    if session is None:
        return {"backend": BACKEND, "status": "stopped", "detached": True}
    return core.operate(core.parser().parse_args(["stop", session]))


def operate(args, path, state, core):
    if args.command == "stop":
        return stop(path, state, core)
    if args.command == "status":
        try:
            client = client_for(state)
            window = target(client, target_id(state), state["target"]) if state.get("target") else None
            if window is None:
                client.windows()
            return {"session": state["id"], "backend": BACKEND, "status": state["status"],
                    "running": True, "window": window, "name": state.get("name")}
        except (HostError, OSError) as exc:
            return {"session": state["id"], "backend": BACKEND, "status": state["status"],
                    "running": False, "error": str(exc)}
    if args.command == "rename":
        state["name"] = core.session_name(args.name)
        core.write_state(path, state)
        return {"session": state["id"], "name": state["name"]}
    client = client_for(state)
    if args.command == "windows":
        return client.windows()
    if args.command == "focus":
        window = target(client, args.window, for_input=True)
        # In host-background this selects an input target; it does not focus the seat.
        state["target"] = pin(window)
        state.pop("capture", None)
        state.pop("pointer", None)
        core.write_state(path, state)
        return {"session": state["id"], "window": window, "host_focus_changed": False}
    if args.command in ("watch", "unwatch"):
        window = target(client, target_id(state), state["target"])
        return {"session": state["id"], "window": window, "closed": True,
                "observation": "native window; switch to its workspace yourself to see the agent cursor"}
    if args.command in ("run", "launch", "exec"):
        if (path / "stop.requested").exists():
            raise HostError("Stop requested")
        client.windows()  # Validate the pinned compositor before launching onto its display.
        env = os.environ.copy()
        for key in core.DROP_ENV:
            env.pop(key, None)
        env.update(state["host_env"])
        env.update(NIRI_AGENT_LAUNCH="1", CODEX_BACKGROUND_GUI_SESSION=state["id"])
        return core.launch_command(args, path, state, env)
    if args.command == "screenshot":
        return capture(args, path, state, client, core)
    result = input_action(args, path, state, client)
    core.write_state(path, state)
    return result
