#!/usr/bin/env python3
"""fruitcap-gui - macOS video/audio capture GUI using AVFoundation + PyQt5.

Author: Phil Jensen <philj@philandamy.org>
"""

import math
import os
import sys
import time
import threading

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QFormLayout, QComboBox, QPushButton, QLabel, QLineEdit,
    QGroupBox, QSplitter, QCheckBox, QStatusBar, QSizePolicy,
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt5.QtGui import QPainter, QColor

import AVFoundation as AVF
import CoreMedia
import Foundation
import Quartz
import objc

# Import shared infrastructure from fruitcap
from fruitcap import (
    Recorder, SampleBufferDelegate, CompressedPreview,
    load_config, parse_bitrate, generate_output_path,
    get_output_file_type_and_extension,
    get_devices, list_devices, find_device_by_selector,
    check_camera_permission, check_microphone_permission,
    dispatch_queue_create,
    RESOLUTION_PRESETS, COLOR_SPACE_PRESETS,
)


class PreviewWidget(QWidget):
    """Widget that hosts an AVCaptureVideoPreviewLayer via its native NSView."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._preview_layer = None
        self.setAttribute(Qt.WA_NativeWindow, True)
        self.setFocusPolicy(Qt.ClickFocus)
        self.setMinimumSize(320, 180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def attach_session(self, session):
        """Attach an AVCaptureSession to display its preview."""
        if self._preview_layer:
            self._preview_layer.removeFromSuperlayer()

        ns_view = objc.objc_object(c_void_p=int(self.winId()))
        ns_view.setWantsLayer_(True)

        self._preview_layer = AVF.AVCaptureVideoPreviewLayer.layerWithSession_(session)
        self._preview_layer.setVideoGravity_(AVF.AVLayerVideoGravityResizeAspect)
        self._preview_layer.setFrame_(ns_view.bounds())
        self._preview_layer.setAutoresizingMask_(2 | 16)  # width + height sizable
        ns_view.layer().addSublayer_(self._preview_layer)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._preview_layer:
            ns_view = objc.objc_object(c_void_p=int(self.winId()))
            self._preview_layer.setFrame_(ns_view.bounds())


class VUMeterWidget(QWidget):
    """Two-channel horizontal VU meter with green/yellow/red bars and peak hold."""

    # dB tick marks to draw on the scale
    _TICK_DB = [-48, -36, -24, -18, -12, -6, -3, 0]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._levels = [0.0, 0.0]      # 0.0–1.0 for L/R
        self._peak_levels = [0.0, 0.0]
        self._peak_decay = 0.95
        self.setMinimumHeight(40)
        self.setMaximumHeight(48)

    def set_levels_db(self, levels_db):
        """Update from dB values (typically -60 to 0)."""
        for i in range(min(2, len(levels_db))):
            db = max(-60.0, min(0.0, levels_db[i]))
            linear = (db + 60.0) / 60.0
            self._levels[i] = linear
            if linear > self._peak_levels[i]:
                self._peak_levels[i] = linear
            else:
                self._peak_levels[i] *= self._peak_decay
        self.update()

    def _db_to_x(self, db, bar_x, bar_w):
        """Convert a dB value (-60..0) to an x pixel position."""
        linear = (max(-60.0, min(0.0, db)) + 60.0) / 60.0
        return bar_x + int(bar_w * linear)

    def paintEvent(self, event):
        painter = QPainter(self)
        w = self.width()
        h = self.height()
        scale_height = 12
        bar_height = max(1, (h - scale_height - 4) // 2)
        label_width = 14
        bar_x = label_width + 2
        bar_w = w - bar_x - 2

        for i, label in enumerate(("L", "R")):
            y = i * (bar_height + 2) + 1
            level = self._levels[i]
            peak = self._peak_levels[i]

            # Channel label
            painter.setPen(QColor(180, 180, 180))
            painter.drawText(0, y, label_width, bar_height, Qt.AlignCenter, label)

            # Background
            painter.fillRect(bar_x, y, bar_w, bar_height, QColor(30, 30, 30))

            # Level bar: green up to -12 dB, yellow -12 to -6, red -6 to 0
            level_w = int(bar_w * level)
            green_end = self._db_to_x(-12, 0, bar_w)
            yellow_end = self._db_to_x(-6, 0, bar_w)

            if level_w > 0:
                gw = min(level_w, green_end)
                if gw > 0:
                    painter.fillRect(bar_x, y, gw, bar_height, QColor(0, 180, 0))
                if level_w > green_end:
                    yw = min(level_w - green_end, yellow_end - green_end)
                    if yw > 0:
                        painter.fillRect(bar_x + green_end, y, yw, bar_height, QColor(220, 200, 0))
                if level_w > yellow_end:
                    rw = level_w - yellow_end
                    painter.fillRect(bar_x + yellow_end, y, rw, bar_height, QColor(220, 30, 0))

            # Peak hold indicator
            peak_x = bar_x + int(bar_w * peak)
            if peak_x > bar_x + 1:
                painter.fillRect(peak_x - 1, y, 2, bar_height, QColor(255, 255, 255))

        # dB scale below bars
        scale_y = 2 * (bar_height + 2) + 1
        font = painter.font()
        font.setPixelSize(9)
        painter.setFont(font)
        painter.setPen(QColor(140, 140, 140))

        for db in self._TICK_DB:
            x = self._db_to_x(db, bar_x, bar_w)
            # Tick mark
            painter.drawLine(x, scale_y, x, scale_y + 3)
            # Label
            text = str(db) if db < 0 else " 0"
            tw = painter.fontMetrics().horizontalAdvance(text)
            painter.drawText(x - tw // 2, scale_y + scale_height, text)

        painter.end()


class GUISampleBufferDelegate(SampleBufferDelegate):
    """Extends SampleBufferDelegate to extract audio levels for the VU meter."""

    def init(self):
        self = objc.super(GUISampleBufferDelegate, self).init()
        if self is None:
            return None
        self.recorder = None
        self.video_output = None
        self.audio_output = None
        self.audio_level_callback = None
        return self

    def captureOutput_didOutputSampleBuffer_fromConnection_(
        self, output, sample_buffer, connection
    ):
        if output is self.audio_output:
            # Extract levels from AVCaptureAudioChannel (works during preview and recording)
            if self.audio_level_callback:
                channels = connection.audioChannels()
                if channels and len(channels) > 0:
                    levels = [ch.averagePowerLevel() for ch in channels]
                    self.audio_level_callback(levels)
            # Forward to recorder for recording
            if self.recorder:
                self.recorder.handle_audio_sample_buffer(sample_buffer)
        elif output is self.video_output and self.recorder:
            self.recorder.handle_video_sample_buffer(sample_buffer)

    def captureOutput_didDropSampleBuffer_fromConnection_(
        self, output, sample_buffer, connection
    ):
        if self.recorder and output is self.video_output:
            self.recorder.frames_dropped += 1


class StatusSignal(QObject):
    """Bridge to send stop notifications from capture threads to the Qt main thread."""
    stopped = pyqtSignal()
    audio_levels = pyqtSignal(list)


class FruitcapGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("fruitcap")
        self.setMinimumSize(800, 500)
        self.resize(1000, 800)

        self._recorder = None
        self._session = None
        self._delegate = None
        self._recording = False
        self._previewing = False
        self._status_signal = StatusSignal()
        self._status_signal.stopped.connect(self._on_recording_stopped)
        self._status_signal.audio_levels.connect(self._on_audio_levels)

        self._build_ui()
        self._populate_devices()

        # Auto-select matching audio device for the initial video device
        self._auto_select_audio_device()

        # Apply initial codec constraints (h264 default = 8-bit only)
        self._on_codec_changed(self._codec_combo.currentData())
        self._on_audio_codec_changed(self._audio_codec_combo.currentData())

        # Remove initial focus from editable fields
        self._preview_widget.setFocus()

        # Status timer for updating recording stats
        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._poll_status)

        # Start preview automatically
        QTimer.singleShot(100, self._start_preview)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(6, 6, 6, 6)

        # Top area: preview + settings side by side
        splitter = QSplitter(Qt.Horizontal)

        # Left: preview + VU meter
        preview_container = QWidget()
        preview_layout = QVBoxLayout(preview_container)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(2)
        self._preview_widget = PreviewWidget()
        preview_layout.addWidget(self._preview_widget, stretch=1)
        self._vu_meter = VUMeterWidget()
        preview_layout.addWidget(self._vu_meter)
        splitter.addWidget(preview_container)

        # Right: settings
        settings_widget = QWidget()
        settings_layout = QVBoxLayout(settings_widget)
        settings_layout.setContentsMargins(4, 4, 4, 4)

        # Device group
        device_group = QGroupBox("Devices")
        device_form = QFormLayout()
        self._video_device_combo = QComboBox()
        self._video_device_combo.currentIndexChanged.connect(self._on_device_changed)
        device_form.addRow("Video:", self._video_device_combo)
        self._audio_device_combo = QComboBox()
        device_form.addRow("Audio:", self._audio_device_combo)
        self._audio_check = QCheckBox("Capture audio")
        self._audio_check.setChecked(True)
        device_form.addRow("", self._audio_check)
        device_group.setLayout(device_form)
        settings_layout.addWidget(device_group)

        # Video settings group
        video_group = QGroupBox("Video")
        video_form = QFormLayout()

        self._codec_combo = QComboBox()
        for label, value in [
            ("H.264", "h264"), ("H.265 (HEVC)", "h265"),
            ("ProRes 422", "prores"), ("ProRes 422 Proxy", "prores_proxy"),
            ("ProRes 422 LT", "prores_lt"), ("ProRes 422 HQ", "prores_hq"),
        ]:
            self._codec_combo.addItem(label, value)
        self._codec_combo.currentIndexChanged.connect(
            lambda _: self._on_codec_changed(self._codec_combo.currentData()))
        video_form.addRow("Codec:", self._codec_combo)

        self._resolution_combo = QComboBox()
        self._resolution_combo.addItems(["4k", "1080p", "720p"])
        self._resolution_combo.setCurrentText("4k")
        video_form.addRow("Resolution:", self._resolution_combo)

        self._fps_combo = QComboBox()
        self._fps_combo.addItems(["Device default", "60", "59.94", "50", "30", "29.97", "25", "24", "23.976"])
        video_form.addRow("Frame rate:", self._fps_combo)

        self._bitrate_edit = QLineEdit("80m")
        video_form.addRow("Bitrate:", self._bitrate_edit)

        self._container_combo = QComboBox()
        for label, value in [("Auto", "auto"), ("MP4", "mp4"), ("MOV", "mov")]:
            self._container_combo.addItem(label, value)
        video_form.addRow("Container:", self._container_combo)

        self._bit_depth_combo = QComboBox()
        self._bit_depth_combo.addItems(["8", "10"])
        video_form.addRow("Bit depth:", self._bit_depth_combo)

        self._chroma_combo = QComboBox()
        for label, value in [("4:2:0", "420"), ("4:2:2", "422")]:
            self._chroma_combo.addItem(label, value)
        self._chroma_combo.currentIndexChanged.connect(
            lambda _: self._on_chroma_changed(self._chroma_combo.currentData()))
        video_form.addRow("Chroma:", self._chroma_combo)

        self._color_space_combo = QComboBox()
        self._color_space_combo.addItems(list(COLOR_SPACE_PRESETS.keys()))
        video_form.addRow("Color space:", self._color_space_combo)

        video_group.setLayout(video_form)
        settings_layout.addWidget(video_group)

        # Audio settings group
        audio_group = QGroupBox("Audio")
        audio_form = QFormLayout()

        self._audio_codec_combo = QComboBox()
        for label, value in [("AAC", "aac"), ("ALAC", "alac"), ("PCM", "pcm")]:
            self._audio_codec_combo.addItem(label, value)
        self._audio_codec_combo.currentIndexChanged.connect(
            lambda _: self._on_audio_codec_changed(self._audio_codec_combo.currentData()))
        audio_form.addRow("Codec:", self._audio_codec_combo)

        self._audio_bitrate_label = QLabel("Bitrate:")
        self._audio_bitrate_edit = QLineEdit("256k")
        audio_form.addRow(self._audio_bitrate_label, self._audio_bitrate_edit)

        self._audio_sample_rate_combo = QComboBox()
        self._audio_sample_rate_combo.addItems(["48000", "44100", "96000"])
        audio_form.addRow("Sample rate:", self._audio_sample_rate_combo)

        self._audio_channels_combo = QComboBox()
        self._audio_channels_combo.addItems(["1", "2"])
        audio_form.addRow("Channels:", self._audio_channels_combo)

        audio_group.setLayout(audio_form)
        settings_layout.addWidget(audio_group)

        # Output
        output_group = QGroupBox("Output")
        output_form = QFormLayout()
        self._output_edit = QLineEdit("capture-%d-%t.mp4")
        self._output_edit.setMinimumWidth(200)
        output_form.addRow("File:", self._output_edit)
        output_group.setLayout(output_form)
        settings_layout.addWidget(output_group)

        settings_layout.addStretch()
        splitter.addWidget(settings_widget)

        # Set initial sizes: ~75% preview, ~25% settings
        splitter.setSizes([750, 250])
        main_layout.addWidget(splitter, stretch=1)

        # Bottom: buttons
        button_layout = QHBoxLayout()

        self._record_btn = QPushButton("Start Recording")
        self._record_btn.setFixedHeight(36)
        self._record_btn.clicked.connect(self._toggle_recording)
        button_layout.addWidget(self._record_btn)

        self._quit_btn = QPushButton("Quit")
        self._quit_btn.setFixedHeight(36)
        self._quit_btn.clicked.connect(self.close)
        button_layout.addWidget(self._quit_btn)

        main_layout.addLayout(button_layout)

        # Status bar
        self._statusbar = QStatusBar()
        self.setStatusBar(self._statusbar)
        self._statusbar.showMessage("Ready")

    def _populate_devices(self):
        check_camera_permission()

        video_devices = get_devices(AVF.AVMediaTypeVideo)
        self._video_devices = video_devices
        for i, dev in enumerate(video_devices):
            self._video_device_combo.addItem(dev.localizedName(), i)

        if check_microphone_permission():
            audio_devices = get_devices(AVF.AVMediaTypeAudio)
            self._audio_devices = audio_devices
            for i, dev in enumerate(audio_devices):
                self._audio_device_combo.addItem(dev.localizedName(), i)
        else:
            self._audio_devices = []
            self._audio_check.setChecked(False)
            self._audio_check.setEnabled(False)

    def _on_device_changed(self, index):
        """Restart preview and auto-select matching audio device."""
        self._auto_select_audio_device()
        if self._previewing and not self._recording:
            self._stop_preview()
            QTimer.singleShot(100, self._start_preview)

    def _auto_select_audio_device(self):
        """Select the audio device whose name best matches the video device."""
        video_dev = self._get_selected_video_device()
        if not video_dev or self._audio_device_combo.count() == 0:
            return

        video_name = video_dev.localizedName().lower()

        # Try exact match first, then substring match
        for i in range(self._audio_device_combo.count()):
            audio_name = self._audio_device_combo.itemText(i).lower()
            if audio_name == video_name:
                self._audio_device_combo.setCurrentIndex(i)
                return

        for i in range(self._audio_device_combo.count()):
            audio_name = self._audio_device_combo.itemText(i).lower()
            if video_name in audio_name or audio_name in video_name:
                self._audio_device_combo.setCurrentIndex(i)
                return

    def _on_codec_changed(self, codec):
        """Update UI constraints when codec changes."""
        is_prores = codec.startswith("prores")
        self._bitrate_edit.setEnabled(not is_prores)
        if is_prores:
            self._bit_depth_combo.setCurrentText("10")
            self._chroma_combo.setCurrentIndex(self._chroma_combo.findData("422"))
            self._bit_depth_combo.setEnabled(False)
            self._chroma_combo.setEnabled(False)
            self._container_combo.setCurrentIndex(self._container_combo.findData("mov"))
            self._audio_codec_combo.setCurrentIndex(self._audio_codec_combo.findData("pcm"))
        elif codec == "h264":
            # H.264 only supports 8-bit 4:2:0 on Apple's hardware encoder
            self._bit_depth_combo.setCurrentText("8")
            self._bit_depth_combo.setEnabled(False)
            self._chroma_combo.setCurrentIndex(self._chroma_combo.findData("420"))
            self._chroma_combo.setEnabled(False)
        else:
            # h265 supports 4:2:0 and 4:2:2, but 4:2:2 forces 10-bit
            self._chroma_combo.setEnabled(True)
            if self._chroma_combo.currentData() == "422":
                self._bit_depth_combo.setCurrentText("10")
                self._bit_depth_combo.setEnabled(False)
            else:
                self._bit_depth_combo.setEnabled(True)

    def _on_chroma_changed(self, chroma):
        """Force 10-bit when 4:2:2 is selected with H.265 (no 8-bit 4:2:2 profile)."""
        codec = self._codec_combo.currentData()
        if codec == "h265" and chroma == "422":
            self._bit_depth_combo.setCurrentText("10")
            self._bit_depth_combo.setEnabled(False)
        elif codec == "h265":
            self._bit_depth_combo.setEnabled(True)

    def _on_audio_codec_changed(self, audio_codec):
        """Show/hide audio bitrate (only relevant for AAC)."""
        show_bitrate = (audio_codec == "aac")
        self._audio_bitrate_label.setVisible(show_bitrate)
        self._audio_bitrate_edit.setVisible(show_bitrate)

    def _get_selected_video_device(self):
        idx = self._video_device_combo.currentData()
        if idx is not None and idx < len(self._video_devices):
            return self._video_devices[idx]
        return None

    def _get_selected_audio_device(self):
        if not self._audio_check.isChecked():
            return None
        idx = self._audio_device_combo.currentData()
        if idx is not None and idx < len(self._audio_devices):
            return self._audio_devices[idx]
        return None

    def _build_config(self):
        """Build a config dict from the current UI settings."""
        overrides = {
            "codec": self._codec_combo.currentData(),
            "resolution": self._resolution_combo.currentText(),
            "container": self._container_combo.currentData(),
            "bit_depth": self._bit_depth_combo.currentText(),
            "chroma": self._chroma_combo.currentData(),
            "color_space": self._color_space_combo.currentText(),
            "bitrate": self._bitrate_edit.text(),
            "output": self._output_edit.text(),
            "audio_codec": self._audio_codec_combo.currentData(),
            "audio_bitrate": self._audio_bitrate_edit.text(),
            "audio_sample_rate": self._audio_sample_rate_combo.currentText(),
            "audio_channels": self._audio_channels_combo.currentText(),
        }

        fps_text = self._fps_combo.currentText()
        if fps_text and fps_text != "Device default":
            overrides["fps"] = fps_text

        if not self._audio_check.isChecked():
            overrides["audio_enabled"] = False

        cfg = load_config(overrides=overrides)
        cfg["output"] = generate_output_path(cfg["output"])

        # Correct output extension
        base, ext = os.path.splitext(cfg["output"])
        _, expected_ext = get_output_file_type_and_extension(cfg)
        if ext.lower() != expected_ext:
            cfg["output"] = base + expected_ext

        return cfg

    def _start_preview(self):
        """Start a capture session with preview and data outputs pre-configured.

        Data outputs and delegate are set up now so that transitioning to
        recording only requires creating a writer — no session reconfiguration,
        so the preview stays seamless.
        """
        device = self._get_selected_video_device()
        if not device:
            self._statusbar.showMessage("No video device selected")
            return

        self._session = AVF.AVCaptureSession.alloc().init()

        if self._session.canSetSessionPreset_(AVF.AVCaptureSessionPresetInputPriority):
            self._session.setSessionPreset_(AVF.AVCaptureSessionPresetInputPriority)

        dev_input, error = AVF.AVCaptureDeviceInput.deviceInputWithDevice_error_(device, None)
        if dev_input is None:
            self._statusbar.showMessage(f"Device error: {error}")
            return

        if self._session.canAddInput_(dev_input):
            self._session.addInput_(dev_input)

        # Set up delegate and video data output (buffers are discarded
        # until a recorder is attached via delegate.recorder)
        self._delegate = GUISampleBufferDelegate.alloc().init()
        self._delegate.audio_level_callback = lambda levels: self._status_signal.audio_levels.emit(levels)

        video_output = AVF.AVCaptureVideoDataOutput.alloc().init()
        video_output.setAlwaysDiscardsLateVideoFrames_(True)
        video_queue = dispatch_queue_create(b"fruitcap.videoQueue")
        video_queue_obj = objc.objc_object(c_void_p=video_queue)
        self._delegate.video_output = video_output
        video_output.setSampleBufferDelegate_queue_(self._delegate, video_queue_obj)

        if self._session.canAddOutput_(video_output):
            self._session.addOutput_(video_output)

        # Set up audio input and output
        audio_device = self._get_selected_audio_device()
        if audio_device:
            audio_input, error = AVF.AVCaptureDeviceInput.deviceInputWithDevice_error_(
                audio_device, None
            )
            if audio_input and self._session.canAddInput_(audio_input):
                self._session.addInput_(audio_input)

                audio_output = AVF.AVCaptureAudioDataOutput.alloc().init()
                audio_queue = dispatch_queue_create(b"fruitcap.audioQueue")
                audio_queue_obj = objc.objc_object(c_void_p=audio_queue)
                self._delegate.audio_output = audio_output
                audio_output.setSampleBufferDelegate_queue_(self._delegate, audio_queue_obj)

                if self._session.canAddOutput_(audio_output):
                    self._session.addOutput_(audio_output)

        self._preview_widget.attach_session(self._session)
        self._session.startRunning()
        self._previewing = True
        self._statusbar.showMessage(f"Preview: {device.localizedName()}")

    def _stop_preview(self):
        """Stop the preview session and tear down all outputs."""
        if self._session and self._previewing:
            self._session.stopRunning()
            self._session = None
            self._delegate = None
            self._previewing = False

    def _toggle_recording(self):
        if self._recording:
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self):
        """Build config, adopt the live session, and start recording.

        The session and data outputs are already running from preview,
        so we just create a writer and point the delegate at the recorder.
        No session reconfiguration means no preview interruption.
        """
        if not self._session or not self._previewing:
            self._statusbar.showMessage("No active preview session")
            return

        try:
            cfg = self._build_config()
        except (ValueError, SystemExit) as e:
            self._statusbar.showMessage(f"Config error: {e}")
            return

        self._recorder = Recorder(cfg)

        # Adopt the running session — no reconfiguration needed
        self._recorder.adopt_session(self._session, self._delegate)
        self._recorder.setup_writer()

        # Set up stop callback that signals the main thread
        def on_stop():
            self._status_signal.stopped.emit()
        self._recorder._stop_callback = on_stop

        self._recorder.start()
        self._recording = True

        # Disable settings while recording
        self._set_settings_enabled(False)
        self._record_btn.setText("Stop Recording")
        self._record_btn.setStyleSheet("background-color: #cc3333; color: white;")
        self._statusbar.showMessage(f"Recording to {cfg['output']}...")
        self._status_timer.start(500)

    def _stop_recording(self):
        if not self._recording or not self._recorder:
            return

        self._recording = False
        self._status_timer.stop()

        # Stop recorder in a background thread to avoid blocking the GUI
        recorder = self._recorder
        def do_stop():
            recorder.stop()
            self._status_signal.stopped.emit()

        threading.Thread(target=do_stop, daemon=True).start()
        self._statusbar.showMessage("Stopping...")

    def _on_recording_stopped(self):
        """Called on the main thread when recording finishes.

        The session is still running (recorder didn't own it), so the
        preview continues uninterrupted.
        """
        if self._recorder:
            frames = self._recorder.frames_written
            dropped = self._recorder.frames_dropped
            output = self._recorder._current_output_path()
            msg = f"Saved {frames} frames"
            if dropped:
                msg += f" ({dropped} dropped)"
            msg += f" to {output}"
            self._statusbar.showMessage(msg)
            self._recorder = None

        self._recording = False
        self._previewing = self._session is not None and self._session.isRunning()
        self._record_btn.setText("Start Recording")
        self._record_btn.setStyleSheet("")
        self._set_settings_enabled(True)

        if not self._previewing:
            QTimer.singleShot(200, self._start_preview)

    def _poll_status(self):
        """Update status bar with recording stats."""
        if not self._recorder or not self._recorder.running:
            return
        if not self._recorder.start_time:
            return

        elapsed = time.monotonic() - self._recorder.start_time
        minutes, seconds = divmod(int(elapsed), 60)
        hours, minutes = divmod(minutes, 60)

        try:
            size_bytes = os.path.getsize(self._recorder._current_output_path())
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

        frames = self._recorder.frames_written
        dropped = self._recorder.frames_dropped
        dropped_str = f"  dropped: {dropped}" if dropped else ""

        self._statusbar.showMessage(
            f"REC  {hours:02d}:{minutes:02d}:{seconds:02d}  "
            f"frames: {frames}{dropped_str}  size: {size_str}"
        )

    def _on_audio_levels(self, levels):
        """Update VU meter from audio callback (runs on main thread via signal)."""
        self._vu_meter.set_levels_db(levels)

    def _set_settings_enabled(self, enabled):
        """Enable/disable all settings controls."""
        for widget in (
            self._video_device_combo, self._audio_device_combo, self._audio_check,
            self._codec_combo, self._resolution_combo, self._fps_combo,
            self._bitrate_edit, self._container_combo, self._bit_depth_combo,
            self._chroma_combo, self._color_space_combo,
            self._audio_codec_combo, self._audio_bitrate_edit,
            self._audio_sample_rate_combo, self._audio_channels_combo,
            self._output_edit,
        ):
            widget.setEnabled(enabled)

    def keyPressEvent(self, event):
        """Handle keyboard shortcuts: Esc stops recording, Q quits."""
        key = event.key()
        if key == Qt.Key_Escape and self._recording:
            self._stop_recording()
        elif key == Qt.Key_Q and not isinstance(self.focusWidget(), QLineEdit):
            self.close()
        else:
            super().keyPressEvent(event)

    def mousePressEvent(self, event):
        """Clear focus from editable fields when clicking elsewhere."""
        self._preview_widget.setFocus()
        super().mousePressEvent(event)

    def closeEvent(self, event):
        """Clean up on window close."""
        if self._recording and self._recorder:
            self._recorder.stop()
        self._stop_preview()
        event.accept()


def main():
    # Suppress fruitcap's quiet-mode print wrapper in GUI context
    import fruitcap
    fruitcap._quiet = True

    app = QApplication(sys.argv)
    app.setApplicationName("fruitcap")

    window = FruitcapGUI()
    window.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
