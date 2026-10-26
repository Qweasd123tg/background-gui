#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Clipboard-free native-window frames from an explicitly capable private niri.

No focus changes, host display connection, global environment changes, or image
transforms occur here. Old sessions fail closed instead of using the stock
clipboard-writing screenshot action.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import shutil
import struct
import tempfile
import threading
import time
import weakref

import bgui

CAPABILITY = "clipboard-free-v1"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_END = b"\0\0\0\0IEND\xaeB`\x82"
_locks_guard = threading.Lock()
_window_locks = weakref.WeakValueDictionary()


@dataclass(frozen=True)
class WindowFrame:
    width: int
    height: int
    stride: int
    format: str
    data: bytes
    timestamp: float
    sequence: int


@dataclass(frozen=True)
class StreamSnapshot:
    frame: WindowFrame | None = None
    error: str | None = None
    closed: bool = False


def _fps(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 1 <= value <= 15:
        raise ValueError("Window stream fps must be within 1..15")
    return float(value)


def _capture_lock(state, window_id):
    key = (state.get("runtime"), window_id)
    with _locks_guard:
        lock = _window_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _window_locks[key] = lock
        return lock


class WindowStream:
    """A bounded single-flight polling stream; start/latest never block GTK.

    ``latest()`` returns an immutable snapshot. A frame's ``format`` is ``PNG``;
    ``data`` is the complete original encoded image and ``stride`` is zero.
    ``timestamp`` uses time.monotonic(). Call ``stop()`` off the GTK thread;
    it waits at most four seconds for bounded in-flight capture/cleanup.
    """

    def __init__(self, state, window_id, fps=5, cursor=False):
        if isinstance(window_id, bool) or not isinstance(window_id, int) or not 0 < window_id < 2**64:
            raise ValueError("Window ID must be a positive uint64")
        self.state = dict(state)
        self.window_id = window_id
        self.cursor = bool(cursor)
        self._fps = _fps(fps)
        self._lock = threading.Lock()
        self._capture_lock = _capture_lock(self.state, window_id)
        self._snapshot = StreamSnapshot()
        self._stop = threading.Event()
        self._thread = None
        self._started = False

    def start(self):
        with self._lock:
            if self._started:
                return self
            self._started = True
            if self.state.get("window_capture") != CAPABILITY:
                self._snapshot = StreamSnapshot(
                    error="Этот сеанс не поддерживает захват отдельных окон без изменения буфера обмена. Создайте новый сеанс.",
                    closed=True,
                )
                return self
            if self._stop.is_set():
                self._snapshot = StreamSnapshot(closed=True)
                return self
            self._thread = threading.Thread(
                target=self._run, name=f"window-stream-{self.window_id}", daemon=True
            )
            self._thread.start()
        return self

    def latest(self):
        with self._lock:
            return self._snapshot

    def set_fps(self, fps):
        fps = _fps(fps)
        with self._lock:
            self._fps = fps

    def stop(self):
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=4)
        with self._lock:
            current = self._snapshot
            self._snapshot = StreamSnapshot(current.frame, current.error, closed=True)

    def _publish(self, *, frame=None, error=None, closed=False):
        with self._lock:
            if self._stop.is_set():
                return
            self._snapshot = StreamSnapshot(frame or self._snapshot.frame, error, closed)

    def _target_closed(self):
        try:
            windows = json.loads(bgui.checked(
                ["niri", "msg", "-j", "windows"], env=bgui.isolated_env(self.state), timeout=1
            ))
            if not isinstance(windows, list) or any(not isinstance(window, dict) or "id" not in window for window in windows):
                return False
            return not any(window["id"] == self.window_id for window in windows)
        except (OSError, ValueError, bgui.GuiError):
            # A failed IPC probe says nothing about the window. Keep retrying
            # unless the session itself has conclusively stopped or lost one
            # of its recorded core processes.
            if self.state.get("status") not in (None, "running"):
                return True
            for record in self.state.get("processes", ()):
                if isinstance(record, dict) and record.get("label") in ("dbus", "weston", "niri"):
                    try:
                        if not bgui.process_alive(record):
                            return True
                    except (OSError, ValueError, KeyError):
                        pass
            return False

    def _read_frame(self, image):
        deadline = time.monotonic() + 1
        while not self._stop.is_set() and time.monotonic() < deadline:
            try:
                data = image.read_bytes()
                if len(data) >= 33 and data.startswith(_PNG_SIGNATURE) and data.endswith(_PNG_END):
                    width, height = struct.unpack(">II", data[16:24])
                    if width and height:
                        return width, height, data
            except FileNotFoundError:
                pass
            self._stop.wait(.01)
        if not self._stop.is_set():
            raise bgui.GuiError("Не удалось получить полный кадр окна")
        return None

    def _run(self):
        scratch = None
        root = None
        sequence = 0
        try:
            # Validate every private endpoint/component before creating or using
            # a capture path. Never derive endpoints from the host environment.
            bgui.isolated_env(self.state)
            root = bgui.private_dir(Path(self.state["runtime"]) / "observer-frames")
            scratch = Path(tempfile.mkdtemp(prefix=f"window-{self.window_id}-", dir=root))
            image = scratch / "frame.png"
            while not self._stop.is_set():
                started = time.monotonic()
                failed = False
                acquired = self._capture_lock.acquire(timeout=.1)
                if not acquired:
                    self._stop.wait(.01)
                    continue
                try:
                    env = bgui.isolated_env(self.state)
                    image.unlink(missing_ok=True)
                    bgui.checked([
                        "niri", "msg", "action", "screenshot-window",
                        "--id", str(self.window_id), "--show-pointer", str(self.cursor).lower(),
                        "--path", str(image),
                    ], env=env, timeout=2)
                    result = self._read_frame(image)
                    if result is not None:
                        width, height, data = result
                        sequence += 1
                        self._publish(frame=WindowFrame(
                            width, height, 0, "PNG", data, time.monotonic(), sequence
                        ))
                except (OSError, ValueError, bgui.GuiError) as exc:
                    failed = True
                    closed = self._target_closed()
                    self._publish(error=str(exc), closed=closed)
                    if closed:
                        break
                finally:
                    image.unlink(missing_ok=True)
                    self._capture_lock.release()
                with self._lock:
                    period = 1 / self._fps
                self._stop.wait(max(1 if failed else .001, period - (time.monotonic() - started)))
        except (OSError, ValueError, bgui.GuiError) as exc:
            self._publish(error=str(exc), closed=True)
        finally:
            if scratch:
                shutil.rmtree(scratch, ignore_errors=True)
            if root:
                try:
                    root.rmdir()
                except OSError:
                    pass
