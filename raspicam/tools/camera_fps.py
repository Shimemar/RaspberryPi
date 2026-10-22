#!/usr/bin/env python3
"""Measure the raw camera frame rate with plain gst-launch (no pyneat, no display).

Run: dk ./tools/camera_fps.py [/dev/videoN]
"""
import subprocess
import sys
import time

dev = sys.argv[1] if len(sys.argv) > 1 else "/dev/video96"
for fmt, caps in (("mjpeg", "image/jpeg"), ("yuyv", "video/x-raw,format=YUY2")):
    for w, h, fps in ((640, 360, 15), (1280, 720, 2)):
        n = 30 if fps > 2 else 6
        cmd = (f"gst-launch-1.0 -q v4l2src device={dev} num-buffers={n} ! "
               f"{caps},width={w},height={h},framerate={fps}/1 ! fakesink")
        # first frame includes device open/negotiation, so time 2 runs and difference them
        times = []
        for k in (2, n + 2):
            t0 = time.monotonic()
            r = subprocess.run(cmd.replace(f"num-buffers={n}", f"num-buffers={k}").split(),
                               capture_output=True, text=True, timeout=120)
            times.append(time.monotonic() - t0)
            if r.returncode:
                print(r.stderr.strip()[:300])
        secs = times[1] - times[0]
        print(f"{fmt} {w}x{h}@{fps}: {n} frames in {secs:.1f}s -> {n / secs:.1f} fps")
