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
    if desktop is None:
        raise ValueError("The accessibility registry is unavailable")
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


def accessibility(env):
    """Resolve a usable accessibility bus before libatspi starts its client."""
    if env.get("DBUS_SESSION_BUS_ADDRESS"):
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = env["DBUS_SESSION_BUS_ADDRESS"]
    try:
        import gi
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi, Gio, GLib
    except (ImportError, ValueError) as exc:
        raise host.HostError("Shared window input needs the AT-SPI runtime") from exc
    try:
        flags = Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
        explicit = os.environ.get("AT_SPI_BUS_ADDRESS")
        connection = None
        if explicit:
            try:
                connection = Gio.DBusConnection.new_for_address_sync(explicit, flags, None, None)
            except GLib.Error:
                pass
        if connection is None:
            session = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            address, = session.call_sync("org.a11y.Bus", "/org/a11y/bus", "org.a11y.Bus", "GetAddress",
                                         None, GLib.VariantType.new("(s)"), Gio.DBusCallFlags.NONE,
                                         1000, None).unpack()
            connection = Gio.DBusConnection.new_for_address_sync(address, flags, None, None)
            os.environ["AT_SPI_BUS_ADDRESS"] = address
        connection.close_sync(None)
        Atspi.set_timeout(1000, 2000)
        return Atspi, GLib
    except GLib.Error as exc:
        raise host.HostError("Application accessibility bus unavailable: " + str(exc)) from exc


def shared_control(state, window, point):
    """Hit-test this window's accessibility tree without changing the seat or widget focus."""
    Atspi, GLib = accessibility(state["host_env"])
    try:
        root = window_root(Atspi.get_desktop(0), window["pid"], window["title"])
        x, y = map(int, point)
        node = root
        hit_node = None
        visited = set()
        while node is not None and (node.app.bus_name, node.path) not in visited:
            visited.add((node.app.bus_name, node.path))
            if len(visited) > 64:
                raise ValueError("Accessibility hit-test exceeded the window tree depth")
            box = node.get_extents(Atspi.CoordType.WINDOW)
            if not (box.x <= x < box.x + box.width and box.y <= y < box.y + box.height):
                break
            hit_node = node
            hit = node.get_accessible_at_point(x, y, Atspi.CoordType.WINDOW)
            if hit is None or hit.path == node.path:
                break
            if hit.app.bus_name != root.app.bus_name:
                raise ValueError("Hit belongs to another application")
            node = hit
        # A toolkit can return the leaf directly. Walk its parents to find a button
        # around a label, but only if the chain terminates at our verified window.
        if hit_node is None:
            raise ValueError("Point outside the accessible window")
        chain = []
        node = hit_node
        while node and len(chain) < 64 and node.app.bus_name == root.app.bus_name:
            chain.append(node)
            if node.path == root.path:
                break
            node = node.get_parent()
        if chain[-1].path != root.path:
            raise ValueError("Hit does not belong to the pinned window")
        for node in chain:
            flags = node.get_state_set()
            if flags.contains(Atspi.StateType.DEFUNCT) or not flags.contains(Atspi.StateType.SENSITIVE):
                raise ValueError("The shared control is unavailable")
            role = node.get_role()
            if role == Atspi.Role.PASSWORD_TEXT:
                raise ValueError("Use a separate window for password entry")
            token = [window["title"], root.app.bus_name, root.path, node.path, node.get_role_name(), node.get_name()]
            if flags.contains(Atspi.StateType.EDITABLE) and node.get_editable_text_iface():
                return node, token, None, Atspi
            if node.get_action_iface():
                for idx in range(node.get_n_actions()):
                    if node.get_action_name(idx).lower() in ("click", "press", "activate", "toggle"):
                        return node, token, idx, Atspi
        raise ValueError("No accessible button or editable field at the selected point")
    except (ValueError, GLib.Error) as exc:
        raise host.HostError("Shared window input: " + str(exc)) from exc


def entry_selection(node, token, point, atspi):
    from gi.repository import GLib
    count = node.get_character_count()
    try:
        x, y = map(int, point)
        offset = node.get_offset_at_point(x, y, atspi.CoordType.WINDOW)
    except GLib.Error:
        offset = count
    return {"token": token, "point": point, "offset": offset if 0 <= offset <= count else count}


def remember_entry(state, window, motion):
    # A later user focus change may occur between the agent's click and type.
    # This lookup is read-only; apps without AT-SPI still get ordinary native clicks.
    point = [motion["x"], motion["y"]]
    try:
        node, token, action, atspi = shared_control(state, window, point)
        if action is None:
            from gi.repository import GLib
            try:
                return dict(entry_selection(node, token, point, atspi), native=True)
            except GLib.Error:
                pass
    except host.HostError:
        pass
    return None


def operate_shared(state, window, point, text, authorize):
    """Select an entry, insert at its agent cursor, or invoke a button once."""
    node, token, index, atspi = shared_control(state, window, point)
    from gi.repository import GLib
    try:
        if text is None and index is not None:
            authorize()
            if not node.do_action(index):
                raise host.HostError("Application declined the shared control action")
            state.pop("shared_entry", None)
            return "invoke"
        if index is not None:
            raise host.HostError("The selected editable field changed")
        if text is None:
            selection = entry_selection(node, token, point, atspi)
            authorize()
            state["shared_entry"] = selection
            return "select-entry"
        count = node.get_character_count()
        selected = state.get("shared_entry")
        if not selected or selected["token"] != token or not 0 <= selected["offset"] <= count:
            raise host.HostError("The selected editable field changed; click the intended field again")
        actual = authorize()
        if actual.get("is_focused") and node.get_state_set().contains(atspi.StateType.FOCUSED):
            raise host.HostError("Both cursors are in the same text field; select a different field")
        if not node.insert_text(selected["offset"], text, len(text.encode("utf-8"))):
            raise host.HostError("Application declined insertion into the selected field")
        selected.pop("native", None)
        selected["offset"] += len(text)
        return "insert"
    except GLib.Error as exc:
        raise host.HostError("Shared control operation failed; input was not retried: " + str(exc)) from exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("inspect", "invoke"))
    parser.add_argument("--window", type=int, required=True, help="niri window ID")
    parser.add_argument("--token", help="exact token returned by inspect")
    args = parser.parse_args()
    if (args.command == "invoke") != bool(args.token):
        parser.error("supply --token for invoke only")
    try:
        client, env = host.host_endpoint()
        Atspi, GLib = accessibility(env)
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    try:
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
