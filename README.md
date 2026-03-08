# fruitcap

A macOS command-line tool for video and audio capture using AVFoundation with Apple hardware-accelerated H.264/H.265 encoding.

## Requirements

- macOS
- Python 3
- [PyObjC](https://pyobjc.readthedocs.io/) (for AVFoundation/CoreMedia/Quartz bindings)

```bash
pip install pyobjc-framework-AVFoundation pyobjc-framework-CoreMedia pyobjc-framework-Quartz
```

## Usage

```bash
python3 fruitcap.py
```

Press `q` then Enter to stop recording. A live status line shows elapsed time, frames captured, file size, and any dropped frames.

## Configuration

Edit `fruitcap.cfg` to change capture settings:

```ini
[capture]
resolution = 4k
codec = h265
bit_depth = 8
chroma = 420
bitrate = 150000000
discard_late_frames = no
output = capture.mp4

[audio]
capture = yes
codec = aac
bitrate = 256000
sample_rate = 48000
channels = 2
```

### Video Settings

- **resolution** - `4k`, `1080p`, `720p`, or custom `WIDTHxHEIGHT` (e.g. `2560x1440`)
- **codec** - `h264` or `h265` (hardware-accelerated)
- **bit_depth** - `8` or `10` (10-bit requires h265)
- **chroma** - `420` or `422` chroma subsampling
- **bitrate** - Video bitrate in bits per second (150000000 = 150 Mbps)
- **discard_late_frames** - `yes` to drop frames if the encoder falls behind, `no` to preserve all frames
- **output** - Output file path (MP4)

### Audio Settings

- **capture** - `yes` or `no` to enable/disable audio recording
- **codec** - `aac` (lossy) or `alac` (lossless)
- **bitrate** - AAC bitrate in bits per second (ignored for ALAC)
- **sample_rate** - Sample rate in Hz (48000 = 48 kHz)
- **channels** - Number of audio channels

### Example: High-Quality 4K Capture (Blackmagic UltraStudio)

```ini
[capture]
resolution = 4k
codec = h265
bit_depth = 10
chroma = 422
bitrate = 150000000
discard_late_frames = no
output = capture.mp4

[audio]
capture = yes
codec = alac
sample_rate = 48000
channels = 2
```
