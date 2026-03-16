# pjcap

A macOS command-line tool for video and audio capture using AVFoundation with Apple hardware-accelerated H.264/H.265/ProRes encoding.

Author: Phil Jensen <philj@philandamy.org>

## Requirements

- macOS
- Python 3
- [PyObjC](https://pyobjc.readthedocs.io/) (for AVFoundation/CoreMedia/Quartz bindings)

```bash
pip install pyobjc-framework-AVFoundation pyobjc-framework-CoreMedia pyobjc-framework-Quartz
```

## Usage

```bash
python3 pjcap.py [options]
```

Press `q` then Enter to stop recording. A live status line shows elapsed time, frames captured, file size, and any dropped frames. By default, output filenames include the current date and time.

### Common Examples

```bash
# Record with defaults from pjcap.cfg
python3 pjcap.py

# Record 10 seconds of ProRes 422 (auto-selects MOV, 10-bit 4:2:2, PCM audio)
python3 pjcap.py --codec prores --time 10

# Record H.265 at 50 Mbps with preview
python3 pjcap.py --codec h265 --bitrate 50m --preview

# List available capture devices
python3 pjcap.py --list-devices

# List supported formats for a specific device
python3 pjcap.py --device "DNxIO" --list-formats

# Record from a specific device with timestamped output
python3 pjcap.py --device 1 -o "capture-%d-%t.mp4"

# Record with segment splitting every 5 minutes
python3 pjcap.py --split-every 300
```

### CLI Options

| Flag | Description |
|------|-------------|
| `--codec` | Video codec: `h264`, `h265`, `prores`, `prores_proxy`, `prores_lt`, `prores_hq` |
| `--container` | Container format: `mp4`, `mov` (auto-selects based on codec) |
| `--bitrate` | Video bitrate, e.g. `80m`, `500k`, `150000000` |
| `--resolution` | `4k`, `1080p`, `720p`, or `WIDTHxHEIGHT` |
| `--fps` | Frame rate, e.g. `29.97`, `24`, `60` |
| `--chroma` | Chroma subsampling: `420` or `422` |
| `--bit-depth` | Bit depth: `8` or `10` |
| `--color-space` | Color space: `bt709`, `bt2020`, `hlg`, `pq` |
| `--discard-late-frames`, `--no-discard-late-frames` | Enable or disable dropping late video frames |
| `-o`, `--output` | Output file path (supports `%d` date, `%t` time tokens) |
| `--no-overwrite` | Append `_1`, `_2`, etc. instead of overwriting |
| `--config` | Path to alternate config file |
| `--device` | Select video device by index or name substring |
| `--audio-device` | Select audio device by index or name substring |
| `--audio-codec` | Audio codec: `aac`, `alac`, `pcm` |
| `--audio-bitrate` | Audio bitrate for AAC, e.g. `256k` |
| `--audio-sample-rate` | Audio sample rate in Hz |
| `--audio-channels` | Audio channel count |
| `--list-devices` | List available video and audio capture devices |
| `--list-formats` | List supported pixel formats and frame rates for the selected device |
| `--time` | Stop recording after N seconds (supports fractional) |
| `--frames` | Stop recording after N frames |
| `--audio-only` | Record audio only |
| `--preview` | Show live source preview window |
| `--preview-compressed` | Show compressed output preview window |
| `-p`, `--preview-both` | Show both source and compressed preview windows |
| `-q`, `--quiet` | Suppress status output |
| `--split-every` | Split into segments every N seconds |
| `--split-size` | Split into segments at size threshold, e.g. `500m`, `2g` |

## Configuration

Edit `pjcap.cfg` to set defaults. All settings can be overridden via CLI flags.

```ini
[capture]
resolution = 4k
codec = h264
bit_depth = 8
chroma = 420
bitrate = 80000000
discard_late_frames = no
output = capture-%d-%t.mp4

[audio]
capture = yes
codec = aac
bitrate = 256000
sample_rate = 48000
channels = 2
```

### Video Settings

- **resolution** — `4k`, `1080p`, `720p`, or custom `WIDTHxHEIGHT`
- **codec** — `h264`, `h265`, `prores`, `prores_proxy`, `prores_lt`, `prores_hq`
- **container** — `mp4`, `mov`, or `auto` (ProRes auto-selects MOV)
- **bit_depth** — `8` or `10` (10-bit requires H.265 or ProRes)
- **chroma** — `420` or `422` chroma subsampling
- **bitrate** — Video bitrate in bps or shorthand (`80m`, `500k`)
- **fps** — Frame rate (omit for device native rate)
- **color_space** — `bt709`, `bt2020`, `hlg`, `pq`
- **discard_late_frames** — `yes` to drop frames if encoder falls behind
- **output** — Output file path (`%d` = `YYYYMMDD`, `%t` = `HHMMSS`)

### Audio Settings

- **capture** — `yes` or `no`
- **codec** — `aac` (lossy), `alac` (lossless), or `pcm` (uncompressed 24-bit)
- **bitrate** — AAC bitrate in bps (ignored for ALAC/PCM)
- **sample_rate** — Sample rate in Hz
- **channels** — Number of audio channels

In `--audio-only` mode, pjcap defaults to `.m4a` output for AAC/ALAC and `.caf` for PCM.

### ProRes

ProRes 422 variants are natively 10-bit 4:2:2 codecs. When ProRes is selected, pjcap automatically:
- Forces 10-bit 4:2:2 pixel format
- Selects MOV container
- Defaults audio to 24-bit PCM

### Example: High-Quality 4K Capture (Avid DNxIO)

```ini
[capture]
resolution = 4k
codec = h265
bit_depth = 10
chroma = 422
bitrate = 150000000
discard_late_frames = no
output = capture-%d-%t.mp4

[audio]
capture = yes
codec = alac
sample_rate = 48000
channels = 2
```

## Tests

```bash
python3 -m pytest test_pjcap.py -v
```
