#!/usr/bin/env python3
"""Show DevKit DRM connector status (is an HDMI monitor attached?)."""
import glob, pathlib, subprocess
for p in sorted(glob.glob("/sys/class/drm/card*-*")):
    st = pathlib.Path(p, "status")
    if st.exists():
        modes = pathlib.Path(p, "modes").read_text().split()[:3]
        print(p, st.read_text().strip(), modes)
print(subprocess.run(["systemctl", "is-active", "lightdm"], capture_output=True, text=True).stdout)
print(subprocess.run(["pgrep", "-af", "python3"], capture_output=True, text=True).stdout)
