#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the private, pinned niri capture variant; never replaces the host niri."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request

URL = "https://codeload.github.com/niri-wm/niri/tar.gz/refs/tags/v26.04"
SHA256 = "134c602d8e0d53413a52d6cd58f9ce7e79a07d03288ee0a51ba1abd5db1b1ad9"
PACKAGES = ("libinput", "gbm", "libseat", "libdisplay-info", "libudev", "xkbcommon",
            "wayland-client", "wayland-server", "egl", "pango", "pangocairo", "pixman-1")


def install(binary):
    destination = Path.home() / ".cache/codex-background-gui/niri-observer"
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.is_symlink() or destination.stat().st_uid != os.getuid():
        raise RuntimeError("Expected an owned private binary directory")
    temporary = destination / "niri.new"
    shutil.copyfile(binary, temporary)
    temporary.chmod(0o755)
    digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
    temporary.replace(destination / "niri")
    metadata = {"version": "26.04", "capability": "clipboard-free-v1", "source": URL,
                "source_sha256": SHA256, "binary_sha256": digest,
                "patch": "assets/niri-observer.patch", "features": "no-default-features"}
    temporary = destination / "build.json.new"
    temporary.write_text(json.dumps(metadata, indent=2))
    temporary.replace(destination / "build.json")
    print(json.dumps({"binary": str(destination / "niri"), **metadata}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path)
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--install-built", type=Path, help="Install an already completed build from this source/patch")
    args = parser.parse_args()
    if args.install_built:
        install(args.install_built)
        return
    if not 1 <= args.jobs <= 8:
        parser.error("--jobs must be within 1..8")
    missing = [p for p in PACKAGES if subprocess.run(["pkg-config", "--exists", p]).returncode]
    if missing:
        parser.error("Missing build dependencies: " + ", ".join(missing) +
                     ". Supply development packages or a private PKG_CONFIG_PATH; nothing was installed.")
    work = (args.build_dir or Path(tempfile.mkdtemp(prefix="bgui-niri-build-"))).expanduser().resolve()
    work.mkdir(parents=True, exist_ok=True)
    print(json.dumps({"build_dir": str(work)}), flush=True)
    archive = work / "niri-v26.04.tar.gz"
    if not archive.exists():
        with urllib.request.urlopen(URL, timeout=60) as source, archive.open("wb") as output:
            shutil.copyfileobj(source, output)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
        raise RuntimeError("Pinned source archive SHA256 mismatch")
    source = work / "niri-26.04"
    if source.exists():
        raise RuntimeError("Source directory already exists; use a fresh build directory")
    with tarfile.open(archive) as tar:
        tar.extractall(work, filter="data")
    patch = Path(__file__).resolve().parent.parent / "assets/niri-observer.patch"
    subprocess.run(["patch", "-p1", "-i", str(patch)], cwd=source, check=True)
    env = os.environ | {"CARGO_TARGET_DIR": str(work / "target"), "CARGO_PROFILE_RELEASE_DEBUG": "0",
                        "CARGO_PROFILE_RELEASE_LTO": "false", "CARGO_PROFILE_RELEASE_OPT_LEVEL": "2",
                        "CARGO_PROFILE_RELEASE_CODEGEN_UNITS": "16"}
    subprocess.run(["cargo", "build", "--locked", "--release", "--no-default-features", "--bin", "niri",
                    "-j", str(args.jobs)], cwd=source, env=env, check=True)
    install(work / "target/release/niri")


if __name__ == "__main__":
    main()
