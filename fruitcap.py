#!/usr/bin/env python3
"""fruitcap - macOS video capture to H.264 using AVFoundation hardware encoder."""

import configparser
import ctypes
import ctypes.util
import os
import sys
import threading

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


def load_config(path="fruitcap.cfg"):
    config = configparser.ConfigParser()
    config.read(path)
    return {
        "width": config.getint("capture", "width", fallback=1920),
        "height": config.getint("capture", "height", fallback=1080),
        "bitrate": config.getint("capture", "bitrate", fallback=5000000),
        "output": config.get("capture", "output", fallback="capture.mp4"),
    }


# PyObjC delegate class for AVCaptureVideoDataOutput
class SampleBufferDelegate(Foundation.NSObject):
    def init(self):
        self = objc.super(SampleBufferDelegate, self).init()
        if self is None:
            return None
        self.recorder = None
        return self

    def captureOutput_didOutputSampleBuffer_fromConnection_(
        self, output, sample_buffer, connection
    ):
        if self.recorder:
            self.recorder.handle_sample_buffer(sample_buffer)


class Recorder:
    def __init__(self, cfg):
        self.cfg = cfg
        self.session = None
        self.writer = None
        self.writer_input = None
        self.running = False
        self.frames_written = 0
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

    def setup_session(self, device):
        self.session = AVF.AVCaptureSession.alloc().init()
        self.session.setSessionPreset_(AVF.AVCaptureSessionPresetHigh)

        # Add device input
        dev_input, error = AVF.AVCaptureDeviceInput.deviceInputWithDevice_error_(device, None)
        if dev_input is None:
            print(f"Error: Could not create device input: {error}")
            sys.exit(1)

        if self.session.canAddInput_(dev_input):
            self.session.addInput_(dev_input)
        else:
            print("Error: Could not add device input to session.")
            sys.exit(1)

        # Add video data output
        video_output = AVF.AVCaptureVideoDataOutput.alloc().init()
        video_output.setAlwaysDiscardsLateVideoFrames_(True)

        video_settings = {
            str(Quartz.kCVPixelBufferPixelFormatTypeKey): int(
                Quartz.kCVPixelFormatType_32BGRA
            ),
        }
        video_output.setVideoSettings_(video_settings)

        # Set up delegate on a serial dispatch queue
        self._delegate = SampleBufferDelegate.alloc().init()
        self._delegate.recorder = self

        queue = dispatch_queue_create(b"fruitcap.videoQueue")
        # Wrap the raw pointer as an ObjC object for PyObjC
        queue_obj = objc.objc_object(c_void_p=queue)
        video_output.setSampleBufferDelegate_queue_(self._delegate, queue_obj)

        if self.session.canAddOutput_(video_output):
            self.session.addOutput_(video_output)
        else:
            print("Error: Could not add video output to session.")
            sys.exit(1)

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

        compression_settings = {
            AVF.AVVideoAverageBitRateKey: bitrate,
            AVF.AVVideoProfileLevelKey: AVF.AVVideoProfileLevelH264HighAutoLevel,
        }

        output_settings = {
            AVF.AVVideoCodecKey: AVF.AVVideoCodecTypeH264,
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
            print("Error: Could not add writer input.")
            sys.exit(1)

    def start(self):
        self.running = True
        self.writer.startWriting()
        self.session.startRunning()
        print(
            f"Recording to {self.cfg['output']} "
            f"({self.cfg['width']}x{self.cfg['height']}, "
            f"{self.cfg['bitrate'] / 1_000_000:.1f} Mbps)"
        )
        print("Press 'q' then Enter to stop recording.")

    def stop(self):
        self.running = False
        self.session.stopRunning()

        if self.writer.status() == AVF.AVAssetWriterStatusWriting:
            self.writer_input.markAsFinished()
            done = threading.Event()
            self.writer.finishWritingWithCompletionHandler_(lambda: done.set())
            done.wait(timeout=10)

        print(f"\nRecording stopped. {self.frames_written} frames written to {self.cfg['output']}")

    def handle_sample_buffer(self, sample_buffer):
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
                    self.started_writing.set()

                if self.writer_input.isReadyForMoreMediaData():
                    self.writer_input.appendSampleBuffer_(sample_buffer)
                    self.frames_written += 1


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

    recorder = Recorder(cfg)
    device = recorder.find_device()
    recorder.setup_session(device)
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
