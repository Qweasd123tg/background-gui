# Background GUI

Run a native Linux application for an AI agent while you keep using your desktop.

| Mode | Where the agent works | Where you see its cursor |
| --- | --- | --- |
| `isolated` (default) | A private Weston → niri display | In the shared observer window; cursor is enabled for new viewer settings |
| `host-desktop` (experimental) | The visible native desktop, routing to windows, popups and layer-shell panels under the agent pointer | Across the desktop above windows; requires the additional [AgentDesktop patch](references/host-desktop.md) |
| `host-background` (experimental) | A real window on your patched host niri, including another workspace | In that native window, drawn by the compositor's agent-input patch |

The native mode needs the **running host compositor** from [niri-computer-use](https://github.com/lihaoze123/niri-computer-use). It does not start Weston or nested niri. Read [host setup and commands](references/host-background.md) before using it:

```bash
python3 <repo>/scripts/bgui.py doctor --backend host-background
python3 <repo>/scripts/bgui.py start --backend host-background --window WINDOW_ID
```

The v2 patch supports parallel agents pinned to separate native application windows. Each has its own colored pointer. See [parallel window input](references/host-background.md#concurrent-agents-in-different-applications-v2).

The native adapter has protocol/lifecycle tests and a GTK smoke check in a nested patched niri on Fedora 44. That check verified background clicks, Unicode text, preserved focus, the visible agent cursor and detach behavior. Main-session and hardware behavior still need verification after logging into a patched niri session. See the optional [systemd/GDM session examples](references/host-background.md#separate-systemdgdm-session) for a separate login entry. `stop-host` is a hotkey target for detaching the active native attachment.

In a shared window, ordinary `click` and `type` address accessible buttons and
editable fields while the user types in another field. The agent keeps its own
insertion position. v3 draws the agent arrow using the desktop's Xcursor theme,
size and hotspot, with a distinct fill color for each agent. On v4 a window-targeted
left click briefly outlines the clicked button, link or field in that same color
when the application reports its bounds.

To work in the *same window* while the user types, the experimental
`scripts/bgui_widgets.py inspect --window ID` lists named actions exposed by the
application through AT-SPI. `scripts/bgui_widgets.py invoke --window ID --token TOKEN`
resolves that action again in the same process, window and widget hierarchy. The
helper never requests keyboard focus. Individual applications decide what their
actions do. A widget that handles several records internally still needs an
application-specific record ID for an operation such as posting to a particular chat.
This helper requires PyGObject and the Atspi 2.0 introspection library.

The rest of this guide describes `isolated`. Each task gets a private Wayland display with its own input, niri IPC, D-Bus session and clipboard:

```text
headless Weston → nested niri → separate application instance
                                  ↓
                       one read-only host observer
```

The dark GTK 4/libadwaita observer shows individual application windows across sessions. Select a thumbnail to inspect a window without changing the application's focus. The selected view supports Fit and physical-pixel 1:1 modes, with configurable **1 / 2 / 5 / 10 / 15 FPS** (default 5, actual rate depends on capture cost). Other thumbnails refresh less often. Observation continues while you work on another workspace.

This is **display and input isolation, not a filesystem or network security sandbox**. Applications run as your user and retain access to your files and network.

## Supported setup

Tested on **Fedora 44, niri 26.04, Python 3.14, GTK 4 and libadwaita**. Python **3.12 or newer** is required. The supported host setup is niri on Linux; this is not a general solution for every Wayland compositor or macOS. The observer requires PyGObject, GTK 4 and libadwaita **1.7 or newer**.

## Install

Clone directly into your Codex user skills directory:

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
git clone https://github.com/Qweasd123tg/background-gui.git "${CODEX_HOME:-$HOME/.codex}/skills/background-gui"
skill_dir="${CODEX_HOME:-$HOME/.codex}/skills/background-gui"
python3 "$skill_dir/scripts/bgui.py" doctor
```

Git refuses to overwrite an existing nonempty destination. Review an existing installation before updating it. For agent instructions, see [SKILL.md](SKILL.md); request `$background-gui` in a new Codex session after setup.

`doctor` reports the prerequisites without installing packages:

| Check | Requirements |
| --- | --- |
| Headless session and input | Python ≥ 3.12, Weston with its headless backend and kiosk shell, niri, wtype, D-Bus tools |
| Pointer helper, built locally on first use | C compiler, `pkg-config`, `wayland-scanner`, Wayland client development files |
| Observer UI | PyGObject, GTK 4, libadwaita ≥ 1.7 |
| Per-window observer capture | The private niri build described below |

The JSON fields `ready`, `viewer_ui_ready` and `viewer_capture_ready` describe separate checks. A headless session can work before the full observer is available.

### Build the private capture compositor

The full per-window observer needs the bundled, pinned niri build. Stock niri 26.04 window screenshots modify its clipboard, and its nested backend cannot provide the required PipeWire capture. The small included patch permits clipboard-free captures in the private compositor.

Provide Rust/Cargo, `patch`, and niri's native development dependencies, then run:

```bash
python3 "$skill_dir/scripts/build_observer_niri.py" --jobs 2
python3 "$skill_dir/scripts/bgui.py" doctor
```

The build downloads pinned upstream source and Cargo dependencies, verifies the source archive, and installs only into `~/.cache/codex-background-gui/niri-observer/`. It does **not** replace system niri or install system packages. New sessions select this build automatically; an already-running session must be recreated to gain this capability. See [observer build details](references/observer-build.md) for recovery and build requirements.

### Route the observer before the first `watch`

Copy the [example niri configuration](examples/background-gui.kdl) and manually include it in your active host config. These commands leave an existing example destination untouched:

```bash
mkdir -p "$HOME/.config/niri/niri-configs"
cp -n "$skill_dir/examples/background-gui.kdl" "$HOME/.config/niri/niri-configs/background-gui.kdl"
```

Add this line to `~/.config/niri/config.kdl` (or your actual active config), adjusting the relative path if needed:

```kdl
include "niri-configs/background-gui.kdl"
```

If you already declare an `Agent` workspace, keep a single declaration. Review existing window rules for conflicts, then validate and reload **from the normal host shell**:

```bash
niri validate
niri msg action load-config-file
```

Use your active config path with `niri validate --config PATH` if it is nonstandard. Reload only after validation succeeds. The example opens the exact observer app ID on `Agent`, at half width, without focusing it. No workspace switch is required; switch to `Agent` yourself when you want to watch. Without this routing, the helper cannot promise that opening the observer preserves host focus.

## Quick start

Run these commands from your normal host shell. Commands return JSON; keep the `session` value from `start`:

```bash
python3 "$skill_dir/scripts/bgui.py" start --name 'My application · UI check' --width 1280 --height 800
SESSION='paste-the-returned-session-id'
python3 "$skill_dir/scripts/bgui.py" watch "$SESSION"
python3 "$skill_dir/scripts/bgui.py" run "$SESSION" --cwd /absolute/project -- /absolute/path/to/application arg1
python3 "$skill_dir/scripts/bgui.py" windows "$SESSION"
```

`run` returns before a window necessarily appears. Recheck `windows` with a bounded wait, typically up to 15 seconds. If no window appears, inspect the returned application log instead of repeatedly launching more instances. Single-instance applications may need a separate profile or a documented new-instance flag.

Capture the private output, inspect the saved image, then use its coordinates:

```bash
python3 "$skill_dir/scripts/bgui.py" screenshot "$SESSION"
python3 "$skill_dir/scripts/bgui.py" click "$SESSION" 320 240
python3 "$skill_dir/scripts/bgui.py" type "$SESSION" 'example text'
python3 "$skill_dir/scripts/bgui.py" key "$SESSION" ctrl+a BackSpace
python3 "$skill_dir/scripts/bgui.py" key "$SESSION" Return
python3 "$skill_dir/scripts/bgui.py" screenshot "$SESSION"
python3 "$skill_dir/scripts/bgui.py" stop "$SESSION"
```

Whole-output screenshots use the pointer coordinate origin. A window-only capture (`screenshot SESSION --window ID`) has a different origin. Focus a window with `focus SESSION ID` inside the private session before interacting with it. See [SKILL.md](SKILL.md) and `bgui.py --help` for scrolling, key chords, synchronous `exec`, naming and lifecycle details. Keep one session per concurrent task; do not export its returned environment into your host shell.

## Observer and session lifetime

- `watch SESSION` registers a session with the shared observer for the current host display. Call it from the host shell, outside the private session.
- Clicking a preview only selects it. Agent input continues through the private helper commands.
- `unwatch SESSION` removes that session's previews; its applications keep running.
- Closing the observer hides it; registered applications keep running. A later `watch` reopens it.
- `stop SESSION` stops only that session's owned processes and unregisters it. Other sessions and the shared observer survive. Stop is idempotent; logs and saved screenshots remain available.

The menu persists refresh rate, cursor visibility, thumbnail size, rounding and Fit/1:1 settings. Cursor visibility defaults to on for new settings; an existing saved preference is respected. A dimmed frame with an age label means updates are unavailable, not that the application is current. UI labels are currently Russian.

## Limits and privacy

An existing host window cannot be moved into the private display: launch a separate application instance. Sessions use software graphics; hardware acceleration, host portals, notifications and an application's existing desktop session may behave differently. This workflow does not establish compatibility with a real monitor or every application backend.

Session state, application logs and explicit screenshots live under `${XDG_STATE_HOME:-$HOME/.local/state}/codex-background-gui/`. Build caches are separate; live capture frames use bounded temporary files in private runtime directories. These files can contain application data. They are local runtime artifacts, not repository content: do not commit sessions, logs, screenshots, caches, compiled binaries or personal desktop configuration.

The repository contains source, the capture patch and a generic configuration example. Code and documentation are **GPL-3.0-or-later**; the bundled virtual-pointer protocol retains its MIT license. See [LICENSE](LICENSE) and [THIRD_PARTY.md](THIRD_PARTY.md).
