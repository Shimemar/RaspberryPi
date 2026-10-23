# Raspberry Pi Zero W + IMX219 NoIR → Modalix DevKit
## 電源再投入後のUSB UVCカメラ復旧手順

更新日：2026-10-18

## 1. 構成と確認済みの動作

- Raspberry Pi Zero W（Raspbian GNU/Linux 13 / Trixie）
- CSIカメラ：IMX219 **NoIR**
- USB Gadget：Linux `g_webcam`（従来型UVC Gadget）
- 映像供給ソフト：`uvc-gadget`（libcamera）
- USB接続：**Pi Zero WのUSBデータ用micro-Bポート** → **Modalix DevKitのUSBホストポート**。Pi Zeroの給電専用PWR INポートはデータ通信に使用できない。
- Modalix側では以前 `/dev/video96`（映像取得用）、`/dev/video97`、`/dev/media0` として認識。**デバイス番号は再起動後に変わることがある**。
- 確認済み映像：MJPEG、640×360。連続ストリーミングの実測値は約2.8fps（今後改善予定）。
- NoIR用チューニングファイル：`/usr/share/libcamera/ipa/rpi/vc4/imx219_noir.json`

> 注意：映像配信の起動には **2つの操作**（`g_webcam` ロード、`uvc-gadget` 起動）が必要。手動実行では再起動ごとに行う。既に自動起動サービスが設定されている場合は二重起動しない。

## 2. 電源再投入後の標準起動手順

### 手順① ケーブルと電源の確認

1. Pi Zero WのUSBデータポート（`USB` と表示）とModalix DevKitのUSBホストを接続。
2. Pi Zero Wの電源を投入し、OSの起動を確認。
3. Windows PCとPi Zero Wを同じネットワークへ接続。PiのSSH/Wi-Fiが利用可能であることを確認。

※ハードウェアの給電方法に応じて、電源容量とGNDを適切に確保する。USBポートの抜き差しは機器を傷めないように行う。

### 手順② Windows PowerShellからSSH接続

```powershell
ssh shimizu@raspberrypi.local
```

`raspberrypi.local` で名前解決できない場合は、Raspberry PiのIPアドレスで接続する。

```powershell
ssh shimizu@<Raspberry_PiのIPアドレス>
```

### 手順③ Raspberry Pi側：UVC Gadgetドライバをロード

```bash
sudo modprobe g_webcam streaming_maxpacket=1024 streaming_interval=1
```

動作確認：

```bash
lsmod | grep g_webcam
cat /sys/module/g_webcam/parameters/streaming_maxpacket
cat /sys/module/g_webcam/parameters/streaming_interval
cat /sys/class/udc/20980000.usb/state
```

期待する設定値は `1024` と `1`。`modprobe` 実行時点ですでにロード済みなら、新しい引数は反映されない。`state` はホスト接続状態で変化する。

### 手順④ Raspberry Pi側：NoIR設定を使って映像配信を開始

```bash
sudo env \
  LIBCAMERA_RPI_TUNING_FILE=/usr/share/libcamera/ipa/rpi/vc4/imx219_noir.json \
  uvc-gadget -c 0 20980000.usb
```

期待する出力例：

```text
Using tuning file /usr/share/libcamera/ipa/rpi/vc4/imx219_noir.json
Using camera imx219 (...)
Device /dev/video1 opened: 20980000.usb (gadget.0).
```

**このコマンドは前面実行のため、SSH画面を閉じずにそのまま維持する。** Modalixから映像取得を開始すると `Starting video stream.` と表示される。

> 重要：`LIBCAMERA_RPI_TUNING_FILE=imx219_noir.json` のようにファイル名だけを渡すと、この環境では `Failed to open tuning file` が発生した。**必ず絶対パスで指定する。**

## 3. Modalix DevKit側：認識・映像受信の確認

Modalixへ別のSSHセッションでログインし、以下を実行。

### 手順⑤ UVCカメラ認識を確認

```bash
v4l2-ctl --list-devices
```

以前の認識例：

```text
Webcam gadget: UVC Camera (usb-0003:01:00.0-3.2):
    /dev/video96
    /dev/video97
    /dev/media0
```

現在のデバイス番号を確認し、以降の `/dev/video96` は必要に応じて読み替える。

```bash
v4l2-ctl -d /dev/video96 -D
v4l2-ctl -d /dev/video96 --list-formats-ext
```

映像取得用ノードは `Video Capture` がある側を選ぶ。

### 手順⑥ JPEGフレームを1枚保存

```bash
rm -f /tmp/imx219.jpg

timeout 15s v4l2-ctl -d /dev/video96 \
  --set-fmt-video=width=640,height=360,pixelformat=MJPG \
  --stream-mmap \
  --stream-count=1 \
  --stream-to=/tmp/imx219.jpg

file /tmp/imx219.jpg
ls -lh /tmp/imx219.jpg
```

成功例：

```text
JPEG image data, JFIF standard 1.01, ... 640x360, components 3
```

### 手順⑦ 連続ストリーミングを確認（任意）

```bash
timeout 20s v4l2-ctl -d /dev/video96 \
  --set-fmt-video=width=640,height=360,pixelformat=MJPG \
  --stream-mmap \
  --stream-poll
```

これまでの実測値は約 **2.8fps**。現在は正常受信の確認が目的で、15fpsは未達。

## 4. 異常時の確認

| 現象 | 対応 |
| --- | --- |
| `g_webcam` が認識されない | Piで `lsmod \| grep g_webcam`、`sudo dmesg \| tail -n 40` を確認。 |
| `No cameras were identified`、チューニングファイルを開けない | NoIR用JSONの**絶対パス**とファイルの存在を確認。 |
| ModalixにUVCカメラが現れない | PiのUSB**データ用**ポート、ケーブル、`g_webcam`、Modalix側 `lsusb -t`、再認識を確認。 |
| `/dev/video96` がない | `v4l2-ctl --list-devices` で現在の番号を調べる。 |
| JPEGを取得できない | Piで `uvc-gadget` が実行中か、`Starting video stream.` が出るか、Modalix側がMJPG 640×360をサポートするか確認。 |
| 色が赤・ピンク・緑になる | NoIR用チューニングが読み込まれているか、実験で変更した `mjpeg_encoder.cpp` のU/V並びが元の `y_rows, u_rows, v_rows` に戻っているか確認。 |
| FPSが低い | 現在約2.8fps。固定JPEGは10〜15fps以上、Pi単体のYUV420撮影は約14.3fpsだったため、MJPEGソフトウェアエンコードとバッファ処理を調査中。 |

確認に使うコマンド（Pi側）：

```bash
lsmod | grep g_webcam
pgrep -af uvc-gadget
sudo dmesg | tail -n 50
ls -l /usr/share/libcamera/ipa/rpi/vc4/imx219_noir.json
```

## 5. 手動で送信を停止・再起動する

1. Pi側で起動中の `uvc-gadget` のターミナルで `Ctrl+C` を押す。
2. 同じコマンドで再起動する。

```bash
sudo env \
  LIBCAMERA_RPI_TUNING_FILE=/usr/share/libcamera/ipa/rpi/vc4/imx219_noir.json \
  uvc-gadget -c 0 20980000.usb
```

`g_webcam` を明示的に入れ直す必要がある場合は、先に `uvc-gadget` を停止したうえで：

```bash
sudo modprobe -r g_webcam
sudo modprobe g_webcam streaming_maxpacket=1024 streaming_interval=1
```

その後、上記の `uvc-gadget` 起動コマンドを実行する。

## 6. Raspberry Pi OS自体を再起動する場合

```bash
sudo reboot
```

再起動後にSSHで再接続し、**手順③以降**を繰り返す。

## 7. 実験中のコードに関する注意

FPS調査のため、`~/uvc-gadget/lib/mjpeg_encoder.cpp` にJPEG圧縮時間の測定コードを追加し、JPEG品質の調整などを実験していた。環境によってソース・インストール済みバイナリが初期状態と異なる可能性がある。

- 元の色成分順序：`JSAMPARRAY rows[] = { y_rows, u_rows, v_rows };`
- NoIR用チューニングは**起動時の環境変数**で指定する。
- JPEG品質や計測コードを変更した場合、改変状態を確認してから使用する。
- `g_webcam` のパラメータを変える場合はプロセスを停止し、モジュールを再ロードする。

## 8. 最短コマンド集

Pi Zero Wの電源投入・SSH接続後：

```bash
sudo modprobe g_webcam streaming_maxpacket=1024 streaming_interval=1
sudo env LIBCAMERA_RPI_TUNING_FILE=/usr/share/libcamera/ipa/rpi/vc4/imx219_noir.json \
  uvc-gadget -c 0 20980000.usb
```

Modalix側：

```bash
v4l2-ctl --list-devices
v4l2-ctl -d /dev/video96 --list-formats-ext
```

以上。
