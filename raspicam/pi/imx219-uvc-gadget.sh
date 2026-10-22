#!/bin/bash
# IMX219 UVC camera gadget (configfs) for Raspberry Pi Zero W.
# Installed as /usr/local/sbin/imx219-uvc-gadget.sh, called by imx219-uvc.service:
#   ExecStartPre=... start   /   ExecStopPost=... stop
#
# Replaces the legacy `modprobe g_webcam`: g_webcam's mode table is fixed in the kernel
# (640x360@15/10/2, 1280x720@2) and uvc-gadget's own copy of it for legacy gadgets has
# intervals >= 1 s (e.g. 720p = 50000000 = 0.2 fps), which truncates to fps=0 and kills
# uvc-gadget with SIGFPE in libcamera_source_set_frame_rate(). With configfs, uvc-gadget
# reads these descriptors instead. Keep every interval < 10000000 (1 s) for that reason.
#
# Bandwidth: streaming_maxpacket=1024 (HS isoc, 1 x 1024 per microframe) = 8.2 MB/s.
#   2048/3072 do NOT work on the Pi Zero W's dwc2 (verified 2026-10-18):
#     3072 -> "dwc2_hsotg_ep_enable: No suitable fifo found", the stream never starts;
#     2048 -> streams, but floods "VS request completed with status -61" (isoc underruns):
#             incomplete frames are dropped by the host (640x360@30 gave 14.9 fps,
#             960x540@15 gave 9.9 fps) and some stream starts deliver no frame at all.
#   So every mode must fit in 8.2 MB/s (YUYV = w*h*2 bytes/frame):
#     640x360@15 = 6.9 MB/s, 640x480@10 = 6.1 MB/s (640x480@15 = 9.2 MB/s does NOT fit).
#
# Only 640-wide sizes are offered: uvc-gadget (libcamera source) stops delivering frames after the
# host switches from a larger frame size (960x540 / 1280x720) back to 640x360, until the
# service is restarted (verified 2026-10-18). Larger sizes would only give ~4-7 fps anyway.
set -euo pipefail

G=/sys/kernel/config/usb_gadget/imx219
UDC=20980000.usb
MAXPACKET=${MAXPACKET:-1024}

# frame <width> <height> <interval_100ns>...   (first interval = default)
frame() {
    local w=$1 h=$2; shift 2
    local d="$F/streaming/uncompressed/u/${w}x${h}"
    mkdir -p "$d"
    echo "$w" > "$d/wWidth"
    echo "$h" > "$d/wHeight"
    echo $((w * h * 2)) > "$d/dwMaxVideoFrameBufferSize"
    echo "$1" > "$d/dwDefaultFrameInterval"
    # The whole list must arrive in ONE write(): configfs replaces the list on every
    # write, and bash's builtin printf/echo write line by line (only the last interval
    # survived). coreutils printf fully buffers to a file -> a single write at exit.
    /usr/bin/printf '%s\n' "$@" > "$d/dwFrameInterval"
}

stop() {
    [ -d "$G" ] || return 0
    echo "" > "$G/UDC" 2>/dev/null || true
    rm -f "$G"/configs/c.1/uvc.0
    rm -f "$G"/functions/uvc.0/control/class/*/h "$G"/functions/uvc.0/streaming/class/*/h
    rm -f "$G"/functions/uvc.0/streaming/header/h/u
    rmdir "$G"/functions/uvc.0/streaming/uncompressed/u/*/ 2>/dev/null || true
    rmdir "$G"/functions/uvc.0/streaming/uncompressed/u \
          "$G"/functions/uvc.0/streaming/header/h \
          "$G"/functions/uvc.0/control/header/h 2>/dev/null || true
    rmdir "$G"/functions/uvc.0 2>/dev/null || true
    rmdir "$G"/configs/c.1/strings/0x409 "$G"/configs/c.1 "$G"/strings/0x409 2>/dev/null || true
    rmdir "$G"
}

start() {
    modprobe -r g_webcam 2>/dev/null || true
    modprobe libcomposite
    modprobe usb_f_uvc
    mountpoint -q /sys/kernel/config || mount -t configfs none /sys/kernel/config
    stop
    test -e "/sys/class/udc/$UDC"

    mkdir "$G"
    echo 0x1d6b > "$G/idVendor"
    echo 0x0104 > "$G/idProduct"
    echo 0x0200 > "$G/bcdUSB"
    echo 0x0100 > "$G/bcdDevice"
    mkdir -p "$G/strings/0x409"
    echo "IMX219-0001" > "$G/strings/0x409/serialnumber"
    echo "Raspberry Pi" > "$G/strings/0x409/manufacturer"
    echo "IMX219 NoIR UVC Camera" > "$G/strings/0x409/product"
    mkdir -p "$G/configs/c.1/strings/0x409"
    echo "UVC Camera" > "$G/configs/c.1/strings/0x409/configuration"
    echo 250 > "$G/configs/c.1/MaxPower"

    F="$G/functions/uvc.0"
    mkdir "$F"
    #     W    H    intervals (100 ns): 666666=15fps 1000000=10 2000000=5
    frame 640  360  666666 1000000 2000000
    # 4:3 full FOV (with UVC_SENSOR_MODE=1640:1232:10): 614 KB/frame -> 10fps = 6.1 MB/s
    frame 640  480  1000000 2000000

    mkdir -p "$F/streaming/header/h"
    ln -s "$F/streaming/uncompressed/u" "$F/streaming/header/h/u"
    for s in fs hs ss; do ln -s "$F/streaming/header/h" "$F/streaming/class/$s/h"; done
    mkdir -p "$F/control/header/h"
    for s in fs ss; do ln -s "$F/control/header/h" "$F/control/class/$s/h"; done

    echo "$MAXPACKET" > "$F/streaming_maxpacket"
    echo 1 > "$F/streaming_interval"
    echo 0 > "$F/streaming_maxburst"

    ln -s "$F" "$G/configs/c.1/uvc.0"
    echo "$UDC" > "$G/UDC"
    echo "imx219 UVC gadget bound to $UDC (maxpacket=$(cat "$F/streaming_maxpacket"))"
}

case "${1:-}" in
    start) start ;;
    stop) stop ;;
    *) echo "usage: $0 {start|stop}" >&2; exit 2 ;;
esac
