#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Inspect and invoke named AT-SPI actions in a specific native window."""
import argparse
import hashlib
import json
import os
import sys

import host_background as host


def children(node):
    for i in range(node.get_child_count()):
        child = node.get_child_at_index(i)
        if child:
            yield child


def window_root(desktop, pid, title):
    candidates = [root for app in children(desktop) if app.get_process_id() == pid
                  for root in children(app) if root.get_name() == title]
    if len(candidates) != 1:
        raise ValueError("Cannot uniquely match the compositor window to an accessible window")
    return candidates[0]


def actions(root, identity, atspi):
    # Match again in a new process before invoking. Paths identify live objects;
    # labels and ancestors distinguish a widget that was reused for another view.
    stack = [(root, ())]
    seen = set()
    while stack:
        node, parents = stack.pop()
        path = node.path
        if path in seen:
            continue
        seen.add(path)
        if len(seen) > 5000:
            raise ValueError("Window exceeds the 5000-element inspection limit")
        name = node.get_name() or ""
        role = node.get_role_name()
        lineage = parents + ((path, role, name),)
        if name and node.get_action_iface():
            state = node.get_state_set()
            if state.contains(atspi.StateType.SENSITIVE) and not state.contains(atspi.StateType.DEFUNCT):
                for i in range(node.get_n_actions()):
                    action = node.get_action_name(i)
                    if not action:
                        continue
                    key = (identity, node.app.bus_name, lineage, action, i)
                    token = hashlib.sha256(json.dumps(key).encode()).hexdigest()
                    yield {"token": token, "name": name, "role": role, "action": action}, node, i
        stack.extend((child, lineage) for child in children(node))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("inspect", "invoke"))
    parser.add_argument("--window", type=int, required=True, help="niri window ID")
    parser.add_argument("--token", help="exact token returned by inspect")
    args = parser.parse_args()
    if (args.command == "invoke") != bool(args.token):
        parser.error("supply --token for invoke only")
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi, GLib
    try:
        client, env = host.host_endpoint()
        if env.get("DBUS_SESSION_BUS_ADDRESS"):
            os.environ["DBUS_SESSION_BUS_ADDRESS"] = env["DBUS_SESSION_BUS_ADDRESS"]
        Atspi.set_timeout(1500, 2500)
        win = host.target(client, args.window)
        root = window_root(Atspi.get_desktop(0), win["pid"], win["title"])
        identity = (client.peer, win["id"], win["pid"], win["app_id"])
        items = actions(root, identity, Atspi)
        if args.command == "inspect":
            result = {"window": args.window, "actions": [info for info, _, _ in items]}
        else:
            for info, node, index in items:
                if info["token"] == args.token:
                    current = host.target(client, args.window, host.pin(win))
                    if current["title"] != win["title"] or root.get_name() != win["title"]:
                        raise ValueError("Window content changed during lookup")
                    # Capture bounds first; invoking may remove the widget or open a menu.
                    center = None
                    try:
                        box = node.get_extents(Atspi.CoordType.WINDOW)
                        if box.width > 0 and box.height > 0:
                            center = (box.x + box.width / 2, box.y + box.height / 2)
                    except GLib.Error:
                        pass
                    if not node.do_action(index):
                        raise ValueError("Application declined the action")
                    shown = False
                    if center:
                        try:
                            shown = client.request({"AgentIndicateAction": {
                                "window_id": args.window, "x": center[0], "y": center[1]}}) == "Handled"
                        except (OSError, host.HostError):
                            pass  # Action already accepted. Never replay for missing visual feedback.
                    result = {"window": args.window, "invoked": info, "indicator": shown}
                    break
            else:
                raise ValueError("Action no longer matches the inspected widget")
        print(json.dumps(result, ensure_ascii=False))
    except (OSError, ValueError, GLib.Error) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
