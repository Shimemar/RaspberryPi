#!/usr/bin/env python3
"""List USB (UVC) cameras and their capture modes on the DevKit.

Run: dk ./tools/probe_camera.py
"""
import glob
import subprocess


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True).stdout


print("$ v4l2-ctl --list-devices")
print(run(["v4l2-ctl", "--list-devices"]))
for dev in sorted(glob.glob("/dev/video*")):
    info = run(["v4l2-ctl", "-d", dev, "--info"])
    if "uvcvideo" not in info:
        continue
    fmts = run(["v4l2-ctl", "-d", dev, "--list-formats-ext"])
    if "Size" not in fmts:
        continue  # UVC metadata node
    print(f"== {dev}")
    print("\n".join(l for l in info.splitlines() if "Card type" in l or "Bus info" in l))
    print(fmts)
print("$ lsusb")
print(run(["lsusb"]))
