fruitcap is a macOS command-line video/audio capture tool written in Python. It uses AVFoundation with Apple hardware-accelerated H.264/H.265 encoding via AVAssetWriter.

## Architecture

- `fruitcap.py` — Single-file application
- `fruitcap.cfg` — INI-style config file with `[capture]` and `[audio]` sections

### Pipeline

1. `AVCaptureSession` with `InputPriority` preset (no resolution constraints, device delivers native format)
2. `AVCaptureVideoDataOutput` delivers frames as `CMSampleBuffer` to a delegate on a serial dispatch queue
3. `AVCaptureAudioDataOutput` delivers audio on a separate serial dispatch queue
4. `AVAssetWriter` with `AVAssetWriterInput` handles H.264/H.265 hardware encoding and MP4 muxing
5. Audio encoded as AAC or ALAC via a second `AVAssetWriterInput`

### Key Classes

- `SampleBufferDelegate` — PyObjC delegate, routes video/audio sample buffers by comparing the output reference
- `Recorder` — Manages session, writer, and all capture state
- `load_config()` — Parses `fruitcap.cfg`, validates settings (codec/bit_depth/chroma combinations)

### Config Options

**[capture]**: resolution (4k/1080p/720p/WIDTHxHEIGHT), codec (h264/h265), bit_depth (8/10), chroma (420/422), bitrate, discard_late_frames, output

**[audio]**: capture (yes/no), codec (aac/alac), bitrate, sample_rate, channels

### Constraints

- 10-bit requires H.265 codec
- Audio capture requires microphone permission (gracefully degrades to video-only if denied)
- Pixel formats use YUV biplanar (not BGRA) for efficiency — the hardware encoder works natively in YUV
