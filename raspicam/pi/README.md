# Pi側: IMX219 UVCカメラを configfs 方式に切り替える

対象: Raspberry Pi Zero W (192.168.88.55) + IMX219 NoIR、`imx219-uvc.service`。

**2026-10-18 に適用済み。** 元に戻すためのバックアップ（Pi上）:
- `/etc/systemd/system/imx219-uvc.service.bak-g_webcam`（g_webcam 方式のサービス）
- `/usr/lib/arm-linux-gnueabihf/libuvcgadget.so.0.4.0.bak-20261018`（改造前の uvc-gadget ライブラリ）
- `~/uvc-gadget/lib/libcamera-source.cpp.before_sensormode`（改造前のソース）

## なぜ
- 元は `modprobe g_webcam`（カーネル固定のモード表: 640x360@15/10/2、1280x720@2）。
- `uvc-gadget` が g_webcam 用に持つモード表（`~/uvc-gadget/lib/configfs.c` の legacy 表）は
  間隔値の桁が1つ多い（10fps → 10000000 = 1fps、2fps → 50000000 = 0.2fps）。0.2fps は整数化で
  0 になり、`libcamera_source_set_frame_rate()` の `1000000 / fps` で **SIGFPE**。
  1280x720、640x360@10/2fps を要求するとサービスが落ちてカメラがUSBから消えていた。
- configfs 方式では uvc-gadget はこのスクリプトが作ったモード表を読むので、どのモードでも落ちない。

## 試して分かった Pi Zero W の限界（実測）
| 設定 | 結果 |
|---|---|
| streaming_maxpacket=3072 | `dwc2_hsotg_ep_enable: No suitable fifo found`、映像が一切流れない |
| streaming_maxpacket=2048 | 流れるが `VS request completed with status -61`（isoc送信の取りこぼし）が多発。640x360@30要求→14.9fps、960x540@15→9.9fps、開始時に0フレームのことも |
| streaming_maxpacket=1024（採用） | 8.2MB/s。640x360@15 = 6.9MB/s で 14.9fps 安定（60秒、HDMI表示あり）。-61 は開始/終了時に十数件のみ |
| 1024 + 960x540@7.5 / 1280x720@4 | 動くが、その後 640x360 に戻すと uvc-gadget がサービス再起動までフレームを送らなくなる |

結論: USB(isoc)帯域から、YUYV では **640x360@15 が上限**。モード表は 640x360 の 15/10/5fps のみにした。
これ以上の解像度/fpsには、Pi側で圧縮が必要（uvc-gadget の MJPEG はソフトウェアJPEGで 3.9fps しか出ない。
VideoCore のハードウェアJPEGエンコーダを使うよう uvc-gadget を改造すれば可能性はある）。

## 適用（Piで実行）
DevKitまたはホストから:
```bash
cd /workspace/raspicam/pi
scp imx219-uvc-gadget.sh imx219-uvc.service shimizu@192.168.88.55:/tmp/
ssh shimizu@192.168.88.55
```
Pi上で:
```bash
sudo cp -a /etc/systemd/system/imx219-uvc.service /etc/systemd/system/imx219-uvc.service.bak-g_webcam
sudo install -m 755 /tmp/imx219-uvc-gadget.sh /usr/local/sbin/imx219-uvc-gadget.sh
sudo install -m 644 /tmp/imx219-uvc.service /etc/systemd/system/imx219-uvc.service
sudo systemctl daemon-reload
sudo systemctl restart imx219-uvc
sleep 8; systemctl is-active imx219-uvc
journalctl -u imx219-uvc --since -1min --no-pager | grep -v "request (req\|bRequest" | tail
```
`imx219 UVC gadget bound to 20980000.usb (maxpacket=3072)` が出ていればOK。
DevKit側のデバイス名は "IMX219 NoIR UVC Camera: UVC Cam" に変わる（raspicam は auto 検出なので設定変更不要）。

## 確認（DevKit側）
```bash
cd /workspace/raspicam
dk ./tools/probe_camera.py          # YUYV 640x360 15/10/5fps、640x480 10/5fps
dk ./main.py --frames 300           # 約15fps
```

## 元に戻す
```bash
sudo cp -a /etc/systemd/system/imx219-uvc.service.bak-g_webcam /etc/systemd/system/imx219-uvc.service
sudo systemctl stop imx219-uvc          # ExecStopPost が configfs gadget を削除する
sudo /usr/local/sbin/imx219-uvc-gadget.sh stop   # 念のため
sudo systemctl daemon-reload
sudo systemctl start imx219-uvc         # g_webcam 方式で起動
```

## 画角（センサーモード）の実測 2026-10-18
libcamera は出力サイズからセンサーモードを自動選択し、モードによって画角が大きく変わる
（rpicam-vid、動画ロール = uvc-gadget と同じ選択。比較画像: `tools/out/fov_compare.jpg`）。

| 出力 | 自動選択されるセンサーモード | 画角 |
|---|---|---|
| 640x360（現行） | 1920x1080（中央切り出し、ビニングなし） | 狭い（水平 約39°） |
| 640x480 | 640x480（中央 1280x960 切り出し + 2x2ビニング） | さらに狭い |
| 640x480 + `--mode 1640:1232` | 1640x1232（全域 2x2ビニング） | 全画角 水平 約62° x 垂直 約49° |
| 640x360 + `--mode 1640:1232` | 1640x1232（上下を切る） | 水平 約62° |

### 全画角化（適用済み）
uvc-gadget にはセンサーモード指定がないので、`lib/libcamera-source.cpp` を改造した
（[uvc-gadget-sensor-mode.patch](uvc-gadget-sensor-mode.patch)）。環境変数
`UVC_SENSOR_MODE=W:H:BITS` があれば `CameraConfiguration::sensorConfig` を固定する。サービスで
`UVC_SENSOR_MODE=1640:1232:10` を設定し、640x360 も 640x480 も全域ビニングモードから作る。
ビルド: Pi上で `ninja -C ~/uvc-gadget/build`（約30秒）→ `build/lib/libuvcgadget.so.0.4.0` を
`/usr/lib/arm-linux-gnueabihf/` へ install（`/usr/bin/uvc-gadget` はこの共有ライブラリを使う）。

| モード | 画角 | 実測（DevKit, YOLO26m + HDMI） |
|---|---|---|
| 640x480@10 | 水平62° x 垂直49° | 60秒平均 8.6fps（7.1-9.4）、-61 取りこぼし 110件/分 |
| 640x360@15 | 水平62° x 垂直35° | 15.1fps 安定、-61 十数件 |
| 640x480@5 | 同上 | 4.8fps |

640x480@10 は 6.1MB/s（帯域の75%）で、Pi Zero の dwc2 が isoc 送信を取りこぼし、欠けたフレームが
ホストで捨てられるため 10fps に届かない。
全サイズが同じセンサーモードになったので、640x480 → 640x360 の切り替えで uvc-gadget が止まる
問題も起きなくなった（以前はセンサーモード自体が切り替わっていた）。

全画角化を外す: `sudo systemctl edit imx219-uvc` で `Environment=UVC_SENSOR_MODE=` を設定して restart。
