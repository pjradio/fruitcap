#!/usr/bin/env python3
"""fruitcap - macOS video/audio capture using AVFoundation hardware encoder.

Author: Phil Jensen <philj@philandamy.org>
"""

import argparse
import configparser
import ctypes
import ctypes.util
import datetime
import math
import os
import select
import signal
import sys
import threading
import time

import AVFoundation as AVF
import CoreMedia
import Foundation
import Quartz
import objc


# Load libdispatch for dispatch_queue_create
_libdispatch = ctypes.cdll.LoadLibrary(ctypes.util.find_library("dispatch"))
_libdispatch.dispatch_queue_create.restype = ctypes.c_void_p
_libdispatch.dispatch_queue_create.argtypes = [ctypes.c_char_p, ctypes.c_void_p]


def dispatch_queue_create(label):
    return _libdispatch.dispatch_queue_create(label, None)


# VideoToolbox / CoreMedia ctypes bindings for compressed preview
_vt_lib = ctypes.cdll.LoadLibrary(ctypes.util.find_library("VideoToolbox"))
_cm_lib = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreMedia"))

kCMVideoCodecType_H264 = 0x61766331  # 'avc1'
kCMVideoCodecType_HEVC = 0x68766331  # 'hvc1'


class CMTimeStruct(ctypes.Structure):
    _fields_ = [
        ("value", ctypes.c_int64),
        ("timescale", ctypes.c_int32),
        ("flags", ctypes.c_uint32),
        ("epoch", ctypes.c_int64),
    ]


VTOutputCallback = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32,
    ctypes.c_uint32, ctypes.c_void_p,
)

_vt_lib.VTCompressionSessionCreate.restype = ctypes.c_int32
_vt_lib.VTCompressionSessionCreate.argtypes = [
    ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32, ctypes.c_uint32,
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    VTOutputCallback, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
]
_vt_lib.VTCompressionSessionEncodeFrame.restype = ctypes.c_int32
_vt_lib.VTCompressionSessionEncodeFrame.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, CMTimeStruct, CMTimeStruct,
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
]
_vt_lib.VTSessionSetProperty.restype = ctypes.c_int32
_vt_lib.VTSessionSetProperty.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
]
_vt_lib.VTCompressionSessionInvalidate.restype = None
_vt_lib.VTCompressionSessionInvalidate.argtypes = [ctypes.c_void_p]

_cm_lib.CMTimebaseCreateWithSourceClock.restype = ctypes.c_int32
_cm_lib.CMTimebaseCreateWithSourceClock.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
]
_cm_lib.CMTimebaseSetTime.restype = ctypes.c_int32
_cm_lib.CMTimebaseSetTime.argtypes = [ctypes.c_void_p, CMTimeStruct]
_cm_lib.CMTimebaseSetRate.restype = ctypes.c_int32
_cm_lib.CMTimebaseSetRate.argtypes = [ctypes.c_void_p, ctypes.c_double]

# Audio level metering — read raw PCM from capture buffers
_cm_lib.CMSampleBufferGetDataBuffer.restype = ctypes.c_void_p
_cm_lib.CMSampleBufferGetDataBuffer.argtypes = [ctypes.c_void_p]
_cm_lib.CMBlockBufferGetDataLength.restype = ctypes.c_size_t
_cm_lib.CMBlockBufferGetDataLength.argtypes = [ctypes.c_void_p]
_cm_lib.CMBlockBufferGetDataPointer.restype = ctypes.c_int32
_cm_lib.CMBlockBufferGetDataPointer.argtypes = [
    ctypes.c_void_p, ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t),
    ctypes.POINTER(ctypes.c_void_p),
]
_cm_lib.CMSampleBufferGetFormatDescription.restype = ctypes.c_void_p
_cm_lib.CMSampleBufferGetFormatDescription.argtypes = [ctypes.c_void_p]
_cm_lib.CMAudioFormatDescriptionGetStreamBasicDescription.restype = ctypes.c_void_p
_cm_lib.CMAudioFormatDescriptionGetStreamBasicDescription.argtypes = [ctypes.c_void_p]


class AudioStreamBasicDescription(ctypes.Structure):
    _fields_ = [
        ("mSampleRate", ctypes.c_double),
        ("mFormatID", ctypes.c_uint32),
        ("mFormatFlags", ctypes.c_uint32),
        ("mBytesPerPacket", ctypes.c_uint32),
        ("mFramesPerPacket", ctypes.c_uint32),
        ("mBytesPerFrame", ctypes.c_uint32),
        ("mChannelsPerFrame", ctypes.c_uint32),
        ("mBitsPerChannel", ctypes.c_uint32),
        ("mReserved", ctypes.c_uint32),
    ]


class AudioSamplePeakAnalyzer:
    """Extract sample peak levels from capture audio sample buffers."""

    _FLAG_IS_FLOAT = 1
    _FLAG_IS_NON_INTERLEAVED = 1 << 5

    def __init__(self):
        self.reset()

    def reset(self):
        self._format_checked = False
        self._sample_ctype = None
        self._bytes_per_sample = 0
        self._peak_divisor = 1.0
        self._channels_per_frame = 1
        self._non_interleaved = False
        self.format_error = None

    @staticmethod
    def peaks_to_dbfs(peaks):
        if peaks is None:
            return None
        return [20.0 * math.log10(max(peak, 1e-6)) for peak in peaks]

    def measure_overall_peak(self, sample_buffer):
        peaks = self.measure_channel_peaks(sample_buffer)
        if peaks is None:
            return None
        return max(peaks, default=0.0)

    def measure_channel_peaks(self, sample_buffer, channel_count_hint=None):
        buf_ptr = objc.pyobjc_id(sample_buffer)
        if not self._ensure_format(buf_ptr, channel_count_hint=channel_count_hint):
            return None

        block_buf = _cm_lib.CMSampleBufferGetDataBuffer(buf_ptr)
        if not block_buf:
            return None

        length = _cm_lib.CMBlockBufferGetDataLength(block_buf)
        if length == 0:
            return None

        data_out = ctypes.c_void_p()
        err = _cm_lib.CMBlockBufferGetDataPointer(
            block_buf, 0, None, None, ctypes.byref(data_out)
        )
        if err != 0 or not data_out.value:
            return None

        num_samples = length // self._bytes_per_sample
        if num_samples == 0:
            return None

        samples = (self._sample_ctype * num_samples).from_address(data_out.value)
        channel_count = max(1, self._channels_per_frame)
        peaks = [0.0] * channel_count

        if self._non_interleaved:
            samples_per_channel = num_samples // channel_count
            if samples_per_channel == 0:
                return None
            for channel_index in range(channel_count):
                start = channel_index * samples_per_channel
                end = start + samples_per_channel
                channel_peak = 0.0
                for sample_index in range(start, end):
                    value = abs(samples[sample_index]) / self._peak_divisor
                    if value > channel_peak:
                        channel_peak = value
                peaks[channel_index] = channel_peak
        else:
            for sample_index, sample in enumerate(samples):
                value = abs(sample) / self._peak_divisor
                channel_index = sample_index % channel_count
                if value > peaks[channel_index]:
                    peaks[channel_index] = value

        return peaks

    def _ensure_format(self, buf_ptr, channel_count_hint=None):
        if self._format_checked:
            return self._sample_ctype is not None

        self._format_checked = True
        fmt = _cm_lib.CMSampleBufferGetFormatDescription(buf_ptr)
        if fmt:
            asbd_ptr = _cm_lib.CMAudioFormatDescriptionGetStreamBasicDescription(fmt)
            if asbd_ptr:
                asbd = AudioStreamBasicDescription.from_address(asbd_ptr)
                is_float = bool(asbd.mFormatFlags & self._FLAG_IS_FLOAT)
                self._non_interleaved = bool(
                    asbd.mFormatFlags & self._FLAG_IS_NON_INTERLEAVED
                )
                self._channels_per_frame = max(
                    1,
                    int(asbd.mChannelsPerFrame or channel_count_hint or 1),
                )
                bits = asbd.mBitsPerChannel

                if is_float and bits == 32:
                    self._sample_ctype = ctypes.c_float
                    self._bytes_per_sample = 4
                    self._peak_divisor = 1.0
                elif is_float and bits == 64:
                    self._sample_ctype = ctypes.c_double
                    self._bytes_per_sample = 8
                    self._peak_divisor = 1.0
                elif not is_float and bits == 16:
                    self._sample_ctype = ctypes.c_int16
                    self._bytes_per_sample = 2
                    self._peak_divisor = 32768.0
                elif not is_float and bits == 32:
                    self._sample_ctype = ctypes.c_int32
                    self._bytes_per_sample = 4
                    self._peak_divisor = 2147483648.0
                else:
                    self.format_error = f"{'float' if is_float else 'int'} {bits}-bit"
                    return False

                return True

        self._sample_ctype = ctypes.c_float
        self._bytes_per_sample = 4
        self._peak_divisor = 1.0
        self._channels_per_frame = max(1, int(channel_count_hint or 1))
        self._non_interleaved = False
        return True


def _vt_cfstr(name):
    """Load a CFString constant from VideoToolbox."""
    return ctypes.c_void_p.in_dll(_vt_lib, name).value


# CoreFoundation boolean constants and number creation for VTSession properties
_cf_lib = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
_cf_true = ctypes.c_void_p.in_dll(_cf_lib, "kCFBooleanTrue").value
_cf_false = ctypes.c_void_p.in_dll(_cf_lib, "kCFBooleanFalse").value
_cf_lib.CFNumberCreate.restype = ctypes.c_void_p
_cf_lib.CFNumberCreate.argtypes = [ctypes.c_void_p, ctypes.c_int64, ctypes.c_void_p]
kCFNumberSInt64Type = 4


def _cf_int(value):
    """Create a CFNumber from a Python integer."""
    val = ctypes.c_int64(value)
    return _cf_lib.CFNumberCreate(None, kCFNumberSInt64Type, ctypes.byref(val))


# CoreAudio format IDs (FourCC)
kAudioFormatMPEG4AAC = 0x61616320      # 'aac '
kAudioFormatAppleLossless = 0x616C6163  # 'alac'
kAudioFormatLinearPCM = 0x6C70636D     # 'lpcm'

_quiet = False


def log(msg):
    """Print a message unless quiet mode is enabled."""
    if not _quiet:
        print(msg)


RESOLUTION_PRESETS = {
    "4k": (3840, 2160),
    "1080p": (1920, 1080),
    "720p": (1280, 720),
}

# Color space presets: (primaries, transfer function, YCbCr matrix)
# These keys are AVFoundation constant names resolved at writer setup time.
COLOR_SPACE_PRESETS = {
    "bt709": {
        "primaries": "ITU_R_709_2",
        "transfer": "ITU_R_709_2",
        "matrix": "ITU_R_709_2",
    },
    "bt2020": {
        "primaries": "ITU_R_2020",
        "transfer": "ITU_R_709_2",  # SDR in BT.2020 container
        "matrix": "ITU_R_2020",
    },
    "hlg": {
        "primaries": "ITU_R_2020",
        "transfer": "ARIB_STD_B67",  # HLG transfer function
        "matrix": "ITU_R_2020",
    },
    "pq": {
        "primaries": "ITU_R_2020",
        "transfer": "SMPTE_ST_2084_PQ",
        "matrix": "ITU_R_2020",
    },
}


def parse_bitrate(value):
    """Parse a bitrate string like '80m', '500k', or '150000000' into an integer (bps).

    Suffixes (case-insensitive):
        k = kilobits/s (×1000)
        m = megabits/s (×1_000_000)
        g = gigabits/s (×1_000_000_000)
    """
    value = str(value).strip()
    if not value:
        raise ValueError("Empty bitrate value")
    suffix = value[-1].lower()
    multipliers = {"k": 1_000, "m": 1_000_000, "g": 1_000_000_000}
    if suffix in multipliers:
        return int(float(value[:-1]) * multipliers[suffix])
    return int(value)


def load_config(path="fruitcap.cfg", overrides=None):
    """Load config from file and apply CLI overrides.

    overrides is a dict of config keys to override, e.g.:
        {"codec": "h265", "bitrate": "80m", "output": "out.mp4", "fps": "29.97"}
    """
    # Disable configparser interpolation so fruitcap's `%d` / `%t`
    # output filename tokens are read literally.
    config = configparser.ConfigParser(interpolation=None)
    config.read(path)

    # Apply CLI overrides into the config sections
    if overrides:
        capture_keys = {
            "resolution", "codec", "container", "color_space", "bit_depth", "chroma",
            "bitrate", "fps", "output", "discard_late_frames",
        }
        audio_keys = {"audio_codec", "audio_bitrate", "audio_sample_rate", "audio_channels"}
        for key, value in overrides.items():
            if value is None:
                continue
            if key in capture_keys:
                if not config.has_section("capture"):
                    config.add_section("capture")
                config.set("capture", key, str(value))
            elif key == "audio_codec":
                if not config.has_section("audio"):
                    config.add_section("audio")
                config.set("audio", "codec", str(value))
            elif key == "audio_bitrate":
                if not config.has_section("audio"):
                    config.add_section("audio")
                config.set("audio", "bitrate", str(value))
            elif key == "audio_sample_rate":
                if not config.has_section("audio"):
                    config.add_section("audio")
                config.set("audio", "sample_rate", str(value))
            elif key == "audio_channels":
                if not config.has_section("audio"):
                    config.add_section("audio")
                config.set("audio", "channels", str(value))
            elif key == "audio_enabled":
                if not config.has_section("audio"):
                    config.add_section("audio")
                config.set("audio", "capture", "yes" if value else "no")

    res = config.get("capture", "resolution", fallback="4k").strip().lower()
    if res in RESOLUTION_PRESETS:
        width, height = RESOLUTION_PRESETS[res]
    elif "x" in res:
        try:
            parts = res.split("x", 1)
            width, height = int(parts[0]), int(parts[1])
        except ValueError:
            print(f"Error: Invalid resolution '{res}'. Use a preset (4k, 1080p, 720p) or WIDTHxHEIGHT.")
            sys.exit(1)
    else:
        print(f"Error: Invalid resolution '{res}'. Use a preset (4k, 1080p, 720p) or WIDTHxHEIGHT.")
        sys.exit(1)

    codec = config.get("capture", "codec", fallback="h265").strip().lower()
    valid_codecs = ("h264", "h265", "prores", "prores_proxy", "prores_lt", "prores_hq")
    if codec not in valid_codecs:
        print(f"Error: Unsupported codec '{codec}'. Use one of: {', '.join(valid_codecs)}.")
        sys.exit(1)

    bit_depth = config.getint("capture", "bit_depth", fallback=8)
    if bit_depth not in (8, 10):
        print(f"Error: Unsupported bit_depth '{bit_depth}'. Use 8 or 10.")
        sys.exit(1)
    if bit_depth == 10 and codec not in ("h265", "prores", "prores_proxy", "prores_lt", "prores_hq"):
        print("Error: 10-bit capture requires h265 or prores codec.")
        sys.exit(1)

    chroma = config.get("capture", "chroma", fallback="420").strip()
    if chroma not in ("420", "422"):
        print(f"Error: Unsupported chroma '{chroma}'. Use '420' or '422'.")
        sys.exit(1)

    # ProRes 422 is natively 10-bit 4:2:2 — force these regardless of config
    if codec.startswith("prores"):
        bit_depth = 10
        chroma = "422"

    # Apple's H.264 hardware encoder only supports 4:2:0
    if codec == "h264" and chroma == "422":
        print("Error: H.264 only supports 4:2:0 chroma on Apple hardware. Use h265 or prores for 4:2:2.")
        sys.exit(1)

    # HEVC has no 8-bit 4:2:2 profile — force 10-bit when 4:2:2 is selected
    if codec == "h265" and chroma == "422":
        bit_depth = 10

    fps = config.get("capture", "fps", fallback="").strip()
    if fps:
        try:
            fps = float(fps)
            if fps <= 0:
                raise ValueError
        except ValueError:
            print(f"Error: Invalid fps '{config.get('capture', 'fps')}'. Use a positive number (e.g., 29.97, 30, 24).")
            sys.exit(1)
    else:
        fps = None

    discard_late = config.getboolean("capture", "discard_late_frames", fallback=False)

    # Audio config
    audio_enabled = config.getboolean("audio", "capture", fallback=True)
    audio_codec = config.get("audio", "codec", fallback="aac").strip().lower()
    if audio_codec not in ("aac", "alac", "pcm"):
        print(f"Error: Unsupported audio codec '{audio_codec}'. Use 'aac', 'alac', or 'pcm'.")
        sys.exit(1)

    # Auto-select PCM audio for ProRes unless user explicitly set audio codec
    audio_overridden = overrides and "audio_codec" in overrides
    if codec.startswith("prores") and not audio_overridden:
        audio_codec = "pcm"

    color_space = config.get("capture", "color_space", fallback="bt709").strip().lower()
    if color_space not in COLOR_SPACE_PRESETS:
        valid = ", ".join(COLOR_SPACE_PRESETS.keys())
        print(f"Error: Unsupported color_space '{color_space}'. Use one of: {valid}.")
        sys.exit(1)

    container = config.get("capture", "container", fallback="auto").strip().lower()
    if container not in ("mp4", "mov", "auto"):
        print(f"Error: Unsupported container '{container}'. Use 'mp4', 'mov', or 'auto'.")
        sys.exit(1)
    # Auto-select container: ProRes → mov, others → mp4
    if container == "auto":
        container = "mov" if codec.startswith("prores") else "mp4"

    video_bitrate_str = config.get("capture", "bitrate", fallback="150000000")
    try:
        video_bitrate = parse_bitrate(video_bitrate_str)
    except ValueError:
        print(f"Error: Invalid bitrate '{video_bitrate_str}'. Use a number or shorthand like '80m', '500k'.")
        sys.exit(1)

    audio_bitrate_str = config.get("audio", "bitrate", fallback="256000")
    try:
        audio_bitrate = parse_bitrate(audio_bitrate_str)
    except ValueError:
        print(f"Error: Invalid audio bitrate '{audio_bitrate_str}'. Use a number or shorthand like '256k'.")
        sys.exit(1)

    audio_only = overrides.get("audio_only", False) if overrides else False

    return {
        "audio_only": audio_only,
        "width": width,
        "height": height,
        "codec": codec,
        "container": container,
        "color_space": color_space,
        "bit_depth": bit_depth,
        "chroma": chroma,
        "fps": fps,
        "discard_late_frames": discard_late,
        "bitrate": video_bitrate,
        "output": config.get("capture", "output", fallback="capture-%d-%t.mp4"),
        "audio_enabled": audio_enabled,
        "audio_codec": audio_codec,
        "audio_bitrate": audio_bitrate,
        "audio_sample_rate": config.getint("audio", "sample_rate", fallback=48000),
        "audio_channels": config.getint("audio", "channels", fallback=2),
    }


# PyObjC delegate class for AVCaptureVideoDataOutput and AVCaptureAudioDataOutput
class SampleBufferDelegate(Foundation.NSObject):
    def init(self):
        self = objc.super(SampleBufferDelegate, self).init()
        if self is None:
            return None
        self.recorder = None
        self.video_output = None
        self.audio_output = None
        return self

    def captureOutput_didOutputSampleBuffer_fromConnection_(
        self, output, sample_buffer, connection
    ):
        if not self.recorder:
            return
        if output is self.video_output:
            self.recorder.handle_video_sample_buffer(sample_buffer)
        elif output is self.audio_output:
            self.recorder.handle_audio_sample_buffer(sample_buffer)

    def captureOutput_didDropSampleBuffer_fromConnection_(
        self, output, sample_buffer, connection
    ):
        if self.recorder and output is self.video_output:
            self.recorder.frames_dropped += 1


def parse_size(value):
    """Parse a size string like '500m', '2g', '100k' into bytes.

    Suffixes (case-insensitive):
        k/kb = kilobytes (×1024)
        m/mb = megabytes (×1024²)
        g/gb = gigabytes (×1024³)
    """
    value = str(value).strip().lower()
    if not value:
        raise ValueError("Empty size value")
    multipliers = {
        "k": 1024, "kb": 1024,
        "m": 1024**2, "mb": 1024**2,
        "g": 1024**3, "gb": 1024**3,
    }
    for suffix, mult in sorted(multipliers.items(), key=lambda x: -len(x[0])):
        if value.endswith(suffix):
            return int(float(value[:-len(suffix)]) * mult)
    return int(value)


def generate_segment_path(base_path, segment_num):
    """Generate a segment file path: base_001.mp4, base_002.mp4, etc."""
    root, ext = os.path.splitext(base_path)
    return f"{root}_{segment_num:03d}{ext}"


def generate_output_path(template, no_overwrite=False, split_segments=False):
    """Generate an output file path, expanding %d (date) and %t (time) tokens.

    Tokens:
        %d → YYYYMMDD
        %t → HHMMSS

    If no_overwrite is True and the target exists, appends _1, _2, etc.
    When split_segments is True, collision checks are based on the first
    segment path rather than the unsuffixed base path.
    """
    def target_exists(candidate):
        if split_segments:
            return os.path.exists(generate_segment_path(candidate, 1))
        return os.path.exists(candidate)

    now = datetime.datetime.now()
    path = template.replace("%d", now.strftime("%Y%m%d"))
    path = path.replace("%t", now.strftime("%H%M%S"))

    if not no_overwrite:
        return path

    if not target_exists(path):
        return path

    base, ext = os.path.splitext(path)
    counter = 1
    while target_exists(f"{base}_{counter}{ext}"):
        counter += 1
    return f"{base}_{counter}{ext}"


def get_output_file_type_and_extension(cfg):
    """Return the AVAssetWriter file type and default extension for cfg."""
    if cfg["audio_only"]:
        if cfg["audio_codec"] == "pcm":
            return AVF.AVFileTypeCoreAudioFormat, ".caf"
        return AVF.AVFileTypeAppleM4A, ".m4a"
    if cfg["container"] == "mov":
        return AVF.AVFileTypeQuickTimeMovie, ".mov"
    return AVF.AVFileTypeMPEG4, ".mp4"


def build_writer_metadata(file_type):
    """Return writer metadata items describing the authoring software."""
    item = AVF.AVMutableMetadataItem.alloc().init()
    item.setValue_("fruitcap.py")
    if file_type in (AVF.AVFileTypeMPEG4, AVF.AVFileTypeAppleM4A):
        item.setKeySpace_(AVF.AVMetadataKeySpaceiTunes)
        item.setKey_(AVF.AVMetadataiTunesMetadataKeyEncodingTool)
        item.setDataType_(AVF.kCMMetadataBaseDataType_UTF8)
    elif file_type == AVF.AVFileTypeQuickTimeMovie:
        item.setKeySpace_(AVF.AVMetadataKeySpaceQuickTimeMetadata)
        item.setKey_(AVF.AVMetadataQuickTimeMetadataKeySoftware)
        item.setDataType_(AVF.kCMMetadataBaseDataType_UTF8)
    else:
        item.setIdentifier_(AVF.AVMetadataCommonIdentifierSoftware)
        item.setLocale_(Foundation.NSLocale.currentLocale())
    return [item]


def get_device_formats(device):
    """Extract supported formats from a capture device.

    Returns a list of dicts with keys:
        width, height, media_subtype, min_fps, max_fps, fps_ranges
    """
    formats = []
    for fmt in device.formats():
        desc = fmt.formatDescription()
        dims = CoreMedia.CMVideoFormatDescriptionGetDimensions(desc)
        media_subtype = CoreMedia.CMFormatDescriptionGetMediaSubType(desc)
        # Convert FourCC int to string, falling back to hex for non-printable codes
        raw = media_subtype.to_bytes(4, "big")
        if all(0x20 <= b < 0x7F for b in raw):
            fourcc = raw.decode("ascii")
        else:
            fourcc = f"0x{media_subtype:08X}"

        fps_ranges = []
        for r in fmt.videoSupportedFrameRateRanges():
            fps_ranges.append({
                "min": r.minFrameRate(),
                "max": r.maxFrameRate(),
            })

        formats.append({
            "width": dims.width,
            "height": dims.height,
            "fourcc": fourcc,
            "fps_ranges": fps_ranges,
        })
    return formats


FOURCC_DESCRIPTIONS = {
    "2vuy": "8-bit 4:2:2 YUV",
    "yuvs": "8-bit 4:2:2 YUV",
    "v210": "10-bit 4:2:2 YUV",
    "r210": "10-bit 4:4:4 RGB",
    "R10k": "10-bit 4:4:4 RGB",
    "BGRA": "8-bit 4:4:4 BGRA",
    "420v": "8-bit 4:2:0 YUV (video range)",
    "420f": "8-bit 4:2:0 YUV (full range)",
    "x420": "10-bit 4:2:0 YUV",
    "x422": "10-bit 4:2:2 YUV",
    "x444": "10-bit 4:4:4 YUV",
    "p210": "10-bit 4:2:2 YUV planar",
    "p216": "16-bit 4:2:2 YUV planar",
    "p010": "10-bit 4:2:0 YUV planar",
    "p416": "16-bit 4:4:4 YUV planar",
    "L008": "8-bit luma only",
    "ARGB": "8-bit 4:4:4 ARGB",
    "0x00000020": "8-bit 4:4:4 ARGB",
}


def format_device_formats(formats):
    """Format device format info into aligned display lines."""
    entries = []
    seen = set()
    for f in formats:
        fps_strs = []
        for r in f["fps_ranges"]:
            if r["min"] == r["max"]:
                fps_strs.append(f"{r['max']:g}")
            else:
                fps_strs.append(f"{r['min']:g}-{r['max']:g}")
        key = (f["width"], f["height"], f["fourcc"], tuple(fps_strs))
        if key in seen:
            continue
        seen.add(key)
        res = f"{f['width']}x{f['height']}"
        fourcc = f["fourcc"]
        desc = FOURCC_DESCRIPTIONS.get(fourcc, "")
        fps_info = ", ".join(fps_strs) + " fps"
        entries.append((res, fourcc, desc, fps_info))

    if not entries:
        return []

    # Calculate column widths for alignment
    res_w = max(len(e[0]) for e in entries)
    fourcc_w = max(len(e[1]) for e in entries)
    desc_w = max(len(e[2]) for e in entries) if any(e[2] for e in entries) else 0

    lines = []
    for res, fourcc, desc, fps_info in entries:
        if desc_w > 0:
            lines.append(f"  {res:<{res_w}}  {fourcc:<{fourcc_w}}  {desc:<{desc_w}}  {fps_info}")
        else:
            lines.append(f"  {res:<{res_w}}  {fourcc:<{fourcc_w}}  {fps_info}")
    return lines


def get_devices(media_type):
    """Get the list of AVCaptureDevice objects for a media type."""
    return AVF.AVCaptureDevice.devicesWithMediaType_(media_type)


def list_devices(devices):
    """Return a list of (index, name, uniqueID) tuples from device objects."""
    result = []
    for i, dev in enumerate(devices):
        result.append((i, dev.localizedName(), dev.uniqueID()))
    return result


def find_device_by_selector(devices, selector=None, label="video"):
    """Find a device by index (int) or name substring (str).

    If selector is None, returns the first device.
    """
    if not devices:
        return None

    if selector is None:
        return devices[0]

    # Try as integer index
    try:
        idx = int(selector)
        if 0 <= idx < len(devices):
            return devices[idx]
        print(f"Error: {label} device index {idx} out of range (0-{len(devices)-1}).")
        sys.exit(1)
    except ValueError:
        pass

    # Try as name substring (case-insensitive)
    selector_lower = selector.lower()
    for dev in devices:
        if selector_lower in dev.localizedName().lower():
            return dev

    print(f"Error: No {label} device matching '{selector}'.")
    print(f"Available {label} devices:")
    for i, dev in enumerate(devices):
        print(f"  [{i}] {dev.localizedName()}")
    sys.exit(1)


class Recorder:
    def __init__(self, cfg):
        self.cfg = cfg
        self.session = None
        self._session_owned = True
        self.writer = None
        self.writer_input = None
        self.audio_writer_input = None
        self.running = False
        self.frames_written = 0
        self.frames_dropped = 0
        self.start_time = None
        self.lock = threading.Lock()
        self.started_writing = threading.Event()
        self.compressed_preview = None
        self.max_frames = None
        self.max_seconds = None
        self._stop_callback = None
        self._start_timestamp = None
        # Segment splitting
        self.split_seconds = None
        self.split_size_bytes = None
        self._segment_num = 1
        self._segment_start_timestamp = None
        self._segment_session_started = False
        self._segment_paths = []
        self._finalization_lock = threading.Lock()
        self._pending_finalizations = []
        self._writer_failure_lock = threading.Lock()
        self._writer_failure_reported = False
        # VU meter
        self._vu_enabled = False
        self._vu_peak = 0.0
        self._vu_clip_time = 0.0
        self._vu_last_time = 0.0
        self._vu_peak_analyzer = AudioSamplePeakAnalyzer()

    def find_device(self, selector=None):
        devices = get_devices(AVF.AVMediaTypeVideo)
        device = find_device_by_selector(devices, selector, "video")
        if not device:
            print("Error: No video capture devices found.")
            sys.exit(1)
        log(f"Using device: {device.localizedName()}")
        return device

    def find_audio_device(self, selector=None):
        devices = get_devices(AVF.AVMediaTypeAudio)
        device = find_device_by_selector(devices, selector, "audio")
        if not device:
            print("Warning: No audio capture devices found. Recording without audio.")
            return None
        log(f"Using audio device: {device.localizedName()}")
        return device

    def setup_session(self, device=None, audio_device=None):
        self.session = AVF.AVCaptureSession.alloc().init()
        self._session_owned = True

        # Set up delegate
        self._delegate = SampleBufferDelegate.alloc().init()
        self._delegate.recorder = self

        if device:
            # No session preset — allow the device to deliver its native format
            if self.session.canSetSessionPreset_(AVF.AVCaptureSessionPresetInputPriority):
                self.session.setSessionPreset_(AVF.AVCaptureSessionPresetInputPriority)

            # Add video device input
            dev_input, error = AVF.AVCaptureDeviceInput.deviceInputWithDevice_error_(device, None)
            if dev_input is None:
                print(f"Error: Could not create device input: {error}")
                sys.exit(1)

            if self.session.canAddInput_(dev_input):
                self.session.addInput_(dev_input)
            else:
                print("Error: Could not add device input to session.")
                sys.exit(1)

            # Set frame rate if configured
            if self.cfg["fps"]:
                fps = self.cfg["fps"]
                # Use integer timescale for clean framerates, high timescale for fractional
                if fps == int(fps):
                    duration = CoreMedia.CMTimeMake(1, int(fps))
                else:
                    duration = CoreMedia.CMTimeMake(1001, round(fps * 1001))
                success, error = device.lockForConfiguration_(None)
                if success:
                    device.setActiveVideoMinFrameDuration_(duration)
                    device.setActiveVideoMaxFrameDuration_(duration)
                    device.unlockForConfiguration()
                else:
                    print(f"Warning: Could not set frame rate: {error}")

            # Add video data output
            video_output = AVF.AVCaptureVideoDataOutput.alloc().init()
            video_output.setAlwaysDiscardsLateVideoFrames_(self.cfg["discard_late_frames"])

            pixel_formats = {
                ("420", 8): Quartz.kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange,
                ("420", 10): Quartz.kCVPixelFormatType_420YpCbCr10BiPlanarVideoRange,
                ("422", 8): Quartz.kCVPixelFormatType_422YpCbCr8BiPlanarVideoRange,
                ("422", 10): Quartz.kCVPixelFormatType_422YpCbCr10BiPlanarVideoRange,
            }
            pixel_format = pixel_formats[(self.cfg["chroma"], self.cfg["bit_depth"])]

            video_settings = {
                str(Quartz.kCVPixelBufferPixelFormatTypeKey): int(pixel_format),
            }
            video_output.setVideoSettings_(video_settings)

            video_queue = dispatch_queue_create(b"fruitcap.videoQueue")
            video_queue_obj = objc.objc_object(c_void_p=video_queue)
            self._delegate.video_output = video_output
            video_output.setSampleBufferDelegate_queue_(self._delegate, video_queue_obj)

            if self.session.canAddOutput_(video_output):
                self.session.addOutput_(video_output)
            else:
                print("Error: Could not add video output to session.")
                sys.exit(1)

        # Add audio input and output
        if audio_device:
            audio_input, error = AVF.AVCaptureDeviceInput.deviceInputWithDevice_error_(
                audio_device, None
            )
            if audio_input is None:
                print(f"Warning: Could not create audio input: {error}")
            elif not self.session.canAddInput_(audio_input):
                print("Warning: Could not add audio input to session.")
            else:
                self.session.addInput_(audio_input)

                audio_output = AVF.AVCaptureAudioDataOutput.alloc().init()
                audio_queue = dispatch_queue_create(b"fruitcap.audioQueue")
                audio_queue_obj = objc.objc_object(c_void_p=audio_queue)
                self._delegate.audio_output = audio_output
                audio_output.setSampleBufferDelegate_queue_(
                    self._delegate, audio_queue_obj
                )

                if self.session.canAddOutput_(audio_output):
                    self.session.addOutput_(audio_output)
                else:
                    print("Warning: Could not add audio output to session.")

    def adopt_session(self, session, delegate):
        """Adopt an externally-managed session and delegate for recording.

        The caller is responsible for session lifecycle (start/stop).
        The recorder only manages the writer and buffer handling.
        """
        self.session = session
        self._session_owned = False
        self._delegate = delegate
        self._delegate.recorder = self

    def _get_output_settings(self):
        """Build and cache video/audio output settings for writer setup."""
        if hasattr(self, "_cached_output_settings"):
            return self._cached_output_settings

        video_settings = None
        if not self.cfg["audio_only"]:
            width = self.cfg["width"]
            height = self.cfg["height"]
            bitrate = self.cfg["bitrate"]
            codec = self.cfg["codec"]

            prores_codec_map = {
                "prores_proxy": AVF.AVVideoCodecTypeAppleProRes422Proxy,
                "prores_lt": AVF.AVVideoCodecTypeAppleProRes422LT,
                "prores": AVF.AVVideoCodecTypeAppleProRes422,
                "prores_hq": AVF.AVVideoCodecTypeAppleProRes422HQ,
            }

            if codec in prores_codec_map:
                codec_type = prores_codec_map[codec]
                compression_settings = {}
            elif codec == "h265":
                codec_type = AVF.AVVideoCodecTypeHEVC
                bit_depth = self.cfg["bit_depth"]
                chroma = self.cfg["chroma"]
                if chroma == "422":
                    hevc_profile = "HEVC_Main42210_AutoLevel"
                elif bit_depth == 10:
                    hevc_profile = "HEVC_Main10_AutoLevel"
                else:
                    hevc_profile = "HEVC_Main_AutoLevel"
                compression_settings = {
                    AVF.AVVideoAverageBitRateKey: bitrate,
                    AVF.AVVideoProfileLevelKey: hevc_profile,
                }
            else:
                codec_type = AVF.AVVideoCodecTypeH264
                compression_settings = {
                    AVF.AVVideoAverageBitRateKey: bitrate,
                    AVF.AVVideoProfileLevelKey: AVF.AVVideoProfileLevelH264HighAutoLevel,
                }

            cs = COLOR_SPACE_PRESETS[self.cfg["color_space"]]
            primaries_key = f"AVVideoColorPrimaries_{cs['primaries']}"
            transfer_key = f"AVVideoTransferFunction_{cs['transfer']}"
            matrix_key = f"AVVideoYCbCrMatrix_{cs['matrix']}"
            color_properties = {
                AVF.AVVideoColorPrimariesKey: getattr(AVF, primaries_key),
                AVF.AVVideoTransferFunctionKey: getattr(AVF, transfer_key),
                AVF.AVVideoYCbCrMatrixKey: getattr(AVF, matrix_key),
            }

            video_settings = {
                AVF.AVVideoCodecKey: codec_type,
                AVF.AVVideoWidthKey: width,
                AVF.AVVideoHeightKey: height,
                AVF.AVVideoColorPropertiesKey: color_properties,
            }
            if compression_settings:
                video_settings[AVF.AVVideoCompressionPropertiesKey] = compression_settings

        audio_settings = None
        audio_active = self.cfg["audio_only"] or (
            self.cfg["audio_enabled"] and self._delegate.audio_output is not None
        )
        if audio_active:
            if self.cfg["audio_codec"] == "pcm":
                audio_settings = {
                    AVF.AVFormatIDKey: kAudioFormatLinearPCM,
                    AVF.AVSampleRateKey: self.cfg["audio_sample_rate"],
                    AVF.AVNumberOfChannelsKey: self.cfg["audio_channels"],
                    AVF.AVLinearPCMBitDepthKey: 24,
                    AVF.AVLinearPCMIsFloatKey: False,
                    AVF.AVLinearPCMIsBigEndianKey: False,
                    AVF.AVLinearPCMIsNonInterleaved: False,
                }
            elif self.cfg["audio_codec"] == "alac":
                audio_settings = {
                    AVF.AVFormatIDKey: kAudioFormatAppleLossless,
                    AVF.AVSampleRateKey: self.cfg["audio_sample_rate"],
                    AVF.AVNumberOfChannelsKey: self.cfg["audio_channels"],
                    AVF.AVEncoderBitDepthHintKey: 24,
                }
            else:
                audio_settings = {
                    AVF.AVFormatIDKey: kAudioFormatMPEG4AAC,
                    AVF.AVSampleRateKey: self.cfg["audio_sample_rate"],
                    AVF.AVNumberOfChannelsKey: self.cfg["audio_channels"],
                    AVF.AVEncoderBitRateKey: self.cfg["audio_bitrate"],
                }

        self._cached_output_settings = (video_settings, audio_settings)
        return self._cached_output_settings

    def _create_writer(self, output_path):
        """Create an AVAssetWriter for the given path with configured settings."""
        if os.path.exists(output_path):
            os.remove(output_path)

        output_url = Foundation.NSURL.fileURLWithPath_(
            os.path.abspath(output_path)
        )
        file_type, _ = get_output_file_type_and_extension(self.cfg)
        writer, error = AVF.AVAssetWriter.alloc().initWithURL_fileType_error_(
            output_url, file_type, None
        )
        if error:
            print(f"Error creating writer: {error}")
            sys.exit(1)
        writer.setMetadata_(build_writer_metadata(file_type))

        video_settings, audio_settings = self._get_output_settings()

        writer_input = None
        if video_settings:
            writer_input = AVF.AVAssetWriterInput.assetWriterInputWithMediaType_outputSettings_(
                AVF.AVMediaTypeVideo, video_settings
            )
            writer_input.setExpectsMediaDataInRealTime_(True)
            if writer.canAddInput_(writer_input):
                writer.addInput_(writer_input)
            else:
                print("Error: Could not add video writer input.")
                sys.exit(1)

        audio_writer_input = None
        if audio_settings:
            audio_writer_input = AVF.AVAssetWriterInput.assetWriterInputWithMediaType_outputSettings_(
                AVF.AVMediaTypeAudio, audio_settings
            )
            audio_writer_input.setExpectsMediaDataInRealTime_(True)
            if writer.canAddInput_(audio_writer_input):
                writer.addInput_(audio_writer_input)
            else:
                print("Warning: Could not add audio writer input.")
                audio_writer_input = None

        return writer, writer_input, audio_writer_input

    def _writer_error_text(self, writer, fallback_error=None):
        if fallback_error is not None:
            return str(fallback_error)
        if writer is None:
            return "unknown error"
        try:
            error = writer.error()
        except Exception:
            error = None
        if error is None:
            return "unknown error"
        try:
            description = error.localizedDescription()
        except Exception:
            description = None
        return str(description or error)

    def _report_writer_failure(self, operation, writer=None, output_path=None, fallback_error=None):
        with self._writer_failure_lock:
            if self._writer_failure_reported:
                return
            self._writer_failure_reported = True
        label = f" for '{output_path}'" if output_path else ""
        detail = self._writer_error_text(writer, fallback_error=fallback_error)
        print(f"\nError: AVAssetWriter failed to {operation}{label}: {detail}")

    def _start_writer(self, writer, output_path):
        try:
            started = writer.startWriting()
        except Exception as exc:
            self._report_writer_failure("start", writer, output_path=output_path, fallback_error=exc)
            return False
        if not started:
            self._report_writer_failure("start", writer, output_path=output_path)
            return False
        return True

    def _finalize_writer_state(self, writer, writer_input=None, audio_writer_input=None, output_path=None):
        """Finalize a writer/input set synchronously."""
        if not writer:
            return

        status = writer.status()
        if status == AVF.AVAssetWriterStatusFailed:
            self._report_writer_failure("finish writing", writer, output_path=output_path)
            return

        if status == AVF.AVAssetWriterStatusWriting:
            if writer_input:
                writer_input.markAsFinished()
            if audio_writer_input:
                audio_writer_input.markAsFinished()
            done = threading.Event()
            writer.finishWritingWithCompletionHandler_(lambda: done.set())
            if not done.wait(timeout=10):
                label = f" '{output_path}'" if output_path else ""
                print(f"\nWarning: Timed out finalizing output file{label}.")

    def _queue_writer_finalization(self, writer, writer_input=None, audio_writer_input=None, output_path=None):
        """Finalize a completed segment on a background thread."""
        if writer is None:
            return

        def worker():
            self._finalize_writer_state(
                writer,
                writer_input=writer_input,
                audio_writer_input=audio_writer_input,
                output_path=output_path,
            )

        thread = threading.Thread(target=worker, daemon=True)
        with self._finalization_lock:
            self._pending_finalizations = [t for t in self._pending_finalizations if t.is_alive()]
            self._pending_finalizations.append(thread)
        thread.start()

    def _wait_for_pending_finalizations(self):
        """Wait for any in-flight segment finalizers to complete."""
        with self._finalization_lock:
            pending = self._pending_finalizations
            self._pending_finalizations = []
        for thread in pending:
            thread.join()

    def _start_current_segment_session(self, timestamp):
        """Start the current writer session at the timestamp of its first sample."""
        self.writer.startSessionAtSourceTime_(timestamp)
        self._segment_start_timestamp = timestamp
        self._segment_session_started = True
        if not self.started_writing.is_set():
            self.start_time = time.monotonic()
            self._start_timestamp = timestamp
            self.started_writing.set()

    def _splitting_enabled(self):
        return self.split_seconds is not None or self.split_size_bytes is not None

    def _output_path_for_segment(self, segment_num):
        if not self._splitting_enabled():
            return self.cfg["output"]
        return generate_segment_path(self.cfg["output"], segment_num)

    def _current_output_path(self):
        return self._output_path_for_segment(self._segment_num)

    def _start_new_segment(self):
        """Swap in a new current segment and return the old writer state."""
        old_output_path = self._current_output_path()
        old_state = (
            self.writer,
            self.writer_input,
            self.audio_writer_input,
            old_output_path,
        )
        next_segment_num = self._segment_num + 1
        new_path = self._output_path_for_segment(next_segment_num)
        new_writer, new_writer_input, new_audio_writer_input = self._create_writer(new_path)
        if not self._start_writer(new_writer, new_path):
            self.writer = None
            self.writer_input = None
            self.audio_writer_input = None
            self._segment_session_started = False
            self._segment_start_timestamp = None
            return old_state, False

        self._segment_num = next_segment_num
        self._segment_paths.append(new_path)
        self.writer = new_writer
        self.writer_input = new_writer_input
        self.audio_writer_input = new_audio_writer_input
        self._segment_start_timestamp = None
        self._segment_session_started = False
        log(f"  Started segment {self._segment_num}: {new_path}")
        return old_state, True

    def setup_writer(self):
        output_path = self._output_path_for_segment(self._segment_num) if self._splitting_enabled() else self.cfg["output"]
        self._segment_paths.append(output_path)
        self.writer, self.writer_input, self.audio_writer_input = self._create_writer(output_path)
        self._segment_start_timestamp = None
        self._segment_session_started = False

    def start(self):
        output_path = self._current_output_path()
        if not self._start_writer(self.writer, output_path):
            self.running = False
            sys.exit(1)
        self.running = True
        if self._session_owned:
            self.session.startRunning()
        if self.cfg["audio_only"]:
            audio_codec_labels = {"alac": "ALAC", "pcm": "PCM", "aac": "AAC"}
            audio_codec = audio_codec_labels.get(self.cfg["audio_codec"], self.cfg["audio_codec"])
            bitrate_str = ""
            if self.cfg["audio_codec"] == "aac":
                bitrate_str = f", {self.cfg['audio_bitrate'] / 1000:.0f} kbps"
            log(
                f"Recording audio to {output_path} "
                f"({audio_codec} "
                f"{self.cfg['audio_sample_rate'] // 1000}kHz/"
                f"{self.cfg['audio_channels']}ch"
                f"{bitrate_str})"
            )
        else:
            codec_labels = {
                "h264": "H.264", "h265": "H.265/HEVC",
                "prores": "ProRes 422", "prores_proxy": "ProRes 422 Proxy",
                "prores_lt": "ProRes 422 LT", "prores_hq": "ProRes 422 HQ",
            }
            codec_label = codec_labels.get(self.cfg["codec"], self.cfg["codec"])
            audio_label = ""
            if self.audio_writer_input:
                audio_codec_labels = {"alac": "ALAC", "pcm": "PCM", "aac": "AAC"}
                audio_codec = audio_codec_labels.get(self.cfg["audio_codec"], self.cfg["audio_codec"])
                audio_label = (
                    f", audio: {audio_codec} "
                    f"{self.cfg['audio_sample_rate'] // 1000}kHz/"
                    f"{self.cfg['audio_channels']}ch"
                )
            bitrate_str = "" if self.cfg["codec"].startswith("prores") else f", {self.cfg['bitrate'] / 1_000_000:.1f} Mbps"
            log(
                f"Recording to {output_path} "
                f"({self.cfg['width']}x{self.cfg['height']}, "
                f"{codec_label}, {self.cfg['bit_depth']}-bit {self.cfg['chroma']}"
                f"{bitrate_str}"
                + (f", {self.cfg['fps']:g}fps" if self.cfg['fps'] else "")
                + f"{audio_label})"
            )
        log("Press 'q' then Enter to stop recording.")

        # Start a watchdog to detect missing input signal (video mode only)
        if not self.cfg["audio_only"]:
            self._signal_watchdog = threading.Timer(
                5.0, self._check_signal_watchdog
            )
            self._signal_watchdog.daemon = True
            self._signal_watchdog.start()

    def _check_signal_watchdog(self):
        if self.running and not self.started_writing.is_set():
            print(
                "\nError: No frames received from capture device. "
                "Check that the input source is active and sending a signal."
            )
            self._trigger_stop()

    def stop(self):
        with self.lock:
            if not self.running:
                return
            self.running = False
        if hasattr(self, "_signal_watchdog"):
            self._signal_watchdog.cancel()
        if self.compressed_preview:
            self.compressed_preview.invalidate()
        if self._session_owned:
            self.session.stopRunning()
        elif self._delegate:
            # Disconnect so buffers stop flowing to this recorder
            self._delegate.recorder = None
        with self.lock:
            writer = self.writer
            writer_input = self.writer_input
            audio_writer_input = self.audio_writer_input
            output_path = self._current_output_path()
            self.writer = None
            self.writer_input = None
            self.audio_writer_input = None
        self._finalize_writer_state(
            writer,
            writer_input=writer_input,
            audio_writer_input=audio_writer_input,
            output_path=output_path,
        )
        self._wait_for_pending_finalizations()

        if self.cfg["audio_only"]:
            elapsed = time.monotonic() - self.start_time if self.start_time else 0
            minutes, seconds = divmod(int(elapsed), 60)
            hours, minutes = divmod(minutes, 60)
            if self._splitting_enabled() and self._segment_num > 1:
                log(f"\nRecording stopped. {hours:02d}:{minutes:02d}:{seconds:02d} across {self._segment_num} segments")
            else:
                log(f"\nRecording stopped. {hours:02d}:{minutes:02d}:{seconds:02d} to {self._current_output_path()}")
        else:
            dropped_msg = f", {self.frames_dropped} dropped" if self.frames_dropped else ""
            if self._splitting_enabled() and self._segment_num > 1:
                log(
                    f"\nRecording stopped. {self.frames_written} frames written"
                    f"{dropped_msg} across {self._segment_num} segments"
                )
            else:
                log(
                    f"\nRecording stopped. {self.frames_written} frames written"
                    f"{dropped_msg} to {self._current_output_path()}"
                )

    def handle_video_sample_buffer(self, sample_buffer):
        if not self.running:
            return

        if not CoreMedia.CMSampleBufferDataIsReady(sample_buffer):
            return

        stop_requested = False
        with self.lock:
            if not self.running or not self.writer or not self.writer_input:
                return
            status = self.writer.status()
            if status == AVF.AVAssetWriterStatusFailed:
                self._report_writer_failure(
                    "write video data",
                    self.writer,
                    output_path=self._current_output_path(),
                )
                stop_requested = True
            elif status == AVF.AVAssetWriterStatusWriting:
                timestamp = CoreMedia.CMSampleBufferGetPresentationTimeStamp(
                    sample_buffer
                )
                if self.writer_input.isReadyForMoreMediaData():
                    if not self._segment_session_started:
                        self._start_current_segment_session(timestamp)
                    try:
                        appended = self.writer_input.appendSampleBuffer_(sample_buffer)
                    except Exception as exc:
                        self._report_writer_failure(
                            "write video data",
                            self.writer,
                            output_path=self._current_output_path(),
                            fallback_error=exc,
                        )
                        stop_requested = True
                        appended = False
                    if not appended:
                        if not stop_requested:
                            self._report_writer_failure(
                                "write video data",
                                self.writer,
                                output_path=self._current_output_path(),
                            )
                            stop_requested = True
                    else:
                        self.frames_written += 1
                        if self.compressed_preview:
                            self.compressed_preview.encode_frame(sample_buffer)
                        self._update_status()

                        # Check segment split conditions
                        if self._splitting_enabled():
                            need_split = False
                            if self.split_seconds:
                                seg_elapsed = CoreMedia.CMTimeGetSeconds(
                                    CoreMedia.CMTimeSubtract(timestamp, self._segment_start_timestamp)
                                )
                                if seg_elapsed >= self.split_seconds:
                                    need_split = True
                            if self.split_size_bytes:
                                try:
                                    cur_size = os.path.getsize(
                                        self._output_path_for_segment(self._segment_num)
                                    )
                                    if cur_size >= self.split_size_bytes:
                                        need_split = True
                                except OSError:
                                    pass
                            if need_split:
                                old_state, next_segment_started = self._start_new_segment()
                                if not next_segment_started:
                                    stop_requested = True
                                self._queue_writer_finalization(
                                    old_state[0],
                                    writer_input=old_state[1],
                                    audio_writer_input=old_state[2],
                                    output_path=old_state[3],
                                )

                        if self.max_frames and self.frames_written >= self.max_frames:
                            stop_requested = True
                        elif self.max_seconds:
                            elapsed = CoreMedia.CMTimeGetSeconds(
                                CoreMedia.CMTimeSubtract(timestamp, self._start_timestamp)
                            )
                            if elapsed >= self.max_seconds:
                                stop_requested = True
        if stop_requested:
            self._trigger_stop()

    def handle_audio_sample_buffer(self, sample_buffer):
        if not self.running or not self.audio_writer_input:
            return

        if not CoreMedia.CMSampleBufferDataIsReady(sample_buffer):
            return

        if self._vu_enabled:
            self._measure_audio_peak(sample_buffer)

        stop_requested = False
        with self.lock:
            if not self.running or not self.writer or not self.audio_writer_input:
                return
            status = self.writer.status()
            if status == AVF.AVAssetWriterStatusFailed:
                self._report_writer_failure(
                    "write audio data",
                    self.writer,
                    output_path=self._current_output_path(),
                )
                stop_requested = True
            elif status == AVF.AVAssetWriterStatusWriting:
                timestamp = None
                if self.audio_writer_input.isReadyForMoreMediaData():
                    if self.cfg["audio_only"]:
                        if not self._segment_session_started:
                            timestamp = CoreMedia.CMSampleBufferGetPresentationTimeStamp(
                                sample_buffer
                            )
                            self._start_current_segment_session(timestamp)
                    elif not self._segment_session_started:
                        return
                    try:
                        appended = self.audio_writer_input.appendSampleBuffer_(sample_buffer)
                    except Exception as exc:
                        self._report_writer_failure(
                            "write audio data",
                            self.writer,
                            output_path=self._current_output_path(),
                            fallback_error=exc,
                        )
                        stop_requested = True
                        appended = False
                    if not appended:
                        if not stop_requested:
                            self._report_writer_failure(
                                "write audio data",
                                self.writer,
                                output_path=self._current_output_path(),
                            )
                            stop_requested = True
                    elif self.cfg["audio_only"]:

                        self._update_status()

                        # Check time limit
                        if self.max_seconds:
                            if timestamp is None:
                                timestamp = CoreMedia.CMSampleBufferGetPresentationTimeStamp(
                                    sample_buffer
                                )
                            elapsed = CoreMedia.CMTimeGetSeconds(
                                CoreMedia.CMTimeSubtract(timestamp, self._start_timestamp)
                            )
                            if elapsed >= self.max_seconds:
                                stop_requested = True

                        # Check segment split conditions
                        if self._splitting_enabled():
                            need_split = False
                            if self.split_seconds:
                                if timestamp is None:
                                    timestamp = CoreMedia.CMSampleBufferGetPresentationTimeStamp(
                                        sample_buffer
                                    )
                                seg_elapsed = CoreMedia.CMTimeGetSeconds(
                                    CoreMedia.CMTimeSubtract(timestamp, self._segment_start_timestamp)
                                )
                                if seg_elapsed >= self.split_seconds:
                                    need_split = True
                            if self.split_size_bytes:
                                try:
                                    cur_size = os.path.getsize(
                                        self._output_path_for_segment(self._segment_num)
                                    )
                                    if cur_size >= self.split_size_bytes:
                                        need_split = True
                                except OSError:
                                    pass
                            if need_split:
                                if timestamp is None:
                                    timestamp = CoreMedia.CMSampleBufferGetPresentationTimeStamp(
                                        sample_buffer
                                    )
                                old_state, next_segment_started = self._start_new_segment()
                                if not next_segment_started:
                                    stop_requested = True
                                self._queue_writer_finalization(
                                    old_state[0],
                                    writer_input=old_state[1],
                                    audio_writer_input=old_state[2],
                                    output_path=old_state[3],
                                )
        if stop_requested:
            self._trigger_stop()


    def _measure_audio_peak(self, sample_buffer):
        """Extract peak audio level from a sample buffer and update VU state."""
        buf_peak = self._vu_peak_analyzer.measure_overall_peak(sample_buffer)
        if buf_peak is None:
            if self._vu_peak_analyzer.format_error:
                log(
                    "Warning: VU meter unsupported audio format "
                    f"({self._vu_peak_analyzer.format_error}); disabling."
                )
                self._vu_enabled = False
            return

        if buf_peak >= 1.0:
            self._vu_clip_time = time.monotonic()

        # Instant attack, exponential decay (~200ms time constant)
        now = time.monotonic()
        if buf_peak >= self._vu_peak:
            self._vu_peak = buf_peak
        elif self._vu_last_time > 0:
            dt = now - self._vu_last_time
            decay = math.exp(-dt / 0.2)
            self._vu_peak = self._vu_peak * decay + buf_peak * (1.0 - decay)
        self._vu_last_time = now

    def _update_status(self):
        if _quiet:
            return
        elapsed = time.monotonic() - self.start_time
        minutes, seconds = divmod(int(elapsed), 60)
        hours, minutes = divmod(minutes, 60)

        try:
            size_bytes = os.path.getsize(self._current_output_path())
        except OSError:
            size_bytes = 0

        if size_bytes >= 1_073_741_824:
            size_str = f"{size_bytes / 1_073_741_824:.2f} GB"
        elif size_bytes >= 1_048_576:
            size_str = f"{size_bytes / 1_048_576:.1f} MB"
        elif size_bytes >= 1024:
            size_str = f"{size_bytes / 1024:.1f} KB"
        else:
            size_str = f"{size_bytes} B"

        if self.cfg["audio_only"]:
            detail = ""
        else:
            dropped = f"  dropped: {self.frames_dropped}" if self.frames_dropped else ""
            detail = f"frames: {self.frames_written}  {dropped}"

        vu_str = ""
        if self._vu_enabled:
            peak = self._vu_peak
            db = 20.0 * math.log10(max(peak, 1e-6))
            db = max(db, -48.0)
            filled = round((db + 48.0) / 48.0 * 20)
            filled = max(0, min(20, filled))
            bar = "\u2588" * filled + "\u2591" * (20 - filled)
            clip = " CLIP" if time.monotonic() - self._vu_clip_time < 2.0 else ""
            vu_str = f"  \u2595{bar}\u258f{db:4.0f}dB{clip}"

        sys.stdout.write(
            f"\r  {hours:02d}:{minutes:02d}:{seconds:02d}  "
            f"{detail}size: {size_str}{vu_str}   "
        )
        sys.stdout.flush()

    def _trigger_stop(self):
        if not self.running:
            return
        if self._stop_callback:
            self._stop_callback()
        else:
            self.stop()



class CompressedPreview:
    """Encode frames via VTCompressionSession and display the decoded result
    through AVSampleBufferDisplayLayer, revealing compression artifacts."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.display_layer = None
        self.session = None
        self._callback_ref = None
        self._timebase_ptr = None
        self._timebase_started = False

    def setup(self):
        # Compressed preview not supported for ProRes (visually lossless)
        if self.cfg["codec"].startswith("prores"):
            print("Warning: Compressed preview not available for ProRes codec.")
            return False

        self.display_layer = AVF.AVSampleBufferDisplayLayer.alloc().init()
        self.display_layer.setVideoGravity_(AVF.AVLayerVideoGravityResizeAspect)

        # Control timebase so the layer presents frames in real time
        host_clock = CoreMedia.CMClockGetHostTimeClock()
        timebase_out = ctypes.c_void_p()
        err = _cm_lib.CMTimebaseCreateWithSourceClock(
            None, objc.pyobjc_id(host_clock), ctypes.byref(timebase_out)
        )
        if err != 0:
            print(f"Warning: Could not create timebase for compressed preview (error {err})")
            return False
        self._timebase_ptr = timebase_out.value
        timebase_obj = objc.objc_object(c_void_p=self._timebase_ptr)
        self.display_layer.setControlTimebase_(timebase_obj)

        # Create VTCompressionSession
        codec = kCMVideoCodecType_HEVC if self.cfg["codec"] == "h265" else kCMVideoCodecType_H264
        session_out = ctypes.c_void_p()
        display_layer = self.display_layer

        @VTOutputCallback
        def output_callback(ref, source_ref, status, flags, sample_buffer):
            if status != 0 or not sample_buffer:
                return
            sb_obj = objc.objc_object(c_void_p=sample_buffer)
            if not self._timebase_started:
                ts = CoreMedia.CMSampleBufferGetPresentationTimeStamp(sb_obj)
                ts_struct = CMTimeStruct(ts.value, ts.timescale, ts.flags, ts.epoch)
                _cm_lib.CMTimebaseSetTime(self._timebase_ptr, ts_struct)
                _cm_lib.CMTimebaseSetRate(self._timebase_ptr, ctypes.c_double(1.0))
                self._timebase_started = True
            display_layer.enqueueSampleBuffer_(sb_obj)

        self._callback_ref = output_callback

        err = _vt_lib.VTCompressionSessionCreate(
            None, self.cfg["width"], self.cfg["height"], codec,
            None, None, None, output_callback, None, ctypes.byref(session_out),
        )
        if err != 0:
            print(f"Warning: Could not create compression preview session (error {err})")
            return False

        self.session = session_out.value

        # Match the writer's encoding settings
        rt_key = _vt_cfstr("kVTCompressionPropertyKey_RealTime")
        reorder_key = _vt_cfstr("kVTCompressionPropertyKey_AllowFrameReordering")
        bitrate_key = _vt_cfstr("kVTCompressionPropertyKey_AverageBitRate")
        profile_key = _vt_cfstr("kVTCompressionPropertyKey_ProfileLevel")

        _vt_lib.VTSessionSetProperty(self.session, rt_key, _cf_true)
        _vt_lib.VTSessionSetProperty(self.session, reorder_key, _cf_false)
        _vt_lib.VTSessionSetProperty(self.session, bitrate_key, _cf_int(self.cfg["bitrate"]))

        if self.cfg["codec"] == "h265":
            chroma = self.cfg["chroma"]
            bit_depth = self.cfg["bit_depth"]
            if chroma == "422":
                profile_val = _vt_cfstr("kVTProfileLevel_HEVC_Main42210_AutoLevel")
            elif bit_depth == 10:
                profile_val = _vt_cfstr("kVTProfileLevel_HEVC_Main10_AutoLevel")
            else:
                profile_val = _vt_cfstr("kVTProfileLevel_HEVC_Main_AutoLevel")
        else:
            profile_val = _vt_cfstr("kVTProfileLevel_H264_High_AutoLevel")
        _vt_lib.VTSessionSetProperty(self.session, profile_key, profile_val)

        return True

    def encode_frame(self, sample_buffer):
        if not self.session:
            return
        pixel_buffer = CoreMedia.CMSampleBufferGetImageBuffer(sample_buffer)
        if pixel_buffer is None:
            return
        ts = CoreMedia.CMSampleBufferGetPresentationTimeStamp(sample_buffer)
        ts_struct = CMTimeStruct(ts.value, ts.timescale, ts.flags, ts.epoch)
        invalid_time = CMTimeStruct(0, 0, 0, 0)
        _vt_lib.VTCompressionSessionEncodeFrame(
            self.session, objc.pyobjc_id(pixel_buffer),
            ts_struct, invalid_time, None, None, None,
        )

    def invalidate(self):
        if self.session:
            _vt_lib.VTCompressionSessionInvalidate(self.session)
            self.session = None


def check_microphone_permission():
    status = AVF.AVCaptureDevice.authorizationStatusForMediaType_(AVF.AVMediaTypeAudio)
    if status == AVF.AVAuthorizationStatusAuthorized:
        return True
    if status == AVF.AVAuthorizationStatusNotDetermined:
        granted = threading.Event()
        result = [False]
        def handler(ok):
            result[0] = ok
            granted.set()
        AVF.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            AVF.AVMediaTypeAudio, handler
        )
        granted.wait(timeout=30)
        if not result[0]:
            print("Warning: Microphone access was denied. Recording without audio.")
            return False
        return True
    if status in (AVF.AVAuthorizationStatusDenied, AVF.AVAuthorizationStatusRestricted):
        print("Warning: Microphone access is denied. Recording without audio.")
        print("Grant access in System Settings > Privacy & Security > Microphone.")
        return False
    return False


def check_camera_permission():
    status = AVF.AVCaptureDevice.authorizationStatusForMediaType_(AVF.AVMediaTypeVideo)
    if status == AVF.AVAuthorizationStatusAuthorized:
        return True
    if status == AVF.AVAuthorizationStatusNotDetermined:
        granted = threading.Event()
        result = [False]
        def handler(ok):
            result[0] = ok
            granted.set()
        AVF.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            AVF.AVMediaTypeVideo, handler
        )
        granted.wait(timeout=30)
        if not result[0]:
            print("Error: Camera access was denied.")
            print("Grant access in System Settings > Privacy & Security > Camera.")
            sys.exit(1)
        return True
    if status == AVF.AVAuthorizationStatusDenied:
        print("Error: Camera access is denied.")
        print("Grant access in System Settings > Privacy & Security > Camera.")
        sys.exit(1)
    if status == AVF.AVAuthorizationStatusRestricted:
        print("Error: Camera access is restricted on this system.")
        sys.exit(1)


class PreviewAppDelegate(Foundation.NSObject):
    def init(self):
        self = objc.super(PreviewAppDelegate, self).init()
        if self is None:
            return None
        self.recorder = None
        return self

    def applicationShouldTerminateAfterLastWindowClosed_(self, app):
        return True

    def applicationShouldTerminate_(self, sender):
        if self.recorder:
            self.recorder.stop()
        return 1  # NSTerminateNow


def run_with_preview(recorder, show_source=True, show_compressed=False):
    import AppKit

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)

    delegate = PreviewAppDelegate.alloc().init()
    delegate.recorder = recorder
    app.setDelegate_(delegate)

    preview_width = 480
    preview_height = 270
    style = (
        AppKit.NSWindowStyleMaskTitled
        | AppKit.NSWindowStyleMaskClosable
        | AppKit.NSWindowStyleMaskMiniaturizable
        | AppKit.NSWindowStyleMaskResizable
    )

    windows = []
    x_offset = 100

    if show_source:
        rect = Foundation.NSMakeRect(x_offset, 100, preview_width, preview_height)
        window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, AppKit.NSBackingStoreBuffered, False
        )
        window.setTitle_("fruitcap preview")
        window.setAspectRatio_(Foundation.NSMakeSize(16, 9))

        content_view = window.contentView()
        content_view.setWantsLayer_(True)

        preview_layer = AVF.AVCaptureVideoPreviewLayer.layerWithSession_(recorder.session)
        preview_layer.setVideoGravity_(AVF.AVLayerVideoGravityResizeAspect)
        preview_layer.setFrame_(content_view.bounds())
        # kCALayerWidthSizable | kCALayerHeightSizable
        preview_layer.setAutoresizingMask_(2 | 16)
        content_view.layer().addSublayer_(preview_layer)

        window.makeKeyAndOrderFront_(None)
        windows.append(window)
        x_offset += preview_width + 20

    if show_compressed and recorder.compressed_preview:
        rect = Foundation.NSMakeRect(x_offset, 100, preview_width, preview_height)
        window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, AppKit.NSBackingStoreBuffered, False
        )
        window.setTitle_("fruitcap compressed")
        window.setAspectRatio_(Foundation.NSMakeSize(16, 9))

        content_view = window.contentView()
        content_view.setWantsLayer_(True)

        layer = recorder.compressed_preview.display_layer
        layer.setFrame_(content_view.bounds())
        layer.setAutoresizingMask_(2 | 16)  # kCALayerWidthSizable | kCALayerHeightSizable
        content_view.layer().addSublayer_(layer)

        window.makeKeyAndOrderFront_(None)
        windows.append(window)

    app.activateIgnoringOtherApps_(True)

    # Press 'q' in the preview window to stop recording
    def key_handler(event):
        if event.characters() == "q":
            app.terminate_(None)
            return None
        return event

    AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
        AppKit.NSEventMaskKeyDown, key_handler
    )

    # Allow Ctrl+C from the terminal to stop cleanly
    signal.signal(signal.SIGINT, lambda *_: app.terminate_(None))

    # Keep references so windows aren't garbage collected
    delegate.windows = windows

    app.run()


def run_headless(recorder):
    """Run capture in headless (no preview) mode with signal handling."""
    signal.signal(signal.SIGTERM, lambda *_: recorder.stop())
    signal.signal(signal.SIGINT, lambda *_: recorder.stop())
    if sys.stdin.isatty():
        try:
            while recorder.running:
                ready, _, _ = select.select([sys.stdin], [], [], 0.25)
                if not ready:
                    continue
                line = sys.stdin.readline()
                if line == "" or line.strip().lower() == "q":
                    break
        except (KeyboardInterrupt, EOFError):
            pass
    else:
        try:
            while recorder.running:
                time.sleep(0.25)
        except KeyboardInterrupt:
            pass
    recorder.stop()


def build_parser():
    parser = argparse.ArgumentParser(
        description="macOS video/audio capture tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--preview", action="store_true", help="Show a live preview window"
    )
    parser.add_argument(
        "--preview-compressed", action="store_true",
        help="Show a live preview of the compressor output",
    )
    parser.add_argument(
        "-p", "--preview-both", action="store_true",
        help="Show both source and compressed preview windows",
    )
    parser.add_argument(
        "--time", type=float, metavar="SECONDS",
        help="Stop recording after the specified number of seconds",
    )
    parser.add_argument(
        "--frames", type=int, metavar="N",
        help="Stop recording after capturing N frames",
    )
    # Config file and overrides
    parser.add_argument(
        "--config", metavar="PATH", default="fruitcap.cfg",
        help="Path to config file (default: fruitcap.cfg)",
    )
    parser.add_argument(
        "--codec",
        choices=["h264", "h265", "prores", "prores_proxy", "prores_lt", "prores_hq"],
        help="Video codec",
    )
    parser.add_argument("--container", choices=["mp4", "mov"], help="Container format")
    parser.add_argument("--bitrate", help="Video bitrate (e.g., 80m, 500k, 150000000)")
    parser.add_argument("--resolution", help="Resolution (4k, 1080p, 720p, WIDTHxHEIGHT)")
    parser.add_argument("--fps", help="Frame rate (e.g., 29.97, 30, 24)")
    parser.add_argument("-o", "--output", help="Output file path")
    parser.add_argument("--chroma", choices=["420", "422"], help="Chroma subsampling")
    parser.add_argument("--bit-depth", dest="bit_depth", choices=["8", "10"], help="Bit depth")
    parser.add_argument(
        "--color-space", dest="color_space",
        choices=["bt709", "bt2020", "hlg", "pq"],
        help="Color space tagging (default: bt709)",
    )
    parser.add_argument(
        "--discard-late-frames",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Drop late video frames if capture falls behind",
    )
    parser.add_argument(
        "--list-devices", action="store_true",
        help="List available video and audio devices and exit",
    )
    parser.add_argument("--device", metavar="NAME_OR_INDEX", help="Video device name or index")
    parser.add_argument("--audio-device", metavar="NAME_OR_INDEX", help="Audio device name or index")
    parser.add_argument(
        "--audio", action=argparse.BooleanOptionalAction, default=None,
        help="Enable or disable audio capture (overrides config)",
    )
    parser.add_argument(
        "--audio-codec",
        dest="audio_codec",
        choices=["aac", "alac", "pcm"],
        help="Audio codec",
    )
    parser.add_argument(
        "--audio-bitrate",
        dest="audio_bitrate",
        help="Audio bitrate for AAC (e.g., 256k, 320000)",
    )
    parser.add_argument(
        "--audio-sample-rate",
        dest="audio_sample_rate",
        type=int,
        metavar="HZ",
        help="Audio sample rate in Hz",
    )
    parser.add_argument(
        "--audio-channels",
        dest="audio_channels",
        type=int,
        metavar="N",
        help="Audio channel count",
    )
    parser.add_argument(
        "--no-overwrite", action="store_true",
        help="Don't overwrite existing files; append _1, _2, etc.",
    )
    parser.add_argument(
        "--list-formats", action="store_true",
        help="List supported formats for the selected video device and exit",
    )
    parser.add_argument(
        "--audio-only", action="store_true",
        help="Record audio only (no video capture)",
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true",
        help="Suppress all output except errors and warnings",
    )
    parser.add_argument(
        "--vu", action="store_true",
        help="Show a VU meter on the status line to monitor audio levels",
    )
    parser.add_argument(
        "--split-every", type=float, metavar="SECONDS",
        help="Split recording into segments of this duration",
    )
    parser.add_argument(
        "--split-size", metavar="SIZE",
        help="Split recording when file reaches this size (e.g., 500m, 2g)",
    )
    return parser


def build_overrides_from_args(args):
    """Build config overrides from parsed CLI args."""
    overrides = {}
    option_names = (
        "codec", "container", "bitrate", "resolution", "fps", "output",
        "chroma", "bit_depth", "color_space", "audio_codec", "audio_bitrate",
        "audio_sample_rate", "audio_channels",
    )
    for name in option_names:
        value = getattr(args, name)
        if value is not None:
            overrides[name] = value
    if args.audio_only:
        overrides["audio_only"] = True
    if args.audio is not None:
        overrides["audio_enabled"] = args.audio
    if args.discard_late_frames is not None:
        overrides["discard_late_frames"] = args.discard_late_frames
    return overrides


def apply_runtime_options(recorder, args, audio_only=False):
    """Apply runtime stop/split options to a recorder before setup_writer()."""
    if args.frames is not None:
        if args.frames <= 0:
            print("Error: --frames must be a positive integer.")
            sys.exit(1)
        if not audio_only:
            recorder.max_frames = args.frames
    if args.time is not None:
        if args.time <= 0:
            print("Error: --time must be greater than 0.")
            sys.exit(1)
        recorder.max_seconds = args.time
    if args.split_every is not None:
        if args.split_every <= 0:
            print("Error: --split-every must be greater than 0.")
            sys.exit(1)
        recorder.split_seconds = args.split_every
    if args.split_size is not None:
        try:
            split_size_bytes = parse_size(args.split_size)
        except ValueError:
            print(f"Error: Invalid split size '{args.split_size}'. Use a number or shorthand like '500m', '2g'.")
            sys.exit(1)
        if split_size_bytes <= 0:
            print("Error: --split-size must be greater than 0.")
            sys.exit(1)
        recorder.split_size_bytes = split_size_bytes


def main():
    banner = (
        "fruitcap.py\n"
        "Copyright (c) 2026 Phil Jensen <philj@philandamy.org>\n"
        "All rights reserved.\n"
    )
    print(banner)
    parser = build_parser()
    args = parser.parse_args()

    if args.preview_both:
        args.preview = True
        args.preview_compressed = True

    global _quiet
    _quiet = args.quiet

    overrides = build_overrides_from_args(args)
    cfg = load_config(args.config, overrides=overrides or None)

    # Match output extension to the actual container/mode
    base, ext = os.path.splitext(cfg["output"])
    _, expected_ext = get_output_file_type_and_extension(cfg)
    if ext.lower() != expected_ext:
        old_output = cfg["output"]
        cfg["output"] = base + expected_ext
        if args.output:
            log(f"Note: Output extension changed from '{ext}' to '{expected_ext}' to match {cfg['container'].upper()} container.")

    # Expand output path tokens and handle --no-overwrite
    cfg["output"] = generate_output_path(
        cfg["output"],
        no_overwrite=args.no_overwrite,
        split_segments=(args.split_every is not None or args.split_size is not None),
    )

    if args.list_devices:
        check_camera_permission()
        print("Video devices:")
        for idx, name, uid in list_devices(get_devices(AVF.AVMediaTypeVideo)):
            print(f"  [{idx}] {name}")
        if check_microphone_permission():
            print("Audio devices:")
            for idx, name, uid in list_devices(get_devices(AVF.AVMediaTypeAudio)):
                print(f"  [{idx}] {name}")
        sys.exit(0)

    if args.list_formats:
        check_camera_permission()
        video_devices = get_devices(AVF.AVMediaTypeVideo)
        device = find_device_by_selector(video_devices, args.device, "video")
        if not device:
            print("Error: No video capture devices found.")
            sys.exit(1)
        print(f"Formats for {device.localizedName()}:")
        for line in format_device_formats(get_device_formats(device)):
            print(line)
        sys.exit(0)

    if cfg["audio_only"]:
        # Audio-only mode: skip camera, require microphone
        if not check_microphone_permission():
            print("Error: Microphone access is required for audio-only recording.")
            sys.exit(1)
        cfg["audio_enabled"] = True
        recorder = Recorder(cfg)
        apply_runtime_options(recorder, args, audio_only=True)
        audio_device = recorder.find_audio_device(args.audio_device)
        if not audio_device:
            print("Error: No audio capture devices found.")
            sys.exit(1)
        recorder.setup_session(audio_device=audio_device)
        recorder.setup_writer()
        if args.frames:
            print("Warning: --frames is ignored in audio-only mode.")
        if args.preview or args.preview_compressed:
            print("Warning: Preview modes are not available in audio-only mode.")
            args.preview = False
            args.preview_compressed = False
    else:
        check_camera_permission()
        if cfg["audio_enabled"]:
            if not check_microphone_permission():
                cfg["audio_enabled"] = False

        recorder = Recorder(cfg)
        apply_runtime_options(recorder, args)
        device = recorder.find_device(args.device)
        audio_device = recorder.find_audio_device(args.audio_device) if cfg["audio_enabled"] else None
        recorder.setup_session(device, audio_device)
        recorder.setup_writer()

        if args.preview_compressed:
            cp = CompressedPreview(cfg)
            if cp.setup():
                recorder.compressed_preview = cp
            else:
                print("Warning: Compressed preview unavailable, continuing without it.")

    if args.vu:
        if cfg["audio_enabled"] or cfg["audio_only"]:
            recorder._vu_enabled = True
        else:
            print("Warning: --vu requires audio capture; ignoring.")

    recorder.start()

    if args.preview or args.preview_compressed:
        import AppKit
        # For preview mode, stop callback terminates the NSApp run loop on the main thread
        def _stop_app():
            app = AppKit.NSApplication.sharedApplication()
            app.performSelectorOnMainThread_withObject_waitUntilDone_(
                objc.selector(app.terminate_, signature=b"v@:@"), None, False
            )
        recorder._stop_callback = _stop_app
        run_with_preview(recorder, show_source=args.preview,
                         show_compressed=args.preview_compressed)
    else:
        run_headless(recorder)


if __name__ == "__main__":
    main()
