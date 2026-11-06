---
name: background-gui
description: Run and visually test native Linux GUI applications in a private Wayland display with a live observer, or through window-addressed input on a patched host niri without changing system focus. Use for application screenshots and background UI interaction with a visible agent cursor. Prefer connected browser tools for web tasks.
---

# Background GUI

Keep `isolated` as the default. When the user wants real native windows on another
workspace, read [references/host-background.md](references/host-background.md)
and use the explicit `host-background` backend only after its doctor reports
ready. The host needs the upstream AgentInput patch; the private observer build
does not provide it. Use the same helper commands, but note the mode differences:

- In host-background, `focus` selects an input target without moving real focus;
  pointer coordinates come directly from the last saved **window** screenshot.
- Let the user switch to the target workspace to see the compositor's agent cursor.
  `watch` does not open an isolated observer for native windows.
- Stop with `stop SESSION` or `stop-host`; native apps remain running.
  Do not fall back to global input when the patch or target is unavailable.
- Use separate app processes/profiles and one host attachment per compositor.
  Use isolated sessions for concurrent independent tasks.

The instructions below describe **isolated sessions**.

Use the bundled helper to give this task its own display: headless Weston → nested niri → application. Show its windows through the shared observer on the user's `Agent` workspace. Each session has separate Wayland, niri IPC, D-Bus and clipboard state. This is display/input isolation, **not a filesystem or network sandbox**.

Resolve `scripts/bgui.py` relative to this skill's directory and invoke it with `python3`. Keep the session ID returned by `start`; do not share a session between concurrent Codex tasks.

On a new host, complete the [installation and niri routing setup](README.md) before using `watch`. The `Agent` workspace and non-focusing window rule are prerequisites for host observation; `doctor` checks dependencies, not the host's routing rules. If routing has not been configured, keep work headless and report that the observer is unavailable rather than opening it on the user's current workspace.

## Start and launch

```bash
python3 <skill-dir>/scripts/bgui.py doctor
python3 <skill-dir>/scripts/bgui.py start --name 'Project · проверка интерфейса' --width 1280 --height 800
python3 <skill-dir>/scripts/bgui.py watch SESSION
python3 <skill-dir>/scripts/bgui.py run SESSION --cwd /absolute/project -- /absolute/path/to/application arg1
python3 <skill-dir>/scripts/bgui.py windows SESSION
```

`run` launches an application in the background and may return before its window appears. Recheck `windows` until the expected app/window appears, using a bounded wait (typically 15 seconds), before taking the first screenshot. If it never appears, inspect the returned application log; do not repeatedly relaunch it. An alive process does not prove that a window opened.

`exec SESSION --cwd PATH -- COMMAND ...` runs a command in that session and waits for it. Pass arguments separately; shell syntax is not interpreted. Use absolute application paths or the project's normal launcher. A single-instance app may need its own profile or documented new-instance flag.

Give each task a short, human-readable `--name` so the user can distinguish concurrent tasks in the observer. Use `rename SESSION 'New task label'` to update a running session without restarting it. Older unnamed sessions remain supported. New sessions give tiled applications the full private output width by default, use modest rounded window geometry, and disable decorative focus/border highlights; dialogs and floating windows retain their normal behavior. Captures reflect that real window appearance, without postprocessing the application image.

## Let the user watch

On a configured niri host, open `watch SESSION` by default unless the user requests headless-only work. Run it from the normal caller shell, **not** through the session's `exec`: the observer window connects to the host display, while its images and all agent input come from the private displays. All watched sessions on the same host display share one observer window. Its top strip contains individual application windows, including dialogs; selecting a thumbnail shows that window below without changing the private application's focus. The observer uses app ID `dev.codex.BackgroundGuiViewer`; the installed [niri rule](examples/background-gui.kdl) routes it to the named `Agent` workspace without focusing it.

The observer uses GTK 4 with libadwaita and a dark appearance confined to its own process. `doctor` reports the observer's UI dependencies separately from the headless session requirements. Rounded display edges do not alter saved screenshots or input coordinates. The application's captured pixels retain their original colors; do not invert or recolor them to match the observer. Reopen an existing observer to load updated UI code; its application can keep running.

Tell the user which named session is open and that they can switch to `Agent` to watch. Do not switch their workspace or focus the observer automatically. The hamburger menu controls refresh rate, cursor, thumbnail size and image rounding; settings persist across observer restarts. The selected window refreshes at the configured rate (default 5 FPS, up to 15, subject to capture cost); other thumbnails refresh less often. This is read-only observation: clicking or typing in an image does not control the application. The private application keeps running when the user returns to another workspace.

The observer defaults to fitting the private image into its window. Its `1:1` mode maps one image pixel to one physical screen pixel, with scrolling when needed; this does not change application geometry or the coordinates used by the agent. A dimmed image with its age means updates are unavailable, not that the displayed application is current. Connection status describes the GUI session, not the agent's reasoning or task progress.

`unwatch SESSION` or `Скрыть сеанс` removes only that session from the shared observer. Closing the observer window or `Скрыть просмотр` hides the whole viewer; applications keep running. `stop SESSION` and the explicit `Завершить сеанс` action stop only the selected session; other sessions and the shared viewer survive. If a watcher cannot open, inspect its error/log and report that observation is unavailable; do not switch to foreground automation as a fallback.

Per-window observation uses a pinned private niri build with a small clipboard-free screenshot patch. `start` selects this build when installed; the system compositor is unchanged. `doctor` reports its readiness separately. Sessions created before this feature need to be recreated to enable per-window images; do not restart another task's running applications automatically. Build/recovery details are in [references/observer-build.md](references/observer-build.md).

## Observe and interact

```bash
python3 <skill-dir>/scripts/bgui.py screenshot SESSION
python3 <skill-dir>/scripts/bgui.py focus SESSION WINDOW_ID
python3 <skill-dir>/scripts/bgui.py click SESSION 320 240
python3 <skill-dir>/scripts/bgui.py type SESSION 'example text'
python3 <skill-dir>/scripts/bgui.py key SESSION ctrl+a BackSpace
python3 <skill-dir>/scripts/bgui.py key SESSION Return
python3 <skill-dir>/scripts/bgui.py scroll SESSION --vertical 120
python3 <skill-dir>/scripts/bgui.py screenshot SESSION --window WINDOW_ID --path /absolute/output.png
```

Read the actual saved image with the available image-viewing tool before choosing coordinates. `screenshot SESSION` captures the whole **private** output; use its top-left origin for pointer coordinates. A window-only screenshot may include decorations/shadows and has a different origin: do not use its coordinates directly for `click`. Window IDs belong to this session's `windows` response. A hidden-workspace window can be captured by ID; focus it **inside the private session** before pointer/keyboard interaction.

Use `move SESSION X Y` to position the pointer before scrolling. Positive vertical scroll values move down. `key` accepts XKB key names such as `Return`, `Escape`, `Tab`, `BackSpace`, `Left`, and chords such as `ctrl+a`, `shift+Tab`, `ctrl+shift+s`. Use `type` for literal text. Inspect `--help` for less common options.

Keep interactions economical: batch a short, predictable sequence in one shell-tool call when its later actions do not depend on unseen UI changes. For example, focus an already-observed entry, type text, and press Enter; then inspect one fresh screenshot. Navigation, dialogs, or uncertain results require observation before continuing. Reuse a current screenshot while the UI is unchanged, return concise results, and bound diagnostic logs. The live observer's local frames do not need to be sent to the model. Normal use does not require rerunning this helper's own validation fixtures or repeatedly reading its implementation.

After a meaningful interaction step that changes the UI, take a fresh screenshot and verify the outcome. Avoid blind action loops. The application may have backend requirements beyond what a GTK fixture proves.

## Session lifecycle and boundaries

- Keep `WAYLAND_DISPLAY`, `NIRI_SOCKET`, `XDG_RUNTIME_DIR` and D-Bus setup inside the helper. Do not export them globally, import them into systemd, or source the session environment into the user's shell.
- Use this helper for all focus, capture and input operations in this workflow. Never replace it with host `niri msg`, `/dev/uinput`, `ydotool`, or a host screenshot when an operation fails. Inspect `status SESSION` and session logs instead.
- An existing host window cannot be moved into this display. Launch a separate instance. If the task specifically requires the already-open host app, a real monitor, desktop portals, hardware rendering, notifications, or the user's existing app session, explain the relevant limitation and choose an appropriate task-authorized route.
- Respect project instructions that reserve visual review for the user. This skill does not require GUI checks for tasks that do not need them.
- Stop the session when work finishes or fails: `python3 <skill-dir>/scripts/bgui.py stop SESSION`. Stop is idempotent. It stops owned processes and retains logs/screenshots for review. If leaving an application running for a requested follow-up, report the session ID.

Report what was actually checked and the saved evidence. Passing an isolated synthetic fixture does not prove the user's real application or main desktop behaves correctly.
