# raspicam — USBカメラ + YOLO物体認識をDevKitのHDMIに表示

Modalix DevKitにUSB接続されたUVCカメラ（Raspberry Pi Zero W + IMX219 の UVC gadget
`IMX219 NoIR UVC Camera`）の映像を、YOLO26m（DevKitのMLA）で物体認識し、検出枠付きで
DevKit本体のHDMIモニタに全画面表示する。オプションでH.264/RTP over UDP配信も可能。
将来はロボットアーム先端のカメラとして物体探索に使う想定。

```
USB camera --v4l2src(YUYV)--> pyneat Graph (NV12) --capture--> LatestQueue
  --pusher (try_push)--> YOLO26m Run (MLA, OverflowPolicy.Block, depth 2)
  --puller (decode_bbox)--> LatestQueue --display--> BGR + boxes + overlay
      --> HDMI (kmssink / hdmi_out.py)   \-> [--udp] pyneat VideoSender (H.264/RTP/UDP)
```
`--no-yolo` のときは capture → display の素通し（カメラビューア）。

## 使い方

```bash
cd /workspace/raspicam
dk ./tools/probe_camera.py          # 接続中のUVCカメラと対応モードを一覧
dk ./main.py --frames 150           # 有限回のスモークテスト
dk ./main.py                        # 連続表示 (Ctrl-C / SIGTERM で終了)
dk ./main.py --classes cup,bottle   # 指定したCOCOクラスだけ表示
dk ./main.py --print-detections     # 毎フレームの検出 (クラス スコア [x1,y1,x2,y2]) を標準出力へ
dk ./main.py --frames 90 --snapshot tools/out/yolo_snapshot.jpg   # 最後の表示フレームを保存
dk ./main.py --model assets/yolo26n-det-bf16-mla_tess-b1.tar.gz  # 軽量モデルと比較
dk ./main.py --no-yolo              # 物体認識なし
dk ./main.py --udp                  # HDMI + UDP(10.42.0.1:9000)
dk ./main.py --udp --no-hdmi        # UDPのみ (デスクトップを止めない)
dk ./main.py --camera-device /dev/video96 --capture-format yuyv
```

設定は [config/default.conf](config/default.conf)（各項目にコメントあり）。CLI引数が優先。

UDP受信側:
```bash
gst-launch-1.0 udpsrc port=9000 caps="application/x-rtp,media=video,encoding-name=H264,payload=96" \
  ! rtph264depay ! h264parse ! avdec_h264 ! videoconvert ! autovideosink sync=false
```
Neat SDKコンテナ内で受ける場合、9000番台はInsightが握っているため
`/workspace/mViewer/devkit_relay.py` 経由にするか、ホストのデスクトップで受信する。

## YOLO（実測 2026-10-18）
- モデル: `assets/yolo26m-det-bf16-mla_tess-b1.tar.gz`（COCO 80クラス、640x640 letterbox）。
  YOLO26 は Ultralytics 系なので `NormalizePreset.COCO_YOLO` + `BoxDecodeType.YoloV26`。
- 60秒連続（HDMI表示あり）: **14.9fps、推論遅延 push→検出 平均27.9ms / p95 28.9ms / 最大38.3ms、
  投入ドロップ0**。律速はカメラ（15fps）で、MLAには約2倍の余裕がある。
- 検出座標は撮影フレーム（640x360）の画素座標で返る（letterbox の逆変換済み、snapshotで確認）。
- `score_threshold=0.35` は仮の値。対象物・照明で最適値が変わるので、`--print-detections` で
  実シーンのスコア分布を見て決める。COCO にない物体（例: クッション）は近いクラス（chair, bed）に
  誤認識されることがある → 対象物が決まったらこのカメラの画像で追加学習するのが効果的。

## 注意点（実機で確認済み 2026-10-18）

- **カメラの正体:** Raspberry Pi Zero W（192.168.88.55、ユーザー shimizu）+ IMX219 NoIR。
  Pi上の `imx219-uvc.service` が configfs UVC gadget + `/usr/bin/uvc-gadget`（libcamera）で
  UVCカメラとして振る舞う。モードは YUYV 640x360 @15/10/5fps のみ（[pi/README.md](pi/README.md)）。
- **既定は 640x480@10・全画角（水平62°x垂直49°）。** Pi側 uvc-gadget を改造し、センサーの
  1640x1232 全域ビニングモードを使う（[pi/README.md](pi/README.md)）。実測 8.6fps（YOLO26m + HDMI、60秒）。
  USB isoc 帯域（8.2MB/s）の75%を使うため Pi Zero の送信取りこぼしで 10fps に届かない。
  速さ優先なら `--width 640 --height 360 --fps 15`（水平62°x垂直35°、15fps安定）。
  MJPEG（Pi Zero のソフトウェアJPEG、3.9fps・強圧縮）は Pi 側で無効にした。
- 元の g_webcam 方式では 720p や 10/2fps を要求すると Pi 側 uvc-gadget が SIGFPE で落ち、
  カメラがUSBから数秒消えていた（configfs 方式で解消）。カメラ番号が変わっても
  `camera_device=auto` が uvcvideo の Video Captureノードを探して追従する。
- **HDMI表示中はlightdm（デスクトップ）を停止する。** kmssinkがDRM masterを取るため。
  正常終了時は自動で再開するが、SIGKILLした場合は `sudo systemctl start lightdm` を手動で。
  HDMI出力の制約（smifbドライバ指定、ディスプレイサイズへの事前リサイズ、skip-vsync）は
  [hdmi_out.py](hdmi_out.py)（Scouterから流用）の docstring 参照。
- pyneatのGraphはgst-pythonの `Gst.init()`（hdmi_out）より**先に**buildしている。逆順だとpyneatがabortする。
- カメラから5秒間フレームが来なければ（抜線など）エラー終了する。
