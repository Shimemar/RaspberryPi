#!/usr/bin/env python3
"""raspicam: USB接続カメラ(UVC)の映像をYOLOで物体認識し、Modalix DevKitのHDMIに表示する。

  USB camera (v4l2src, YUYV/MJPEG) --pyneat Graph--> NV12 --capture--> LatestQueue
      --pusher (try_push)--> YOLO26 Model Run (MLA, OverflowPolicy.Block) --puller (decode)-->
      LatestQueue --display--> BGR + boxes + overlay --> HDMI (kmssink, hdmi_out.py)
                                                  \\-> (optional) H.264/RTP over UDP

--no-yolo なら capture --> display の素通し(カメラビューア)。

camera_device=auto なら /sys/class/video4linux から uvcvideo の Video Capture ノード
(index 0。index 1 はメタデータノード)を探す。USB再接続でデバイス番号が変わっても動く。

Run on the DevKit:
  dk ./main.py --frames 150            # 有限回のスモークテスト(まずこれ)
  dk ./main.py --classes cup,bottle    # 指定クラスだけ表示
  dk ./main.py --no-yolo               # 物体認識なしのカメラビューア
  dk ./main.py                         # 連続表示 (Ctrl-C / SIGTERM で終了、lightdmは再開)
  dk ./main.py --udp --no-hdmi         # HDMIを使わずUDP(H.264/RTP)で配信
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, fields
import glob
import os
from pathlib import Path
import queue
import signal
import sys
import threading
import time

import cv2
import numpy as np
import pyneat

import hdmi_out

APP_DIR = Path(__file__).resolve().parent


@dataclass
class Config:
    camera_device: str = "auto"
    capture_format: str = "yuyv"       # yuyv / mjpeg
    capture_width: int = 640
    capture_height: int = 360
    capture_fps: int = 15
    camera_flip: str = "none"          # none / rotate-180 / horizontal-flip / vertical-flip
    overlay: bool = True
    frames: int = 0                    # 0 = run forever
    hdmi_enabled: bool = True
    hdmi_driver: str = "smifb"
    hdmi_width: int = 0
    hdmi_height: int = 0
    udp_enabled: bool = False
    udp_host: str = "10.42.0.1"
    udp_port: int = 9000
    bitrate_kbps: int = 2000
    yolo_enabled: bool = True
    model_path: str = "assets/yolo26m-det-bf16-mla_tess-b1.tar.gz"
    model_width: int = 640
    model_height: int = 640
    score_threshold: float = 0.35
    nms_iou: float = 0.50
    top_k: int = 100
    num_classes: int = 80
    model_queue_depth: int = 2
    classes: str = ""                  # 表示するクラス名(カンマ区切り)。空 = 全80クラス
    print_detections: bool = False     # 毎フレームの検出結果を標準出力に出す
    snapshot: str = ""                 # 終了時に最後の表示フレームをこのJPEGに保存(確認用)


COCO_LABELS = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck",
    "boat", "traffic light", "fire hydrant", "stop sign", "parking meter", "bench",
    "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra",
    "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove",
    "skateboard", "surfboard", "tennis racket", "bottle", "wine glass", "cup",
    "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier",
    "toothbrush",
]


# ---------------------------------------------------------------- config / CLI

def apply_config_value(cfg: Config, key: str, value: str) -> None:
    types = {f.name: f.type for f in fields(Config)}
    if key not in types:
        raise ValueError(f"unknown config key: {key}")
    kind = types[key]
    if kind == "bool":
        parsed = value.lower() in ("1", "true", "yes", "on")
    elif kind == "int":
        parsed = int(value)
    elif kind == "float":
        parsed = float(value)
    else:
        parsed = value
    setattr(cfg, key, parsed)


def load_config_file(cfg: Config, path: Path) -> None:
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if "=" not in line:
            raise ValueError(f"{path}:{line_no}: expected key=value")
        key, value = [part.strip() for part in line.split("=", 1)]
        apply_config_value(cfg, key, value)


def parse_args(argv: list[str] | None) -> Config:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=APP_DIR / "config" / "default.conf")
    parser.add_argument("--camera-device", help="/dev/videoN または auto")
    parser.add_argument("--capture-format", choices=["mjpeg", "yuyv"])
    parser.add_argument("--width", type=int, dest="capture_width")
    parser.add_argument("--height", type=int, dest="capture_height")
    parser.add_argument("--fps", type=int, dest="capture_fps")
    parser.add_argument("--camera-flip", choices=["none", "rotate-180", "horizontal-flip", "vertical-flip"])
    parser.add_argument("--frames", type=int, help="0 = run forever")
    parser.add_argument("--no-overlay", action="store_false", dest="overlay", default=None)
    parser.add_argument("--hdmi", action="store_true", dest="hdmi_enabled", default=None)
    parser.add_argument("--no-hdmi", action="store_false", dest="hdmi_enabled", default=None,
                        help="HDMIに出さない(lightdmも止めない)")
    parser.add_argument("--udp", action="store_true", dest="udp_enabled", default=None,
                        help="H.264/RTP over UDPでも配信する")
    parser.add_argument("--no-udp", action="store_false", dest="udp_enabled", default=None)
    parser.add_argument("--udp-host")
    parser.add_argument("--udp-port", type=int)
    parser.add_argument("--yolo", action="store_true", dest="yolo_enabled", default=None)
    parser.add_argument("--no-yolo", action="store_false", dest="yolo_enabled", default=None,
                        help="物体認識をしない(カメラ映像の素通し表示)")
    parser.add_argument("--model", dest="model_path",
                        help="例: assets/yolo26n-det-bf16-mla_tess-b1.tar.gz")
    parser.add_argument("--score", type=float, dest="score_threshold")
    parser.add_argument("--classes", help='表示するクラス名(カンマ区切り) 例: "cup,bottle"')
    parser.add_argument("--print-detections", action="store_true", default=None)
    parser.add_argument("--snapshot", help="終了時に最後の表示フレームを保存するJPEGパス")
    args = parser.parse_args(argv)

    cfg = Config()
    if args.config and args.config.exists():
        load_config_file(cfg, args.config)
    for f in fields(Config):
        value = getattr(args, f.name, None)
        if value is not None:
            setattr(cfg, f.name, value)
    return cfg


# ---------------------------------------------------------------- camera discovery

def find_uvc_cameras() -> list[tuple[str, str]]:
    """-> [(/dev/videoN, card name)] for uvcvideo Video Capture nodes (index 0)."""
    found = []
    for sys_dir in glob.glob("/sys/class/video4linux/video*"):
        try:
            driver = os.path.basename(os.path.realpath(os.path.join(sys_dir, "device", "driver")))
            index = Path(sys_dir, "index").read_text().strip()
            name = Path(sys_dir, "name").read_text().strip()
        except OSError:
            continue
        if driver == "uvcvideo" and index == "0":
            found.append(("/dev/" + os.path.basename(sys_dir), name))
    return sorted(found, key=lambda d: int(d[0].removeprefix("/dev/video")))


def resolve_camera(cfg: Config) -> str:
    cams = find_uvc_cameras()
    for dev, name in cams:
        print(f"USB camera: {dev} ({name})", flush=True)
    if cfg.camera_device != "auto":
        return cfg.camera_device
    if not cams:
        raise RuntimeError("USBカメラ(uvcvideo)が見つかりません。接続を確認してください")
    return cams[0][0]


# ---------------------------------------------------------------- graphs

def camera_fragment(cfg: Config, device: str) -> str:
    w, h, fps = cfg.capture_width, cfg.capture_height, cfg.capture_fps
    if cfg.capture_format == "mjpeg":
        frag = (f"v4l2src device={device} io-mode=mmap"
                f" ! image/jpeg,width={w},height={h},framerate={fps}/1"
                f" ! queue leaky=downstream max-size-buffers=2 ! jpegparse ! jpegdec")
    else:
        frag = (f"v4l2src device={device} io-mode=mmap"
                f" ! video/x-raw,format=YUY2,width={w},height={h},framerate={fps}/1"
                f" ! queue leaky=downstream max-size-buffers=2")
    if cfg.camera_flip != "none":
        frag += f" ! videoflip method={cfg.camera_flip}"
    frag += (f" ! videoconvert n-threads=4"
             f" ! video/x-raw,format=NV12,width={w},height={h},framerate={fps}/1"
             f" ! queue leaky=downstream max-size-buffers=2")
    return frag


def build_source_run(cfg: Config, device: str):
    graph = pyneat.Graph(f"usb_camera_{cfg.capture_width}x{cfg.capture_height}")
    graph.add(pyneat.nodes.custom(camera_fragment(cfg, device), pyneat.InputRole.Source))
    graph.add(pyneat.nodes.output(pyneat.OutputOptions.every_frame(1)))
    run_opts = pyneat.RunOptions()
    run_opts.preset = pyneat.RunPreset.Realtime
    run_opts.queue_depth = 3
    run_opts.overflow_policy = pyneat.OverflowPolicy.KeepLatest
    run_opts.output_memory = pyneat.OutputMemory.Owned
    return graph, graph.build(run_opts)


def make_nv12_input_options(width: int, height: int, fps: int):
    input_opt = pyneat.InputOptions()
    input_opt.payload_type = pyneat.PayloadType.Image
    input_opt.format = pyneat.Format.NV12
    input_opt.width = width
    input_opt.height = height
    input_opt.depth = 1
    input_opt.max_width = width
    input_opt.max_height = height
    input_opt.max_depth = 1
    input_opt.fps_n = max(1, fps)
    input_opt.fps_d = 1
    input_opt.caps_override = (
        f"video/x-raw,format=NV12,width={width},height={height},framerate={max(1, fps)}/1"
    )
    input_opt.memory_policy = pyneat.InputMemoryPolicy.SystemMemory
    return input_opt


def build_video_run(cfg: Config, width: int, height: int, fps: int):
    sender_opt = pyneat.VideoSenderOptions.h264_rtp_udp_from_raw(width, height, max(1, fps))
    sender_opt.host = cfg.udp_host
    sender_opt.channel = 0
    sender_opt.video_port_base = cfg.udp_port
    sender_opt.encoder.bitrate_kbps = cfg.bitrate_kbps
    graph = pyneat.Graph("video_out")
    graph.add(pyneat.nodes.input(make_nv12_input_options(width, height, fps)))
    graph.add(pyneat.groups.video_sender(sender_opt))
    seed_nv12 = np.full((height * 3 // 2, width), 128, dtype=np.uint8)
    seed_nv12[:height, :] = 16
    return graph, graph.build([make_nv12_tensor(seed_nv12, width, height)]), sender_opt.video_port


def resolve_app_path(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else APP_DIR / p


def make_model_options(cfg: Config, width: int, height: int):
    opt = pyneat.ModelOptions()
    opt.preprocess.kind = pyneat.InputKind.Image
    opt.preprocess.enable = pyneat.AutoFlag.On
    # must match the actually captured size (mismatch only throws at the first pull)
    opt.preprocess.input_max_width = width
    opt.preprocess.input_max_height = height
    opt.preprocess.input_max_depth = 3          # NV12 -> RGB needs 3 (1 fails validation)
    opt.preprocess.resize.enable = pyneat.AutoFlag.On
    opt.preprocess.resize.width = cfg.model_width
    opt.preprocess.resize.height = cfg.model_height
    opt.preprocess.resize.mode = pyneat.ResizeMode.Letterbox
    opt.preprocess.resize.pad_value = 114
    opt.preprocess.color_convert.input_format = pyneat.PreprocessColorFormat.NV12
    opt.preprocess.color_convert.output_format = pyneat.PreprocessColorFormat.RGB
    # YOLO26 is an Ultralytics family: x/255 input + NMS-free raw l/t/r/b heads
    opt.preprocess.preset = pyneat.NormalizePreset.COCO_YOLO
    opt.decode_type = pyneat.BoxDecodeType.YoloV26
    opt.score_threshold = cfg.score_threshold
    opt.nms_iou_threshold = cfg.nms_iou
    opt.top_k = cfg.top_k
    opt.num_classes = cfg.num_classes
    return opt


def build_model_run(cfg: Config, width: int, height: int, fps: int):
    """Plain Graph + build without a seed (Model.build([seed]) times out on this device)."""
    model = pyneat.Model(str(resolve_app_path(cfg.model_path)), make_model_options(cfg, width, height))
    graph = pyneat.Graph("yolo")
    graph.add(pyneat.nodes.input(make_nv12_input_options(width, height, fps)))
    graph.add(model)
    graph.add(pyneat.nodes.output("detections", pyneat.OutputOptions.every_frame(1)))
    run_opts = pyneat.RunOptions()
    run_opts.preset = pyneat.RunPreset.Reliable
    run_opts.queue_depth = cfg.model_queue_depth
    run_opts.overflow_policy = pyneat.OverflowPolicy.Block   # never make this one drop
    run_opts.output_memory = pyneat.OutputMemory.ZeroCopy
    return model, graph, graph.build(run_opts)


def extract_tensors(sample) -> list:
    if sample is None or not hasattr(sample, "kind"):
        return []
    if sample.kind == pyneat.SampleKind.Tensor and sample.tensor is not None:
        return [sample.tensor]
    if sample.kind == pyneat.SampleKind.TensorSet:
        return list(sample.tensors)
    tensors = []
    for f in getattr(sample, "fields", []):
        tensors.extend(extract_tensors(f))
    return tensors


def decode_detections(sample, width: int, height: int, cfg: Config, allowed: set[int] | None):
    """-> list of (x1, y1, x2, y2, score, class_id) in captured-frame pixels."""
    tensors = extract_tensors(sample)
    if not tensors:
        return []
    dets = []
    for tensor in pyneat.decode_bbox(tensors, clamp_to=(width, height), top_k=cfg.top_k):
        arr = np.asarray(tensor.to_numpy(copy=True), dtype=np.float32).reshape((-1, 6))
        for x1, y1, x2, y2, score, cid in arr:
            cid = int(cid)
            if score < cfg.score_threshold or (allowed is not None and cid not in allowed):
                continue
            dets.append((int(x1), int(y1), int(x2), int(y2), float(score), cid))
    return dets[: cfg.top_k]


def class_label(cid: int) -> str:
    return COCO_LABELS[cid] if 0 <= cid < len(COCO_LABELS) else f"class_{cid}"


def parse_classes(spec: str) -> set[int] | None:
    names = [s.strip() for s in spec.split(",") if s.strip()]
    if not names:
        return None
    unknown = [n for n in names if n not in COCO_LABELS]
    if unknown:
        raise ValueError(f"unknown class name(s): {unknown} (COCO 80クラス名で指定)")
    return {COCO_LABELS.index(n) for n in names}


# ---------------------------------------------------------------- tensors / color

def tensor_dim(tensor, name: str) -> int:
    value = getattr(tensor, name)
    return int(value() if callable(value) else value)


def tensor_nv12_from_decoded(tensor):
    if not tensor.is_nv12():
        raise RuntimeError("expected decoded NV12 frame")
    width = tensor_dim(tensor, "width")
    height = tensor_dim(tensor, "height")
    payload = np.frombuffer(tensor.copy_payload_bytes(), dtype=np.uint8)
    expected = width * height * 3 // 2
    if payload.size < expected:
        raise RuntimeError(f"NV12 payload too small: {payload.size} < {expected}")
    return np.ascontiguousarray(payload[:expected].reshape((height * 3 // 2, width))).copy(), width, height


def bgr_to_nv12(bgr):
    """cv2 has no BGR->NV12, so go through planar I420 and interleave U/V."""
    h, w = bgr.shape[:2]
    i420 = cv2.cvtColor(bgr, cv2.COLOR_BGR2YUV_I420)
    nv12 = np.empty((h * 3 // 2, w), dtype=np.uint8)
    nv12[:h] = i420[:h]
    uv = nv12[h:].reshape(h // 2, w // 2, 2)
    uv[..., 0] = i420[h:h + h // 4].reshape(h // 2, w // 2)
    uv[..., 1] = i420[h + h // 4:].reshape(h // 2, w // 2)
    return nv12


def make_nv12_tensor(nv12, width: int, height: int):
    tensor = pyneat.Tensor.from_numpy(np.ascontiguousarray(nv12), copy=True,
                                      layout=pyneat.TensorLayout.HW, memory=pyneat.TensorMemory.CPU)
    tensor.shape = [height, width]
    tensor.strides_bytes = [width, 1]
    tensor.byte_offset = 0
    image = pyneat.ImageSpec()
    image.format = pyneat.PixelFormat.NV12
    semantic = tensor.semantic
    semantic.image = image
    tensor.semantic = semantic
    y = pyneat.Plane()
    y.role = pyneat.PlaneRole.Y
    y.shape = [height, width]
    y.strides_bytes = [width, 1]
    y.byte_offset = 0
    uv = pyneat.Plane()
    uv.role = pyneat.PlaneRole.UV
    uv.shape = [height // 2, width]
    uv.strides_bytes = [width, 1]
    uv.byte_offset = width * height
    tensor.planes = [y, uv]
    return tensor


# ---------------------------------------------------------------- pipeline

class LatestQueue:
    """Single-slot mailbox: always holds the most recent item, dropping older ones."""

    def __init__(self) -> None:
        self._q: "queue.Queue" = queue.Queue(maxsize=1)

    def put(self, item) -> None:
        try:
            self._q.put_nowait(item)
        except queue.Full:
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(item)
            except queue.Full:
                pass

    def get(self, timeout: float):
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None


class FpsMeter:
    def __init__(self, window: int = 30) -> None:
        self._t: list[float] = []
        self._window = window

    def tick(self) -> float:
        now = time.monotonic()
        self._t = (self._t + [now])[-self._window:]
        return (len(self._t) - 1) / (self._t[-1] - self._t[0]) if len(self._t) > 1 else 0.0


def capture_loop(source_run, capture_q, stop_event, errors) -> None:
    """Stage 1: pull decoded NV12 frames from the camera graph (5 s without a frame = error)."""
    last_frame = time.monotonic()
    try:
        while not stop_event.is_set():
            tensors = source_run.pull_tensors(timeout_ms=1000)
            if not tensors:
                if time.monotonic() - last_frame > 5.0:
                    raise RuntimeError("カメラから5秒間フレームが来ません(切断された?)")
                continue
            try:
                capture_q.put(tensor_nv12_from_decoded(tensors[0]))
                last_frame = time.monotonic()
            except RuntimeError:
                continue
    except Exception as exc:  # noqa: BLE001
        errors.append(f"capture: {exc}")
        stop_event.set()


class YoloStats:
    def __init__(self) -> None:
        self.pushed = 0
        self.push_dropped = 0
        self.pulled = 0
        self.infer_ms: list[float] = []   # push -> pull latency (queue wait + MLA + decode)

    def add_latency(self, ms: float) -> None:
        self.infer_ms = (self.infer_ms + [ms])[-300:]

    def latency_summary(self) -> str:
        if not self.infer_ms:
            return "n/a"
        s = sorted(self.infer_ms)
        return (f"mean={sum(s) / len(s):.1f}ms p95={s[min(len(s) - 1, int(len(s) * 0.95))]:.1f}ms "
                f"max={s[-1]:.1f}ms")


def pusher_loop(model_run, capture_q, pending_q, stats, stop_event, errors) -> None:
    """Stage 2: submit the newest frame to the model WITHOUT blocking.

    try_push(), not push(): Run.push() waits a hard-coded 5 s on a full queue and then stops
    the whole Run for good. A full queue just means "drop this frame" (capture_q only holds
    the newest one anyway). The model queue itself stays OverflowPolicy.Block.
    """
    try:
        while not stop_event.is_set():
            item = capture_q.get(timeout=0.5)
            if item is None:
                continue
            nv12, w, h = item
            t_push = time.perf_counter()
            if model_run.try_push([make_nv12_tensor(nv12, w, h)]):
                pending_q.put((item, t_push))
                stats.pushed += 1
            else:
                stats.push_dropped += 1
                time.sleep(0.005)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"push: {exc}")
        stop_event.set()


def puller_loop(cfg, model_run, pending_q, display_q, allowed, stats, stop_event, errors) -> None:
    """Stage 3: pull detections (FIFO-matched to pending frames) and decode boxes."""
    try:
        while not stop_event.is_set():
            sample = model_run.pull("detections", 1000)
            if sample is None:
                continue
            try:
                (nv12, w, h), t_push = pending_q.get(timeout=2.0)
            except queue.Empty:
                continue
            dets = decode_detections(sample, w, h, cfg, allowed)
            infer_ms = (time.perf_counter() - t_push) * 1000.0
            stats.pulled += 1
            if stats.pulled > 10:          # skip warm-up frames in the summary
                stats.add_latency(infer_ms)
            if cfg.print_detections:
                print(f"t={time.time():.3f} dets=" + "; ".join(
                    f"{class_label(c)} {s:.2f} [{x1},{y1},{x2},{y2}]" for x1, y1, x2, y2, s, c in dets),
                    flush=True)
            display_q.put((nv12, w, h, dets, infer_ms))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"pull: {exc}")
        stop_event.set()


def class_color(cid: int) -> tuple[int, int, int]:
    """Stable, saturated BGR color per class id."""
    hsv = np.uint8([[[(cid * 47) % 180, 220, 255]]])
    return tuple(int(v) for v in cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)[0, 0])


def draw_detections(bgr, dets) -> None:
    for x1, y1, x2, y2, score, cid in dets:
        color = class_color(cid)
        cv2.rectangle(bgr, (x1, y1), (x2, y2), color, 2)
        label = f"{class_label(cid)} {score:.2f}"
        (tw, th), base = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        ty = y1 - 4 if y1 - th - 6 >= 0 else y1 + th + 4
        cv2.rectangle(bgr, (x1, ty - th - 3), (x1 + tw + 4, ty + base - 1), color, -1)
        cv2.putText(bgr, label, (x1 + 2, ty - 1), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1, cv2.LINE_AA)


def draw_overlay(bgr, device: str, fps: float, infer_ms: float | None, n_dets: int | None) -> None:
    h, w = bgr.shape[:2]
    text = f"{device}  {w}x{h}  {fps:4.1f} fps"
    if infer_ms is not None:
        text += f"  yolo {infer_ms:4.0f}ms  {n_dets} obj"
    text += f"  {time.strftime('%H:%M:%S')}"
    scale = max(0.4, w / 1280)
    cv2.putText(bgr, text, (10, h - 12), cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(bgr, text, (10, h - 12), cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), 1, cv2.LINE_AA)


def display_loop(cfg, device, display_q, hdmi, video_run, stop_event, errors, counter) -> None:
    """Stage 4: NV12 -> BGR (+boxes, overlay) -> HDMI and/or UDP. Counts frames for --frames.

    Items are (nv12, w, h) without YOLO, or (nv12, w, h, dets, infer_ms) with it."""
    meter = FpsMeter()
    try:
        while not stop_event.is_set():
            item = display_q.get(timeout=0.5)
            if item is None:
                continue
            nv12, w, h = item[:3]
            dets, infer_ms = (item[3], item[4]) if len(item) > 3 else (None, None)
            fps = meter.tick()
            if cfg.overlay or dets or hdmi is not None or cfg.snapshot:
                bgr = cv2.cvtColor(nv12, cv2.COLOR_YUV2BGR_NV12)
                if dets:
                    draw_detections(bgr, dets)
                if cfg.overlay:
                    draw_overlay(bgr, device, fps, infer_ms, None if dets is None else len(dets))
                if cfg.snapshot:
                    counter[1] = bgr
                if hdmi is not None:
                    hdmi.push(bgr)
                    if hdmi.error:
                        raise RuntimeError(f"HDMI: {hdmi.error}")
                if video_run is not None:
                    nv12 = bgr_to_nv12(bgr)
            if video_run is not None:
                try:
                    video_run.push([make_nv12_tensor(nv12, w, h)])
                except RuntimeError:
                    pass
            counter[0] += 1
            if counter[0] % 75 == 0:
                msg = f"frames={counter[0]} fps={fps:.1f}"
                if dets is not None:
                    msg += f" yolo={infer_ms:.0f}ms objs=" + (
                        ",".join(f"{class_label(c)}({s:.2f})" for *_, s, c in dets) or "-")
                print(msg, flush=True)
            if cfg.frames and counter[0] >= cfg.frames:
                stop_event.set()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"display: {exc}")
        stop_event.set()


def main(argv: list[str] | None = None) -> int:
    cfg = parse_args(argv)
    if cfg.capture_format not in ("mjpeg", "yuyv"):
        raise ValueError("capture_format must be mjpeg or yuyv")
    if not (cfg.hdmi_enabled or cfg.udp_enabled):
        raise ValueError("hdmi_enabled と udp_enabled の少なくとも一方を有効にしてください")
    allowed = parse_classes(cfg.classes)
    if cfg.yolo_enabled and not resolve_app_path(cfg.model_path).exists():
        raise FileNotFoundError(f"model not found: {resolve_app_path(cfg.model_path)}")

    device = resolve_camera(cfg)
    print(f"capture: {device} {cfg.capture_format} {cfg.capture_width}x{cfg.capture_height}"
          f"@{cfg.capture_fps}", flush=True)

    # pyneat graphs must be built before hdmi_out calls Gst.init() (pyneat aborts otherwise).
    _source_graph, source_run = build_source_run(cfg, device)
    video_run = None
    if cfg.udp_enabled:
        _video_graph, video_run, port = build_video_run(cfg, cfg.capture_width, cfg.capture_height,
                                                        cfg.capture_fps)
        print(f"UDP: H.264/RTP -> {cfg.udp_host}:{port}", flush=True)
    model_run = None
    if cfg.yolo_enabled:
        t0 = time.perf_counter()
        _model, _model_graph, model_run = build_model_run(cfg, cfg.capture_width, cfg.capture_height,
                                                          cfg.capture_fps)
        print(f"YOLO: {resolve_app_path(cfg.model_path).name} {cfg.model_width}x{cfg.model_height} "
              f"score>={cfg.score_threshold} classes={cfg.classes or 'all'} "
              f"(built in {time.perf_counter() - t0:.1f}s)", flush=True)

    hdmi = None
    if cfg.hdmi_enabled:
        hdmi = hdmi_out.HdmiOutput(driver_name=cfg.hdmi_driver, width=cfg.hdmi_width,
                                   height=cfg.hdmi_height)
        print("HDMI: stopping lightdm (desktop) to take over the DevKit HDMI output...", flush=True)
        try:
            dw, dh = hdmi.start()
        except Exception:
            hdmi.close()
            raise
        print(f"HDMI: {dw}x{dh} via kmssink ({cfg.hdmi_driver}); lightdm is restarted on exit",
              flush=True)

    stop_event = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop_event.set())
    errors: list[str] = []
    counter = [0, None]   # [frames displayed, last drawn BGR frame for --snapshot]
    capture_q = LatestQueue()
    display_q = LatestQueue() if model_run is not None else capture_q
    stats = YoloStats()
    threads = [
        threading.Thread(target=capture_loop, args=(source_run, capture_q, stop_event, errors),
                         name="capture", daemon=True),
        threading.Thread(target=display_loop,
                         args=(cfg, device, display_q, hdmi, video_run, stop_event, errors, counter),
                         name="display", daemon=True),
    ]
    if model_run is not None:
        pending_q: "queue.Queue" = queue.Queue()   # FIFO: pushed frames awaiting detections
        threads += [
            threading.Thread(target=pusher_loop,
                             args=(model_run, capture_q, pending_q, stats, stop_event, errors),
                             name="model-push", daemon=True),
            threading.Thread(target=puller_loop,
                             args=(cfg, model_run, pending_q, display_q, allowed, stats, stop_event,
                                   errors),
                             name="model-pull", daemon=True),
        ]
    start = time.monotonic()
    try:
        for t in threads:
            t.start()
        while not stop_event.is_set():
            stop_event.wait(0.5)
    finally:
        stop_event.set()
        for t in threads:
            t.join(timeout=3.0)
        for run in (source_run, model_run, video_run):
            if run is not None:
                try:
                    run.close()
                except Exception:  # noqa: BLE001
                    pass
        if hdmi is not None:
            hdmi.close()
    elapsed = time.monotonic() - start
    if cfg.snapshot and counter[1] is not None:
        cv2.imwrite(str(resolve_app_path(cfg.snapshot)), counter[1])
        print(f"snapshot: {resolve_app_path(cfg.snapshot)}")
    print(f"=== frames={counter[0]} elapsed={elapsed:.1f}s avg_fps={counter[0] / max(elapsed, 1e-6):.1f}")
    if model_run is not None:
        print(f"=== yolo pushed={stats.pushed} push_dropped={stats.push_dropped} "
              f"latency(push->detections) {stats.latency_summary()}")
    for err in errors:
        print(f"[ERR] {err}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
