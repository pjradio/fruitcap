#!/usr/bin/env python3
"""fruitcap - macOS video capture to H.264 using AVFoundation hardware encoder.

Author: Phil Jensen <philj@philandamy.org>
"""

import argparse
import configparser
import ctypes
import ctypes.util
import os
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

RESOLUTION_PRESETS = {
    "4k": (3840, 2160),
    "1080p": (1920, 1080),
    "720p": (1280, 720),
}


def load_config(path="fruitcap.cfg"):
    config = configparser.ConfigParser()
    config.read(path)

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
    if codec not in ("h264", "h265"):
        print(f"Error: Unsupported codec '{codec}'. Use 'h264' or 'h265'.")
        sys.exit(1)

    bit_depth = config.getint("capture", "bit_depth", fallback=8)
    if bit_depth not in (8, 10):
        print(f"Error: Unsupported bit_depth '{bit_depth}'. Use 8 or 10.")
        sys.exit(1)
    if bit_depth == 10 and codec != "h265":
        print("Error: 10-bit capture requires h265 codec.")
        sys.exit(1)

    chroma = config.get("capture", "chroma", fallback="420").strip()
    if chroma not in ("420", "422"):
        print(f"Error: Unsupported chroma '{chroma}'. Use '420' or '422'.")
        sys.exit(1)

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
    if audio_codec not in ("aac", "alac"):
        print(f"Error: Unsupported audio codec '{audio_codec}'. Use 'aac' or 'alac'.")
        sys.exit(1)

    return {
        "width": width,
        "height": height,
        "codec": codec,
        "bit_depth": bit_depth,
        "chroma": chroma,
        "fps": fps,
        "discard_late_frames": discard_late,
        "bitrate": config.getint("capture", "bitrate", fallback=150000000),
        "output": config.get("capture", "output", fallback="capture.mp4"),
        "audio_enabled": audio_enabled,
        "audio_codec": audio_codec,
        "audio_bitrate": config.getint("audio", "bitrate", fallback=256000),
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


class Recorder:
    def __init__(self, cfg):
        self.cfg = cfg
        self.session = None
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

    def find_device(self):
        devices = AVF.AVCaptureDevice.devicesWithMediaType_(AVF.AVMediaTypeVideo)
        if not devices:
            print("Error: No video capture devices found.")
            sys.exit(1)
        device = devices[0]
        print(f"Using device: {device.localizedName()}")
        return device

    def find_audio_device(self):
        devices = AVF.AVCaptureDevice.devicesWithMediaType_(AVF.AVMediaTypeAudio)
        if not devices:
            print("Warning: No audio capture devices found. Recording without audio.")
            return None
        device = devices[0]
        print(f"Using audio device: {device.localizedName()}")
        return device

    def setup_session(self, device, audio_device=None):
        self.session = AVF.AVCaptureSession.alloc().init()
        # No session preset — allow the device to deliver its native format
        if self.session.canSetSessionPreset_(AVF.AVCaptureSessionPresetInputPriority):
            self.session.setSessionPreset_(AVF.AVCaptureSessionPresetInputPriority)
        else:
            pass  # Device doesn't support InputPriority; default preset works fine

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

        # Set up delegate
        self._delegate = SampleBufferDelegate.alloc().init()
        self._delegate.recorder = self

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

    def setup_writer(self):
        output_path = self.cfg["output"]
        if os.path.exists(output_path):
            os.remove(output_path)

        output_url = Foundation.NSURL.fileURLWithPath_(
            os.path.abspath(output_path)
        )

        self.writer, error = AVF.AVAssetWriter.alloc().initWithURL_fileType_error_(
            output_url, AVF.AVFileTypeMPEG4, None
        )
        if error:
            print(f"Error creating writer: {error}")
            sys.exit(1)

        width = self.cfg["width"]
        height = self.cfg["height"]
        bitrate = self.cfg["bitrate"]

        if self.cfg["codec"] == "h265":
            codec_type = AVF.AVVideoCodecTypeHEVC
            # Select HEVC profile based on bit depth and chroma subsampling
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

        # Explicitly tag color space to prevent VideoToolbox from guessing wrong
        # (e.g., tagging BT.709 content as BT.2020, causing luminance shifts)
        color_properties = {
            AVF.AVVideoColorPrimariesKey: AVF.AVVideoColorPrimaries_ITU_R_709_2,
            AVF.AVVideoTransferFunctionKey: AVF.AVVideoTransferFunction_ITU_R_709_2,
            AVF.AVVideoYCbCrMatrixKey: AVF.AVVideoYCbCrMatrix_ITU_R_709_2,
        }

        output_settings = {
            AVF.AVVideoCodecKey: codec_type,
            AVF.AVVideoWidthKey: width,
            AVF.AVVideoHeightKey: height,
            AVF.AVVideoColorPropertiesKey: color_properties,
            AVF.AVVideoCompressionPropertiesKey: compression_settings,
        }

        self.writer_input = AVF.AVAssetWriterInput.assetWriterInputWithMediaType_outputSettings_(
            AVF.AVMediaTypeVideo, output_settings
        )
        self.writer_input.setExpectsMediaDataInRealTime_(True)

        if self.writer.canAddInput_(self.writer_input):
            self.writer.addInput_(self.writer_input)
        else:
            print("Error: Could not add video writer input.")
            sys.exit(1)

        # Add audio writer input if audio is enabled and we have an audio source
        if self.cfg["audio_enabled"] and self._delegate.audio_output is not None:
            if self.cfg["audio_codec"] == "alac":
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

            self.audio_writer_input = AVF.AVAssetWriterInput.assetWriterInputWithMediaType_outputSettings_(
                AVF.AVMediaTypeAudio, audio_settings
            )
            self.audio_writer_input.setExpectsMediaDataInRealTime_(True)

            if self.writer.canAddInput_(self.audio_writer_input):
                self.writer.addInput_(self.audio_writer_input)
            else:
                print("Warning: Could not add audio writer input.")
                self.audio_writer_input = None

    def start(self):
        self.running = True
        self.writer.startWriting()
        self.session.startRunning()
        codec_label = "H.265/HEVC" if self.cfg["codec"] == "h265" else "H.264"
        audio_label = ""
        if self.audio_writer_input:
            audio_codec = "ALAC" if self.cfg["audio_codec"] == "alac" else "AAC"
            audio_label = (
                f", audio: {audio_codec} "
                f"{self.cfg['audio_sample_rate'] // 1000}kHz/"
                f"{self.cfg['audio_channels']}ch"
            )
        print(
            f"Recording to {self.cfg['output']} "
            f"({self.cfg['width']}x{self.cfg['height']}, "
            f"{codec_label}, {self.cfg['bit_depth']}-bit {self.cfg['chroma']}, "
            f"{self.cfg['bitrate'] / 1_000_000:.1f} Mbps"
            + (f", {self.cfg['fps']:g}fps" if self.cfg['fps'] else "")
            + f"{audio_label})"
        )
        print("Press 'q' then Enter to stop recording.")

    def stop(self):
        if not self.running:
            return
        self.running = False
        if self.compressed_preview:
            self.compressed_preview.invalidate()
        self.session.stopRunning()

        if self.writer.status() == AVF.AVAssetWriterStatusWriting:
            self.writer_input.markAsFinished()
            if self.audio_writer_input:
                self.audio_writer_input.markAsFinished()
            done = threading.Event()
            self.writer.finishWritingWithCompletionHandler_(lambda: done.set())
            done.wait(timeout=10)

        dropped_msg = f", {self.frames_dropped} dropped" if self.frames_dropped else ""
        print(
            f"\nRecording stopped. {self.frames_written} frames written"
            f"{dropped_msg} to {self.cfg['output']}"
        )

    def handle_video_sample_buffer(self, sample_buffer):
        if not self.running:
            return

        if not CoreMedia.CMSampleBufferDataIsReady(sample_buffer):
            return

        with self.lock:
            if self.writer.status() == AVF.AVAssetWriterStatusWriting:
                timestamp = CoreMedia.CMSampleBufferGetPresentationTimeStamp(
                    sample_buffer
                )
                if not self.started_writing.is_set():
                    self.writer.startSessionAtSourceTime_(timestamp)
                    self.start_time = time.monotonic()
                    self._start_timestamp = timestamp
                    self.started_writing.set()

                if self.writer_input.isReadyForMoreMediaData():
                    self.writer_input.appendSampleBuffer_(sample_buffer)
                    self.frames_written += 1
                    if self.compressed_preview:
                        self.compressed_preview.encode_frame(sample_buffer)
                    self._update_status()
                    if self.max_frames and self.frames_written >= self.max_frames:
                        self._trigger_stop()
                    elif self.max_seconds:
                        elapsed = CoreMedia.CMTimeGetSeconds(
                            CoreMedia.CMTimeSubtract(timestamp, self._start_timestamp)
                        )
                        if elapsed >= self.max_seconds:
                            self._trigger_stop()

    def handle_audio_sample_buffer(self, sample_buffer):
        if not self.running or not self.audio_writer_input:
            return

        if not CoreMedia.CMSampleBufferDataIsReady(sample_buffer):
            return

        with self.lock:
            if (
                self.writer.status() == AVF.AVAssetWriterStatusWriting
                and self.started_writing.is_set()
                and self.audio_writer_input.isReadyForMoreMediaData()
            ):
                self.audio_writer_input.appendSampleBuffer_(sample_buffer)


    def _update_status(self):
        elapsed = time.monotonic() - self.start_time
        minutes, seconds = divmod(int(elapsed), 60)
        hours, minutes = divmod(minutes, 60)

        output_path = self.cfg["output"]
        try:
            size_bytes = os.path.getsize(output_path)
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

        dropped = f"  dropped: {self.frames_dropped}" if self.frames_dropped else ""
        sys.stdout.write(
            f"\r  {hours:02d}:{minutes:02d}:{seconds:02d}  "
            f"frames: {self.frames_written}  "
            f"size: {size_str}{dropped}   "
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


def main():
    parser = argparse.ArgumentParser(description="macOS video/audio capture tool")
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
    args = parser.parse_args()

    if args.preview_both:
        args.preview = True
        args.preview_compressed = True

    cfg = load_config()
    check_camera_permission()
    if cfg["audio_enabled"]:
        if not check_microphone_permission():
            cfg["audio_enabled"] = False

    recorder = Recorder(cfg)
    device = recorder.find_device()
    audio_device = recorder.find_audio_device() if cfg["audio_enabled"] else None
    recorder.setup_session(device, audio_device)
    recorder.setup_writer()

    if args.preview_compressed:
        cp = CompressedPreview(cfg)
        if cp.setup():
            recorder.compressed_preview = cp
        else:
            print("Warning: Compressed preview unavailable, continuing without it.")

    if args.frames:
        recorder.max_frames = args.frames
    if args.time:
        recorder.max_seconds = args.time

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
        try:
            while recorder.running:
                line = input()
                if line.strip().lower() == "q":
                    break
        except (KeyboardInterrupt, EOFError):
            pass
        recorder.stop()


if __name__ == "__main__":
    main()
