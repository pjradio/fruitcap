pjcap is a macOS video/audio capture toolkit written in Python. It uses AVFoundation with Apple hardware-accelerated H.264/H.265/ProRes encoding via AVAssetWriter.

## Architecture

- `pjcap.py` — Command-line capture tool (single-file application)
- `pjcap-gui.py` — PyQt5 GUI wrapping the same capture engine
- `pjcap.cfg` — INI-style config file with `[capture]` and `[audio]` sections
- `test_pjcap.py` — Unit tests (150 tests)
- `aja-capture.cpp` — C++ helper that captures raw frames from AJA NTV2 devices and streams them to stdout
- `CMakeLists.txt` — CMake build for `aja-capture` (links against AJA NTV2 SDK)
- `frametimes.py` — Frame timing analysis tool (requires ffprobe, optionally mediainfo)
- `qpdump.py` — Per-frame QP dump tool (requires ffmpeg/ffprobe)
- `list_encoders.py` — Lists all VideoToolbox encoders on the system

### Pipeline (AVFoundation mode)

1. `AVCaptureSession` with `InputPriority` preset if supported (falls back to default preset for devices like Avid DNxIO)
2. `AVCaptureVideoDataOutput` delivers frames as `CMSampleBuffer` to a delegate on a serial dispatch queue
3. `AVCaptureAudioDataOutput` delivers audio on a separate serial dispatch queue
4. `AVAssetWriter` with `AVAssetWriterInput` handles H.264/H.265/ProRes hardware encoding and MP4/MOV muxing
5. Audio encoded as AAC, ALAC, or PCM via a second `AVAssetWriterInput`

### Pipeline (AJA mode)

1. `aja-capture` C++ subprocess opens the AJA device via NTV2 SDK, configures input routing, pixel format, and audio
2. Frames stream to stdout as `[4B BE video_size][video_data][4B BE audio_size][audio_data]`; first line is a JSON header with signal info
3. Python reads frames in a background thread, creates `CVPixelBuffer`s, and displays them via `AVSampleBufferDisplayLayer`
4. When recording, frames are fed to `AVAssetWriter` via `AVAssetWriterInputPixelBufferAdaptor`; audio is converted from raw PCM to `CMSampleBuffer`s
5. `"stop\n"` on stdin triggers graceful shutdown of the helper

### Key Classes (pjcap.py)

- `SampleBufferDelegate` — PyObjC delegate, routes video/audio sample buffers by comparing the output reference
- `Recorder` — Manages session, writer, and all capture state; supports segment splitting and auto-stop by duration/frame count
- `CompressedPreview` — VTCompressionSession-based preview that re-encodes frames and displays them via AVSampleBufferDisplayLayer, showing compression artifacts in real time
- `load_config()` — Parses `pjcap.cfg`, validates settings, applies CLI overrides

### Key Classes (pjcap-gui.py)

- `PjcapGUI` — Main window; manages preview session, recording lifecycle, and UI state
- `PreviewWidget` — Qt widget hosting an `AVCaptureVideoPreviewLayer` via its native NSView
- `AudioLevelMeterWidget` — Two-channel horizontal dBFS meter with color-coded levels (green/yellow/red) and tick marks
- `GUISampleBufferDelegate` — Extends `SampleBufferDelegate` to extract dBFS audio levels from `AVCaptureConnection.audioChannels()` for the meter
- `StatusSignal` — QObject bridge emitting `pyqtSignal`s to route stop/level notifications from capture threads to the Qt main thread

### GUI Architecture

The GUI imports `Recorder`, `SampleBufferDelegate`, `CompressedPreview`, `load_config`, and other functions directly from `pjcap.py`. The session is created at preview start with data outputs and delegate already attached. When recording starts, a `Recorder` is created and adopts the running session via `adopt_session()` — no session reconfiguration, so the preview stays seamless. Recording stops in a background thread to avoid blocking the GUI, with completion signaled via `StatusSignal.stopped`.

### AJA Capture Architecture

- `aja-capture.cpp` is a standalone C++ binary using the AJA NTV2 SDK; it handles device acquisition, signal detection, input routing, and DMA transfer via `AutoCirculate`
- The binary supports `--pixel-format` to select between `8BitYCbCr` (UYVY) and `10BitYCbCr` (v210); both are 4:2:2 — AJA devices have no 4:2:0 framebuffer format, so chroma subsampling to 4:2:0 happens at the encoder level
- `_AJA_PIXEL_FORMATS` in `pjcap.py` maps AJA format names to CVPixelBuffer format types (`8BitYCbCr` → `kCVPixelFormatType_422YpCbCr8`, `10BitYCbCr` → `kCVPixelFormatType_422YpCbCr10`)
- Both CLI (`run_aja_capture`) and GUI (`_start_aja_preview`) pass `--pixel-format 10BitYCbCr` when 10-bit is selected
- The GUI auto-detects AJA devices at startup and defaults to AJA mode if one is found
- Changing bit depth or chroma in the GUI restarts the AJA preview subprocess so the device reconfigures
- HDMI input is auto-detected; the helper also supports explicit `--input` for HDMI1-4 and SDI sources
- Audio is captured as raw 32-bit signed integer PCM at 48kHz with all available channels (typically 16 for HDMI); pjcap extracts the configured channel count for encoding

### Key Functions (pjcap.py)

- `log(msg)` — Quiet-mode-aware print wrapper
- `parse_bitrate(value)` — Parses shorthand like "80m", "500k" to int bps
- `parse_size(value)` — Parses shorthand like "500m", "2g" to int bytes
- `generate_output_path(template, no_overwrite)` — Expands %d/%t tokens, handles --no-overwrite
- `generate_segment_path(base_path, segment_num)` — Segment file naming (_001, _002, etc.)
- `get_device_formats(device)` — Extracts format info from AVCaptureDevice
- `format_device_formats(formats)` — Formats device format info into aligned display lines with FourCC descriptions
- `get_devices(media_type)` — Wraps AVCaptureDevice.devicesWithMediaType_
- `list_devices(devices)` — Returns (index, name, uid) tuples
- `find_device_by_selector(devices, selector, label)` — Find device by index or name substring
- `select_device_format(device, width, height, fps)` — Find and activate a matching device format
- `build_capture_video_output_settings(chroma, bit_depth)` — Build pixel format settings dict for AVCaptureVideoDataOutput
- `make_frame_duration(fps)` — Create a CMTime for the given frame rate
- `get_output_file_type_and_extension(cfg)` — Determine AVFileType and extension from config
- `run_headless(recorder)` — Headless mode with SIGTERM/SIGINT handling

### Config Options

**[capture]**: resolution (4k/1080p/720p/WIDTHxHEIGHT), codec (h264/h265/prores/prores_proxy/prores_lt/prores_hq), container (mp4/mov/auto), bit_depth (8/10), chroma (420/422), bitrate, fps, color_space (bt709/bt2020/hlg/pq), discard_late_frames, output

**[audio]**: capture (yes/no), codec (aac/alac/pcm), bitrate, sample_rate, channels

### CLI Flags

- `--codec`, `--bitrate`, `--resolution`, `--fps`, `--chroma`, `--bit-depth` — Override config values
- `--container` — Force mp4 or mov (auto-selects based on codec by default)
- `--color-space` — Color space preset (bt709/bt2020/hlg/pq)
- `-o` / `--output` — Output file path (supports %d date and %t time tokens)
- `--no-overwrite` — Append _1, _2 etc. instead of overwriting
- `--config` — Use alternate config file
- `--device` / `--audio-device` — Select capture device by index or name
- `--audio` / `--no-audio` — Enable or disable audio capture (overrides config)
- `--list-devices` — List available video/audio capture devices
- `--list-formats` — List supported formats for the selected device
- `--time SECONDS` — Stop after duration (supports fractional seconds)
- `--frames N` — Stop after N frames
- `--preview` / `--preview-compressed` / `-p` — Preview modes
- `--vu` — Show a VU meter on the status line for audio level monitoring
- `-q` / `--quiet` — Suppress status output
- `--split-every SECONDS` / `--split-size SIZE` — Segment splitting
- `--audio-only` — Record audio only (no video)
- `--aja` — Capture from AJA device via aja-capture helper instead of AVFoundation
- `--aja-device SPEC` — AJA device index, serial, or model name (default: 0)
- `--aja-channel N` — AJA input channel 1-8 (default: 1)
- `--aja-input SOURCE` — AJA input source: hdmi, hdmi1-4, sdi (default: auto-detect)

### ProRes Behavior

- ProRes 422 variants are natively 10-bit 4:2:2; `load_config` forces `bit_depth=10` and `chroma="422"` regardless of config
- Container auto-selects to MOV; output extension is corrected to match
- Audio auto-selects to 24-bit PCM (overridable via CLI)
- Bitrate is not displayed in status line (quality-based codec)

### HEVC Profile Selection

The encoder sets the HEVC profile based on bit_depth and chroma config:
- **8-bit 4:2:0** → Main (`HEVC_Main_AutoLevel`)
- **10-bit 4:2:0** → Main 10 (`HEVC_Main10_AutoLevel`)
- **4:2:2 (any bit depth)** → Main 4:2:2 10 (`HEVC_Main42210_AutoLevel`) — HEVC has no 8-bit-only 4:2:2 profile, so 8-bit 4:2:2 input is encoded as 10-bit 4:2:2

### Preview Modes

- `--preview` — Shows a live source preview window using `AVCaptureVideoPreviewLayer`
- `--preview-compressed` — Shows a second window with the compressed output via `VTCompressionSession` + `AVSampleBufferDisplayLayer`, useful for evaluating compression artifacts at the current bitrate/codec settings
- `-p` / `--preview-both` — Shows both source and compressed preview windows side by side

### CoreFoundation ctypes Bindings

VTSession properties use CoreFoundation types directly via ctypes (`kCFBooleanTrue`/`kCFBooleanFalse`, `CFNumberCreate`) rather than PyObjC's `Foundation.NSNumber`, because PyObjC bridges NSNumber back to Python primitives which breaks `objc.pyobjc_id()`.

### Constraints

- 10-bit requires H.265 or ProRes codec
- 4:2:2 chroma always produces 10-bit output (HEVC Main 4:2:2 10 is the only 4:2:2 profile)
- Audio capture requires microphone permission (gracefully degrades to video-only if denied)
- Pixel formats use YUV biplanar (not BGRA) for efficiency — the hardware encoder works natively in YUV

### frametimes.py

Analyzes frame timing in H.264/H.265 MP4 files using ffprobe. Reports container/stream info, per-frame duration statistics (min/avg/max/jitter), and a duration distribution histogram. Optionally uses mediainfo for frame rate mode (CFR/VFR) and min/max frame rate. Falls back from per-frame `duration_time` to PTS deltas if durations are unavailable.

### qpdump.py

Dumps per-frame QP values from H.264/H.265 files using ffmpeg's `trace_headers` bitstream filter. Parses `pic_init_qp_minus26` from PPS and `slice_qp_delta` from slice headers to compute per-slice QP. For multi-slice frames, reports average QP. Supports summary (default), `--detailed` per-frame table, and `--csv` output modes. Handles both H.264 and HEVC slice type numbering.

### aja-capture.cpp

C++ helper using AJA NTV2 SDK to capture raw video+audio from AJA devices (e.g., Io 4K, Corvid). Uses `AutoCirculate` for DMA frame transfer with a 10-frame circular buffer. Producer thread captures frames from the device; consumer thread writes them to stdout in a simple framed binary protocol. Supports HDMI and SDI inputs with auto-detection, 4K TSI routing for non-12G devices, and configurable pixel format (8-bit/10-bit YCbCr). Controlled via stdin (`"stop\n"` for graceful shutdown). Built with CMake against the NTV2 SDK.

### list_encoders.py

Uses VideoToolbox `VTCopyVideoEncoderList` via ctypes to enumerate hardware and software encoders. Displays FourCC, codec name, and encoder name for each.

### Testing

PyObjC classes can't be mocked with `mock.patch` (ObjC selectors can't be deleted). Functions like `find_device_by_selector()` and `list_devices()` accept device lists as parameters rather than calling AVFoundation directly, making them testable without mocking ObjC.

```bash
python3 -m pytest test_pjcap.py -v
```
