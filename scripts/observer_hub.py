# SPDX-License-Identifier: GPL-3.0-or-later
"""Display-scoped observer registry; the shared viewer belongs to no GUI session."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import bgui


def atomic_json(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_directory(path):
    path = Path(path)
    parent = bgui.root_dir() / "observers"
    if path.parent != parent or not path.name.isalnum():
        raise bgui.GuiError("Unexpected shared observer directory")
    bgui.private_dir(parent)
    return bgui.private_dir(path)


def directory(env):
    display = Path(env["WAYLAND_DISPLAY"])
    if not display.is_absolute():
        display = Path(env["XDG_RUNTIME_DIR"]) / display
    key = hashlib.sha256(str(display.resolve()).encode()).hexdigest()[:24]
    return validate_directory(bgui.root_dir() / "observers" / key)


@contextlib.contextmanager
def locked(path):
    validate_directory(path)
    with (path / ".lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def read_subscriptions(path):
    data = read_json(Path(path) / "subscriptions.json", [])
    if not isinstance(data, list):
        return []
    return list(dict.fromkeys(s for s in data if isinstance(s, str) and bgui.SESSION_PATTERN.fullmatch(s)))


def remove(path, session):
    path = validate_directory(path)
    with locked(path):
        subscriptions = read_subscriptions(path)
        atomic_json(path / "subscriptions.json", [s for s in subscriptions if s != session])


def unwatch(state):
    removed = []
    errors = []
    # No hub process is terminated here. Other tasks can still be using it.
    for value in state.get("observer_hubs", []):
        try:
            remove(Path(value), state["id"])
            removed.append(value)
        except (bgui.GuiError, OSError, ValueError) as exc:
            errors.append({"hub": value, "error": str(exc)})
    return {"removed": removed, "errors": errors}


def watch(path, state, env):
    hub = directory(env)
    hubs = state.setdefault("observer_hubs", [])
    if str(hub) not in hubs:
        hubs.append(str(hub))
        bgui.write_state(path, state)
    marker = "observer-" + hub.name
    env = env | {"CODEX_BACKGROUND_GUI_SESSION": marker}
    with locked(hub):
        subscriptions = read_subscriptions(hub)
        previous = list(subscriptions)
        if state["id"] not in subscriptions:
            subscriptions.append(state["id"])
            atomic_json(hub / "subscriptions.json", subscriptions)
        record = read_json(hub / "process.json", {})
        existing = bool(record.get("pid") and record.get("start") and bgui.process_alive(record))
        if not existing:
            ready = hub / (uuid.uuid4().hex + ".ready.json")
            log = hub / "viewer.log"
            argv = [sys.executable, str(Path(__file__).with_name("viewer.py")),
                    "--hub-dir", str(hub), "--ready", str(ready)]
            proc = None
            spawned_record = None
            try:
                with log.open("ab") as output:
                    proc = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL,
                                            stdout=output, stderr=output, start_new_session=True)
                info = bgui.process_info(proc.pid)
                if not info:
                    raise bgui.GuiError(f"Cannot start shared observer; inspect {log}")
                record = spawned_record = {"pid": proc.pid, "start": info["start"], "label": marker,
                                          "log": str(log), "argv": argv}
                atomic_json(hub / "process.json", record)
                bgui.wait_until(ready.is_file, [(proc, record)], timeout=10)
            except BaseException:
                if spawned_record:
                    bgui.terminate_group(spawned_record, marker)
                elif proc and proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                atomic_json(hub / "subscriptions.json", previous)
                raise
            finally:
                ready.unlink(missing_ok=True)
    return {"session": state["id"], "name": state.get("name"), "app_id": bgui.VIEWER_APP_ID,
            "workspace": "Agent", "pid": record["pid"], "log": record["log"],
            "running": True, "existing": existing, "shared": True,
            "hub_dir": str(hub), "preview_status": str(hub / "status.json")}
