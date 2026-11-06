# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit desktop-wide virtual pointer on the AgentDesktop niri extension."""
from __future__ import annotations

import math
import os
from pathlib import Path
import tempfile
import time
import uuid

import host_background as host

BACKEND = "host-desktop"


def probe(client):
    # No input, cursor or files are created by these probes.
    try:
        client.request({"AgentDesktopScreenshot": {"output": "", "path": "/unused-bgui-probe.png",
                                                   "show_pointer": False}})
    except host.ReplyError as exc:
        if str(exc) != "no output named ":
            raise host.HostError("Host niri lacks the desktop-agent patch or the session is locked") from exc
    else:
        raise host.HostError("Unexpected desktop capture probe reply")
    if client.request({"AgentDesktopInput": {"events": []}}) != "Handled":
        raise host.HostError("Unexpected desktop input probe reply")


def outputs(client):
    result = client.request("Outputs")
    if not isinstance(result, dict) or not isinstance(result.get("Outputs"), dict):
        raise host.HostError("Invalid niri outputs response")
    return {name: value for name, value in result["Outputs"].items() if value.get("logical")}


def doctor():
    try:
        client, _ = host.host_endpoint()
        probe(client)
        return {"ready": True, "backend": BACKEND, "outputs": outputs(client),
                "agent_cursor": "desktop overlay; clicks select the agent keyboard target"}
    except (host.HostError, OSError) as exc:
        return {"ready": False, "backend": BACKEND, "error": str(exc)}


def send(client, events):
    if client.request({"AgentDesktopInput": {"events": events}}) != "Handled":
        raise host.HostError("Unexpected desktop input reply; input was not retried")


def start(args, core):
    if args.window is not None:
        raise host.HostError("host-desktop selects surfaces by clicking; omit --window")
    result = host.start(args, core, backend=BACKEND, probe=probe)
    try:
        path = core.session_dir(result["session"])
        state = core.read_state(path)
        client = host.client_for(state)
        available = outputs(client)
        if not available:
            raise host.HostError("No active monitors")
        geo = next(iter(available.values()))["logical"]
        send(client, [{"Motion": {"x": geo["x"] + geo["width"] / 2,
                                   "y": geo["y"] + geo["height"] / 2}}])
        return dict(result, outputs=available, agent_cursor="desktop overlay")
    except BaseException:
        # Release the lease if initialization fails, without terminating native applications.
        stop(path, state, core)
        raise


def fingerprint(output):
    return {key: output.get(key) for key in ("name", "make", "model", "serial", "current_mode", "logical")}


def capture(args, path, state, client, core):
    if args.window is not None:
        raise host.HostError("Use --output MONITOR for desktop capture")
    available = outputs(client)
    name = args.output
    if name is None:
        if len(available) != 1:
            raise host.HostError("Choose --output from: " + ", ".join(available))
        name = next(iter(available))
    if name not in available:
        raise host.HostError("Monitor is unavailable; list outputs with doctor --backend host-desktop")
    image = Path(args.path) if args.path else path / "screenshots" / (uuid.uuid4().hex + ".png")
    if not image.is_absolute() or image.exists() or image.is_symlink():
        raise host.HostError("Screenshot path must be absolute and must not already exist")
    image.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="desktop-capture-", dir=path / "screenshots") as temporary:
        pending = Path(temporary) / "frame.png"
        result = client.request({"AgentDesktopScreenshot": {"output": name, "path": str(pending),
                                                            "show_pointer": args.show_pointer}})
        info = result.get("AgentDesktopScreenshot") if isinstance(result, dict) else None
        if (not isinstance(info, dict)
            or not all(type(info.get(k)) is int for k in ("width", "height", "logical_x", "logical_y"))
            or type(info.get("scale")) not in (int, float)
            or not math.isfinite(info["scale"]) or info["scale"] <= 0
            or core.png_size(pending) != (info["width"], info["height"])):
            raise host.HostError("Invalid desktop screenshot or coordinate metadata")
        current = outputs(client).get(name)
        if not current or fingerprint(current) != fingerprint(available[name]):
            raise host.HostError("Monitor layout changed during capture; take a fresh screenshot")
        logical = current["logical"]
        if (info["logical_x"], info["logical_y"], info["scale"]) != (logical["x"], logical["y"], logical["scale"]):
            raise host.HostError("Desktop screenshot origin/scale disagrees with current monitor")
        with os.fdopen(os.open(image, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as output:
            output.write(pending.read_bytes())
    state["capture"] = dict(info, output=name, fingerprint=fingerprint(current))
    core.write_state(path, state)
    return {"session": state["id"], "backend": BACKEND, "path": str(image), "output": name,
            "coordinate_space": "monitor-image-pixels", **info}


def position(state, client, x, y):
    info = state.get("capture")
    if not info:
        raise host.HostError("Take a monitor screenshot before pointer input")
    current = outputs(client).get(info["output"])
    if not current or fingerprint(current) != info["fingerprint"]:
        raise host.HostError("Monitor layout changed; take a new screenshot")
    if not all(math.isfinite(v) for v in (x, y)) or not (0 <= x < info["width"] and 0 <= y < info["height"]):
        raise host.HostError("Coordinates must be inside the last monitor screenshot")
    return {"Motion": {"x": x / info["scale"] + info["logical_x"],
                       "y": y / info["scale"] + info["logical_y"]}}


def stop(path, state, core):
    error = None
    if state["status"] == "running":
        try:
            if host.client_for(state).request("AgentDesktopStop") != "Handled":
                raise host.HostError("Unexpected desktop stop reply")
        except (host.HostError, OSError) as exc:
            error = str(exc)
    result = host.stop(path, state, core)
    return dict(result, backend=BACKEND, input_cleanup_error=error)


def operate(args, path, state, core):
    command = args.command
    if command == "stop":
        return stop(path, state, core)
    if command in ("run", "launch", "exec", "rename", "windows", "status"):
        result = host.operate(args, path, state, core)
        if isinstance(result, dict) and "backend" in result:
            result["backend"] = BACKEND
        return result
    client = host.client_for(state)
    if command in ("watch", "unwatch"):
        return {"session": state["id"], "backend": BACKEND, "observation": "native desktop cursor"}
    if command == "focus":
        raise host.HostError("host-desktop selects the agent keyboard target with click")
    if command == "screenshot":
        return capture(args, path, state, client, core)
    if (path / "stop.requested").exists():
        raise host.HostError("Stop requested")
    if command in ("move", "click"):
        events = [position(state, client, args.x, args.y)]
        if command == "click":
            events.extend({"Button": {"button": host.BUTTONS[args.button], "pressed": p}} for p in (True, False))
    elif command == "scroll":
        if not all(math.isfinite(v) and abs(v) <= 1e6 for v in (args.vertical, args.horizontal)):
            raise host.HostError("Scroll amounts must be finite and within +/-1000000")
        if not state.get("pointer"):
            raise host.HostError("Position the pointer with move or click before scrolling")
        events = [{"Motion": state["pointer"]}, {"Axis": {"vertical": args.vertical, "horizontal": args.horizontal}}]
    elif command == "drag":
        if not math.isfinite(args.duration) or not .1 <= args.duration <= 2:
            raise host.HostError("Drag duration must be within 0.1..2 seconds")
        begin = position(state, client, args.x, args.y)
        end = position(state, client, args.end_x, args.end_y)
        button = host.BUTTONS[args.button]
        try:
            send(client, [begin, {"Button": {"button": button, "pressed": True}}])
            steps = max(2, round(args.duration * 30))
            for i in range(1, steps + 1):
                time.sleep(args.duration / steps)
                if (path / "stop.requested").exists():
                    raise host.HostError("Drag stopped")
                position(state, client, args.end_x, args.end_y)
                point = {k: begin["Motion"][k] + (end["Motion"][k] - begin["Motion"][k]) * i / steps
                         for k in ("x", "y")}
                send(client, [{"Motion": point}])
            send(client, [{"Button": {"button": button, "pressed": False}}])
        except BaseException:
            try:
                client.request("AgentDesktopStop")
            except (host.HostError, OSError):
                pass
            raise
        state["pointer"] = end["Motion"]
        core.write_state(path, state)
        return {"session": state["id"], "backend": BACKEND, "action": command,
                "host_focus_changed": False}
    elif command == "key":
        events = host.key_events(args.keys)
    elif command == "type":
        if len(args.text) > 4096:
            raise host.HostError("Type at most 4096 characters per call")
        method = "Keymap" if args.unicode_input == "auto" else args.unicode_input
        events = [{"Text": {"text": args.text, "unicode_input": method}}]
    else:
        raise host.HostError(f"{command} is unavailable for {BACKEND}")
    try:
        send(client, events)
    except BaseException:
        # An unacknowledged batch can include a successful press. Stop releases it; never replay.
        try:
            client.request("AgentDesktopStop")
        except (host.HostError, OSError):
            pass
        raise
    if command in ("move", "click"):
        state["pointer"] = events[0]["Motion"]
    core.write_state(path, state)
    return {"session": state["id"], "backend": BACKEND, "action": command, "host_focus_changed": False}
