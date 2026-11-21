"""Check concurrent agents with GTK on an owned headless Weston/niri display.

Usage: python3 tests/run_native_parallel.py /path/to/patched/niri /path/to/new/artifacts
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--scale", type=float, choices=(1, 1.5, 2), default=1)
    args = parser.parse_args()
    artifacts = args.directory.absolute()
    artifacts.mkdir(parents=True, exist_ok=False, mode=0o700)
    env = os.environ | {"XDG_STATE_HOME": str(artifacts / "outer-state")}
    repo = Path(__file__).resolve().parents[1]

    def cli(*argv):
        p = subprocess.run([sys.executable, str(repo / "scripts/bgui.py"), *argv],
                           capture_output=True, text=True, env=env, timeout=45)
        if p.returncode:
            raise RuntimeError(p.stdout + p.stderr)
        return json.loads(p.stdout)

    session = cli("start", "--name", "Parallel GTK verification", "--width", str(round(1600 * args.scale)),
                  "--height", str(round(1000 * args.scale)))["session"]
    owned = artifacts / "inner"
    try:
        launch = cli("run", session, "--", sys.executable, str(repo / "tests/native_desktop_supervisor.py"),
                     "--binary", str(args.binary.resolve()), "--artifacts", str(owned), "--scale", str(args.scale), "--parallel-windows")
        for _ in range(300):
            if (owned / "stopped").exists():
                break
            if not Path("/proc", str(launch["pid"])).exists():
                raise RuntimeError("Fixture exited. See " + launch["log"])
            time.sleep(.5)
        else:
            raise RuntimeError("Native fixture timed out")
        if (owned / "error.txt").exists():
            raise RuntimeError((owned / "error.txt").read_text())
        result = json.loads((owned / "result.json").read_text())
        ready = json.loads((owned / "ready.json").read_text())
        for agent in ready["agents"].values():
            detach = json.loads((owned / ("detach-" + agent["session"] + ".json")).read_text())
            assert detach["detached"] and detach["input_cleanup_error"] is None, detach
        print(json.dumps(result, ensure_ascii=False))
    finally:
        if owned.exists():
            (owned / "stop").touch()
        cli("stop", session)


if __name__ == "__main__":
    main()
