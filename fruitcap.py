#!/usr/bin/env python3
"""fruitcap - macOS video capture to H.264 using AVFoundation hardware encoder."""

import configparser
import ctypes
import ctypes.util
import os
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
            compression_settings = {
                AVF.AVVideoAverageBitRateKey: bitrate,
            }
        else:
            codec_type = AVF.AVVideoCodecTypeH264
            compression_settings = {
                AVF.AVVideoAverageBitRateKey: bitrate,
                AVF.AVVideoProfileLevelKey: AVF.AVVideoProfileLevelH264HighAutoLevel,
            }

        output_settings = {
            AVF.AVVideoCodecKey: codec_type,
            AVF.AVVideoWidthKey: width,
            AVF.AVVideoHeightKey: height,
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
            f"{self.cfg['bitrate'] / 1_000_000:.1f} Mbps{audio_label})"
        )
        print("Press 'q' then Enter to stop recording.")

    def stop(self):
        self.running = False
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
                    self.started_writing.set()

                if self.writer_input.isReadyForMoreMediaData():
                    self.writer_input.appendSampleBuffer_(sample_buffer)
                    self.frames_written += 1
                    self._update_status()

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


def main():
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
    recorder.start()

    try:
        while True:
            line = input()
            if line.strip().lower() == "q":
                break
    except (KeyboardInterrupt, EOFError):
        pass

    recorder.stop()


if __name__ == "__main__":
    main()
