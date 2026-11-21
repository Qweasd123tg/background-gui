"""Native fixture checks for native_desktop_supervisor.py --parallel-windows."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, BrokenBarrierError
import json
import time


def check_parallel(cli, artifacts, ready, type_as_user, focus_as_user):
    from PIL import Image
    expected = {"left": ("Фокус A 123", " / левый ☃"), "right": ("Правый B 456", " / правый ✓")}
    colors = {"violet": (145, 92, 255), "teal": (20, 184, 176)}
    barrier = Barrier(2, timeout=25)
    user = next(w for w in ready["windows_before"] if w["app_id"] == "dev.codex.DesktopUser")
    alternate = next(w for w in ready["windows_before"] if w["app_id"] == "dev.codex.DesktopAlternate")
    assert user["is_focused"]
    type_as_user("user-")
    def switch_window():
        focus_as_user(alternate["id"])
        type_as_user("second-window")
    switch = Barrier(2, action=switch_window, timeout=25)

    def exercise(role):
        agent = ready["agents"][role]
        session = agent["session"]
        layout = json.loads((artifacts / (role + ".geometry.json")).read_text())
        shot = cli("screenshot", session)
        def point(widget):
            x, y = layout[widget]
            return [str(round(x * shot["scale"] + shot["geometry_x"])),
                    str(round(y * shot["scale"] + shot["geometry_y"]))]
        def check_focus(window):
            assert next(w for w in cli("windows", session) if w["is_focused"])["id"] == window["id"]
        barrier.wait()
        cli("click", session, *point("entry"))
        barrier.wait()
        cli("type", session, expected[role][0])
        check_focus(user)
        switch.wait()
        cli("type", session, expected[role][1])
        check_focus(alternate)
        barrier.wait()
        cli("click", session, *point("button"))
        check_focus(alternate)
        frame = cli("screenshot", session, "--show-pointer", "--path",
                    str(artifacts / (role + "-after.png")))
        for _ in range(30):
            if (artifacts / (role + ".json")).exists():
                break
            time.sleep(.1)
        result = json.loads((artifacts / (role + ".json")).read_text())
        assert result["text"] == "".join(expected[role]) and result["clicked"], result
        # Verify the real rendered arrow in the output crop around the button press.
        x, y = map(int, point("button"))
        with Image.open(frame["path"]) as image:
            pixels = image.crop((x, y, x + 26, y + 34)).convert("RGB").getdata()
            color = colors[agent["color"]]
            matched = sum(max(abs(a-b) for a, b in zip(pixel, color)) <= 5 for pixel in pixels)
        assert matched >= 12, (role, "missing cursor pixels", matched)
        return dict(result, color=agent["color"], arrow_pixels=matched)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(exercise, role) for role in ("left", "right")]
        errors = [future.exception() for future in futures]
        for error in errors:
            if error and not isinstance(error, BrokenBarrierError):
                raise error
        results = [future.result() for future in futures]
    focus_as_user(user["id"])
    type_as_user("123")
    user_file = artifacts / "user.json"
    for _ in range(30):
        if user_file.exists() and json.loads(user_file.read_text())["text"] == "user-123":
            break
        time.sleep(.1)
    else:
        raise AssertionError("The focused GTK entry did not receive the user's keystrokes")
    actual = json.loads((artifacts / "alternate.json").read_text())["text"]
    assert actual == "second-window", actual
    return {"agents": results, "focused_window": user["id"], "user_text": "user-123",
            "user_focus_sequence": [user["id"], alternate["id"], user["id"]], "alternate_text": actual,
            "keyboard_layout": "us,ru (second group)"}


def check_click_outline(frame, bounds, expected_color):
    from PIL import Image
    scale = frame["scale"]
    x, y, width, height = bounds
    left, top = round(x*scale+frame["geometry_x"]), round(y*scale+frame["geometry_y"])
    right, bottom = round(left+width*scale), round(top+height*scale)
    band = round(4*scale)
    with Image.open(frame["path"]) as source:
        image = source.convert("RGB")
        def colored(region):
            crop = image.crop(region)
            return sum(max(abs(c-v) for c, v in zip(pixel, expected_color)) < 24
                       for pixel in crop.getdata())
        return [colored(region) for region in [
            (left+band,top-band,left+(right-left)//3,top+band),
            (left+band,bottom-band,left+(right-left)//3,bottom+band),
            (left-band,top+band,left+band,bottom-band),
            (right-band,top+band,right+band,bottom-band)]]
