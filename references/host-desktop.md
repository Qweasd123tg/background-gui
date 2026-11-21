# Native desktop pointer (experimental)

`host-desktop` draws an agent cursor above the native desktop and routes input to
the Wayland surface under it. The current patch draws a violet arrow and an amber
ring at the button-down location. It covers visible windows, their popups and
layer-shell panels. Clicking selects the agent's keyboard target. Hovering another
window does not change that target. The real seat pointer location, keyboard focus
and window activation are preserved. No viewer or special application is needed.

This requires **both** the upstream AgentInput integration and the additional
[`assets/niri-agent-desktop.patch`](../assets/niri-agent-desktop.patch) in the
**running** host compositor. Installing a new executable does not update an already
running compositor; use a separate login entry and log into it manually. Never
restart the user's active compositor automatically.

## Commands

```bash
python3 scripts/bgui.py doctor --backend host-desktop
python3 scripts/bgui.py start --backend host-desktop --name 'Native desktop task'
# Keep the returned session ID. Doctor lists active monitor names.
python3 scripts/bgui.py screenshot SESSION --output eDP-1
# Inspect the actual saved image before choosing coordinates.
python3 scripts/bgui.py click SESSION 320 240
python3 scripts/bgui.py type SESSION 'Пример текста'
python3 scripts/bgui.py move SESSION 600 450
python3 scripts/bgui.py scroll SESSION --vertical 120
python3 scripts/bgui.py key SESSION ctrl+a Return
python3 scripts/bgui.py stop SESSION
```

Capture each monitor separately, including its complete desktop and panels.
`--output` is required when several monitors are active. Coordinates for `move`,
`click` and `drag` are physical pixels in the **last captured monitor image**.
The adapter converts them into global logical coordinates, including fractional
scale and negative monitor positions. A changed monitor layout invalidates the
capture. `--show-pointer` includes both real and agent cursors in a capture.
Observe a new frame after UI changes; an immediate frame can precede an
application's next commit, so capture again if it still shows the previous state.

The cursor appears on `start`, including over empty desktop space. It hides on
`stop`, the existing `stop-host` hotkey, or after 120 seconds without agent input.
Empty `AgentDesktopInput` events keep an existing cursor alive. The compositor
releases held agent buttons/keys on detach or expiry. Host windows stay open.
Window and desktop attachments share one compositor lease.

For Chromium/Electron's missing Unicode characters, use
`type SESSION '…' --unicode-input CtrlShiftU` if needed. Desktop `auto` uses
`Keymap`; unlike the window adapter, it does not infer the clicked application's
ID. Dragging retains the original surface while a button is held. Cross-client
Wayland drag-and-drop is not implemented.

## Shared desktop limits

Wayland clients cannot distinguish two independent pointer/keyboard streams on
their existing seat. The desktop extension refuses delivery to a client while the
real pointer or keyboard is using that client. Cursor motion remains visible over
such a client, but clicking/typing returns an error. Use a separate app process or
browser profile when necessary. This is a compositor-visible second pointer, not
an independent login or a second set of workspaces.

Compositor-owned UI (overview, exit dialog, screenshot selector, server-side window
borders) is not a Wayland client and is not driven by this API. Keyboard events go
to the selected app rather than executing compositor shortcuts. Input and desktop
capture are rejected while the session is locked. This extension targets the
pinned Linux niri build, not macOS or arbitrary Wayland compositors.

## Build and provenance

The incremental patch applies **after** these exact upstream inputs:

| Source | Commit |
| --- | --- |
| `niri-wm/niri` | `4263c9a17f80fc8019bd474f85b1cc1dcbac785c` |
| `lihaoze123/niri-computer-use` | `bd6e39f3822ab5e14158def4817f89725064061d` |
| `Smithay/smithay` | `9786228efad25d65af0bb516642c1e8b0c359f37` |

First apply the integration's `niri-ipc-tiled-window-position.patch` and
`niri-agent-input.patch` to niri, and its `smithay-input-method-keymap.patch` to
Smithay. Follow the integration's build setup for the patched Smithay dependency.
Then apply this repository's extension:

```bash
git -C /absolute/niri apply --check /absolute/background-gui/assets/niri-agent-desktop.patch
git -C /absolute/niri apply /absolute/background-gui/assets/niri-agent-desktop.patch
cd /absolute/niri
export CARGO_PROFILE_RELEASE_DEBUG=0 CARGO_PROFILE_RELEASE_LTO=false
export CARGO_PROFILE_RELEASE_OPT_LEVEL=2 CARGO_PROFILE_RELEASE_CODEGEN_UNITS=16
export NIRI_BUILD_VERSION_STRING=26.04-agent-desktop-v2
cargo test --locked --release --lib -j 2 agent_input::
cargo build --locked --release --bin niri -j 2
```

Keep niri's default features for a real session (D-Bus, systemd and PipeWire
support). Fedora 44's bindgen may need
`BINDGEN_EXTRA_CLANG_ARGS=-resource-dir=$(clang -print-resource-dir)`.
The local archive-based build used the pinned patched Smithay through local Cargo
paths, removing only those two source entries from Cargo.lock and preserving
dependency versions. Personal paths and compiled binaries are not in this repo.

Install the finished executable into a versioned private directory, then point
`~/.local/lib/background-gui-host/current` at that directory. The
[session launcher](../examples/niri-agent-session) and
[systemd service](../examples/niri-agent.service) use `current`; keep the old
versioned directory for rollback. The launcher validates the config and checks
`build.json`'s `binary_sha256` when that manifest is present. Update the symlink
only after the build and private checks pass. See
[separate login setup](host-background.md#separate-systemdgdm-session).

## Validation

The repository contains adapter/lifecycle tests in `tests/test_host_desktop.py`
and a native visual fixture in `tests/native_desktop_supervisor.py`.
Run the native supervisor **inside an owned isolated bgui session**, never on the
host display directly. It starts a nested compositor, three separate GTK clients
and a layer-shell panel, and writes a scoped `test-env.json` plus `ready.json`.
Use the helper through that scoped environment, inspect captures before input,
then touch its artifacts directory's `stop` file and stop the outer session.
Only its own child processes are terminated.

To exercise the v2 parallel window path, run the following from the Background
GUI checkout with a new artifacts directory:

```bash
python3 tests/run_native_parallel.py /absolute/niri/target/release/niri /absolute/path/to/new-artifacts
```

This runner switches the nested seat's keyboard focus between two GTK windows
while two agent attachments type into two other GTK windows. It checks all four
text values, rendered cursor colors, focused window IDs and both detach receipts. The
test uses the development machine's PyGObject (GTK 4) and Pillow.

The v1 Fedora 44 nested check verified both application clients receiving Cyrillic
and Unicode, a layer-shell button, popup input, visible cursor over empty desktop,
preserved user focus, refusal while the user pointer occupied a target client,
and clean detach. The final executable was additionally checked at scale 1.5
with global monitor origin (-200, 100): Unicode, click position, visible desktop
cursor and preserved user focus passed. Two niri library tests cover crossing
outputs without changing the real seat and rejecting malformed batches before
attaching. A nested software-rendered check does not establish hardware,
mixed-monitor or main-session behavior; verify those after a deliberate login.
