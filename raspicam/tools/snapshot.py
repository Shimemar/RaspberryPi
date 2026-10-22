#!/usr/bin/env python3
"""Save one MJPEG and one YUYV frame from the UVC camera to tools/out/ (as-is from the camera).

Run: dk ./tools/snapshot.py [/dev/videoN] [WxH]
"""
import pathlib
import subprocess
import sys

dev = sys.argv[1] if len(sys.argv) > 1 else "/dev/video96"
w, h = (sys.argv[2] if len(sys.argv) > 2 else "640x360").split("x")
out = pathlib.Path(__file__).resolve().parent / "out"
out.mkdir(exist_ok=True)
# skip the first frames (auto-exposure settling)
cmds = {
    f"mjpeg_{w}x{h}.jpg": f"v4l2src device={dev} num-buffers=8 ! image/jpeg,width={w},height={h} "
                          f"! multifilesink location={out}/mjpeg_{w}x{h}_%d.jpg",
    f"yuyv_{w}x{h}.png": f"v4l2src device={dev} num-buffers=8 ! video/x-raw,format=YUY2,width={w},height={h} "
                         f"! videoconvert ! pngenc ! multifilesink location={out}/yuyv_{w}x{h}_%d.png",
}
for name, pipe in cmds.items():
    r = subprocess.run(["gst-launch-1.0", "-q", *pipe.split()], capture_output=True, text=True, timeout=120)
    print(name, "rc", r.returncode, r.stderr.strip()[:200])
for p in sorted(out.iterdir()):
    print(p.name, p.stat().st_size)
