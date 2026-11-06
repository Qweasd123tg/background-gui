# License and attribution

Original Background GUI code and documentation: Copyright (C) 2026 Qweasd123tg.
Licensed under GPL-3.0-or-later, except for the third-party protocol file below.
See [LICENSE](LICENSE) for the full GNU GPL version 3 text.

## Private niri patch

`assets/niri-observer.patch` modifies `src/niri.rs` from
[niri v26.04](https://github.com/niri-wm/niri/tree/v26.04), a project by Ivan
Molodetskikh and the niri contributors. Upstream declares
[GPL-3.0-or-later](https://github.com/niri-wm/niri/blob/v26.04/Cargo.toml).
The patch retains upstream source context and is distributed under the same license.

The modification suppresses screenshot clipboard publication only when the private
compositor is started with `CODEX_BACKGROUND_GUI_NO_SCREENSHOT_CLIPBOARD=1`.
It preserves PNG file output and screenshot-completion events.
`scripts/build_observer_niri.py` downloads a pinned upstream source archive,
verifies its SHA256, applies the patch, and builds a separate local executable.
This repository distributes source and the patch, not niri binaries or vendored
Rust dependencies. The local binary is not the system compositor.

Upstream archive:
`https://codeload.github.com/niri-wm/niri/tar.gz/refs/tags/v26.04`

Archive SHA256:
`134c602d8e0d53413a52d6cd58f9ce7e79a07d03288ee0a51ba1abd5db1b1ad9`

## wlr virtual pointer protocol

`assets/virtual-pointer.xml` is the
[wlr virtual pointer protocol](https://gitlab.freedesktop.org/wlroots/wlr-protocols/-/blob/master/unstable/wlr-virtual-pointer-unstable-v1.xml)
from wlr-protocols. Copyright (C) 2019 Josef Gajdusek. It remains under its
original MIT license, including the complete copyright and permission notice
inside the XML file. That notice also accompanies generated protocol bindings.
The protocol file is unmodified.

Protocol file SHA256:
`3ff6d540be0bc5228195bf072bde42117ea17945a5c2061add5d3cf97d6bb524`

## Runtime dependencies

The optional host-background adapter implements the `AgentInput` / `AgentScreenshot`
IPC contract developed by [lihaoze123/niri-computer-use](https://github.com/lihaoze123/niri-computer-use).
Its compositor modifications are GPL-3.0-or-later and are installed separately.
This repository does not redistribute that patch or a patched host binary.

Weston, niri, wtype, D-Bus, Wayland, GTK, libadwaita, PyGObject and other system
or build dependencies are separate upstream projects with their own licenses.
They are not included in this source repository.
