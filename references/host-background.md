# Native background windows (experimental)

`host-background` connects directly to the **running host** niri compositor patched by
[lihaoze123/niri-computer-use](https://github.com/lihaoze123/niri-computer-use).
Applications stay on your normal desktop and keep its rendering acceleration.
Place them on `Agent`, keep working elsewhere, and switch there yourself to watch.
The patch paints a separate agent cursor inside the native window; Background GUI
does not move the system cursor, activate a window, or switch workspaces.

## Prerequisites

The IPC contract was checked against upstream commit
[`bd6e39f3822ab5e14158def4817f89725064061d`](https://github.com/lihaoze123/niri-computer-use/tree/bd6e39f3822ab5e14158def4817f89725064061d),
which pins niri source `4263c9a17f80fc8019bd474f85b1cc1dcbac785c`.
See its [protocol and limits](https://github.com/lihaoze123/niri-computer-use/blob/bd6e39f3822ab5e14158def4817f89725064061d/docs/agent-input.md).
Stock niri and our private observer build do **not** implement this protocol.

Use upstream's NixOS integration, or build its standalone `niri-patched` package
on a Linux machine with Nix already configured:

```bash
nix build github:lihaoze123/niri-computer-use/bd6e39f3822ab5e14158def4817f89725064061d#niri-patched
```

This builds a binary; it does not patch the compositor you are currently running.
Configure your normal niri session launcher to use the resulting binary and
log out/in before testing. Keep your existing session entry as a recovery option.
Follow upstream for compositor dependencies and upgrades. No installer in this
repository replaces system niri or edits your desktop configuration.

## Separate systemd/GDM session

The optional [launcher](../examples/niri-agent-session),
[user service](../examples/niri-agent.service),
[config wrapper](../examples/niri-agent-config.kdl) and
[GDM entry template](../examples/niri-agent.desktop.in) keep a separate login
entry for testing. Review the destination files before copying these examples.

The default layout expects the patched executable at
`~/.local/lib/background-gui-host/current/bin/niri` and its config at
`~/.config/niri/agent-input.kdl`. Make `current` a symlink to a versioned private
installation directory, keeping the previous directory for rollback. Install the launcher as
`~/.local/bin/niri-agent-session`, the service under `~/.config/systemd/user/`,
and the config wrapper as `~/.config/niri/agent-input.kdl`. The wrapper includes
your regular `config.kdl` plus `niri-configs/host-background.kdl`; the regular
config must declare `Agent` once. Copy/review the host rule example separately.

The launcher accepts `BGUI_NIRI_INSTALL_DIR`, `BGUI_NIRI_CONFIG` and
`BGUI_NIRI_SYSTEMD_UNIT` overrides. Update the service's paths too when choosing
a different layout. If `build.json` exists beside `bin/`, the launcher verifies
its `binary_sha256`; a separately built executable can also be used without
that optional manifest.

Replace `@LAUNCHER@` in the GDM template with the absolute installed launcher
path, quoting it if it contains spaces. Install the resulting desktop file as
`/usr/share/wayland-sessions/niri-agent.desktop` with administrator privileges.
Keep the normal Niri session entry for returning to the previous setup.

From a normal terminal, validate the installation before logging out:

```bash
systemctl --user daemon-reload
"$HOME/.local/bin/niri-agent-session" --check
```

The launcher refuses to replace a running graphical session. Log out yourself
and select **Niri AgentInput (test)** in GDM. The launcher imports the new session
environment and starts `niri-agent.service` with the patched executable on PATH.
It treats `reset-failed` as optional: on a fresh user manager the unit may not
be loaded yet, and that must not prevent its first start. Errors from the actual
service start still propagate.

If GDM returns to the login screen, inspect both startup output and service logs:

```bash
journalctl --user -u niri-agent.service -b
journalctl -b -g 'gdm-wayland-session|niri-agent'
```

## Check the running compositor

Run the helper from your normal niri shell. It needs the current session's
`XDG_RUNTIME_DIR`, `WAYLAND_DISPLAY` and `NIRI_SOCKET`. It uses Python's Unix
socket client directly, so a stock `niri msg` executable on PATH is fine.
When a caller survives logout and retains a missing old socket, the helper can
recover the current scoped endpoint from the user manager, requiring the same
runtime directory and an IPC peer PID belonging to the active niri service. It
does not import the manager environment or copy unrelated variables.
`doctor` probes the two new request variants using a nonexistent window ID;
the probes do not send input to any window or create a screenshot.

```bash
python3 <repo>/scripts/bgui.py doctor --backend host-background
```

## Behavior references

- [Codex background computer use on macOS](https://openai.com/index/codex-for-almost-everything/) demonstrates independent agent cursors while a person works in another app. The public announcement describes the behavior.
- [Sway seats](https://github.com/swaywm/sway/blob/master/sway/sway-input.5.scd) show how a compositor can maintain distinct pointers and keyboard focus per seat.
- [The upstream niri integration](https://github.com/lihaoze123/niri-computer-use/blob/bd6e39f3822ab5e14158def4817f89725064061d/docs/agent-input.md) documents the window-directed Wayland input used by this project and its limits with clients sharing one seat.
- [Pixel Agents](https://github.com/pixel-agents-hq/pixel-agents) visualizes the activities of several coding agents using events. Its activity view is a separate problem from delivering native window input.

## Concurrent agents in different applications (v2)

Start a pinned attachment for each independent application process:

```bash
python3 scripts/bgui.py start --backend host-background --parallel --window 11 --name Editor
python3 scripts/bgui.py start --backend host-background --parallel --window 12 --name Calculator
```

Use each returned session ID with the usual screenshot, move, click, scroll, type
and key commands. Each session gets a different arrow color (violet, teal, pink or
blue). Niri sends a complete click or key chord to the selected Wayland client and
releases that stream before the next batch. A client's application state, such as
its selected entry, persists between batches.

If the user takes focus in the target client, the helper routes ordinary left
clicks and text to the application's accessibility tree. It matches the widget
using the window screenshot's logical coordinates. A button receives its
published action. Clicking an editable field selects it for the agent. Later
`type` commands insert at the agent's position while the user types in another
field. When the app omits character hit-testing, insertion starts at the end of
the field. The helper also remembers an accessible entry selected before a focus
change. A new window selection clears that binding. Shared editing requires
PyGObject and the Atspi 2.0 introspection library. Custom-drawn controls without
an accessible action, keys, right clicks and drags require independent window
input. Give each typist a separate field.

Each session is pinned until stopped, including if the window closes. Timed drags use an exclusive attachment. One global desktop
attachment and the parallel window group are mutually exclusive. `stop SESSION`
releases only that session; `stop-host` detaches the group. The v1 binary supports
the existing exclusive modes only.

v4 outlines an accessible button, link or field for 1.2 seconds on a left
click. The helper reads the control bounds before dispatch, so a navigation can
remove the control immediately after the click. Niri clips the outline to the
clicked window and uses the agent's cursor color. When a widget provides no
reliable bounds, ordinary input retains its point indicator. Firefox launched
through the window helper receives `GNOME_ACCESSIBILITY=1` by default. An
already-running browser can have a different accessibility configuration.

A window screenshot with `--show-pointer` on v2 or later includes the agent arrow. For an
action that depends on exactly what was visible, inspect a screenshot taken without
cursors, then pass its exact path to the click:

```bash
python3 scripts/bgui.py click SESSION 240 180 --seen /absolute/inspected-frame.png
```

The helper compares a new window capture with the inspected PNG and checks the
current title. Any changed pixels (including animations), geometry or title
invalidate the click. There is still a small interval between validation and
input. Verify the effect in the application, especially before retrying any send.

## Existing window on another workspace

Move an independent application window to `Agent` yourself, return to your own
workspace, and choose its ID from the doctor's window list:

```bash
python3 <repo>/scripts/bgui.py start --backend host-background --window 4 --name 'Agent · UI test'
SESSION='paste-the-returned-session-id'
python3 <repo>/scripts/bgui.py screenshot "$SESSION"
python3 <repo>/scripts/bgui.py click "$SESSION" 320 240
python3 <repo>/scripts/bgui.py type "$SESSION" 'Привет 🙂'
python3 <repo>/scripts/bgui.py key "$SESSION" ctrl+a Return
python3 <repo>/scripts/bgui.py drag "$SESSION" 100 100 300 200 --duration 0.5
python3 <repo>/scripts/bgui.py screenshot "$SESSION"
python3 <repo>/scripts/bgui.py stop "$SESSION"
```

Coordinates are **physical pixels in the last saved window image**. The adapter
converts them to window logical coordinates using `(pixel - geometry_offset) / scale`.
Take a new screenshot after selecting a target or resizing it. `move`/`click`
positions the pointer before `scroll`; scroll distances are logical pixels.
`key` supports common named keys, F1–F12 and modifier chords at US physical key
positions. Use `type` for literal text. `type --unicode-input auto` selects
`CtrlShiftU` for recognized Chromium/Electron app IDs and `Keymap` otherwise;
override it for an app with a custom ID. Do not retry failed text blindly: it
may have been partially delivered.

`focus SESSION ID` **selects the adapter's target**, without changing real focus.
`windows SESSION` lists native windows. `watch SESSION` reports where to watch;
it does not open the isolated observer. See the real window and its agent cursor
by switching to its workspace yourself. The cursor is a compositor decoration,
and v2+ window captures include it with `--show-pointer`.

## Launch a separate application instance

An attachment can start without a target, then launch an app and select its window:

```bash
python3 <repo>/scripts/bgui.py start --backend host-background --name 'Agent task'
SESSION='paste-the-returned-session-id'
python3 <repo>/scripts/bgui.py launch "$SESSION" -- kitty --class bgui-agent
python3 <repo>/scripts/bgui.py windows "$SESSION"
python3 <repo>/scripts/bgui.py focus "$SESSION" WINDOW_ID
python3 <repo>/scripts/bgui.py screenshot "$SESSION"
```

`launch` is an alias of `run`. Launches set `NIRI_AGENT_LAUNCH=1` and clear
activation tokens. The patched compositor keeps new windows from taking focus.
Use the optional [host rule](../examples/host-background.kdl) to put an app with
ID `bgui-agent` on `Agent`. Review/include the rule yourself and validate the
config with the patched niri before reloading it. Reuse a single `Agent`
workspace declaration. Existing windows keep their workspace.

Single-instance applications may forward requests to an existing process and
ignore the new environment. Give Chromium a separate `--user-data-dir`, Firefox
a separate profile/new instance, or use the application's documented equivalent.

## Stop and interference

`stop SESSION` detaches and leaves native applications running, including apps
launched through this attachment. It never signals the host compositor or kills
host windows. `stop-host` detaches the current compositor's attachment without
needing its session ID, and can be bound to a hotkey. A bounded drag notices the
stop request between steps and releases its button. A request already accepted
by the compositor cannot be retracted.

A focused process can be attached and captured. Shared left clicks and text use
the selected application's controls. Move paints a window-local marker. v3 uses
the configured Xcursor arrow, size and hotspot and keeps the session color for
accessibility actions. If the application does not expose the selected widget,
the helper reports the failed hit. Gesture errors and missing acknowledgments
are never replayed. Independent processes can use the pinned parallel mode.

Wayland clipboard and cross-application drag-and-drop remain upstream limits.
In-app drag is supported; universal application compatibility is not established.
Protocol/lifecycle tests cover the adapter and cold session-launcher startup.
A nested patched niri GTK check on Fedora 44 verified Unicode text, background
input, preserved focus, cursor rendering and detach without killing applications.
This uses software rendering; main-session hardware behavior still requires a
test after logging into the patched host session.

## Other ready-made options

| Project | Appropriate use |
| --- | --- |
| [agent-workspace-linux](https://github.com/agent-sh/agent-workspace-linux) | A separate desktop and native viewer over MCP, with pause/stop controls; no host niri patch required. |
| [niri-computer-use](https://github.com/lihaoze123/niri-computer-use) | Native windows on another niri workspace, with a compositor-rendered agent cursor. |
| [Cua Driver](https://cua.ai/docs/cua-driver/concepts/platform-support) | Cross-platform input/inspection; stock Wayland raw background input has compositor limits. |
