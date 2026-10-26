# Private observer compositor

The observer polls individual native windows by niri IPC ID. It never focuses a window or changes the private clipboard. Window IDs are scoped by session; matching titles do not merge windows. Frames use bounded temporary files in the private runtime directory (tmpfs), not a persistent screenshot archive.

Stock niri 26.04 `screenshot-window` also replaces its clipboard. Its nested Winit backend cannot start PipeWire screencasts. `assets/niri-observer.patch` changes only screenshot clipboard publication when `CODEX_BACKGROUND_GUI_NO_SCREENSHOT_CLIPBOARD=1`. The helper sets this flag only on its own private compositor; PNG file writing and screenshot completion remain intact. Normal host niri is never replaced or reconfigured.

The installed binary and build manifest live in `~/.cache/codex-background-gui/niri-observer/`. The helper verifies the binary digest before using it. The source is upstream GPL-3.0-or-later niri v26.04, pinned by archive SHA256 in `scripts/build_observer_niri.py`; the complete local patch is included in this skill.

To rebuild, provide Rust/Cargo, `patch`, and the normal niri native development dependencies, then run:

```bash
python3 <skill-dir>/scripts/build_observer_niri.py --jobs 2
python3 <skill-dir>/scripts/bgui.py doctor
```

Use Python 3.12 or newer. The build script checks these `pkg-config` modules:

```text
libinput gbm libseat libdisplay-info libudev xkbcommon
wayland-client wayland-server egl pango pangocairo pixman-1
```

Install their development headers and libraries using your distribution's package
manager. See the pinned upstream [niri v26.04 build instructions](https://github.com/niri-wm/niri/blob/v26.04/docs/wiki/Getting-Started.md#building)
for distribution package names. This build uses `--no-default-features`, so the
upstream optional D-Bus/systemd/PipeWire build features are disabled; its native
graphics/input build dependencies remain required.

`--build-dir PATH` selects a persistent build location; both relative paths and
`~`-prefixed paths are normalized before Cargo runs. Use a fresh build directory
after a failed attempt: an existing extracted source directory is intentionally
not overwritten. `--install-built PATH` is only for a binary that you have already
built from this exact pinned source and included patch; it records the supplied
binary's digest rather than independently proving its provenance.

The build script never installs system packages. A private `PKG_CONFIG_PATH` can point to unpacked development RPMs. New sessions pick up the build; existing compositors cannot be replaced without relaunching their applications. Logs and source/build files remain in the reported build directory for diagnosis.

The shared observer registry, settings, process identity and status are under `~/.local/state/codex-background-gui/observers/<host-display-hash>/`. Session processes never own the shared observer. Closing it keeps subscriptions for the next `watch`; `unwatch` and `stop` unregister only their session. An empty observer remains open for subsequent tasks.
