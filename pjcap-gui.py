#!/usr/bin/env python3
"""pjcap-gui - macOS video/audio capture GUI using AVFoundation + PyQt5.

Author: Phil Jensen <philj@philandamy.org>
"""

import os
import signal
import sys
import threading
import time
from ctypes import c_void_p

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

# Import shared infrastructure from pjcap
from pjcap import (
    Recorder, SampleBufferDelegate, CompressedPreview,
    load_config, parse_bitrate, parse_size, generate_output_path,
    build_capture_video_output_settings,
    make_frame_duration,
    get_output_file_type_and_extension,
    get_devices, list_devices, find_device_by_selector,
    select_device_format,
    check_camera_permission, check_microphone_permission,
    dispatch_queue_create,
    COLOR_SPACE_PRESETS,
    apply_runtime_options,
    _AJA_PIXEL_FORMATS,
    _aja_create_audio_format_desc, _aja_extract_audio_channels,
    _aja_make_audio_sample_buffer,
    _read_be32, _readinto_exact,
)


class PreviewWidget(QWidget):
    """Widget that hosts an AVCaptureVideoPreviewLayer or AVSampleBufferDisplayLayer."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._preview_layer = None
        self._display_layer = None
        self.setAttribute(Qt.WA_NativeWindow, True)
        self.setFocusPolicy(Qt.ClickFocus)
        self.setMinimumSize(320, 180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def attach_session(self, session):
        """Attach an AVCaptureSession to display its preview."""
        self._remove_layers()

        ns_view = objc.objc_object(c_void_p=int(self.winId()))
        ns_view.setWantsLayer_(True)

        self._preview_layer = AVF.AVCaptureVideoPreviewLayer.layerWithSession_(session)
        self._preview_layer.setVideoGravity_(AVF.AVLayerVideoGravityResizeAspect)
        self._preview_layer.setFrame_(ns_view.bounds())
        self._preview_layer.setAutoresizingMask_(2 | 16)  # width + height sizable
        ns_view.layer().addSublayer_(self._preview_layer)

    def attach_display_layer(self):
        """Create and attach an AVSampleBufferDisplayLayer for raw frame display."""
        self._remove_layers()

        ns_view = objc.objc_object(c_void_p=int(self.winId()))
        ns_view.setWantsLayer_(True)

        self._display_layer = AVF.AVSampleBufferDisplayLayer.alloc().init()
        self._display_layer.setVideoGravity_(AVF.AVLayerVideoGravityResizeAspect)
        self._display_layer.setFrame_(ns_view.bounds())
        self._display_layer.setAutoresizingMask_(2 | 16)
        ns_view.layer().addSublayer_(self._display_layer)
        return self._display_layer

    def _remove_layers(self):
        if self._preview_layer:
            self._preview_layer.removeFromSuperlayer()
            self._preview_layer = None
        if self._display_layer:
            self._display_layer.removeFromSuperlayer()
            self._display_layer = None

    def resizeEvent(self, event):
        super().resizeEvent(event)
        layer = self._preview_layer or self._display_layer
        if layer:
            ns_view = objc.objc_object(c_void_p=int(self.winId()))
            layer.setFrame_(ns_view.bounds())


class AudioLevelMeterWidget(QWidget):
    """Two-channel horizontal dBFS meter."""

    # dB tick marks to draw on the scale
    _TICK_DB = [-48, -36, -24, -18, -12, -6, -3, 0]
    _MIN_DB = -60.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self._average_db = [self._MIN_DB, self._MIN_DB]
        self.setMinimumHeight(48)
        self.setMaximumHeight(56)

    def clear(self):
        self._average_db = [self._MIN_DB, self._MIN_DB]
        self.update()

    def set_levels_db(self, meter_data):
        """Update from capture metering data."""
        average_db = meter_data.get("average_db", [])
        for i in range(2):
            if i < len(average_db):
                self._average_db[i] = max(self._MIN_DB, min(0.0, average_db[i]))
            else:
                self._average_db[i] = self._MIN_DB
        self.update()

    def _db_to_x(self, db, bar_x, bar_w):
        """Convert a dB value (-60..0) to an x pixel position."""
        linear = (max(self._MIN_DB, min(0.0, db)) + abs(self._MIN_DB)) / abs(self._MIN_DB)
        return bar_x + int(bar_w * linear)

    def paintEvent(self, event):
        painter = QPainter(self)
        w = self.width()
        h = self.height()
        scale_height = 14
        bottom_pad = 4
        bar_height = max(1, (h - scale_height - bottom_pad - 4) // 2)
        label_width = 14
        bar_x = label_width + 2
        right_pad = 32
        bar_w = max(20, w - bar_x - right_pad)

        for i, label in enumerate(("L", "R")):
            y = i * (bar_height + 2) + 1
            level_db = self._average_db[i]

            # Channel label
            painter.setPen(QColor(180, 180, 180))
            painter.drawText(0, y, label_width, bar_height, Qt.AlignCenter, label)

            # Background
            painter.fillRect(bar_x, y, bar_w, bar_height, QColor(30, 30, 30))

            if level_db > self._MIN_DB:
                green_end = self._db_to_x(min(level_db, -12.0), bar_x, bar_w)
                if green_end > bar_x:
                    painter.fillRect(bar_x, y, green_end - bar_x, bar_height, QColor(0, 180, 0))

                if level_db > -12.0:
                    yellow_start = self._db_to_x(-12.0, bar_x, bar_w)
                    yellow_end = self._db_to_x(min(level_db, -6.0), bar_x, bar_w)
                    if yellow_end > yellow_start:
                        painter.fillRect(
                            yellow_start, y, yellow_end - yellow_start, bar_height, QColor(220, 200, 0)
                        )

                if level_db > -6.0:
                    red_start = self._db_to_x(-6.0, bar_x, bar_w)
                    red_end = self._db_to_x(level_db, bar_x, bar_w)
                    if red_end > red_start:
                        painter.fillRect(
                            red_start, y, red_end - red_start, bar_height, QColor(220, 30, 0)
                        )

        # dBFS scale below bars
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

        painter.drawText(bar_x + bar_w + 4, scale_y + scale_height, "dBFS")

        painter.end()


class GUISampleBufferDelegate(SampleBufferDelegate):
    """Extends SampleBufferDelegate to extract dBFS audio levels for the GUI meter."""

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
            if self.audio_level_callback:
                channels = connection.audioChannels() or []
                average_db = [ch.averagePowerLevel() for ch in channels]
                if average_db:
                    self.audio_level_callback({"average_db": average_db})
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
    stop_requested = pyqtSignal()
    stopped = pyqtSignal()
    audio_levels = pyqtSignal(object)


class PjcapGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("pjcap")
        self.setMinimumSize(800, 500)
        self.resize(1000, 800)

        self._recorder = None
        self._session = None
        self._delegate = None
        self._recording = False
        self._previewing = False
        self._aja_proc = None
        self._aja_thread = None
        self._aja_header = None
        self._aja_display_layer = None
        self._aja_cv_pixfmt = None
        self._aja_preview_running = False
        self._aja_adaptor = None
        self._status_signal = StatusSignal()
        self._status_signal.stop_requested.connect(self._stop_recording)
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

        # Left: preview + audio meter
        preview_container = QWidget()
        preview_layout = QVBoxLayout(preview_container)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(2)
        self._preview_widget = PreviewWidget()
        preview_layout.addWidget(self._preview_widget, stretch=1)
        self._audio_meter = AudioLevelMeterWidget()
        preview_layout.addWidget(self._audio_meter)
        splitter.addWidget(preview_container)

        # Right: settings
        settings_widget = QWidget()
        settings_layout = QVBoxLayout(settings_widget)
        settings_layout.setContentsMargins(4, 4, 4, 4)

        # Shared form layout helper for consistent label alignment
        label_min_width = 80

        def make_form():
            form = QFormLayout()
            form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
            form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
            return form

        def add_row(form, text, widget):
            label = QLabel(text)
            label.setFixedWidth(label_min_width)
            label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            form.addRow(label, widget)
            return label

        # Device group
        device_group = QGroupBox("Devices")
        device_form = make_form()
        self._video_device_combo = QComboBox()
        self._video_device_combo.currentIndexChanged.connect(self._on_device_changed)
        add_row(device_form, "Video:", self._video_device_combo)
        self._audio_device_combo = QComboBox()
        self._audio_device_combo.currentIndexChanged.connect(self._restart_preview_if_idle)
        add_row(device_form, "Audio:", self._audio_device_combo)
        self._audio_check = QCheckBox("Capture audio")
        self._audio_check.setChecked(True)
        self._audio_check.stateChanged.connect(self._restart_preview_if_idle)
        add_row(device_form, "", self._audio_check)
        self._aja_check = QCheckBox("AJA capture")
        self._aja_check.stateChanged.connect(self._on_aja_toggled)
        add_row(device_form, "", self._aja_check)
        device_group.setLayout(device_form)
        settings_layout.addWidget(device_group)

        # Video settings group
        video_group = QGroupBox("Video")
        video_form = make_form()

        self._codec_combo = QComboBox()
        for label, value in [
            ("H.264", "h264"), ("H.265 (HEVC)", "h265"),
            ("ProRes 422", "prores"), ("ProRes 422 Proxy", "prores_proxy"),
            ("ProRes 422 LT", "prores_lt"), ("ProRes 422 HQ", "prores_hq"),
        ]:
            self._codec_combo.addItem(label, value)
        self._codec_combo.currentIndexChanged.connect(
            lambda _: self._on_codec_changed(self._codec_combo.currentData()))
        add_row(video_form, "Codec:", self._codec_combo)

        self._resolution_combo = QComboBox()
        self._resolution_combo.addItems(["4k", "1080p", "720p"])
        self._resolution_combo.setCurrentText("4k")
        self._resolution_combo.currentIndexChanged.connect(self._restart_preview_if_idle)
        add_row(video_form, "Resolution:", self._resolution_combo)

        self._fps_combo = QComboBox()
        self._fps_combo.addItems(["Device default", "60", "59.94", "50", "30", "29.97", "25", "24", "23.976"])
        self._fps_combo.currentIndexChanged.connect(self._restart_preview_if_idle)
        add_row(video_form, "Frame rate:", self._fps_combo)

        self._bitrate_edit = QLineEdit("80m")
        self._bitrate_edit.setMaximumWidth(80)
        add_row(video_form, "Bitrate:", self._bitrate_edit)

        self._container_combo = QComboBox()
        for label, value in [("Auto", "auto"), ("MP4", "mp4"), ("MOV", "mov")]:
            self._container_combo.addItem(label, value)
        add_row(video_form, "Container:", self._container_combo)

        self._bit_depth_combo = QComboBox()
        self._bit_depth_combo.addItems(["8", "10"])
        self._bit_depth_combo.currentIndexChanged.connect(self._restart_preview_if_idle)
        add_row(video_form, "Bit depth:", self._bit_depth_combo)

        self._chroma_combo = QComboBox()
        for label, value in [("4:2:0", "420"), ("4:2:2", "422")]:
            self._chroma_combo.addItem(label, value)
        self._chroma_combo.currentIndexChanged.connect(self._restart_preview_if_idle)
        self._chroma_combo.currentIndexChanged.connect(
            lambda _: self._on_chroma_changed(self._chroma_combo.currentData()))
        add_row(video_form, "Chroma:", self._chroma_combo)

        self._color_space_combo = QComboBox()
        self._color_space_combo.addItems(list(COLOR_SPACE_PRESETS.keys()))
        add_row(video_form, "Color space:", self._color_space_combo)

        video_group.setLayout(video_form)
        settings_layout.addWidget(video_group)

        # Audio settings group
        audio_group = QGroupBox("Audio")
        audio_form = make_form()

        self._audio_codec_combo = QComboBox()
        for label, value in [("AAC", "aac"), ("ALAC", "alac"), ("PCM", "pcm")]:
            self._audio_codec_combo.addItem(label, value)
        self._audio_codec_combo.currentIndexChanged.connect(
            lambda _: self._on_audio_codec_changed(self._audio_codec_combo.currentData()))
        add_row(audio_form, "Codec:", self._audio_codec_combo)

        self._audio_bitrate_edit = QLineEdit("256k")
        self._audio_bitrate_edit.setMaximumWidth(80)
        self._audio_bitrate_label = add_row(audio_form, "Bitrate:", self._audio_bitrate_edit)

        self._audio_sample_rate_combo = QComboBox()
        self._audio_sample_rate_combo.addItems(["48000", "44100", "96000"])
        add_row(audio_form, "Sample rate:", self._audio_sample_rate_combo)

        self._stereo_check = QCheckBox("Stereo")
        add_row(audio_form, "Channels:", self._stereo_check)

        audio_group.setLayout(audio_form)
        settings_layout.addWidget(audio_group)

        # Output
        output_group = QGroupBox("Output")
        output_form = make_form()
        self._output_edit = QLineEdit("capture-%d-%t.mp4")
        self._output_edit.setMinimumWidth(200)
        add_row(output_form, "File:", self._output_edit)
        self._split_duration_edit = QLineEdit()
        self._split_duration_edit.setMaximumWidth(80)
        self._split_duration_edit.setPlaceholderText("seconds")
        add_row(output_form, "Split every:", self._split_duration_edit)
        self._split_size_edit = QLineEdit()
        self._split_size_edit.setMaximumWidth(80)
        self._split_size_edit.setPlaceholderText("e.g. 500m, 2g")
        add_row(output_form, "Split size:", self._split_size_edit)
        self._stop_after_edit = QLineEdit()
        self._stop_after_edit.setMaximumWidth(80)
        self._stop_after_edit.setPlaceholderText("seconds")
        add_row(output_form, "Stop after:", self._stop_after_edit)
        self._max_frames_edit = QLineEdit()
        self._max_frames_edit.setMaximumWidth(80)
        self._max_frames_edit.setPlaceholderText("frames")
        add_row(output_form, "Max frames:", self._max_frames_edit)
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
        self._restart_preview_if_idle()

    def _restart_preview_if_idle(self, _=None):
        """Restart preview session if previewing but not recording."""
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
        cfg = load_config(overrides=self._build_overrides())
        cfg["output"] = generate_output_path(cfg["output"])

        # Correct output extension
        base, ext = os.path.splitext(cfg["output"])
        _, expected_ext = get_output_file_type_and_extension(cfg)
        if ext.lower() != expected_ext:
            cfg["output"] = base + expected_ext

        return cfg

    def _build_overrides(self):
        """Build config overrides from the current UI settings."""
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
            "audio_channels": "2" if self._stereo_check.isChecked() else "1",
        }

        fps_text = self._fps_combo.currentText()
        if fps_text and fps_text != "Device default":
            overrides["fps"] = fps_text

        if not self._audio_check.isChecked():
            overrides["audio_enabled"] = False

        return overrides

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

        try:
            cfg = load_config(overrides=self._build_overrides())
        except (ValueError, SystemExit) as e:
            self._statusbar.showMessage(f"Config error: {e}")
            return

        self._audio_meter.clear()

        self._session = AVF.AVCaptureSession.alloc().init()

        if self._session.canSetSessionPreset_(AVF.AVCaptureSessionPresetInputPriority):
            self._session.setSessionPreset_(AVF.AVCaptureSessionPresetInputPriority)

        dev_input, error = AVF.AVCaptureDeviceInput.deviceInputWithDevice_error_(device, None)
        if dev_input is None:
            self._statusbar.showMessage(f"Device error: {error}")
            return

        if self._session.canAddInput_(dev_input):
            self._session.addInput_(dev_input)

        width = cfg["width"]
        height = cfg["height"]
        fps = cfg["fps"]
        if not select_device_format(device, width=width, height=height, fps=fps):
            # Fall back to setting frame rate only (no format change)
            if fps is not None:
                try:
                    duration = make_frame_duration(fps)
                    success, error = device.lockForConfiguration_(None)
                    if success:
                        device.setActiveVideoMinFrameDuration_(duration)
                        device.setActiveVideoMaxFrameDuration_(duration)
                        device.unlockForConfiguration()
                except (ValueError, AttributeError):
                    pass

        # Set up delegate and video data output (buffers are discarded
        # until a recorder is attached via delegate.recorder)
        self._delegate = GUISampleBufferDelegate.alloc().init()
        self._delegate.audio_level_callback = lambda levels: self._status_signal.audio_levels.emit(levels)

        video_output = AVF.AVCaptureVideoDataOutput.alloc().init()
        video_output.setAlwaysDiscardsLateVideoFrames_(cfg["discard_late_frames"])
        video_output.setVideoSettings_(
            build_capture_video_output_settings(cfg["chroma"], cfg["bit_depth"])
        )
        video_queue = dispatch_queue_create(b"pjcap.videoQueue")
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
                audio_queue = dispatch_queue_create(b"pjcap.audioQueue")
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
            self._audio_meter.clear()

    def _on_aja_toggled(self, state):
        """Start/stop AJA preview when toggled."""
        aja_mode = bool(state)
        self._video_device_combo.setEnabled(not aja_mode)
        self._audio_device_combo.setEnabled(not aja_mode)
        if aja_mode:
            self._stop_preview()
            self._start_aja_preview()
        else:
            self._stop_aja_preview()
            if not self._previewing:
                self._start_preview()

    def _start_aja_preview(self):
        """Launch aja-capture subprocess and display frames in preview widget."""
        import json
        import subprocess

        script_dir = os.path.dirname(os.path.abspath(__file__))
        aja_bin = os.path.join(script_dir, "build", "aja-capture")
        if not os.path.isfile(aja_bin):
            aja_bin = os.path.join(script_dir, "aja-capture")
        if not os.path.isfile(aja_bin):
            self._statusbar.showMessage("Error: aja-capture binary not found")
            return

        cmd = [aja_bin]
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stdin=subprocess.PIPE,
            stderr=None, bufsize=16 * 1024 * 1024,
        )

        header_line = proc.stdout.readline()
        if not header_line:
            self._statusbar.showMessage("AJA: No signal detected")
            proc.wait()
            return

        header = json.loads(header_line)
        self._aja_proc = proc
        self._aja_header = header
        self._aja_preview_running = True

        # Attach display layer to preview widget
        self._aja_display_layer = self._preview_widget.attach_display_layer()

        # Create video format description for display
        cv_pixfmt = _AJA_PIXEL_FORMATS.get(header["pixel_format"])
        if cv_pixfmt is None:
            self._statusbar.showMessage(f"Unsupported pixel format: {header['pixel_format']}")
            self._stop_aja_preview()
            return

        self._aja_cv_pixfmt = cv_pixfmt
        self._previewing = True
        self._statusbar.showMessage(
            f"AJA: {header['width']}x{header['height']} "
            f"{header['fps_num']}/{header['fps_den']}fps")

        # Start preview thread
        self._aja_thread = threading.Thread(target=self._aja_preview_loop, daemon=True)
        self._aja_thread.start()

    def _stop_aja_preview(self):
        """Stop the AJA preview subprocess."""
        self._aja_preview_running = False
        if self._aja_proc:
            try:
                self._aja_proc.stdin.write(b"stop\n")
                self._aja_proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            try:
                self._aja_proc.wait(timeout=3)
            except Exception:
                self._aja_proc.kill()
                self._aja_proc.wait()
            self._aja_proc = None
        self._aja_thread = None
        self._aja_display_layer = None
        self._aja_header = None
        self._previewing = False
        self._preview_widget._remove_layers()

    def _aja_preview_loop(self):
        """Background thread: read frames from aja-capture, display + optionally encode."""
        proc = self._aja_proc
        header = self._aja_header
        pipe = proc.stdout
        display_layer = self._aja_display_layer
        cv_pixfmt = self._aja_cv_pixfmt

        width = header["width"]
        height = header["height"]
        fps_num = header.get("fps_num", 30000)
        fps_den = header.get("fps_den", 1001)
        frame_duration_ticks = int(30000 * fps_den / fps_num) if fps_num else 1001

        # Pre-allocate buffers
        expected_video_size = width * 2 * height  # UYVY rough estimate
        video_buf = bytearray(expected_video_size + 4096)
        audio_buf = bytearray(256 * 1024)
        src_audio_channels = header.get("audio_channels", 0)

        # Create a pixel buffer to get stride info
        err0, pb0 = Quartz.CVPixelBufferCreate(None, width, height, cv_pixfmt, None, None)
        if err0 == 0 and pb0:
            Quartz.CVPixelBufferLockBaseAddress(pb0, 0)
            dst_bpr = Quartz.CVPixelBufferGetBytesPerRow(pb0)
            Quartz.CVPixelBufferUnlockBaseAddress(pb0, 0)
            # Create format description from this pixel buffer
            _, vid_fmt_desc = CoreMedia.CMVideoFormatDescriptionCreateForImageBuffer(None, pb0, None)
            del pb0
        else:
            return

        dst_buf_size = dst_bpr * height
        strides_match = None
        frame_num = 0
        first_frame_skipped = False

        while self._aja_preview_running:
            try:
                video_size = _read_be32(pipe)
            except EOFError:
                break

            if video_size > len(video_buf):
                video_buf = bytearray(video_size)
            _readinto_exact(pipe, video_buf, video_size)

            # Read audio (consumed for recording, discarded for preview-only)
            audio_size = _read_be32(pipe)
            if audio_size > 0:
                if audio_size > len(audio_buf):
                    audio_buf = bytearray(audio_size)
                _readinto_exact(pipe, audio_buf, audio_size)

            if not first_frame_skipped:
                first_frame_skipped = True
                continue

            # Audio metering
            if audio_size > 0 and src_audio_channels >= 2:
                self._aja_compute_audio_levels(audio_buf, audio_size, src_audio_channels)

            # Create pixel buffer
            err, pb = Quartz.CVPixelBufferCreate(None, width, height, cv_pixfmt, None, None)
            if err != 0 or not pb:
                continue

            Quartz.CVPixelBufferLockBaseAddress(pb, 0)
            base = Quartz.CVPixelBufferGetBaseAddress(pb)
            dst = base.as_buffer(dst_buf_size)

            if strides_match is None:
                src_bpr = video_size // height if height else dst_bpr
                strides_match = (src_bpr == dst_bpr)

            if strides_match:
                copy_len = min(video_size, dst_buf_size)
                dst[:copy_len] = video_buf[:copy_len]
            else:
                src_bpr = video_size // height
                row_copy = min(src_bpr, dst_bpr)
                for row in range(height):
                    src_off = row * src_bpr
                    dst_off = row * dst_bpr
                    dst[dst_off:dst_off + row_copy] = video_buf[src_off:src_off + row_copy]

            Quartz.CVPixelBufferUnlockBaseAddress(pb, 0)

            # Display via AVSampleBufferDisplayLayer
            pts = CoreMedia.CMTimeMake(frame_num * frame_duration_ticks, 30000)
            dur = CoreMedia.CMTimeMake(frame_duration_ticks, 30000)
            timing = CoreMedia.CMSampleTimingInfo()
            timing.duration = dur
            timing.presentationTimeStamp = pts
            timing.decodeTimeStamp = CoreMedia.CMTime(value=0, timescale=0, flags=0, epoch=0)
            _, display_sb = CoreMedia.CMSampleBufferCreateReadyWithImageBuffer(
                None, pb, vid_fmt_desc, timing, None
            )
            if display_sb and display_layer:
                display_layer.enqueueSampleBuffer_(display_sb)

            # If recording, also feed the pixel buffer adaptor
            recorder = self._recorder
            if recorder and recorder.running and hasattr(self, '_aja_adaptor') and self._aja_adaptor:
                adaptor = self._aja_adaptor
                rec_pts = CoreMedia.CMTimeMake(
                    self._aja_rec_frame_num * frame_duration_ticks, 30000)
                if recorder.writer_input and recorder.writer_input.isReadyForMoreMediaData():
                    adaptor.appendPixelBuffer_withPresentationTime_(pb, rec_pts)
                    self._aja_rec_frame_num += 1
                    recorder.frames_written = self._aja_rec_frame_num

                # Audio
                if (audio_size > 0 and hasattr(self, '_aja_audio_fmt_desc')
                        and self._aja_audio_fmt_desc and recorder.audio_writer_input):
                    src_ch = header.get("audio_channels", 0)
                    out_ch = self._aja_out_audio_channels
                    extracted_size = _aja_extract_audio_channels(
                        audio_buf, audio_size, src_ch, out_ch, self._aja_audio_extract_buf
                    )
                    audio_pts = CoreMedia.CMTimeMake(self._aja_audio_sample_offset, 48000)
                    audio_sb, _ref = _aja_make_audio_sample_buffer(
                        self._aja_audio_extract_buf, extracted_size,
                        self._aja_audio_fmt_desc, audio_pts
                    )
                    if audio_sb and recorder.audio_writer_input.isReadyForMoreMediaData():
                        recorder.audio_writer_input.appendSampleBuffer_(audio_sb)
                    self._aja_audio_sample_offset += extracted_size // (out_ch * 4)

            frame_num += 1

    def _aja_compute_audio_levels(self, audio_buf, audio_size, src_channels):
        """Compute dBFS from raw 32-bit signed int PCM and update the meter."""
        import struct, math
        bytes_per_sample = src_channels * 4
        num_samples = audio_size // bytes_per_sample
        if num_samples == 0:
            return

        # Compute RMS for channels 0 and 1
        max_val = 2147483647.0  # 2^31 - 1
        db = []
        for ch in range(min(2, src_channels)):
            sum_sq = 0.0
            for i in range(0, num_samples, 8):  # sample every 8th for speed
                off = i * bytes_per_sample + ch * 4
                val = struct.unpack_from('<i', audio_buf, off)[0]
                normalized = val / max_val
                sum_sq += normalized * normalized
            count = (num_samples + 7) // 8
            rms = math.sqrt(sum_sq / count) if count > 0 else 0.0
            db_val = 20.0 * math.log10(rms) if rms > 0 else -60.0
            db.append(max(-60.0, min(0.0, db_val)))

        # Pad to 2 channels if mono
        while len(db) < 2:
            db.append(db[0] if db else -60.0)

        self._status_signal.audio_levels.emit({"average_db": db})

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
        if self._aja_check.isChecked():
            return self._start_aja_recording()

        if not self._session or not self._previewing:
            self._statusbar.showMessage("No active preview session")
            return

        try:
            cfg = self._build_config()
        except (ValueError, SystemExit) as e:
            self._statusbar.showMessage(f"Config error: {e}")
            return

        self._recorder = Recorder(cfg)
        if not self._apply_recording_options():
            return

        # Adopt the running session — no reconfiguration needed
        self._recorder.adopt_session(self._session, self._delegate)
        self._recorder.setup_writer()

        # Auto-stop can be triggered from the capture delegate thread, so
        # bounce the actual stop/finalization work to the Qt main thread.
        def on_stop():
            self._status_signal.stop_requested.emit()
        self._recorder._stop_callback = on_stop

        self._recorder.start()
        self._recording = True

        # Disable settings while recording
        self._set_settings_enabled(False)
        self._record_btn.setText("Stop Recording")
        self._record_btn.setStyleSheet("background-color: #cc3333; color: white;")
        self._statusbar.showMessage(f"Recording to {cfg['output']}...")
        self._status_timer.start(500)

    def _apply_recording_options(self):
        """Apply split/stop options to self._recorder. Returns False on error."""
        split_dur = self._split_duration_edit.text().strip()
        if split_dur:
            try:
                split_seconds = float(split_dur)
                if split_seconds <= 0:
                    raise ValueError("must be positive")
                self._recorder.split_seconds = split_seconds
            except ValueError:
                self._statusbar.showMessage(f"Invalid split duration: {split_dur!r}")
                self._recorder = None
                return False

        split_sz = self._split_size_edit.text().strip()
        if split_sz:
            try:
                split_size_bytes = parse_size(split_sz)
                if split_size_bytes <= 0:
                    raise ValueError("must be positive")
                self._recorder.split_size_bytes = split_size_bytes
            except ValueError:
                self._statusbar.showMessage(f"Invalid split size: {split_sz!r}")
                self._recorder = None
                return False

        stop_after = self._stop_after_edit.text().strip()
        if stop_after:
            try:
                max_seconds = float(stop_after)
                if max_seconds <= 0:
                    raise ValueError("must be positive")
                self._recorder.max_seconds = max_seconds
            except ValueError:
                self._statusbar.showMessage(f"Invalid stop-after duration: {stop_after!r}")
                self._recorder = None
                return False

        max_frames = self._max_frames_edit.text().strip()
        if max_frames:
            try:
                n = int(max_frames)
                if n <= 0:
                    raise ValueError("must be positive")
                self._recorder.max_frames = n
            except ValueError:
                self._statusbar.showMessage(f"Invalid max frames: {max_frames!r}")
                self._recorder = None
                return False
        return True

    def _start_aja_recording(self):
        """Start recording from the already-running AJA preview subprocess."""
        if not self._aja_proc or not self._aja_header:
            self._statusbar.showMessage("No AJA preview active — check AJA capture first")
            return

        header = self._aja_header
        try:
            cfg = self._build_config()
        except (ValueError, SystemExit) as e:
            self._statusbar.showMessage(f"Config error: {e}")
            return

        # Override cfg with detected signal
        cfg["width"] = header["width"]
        cfg["height"] = header["height"]
        if header["fps_den"] and header["fps_num"]:
            cfg["fps"] = header["fps_num"] / header["fps_den"]
        if header["audio_channels"] > 0:
            cfg["audio_sample_rate"] = header.get("audio_sample_rate", 48000)

        cv_pixfmt = self._aja_cv_pixfmt

        # Create recorder and writer
        self._recorder = Recorder(cfg)
        if not self._apply_recording_options():
            return

        self._recorder.setup_writer()

        # Pixel buffer adaptor — the preview loop will use this
        pb_attrs = {
            str(Quartz.kCVPixelBufferPixelFormatTypeKey): int(cv_pixfmt),
            str(Quartz.kCVPixelBufferWidthKey): header["width"],
            str(Quartz.kCVPixelBufferHeightKey): header["height"],
        }
        self._aja_adaptor = AVF.AVAssetWriterInputPixelBufferAdaptor.alloc()\
            .initWithAssetWriterInput_sourcePixelBufferAttributes_(
                self._recorder.writer_input, pb_attrs
            )

        self._recorder.start()

        if not self._recorder.writer or self._recorder.writer.status() != AVF.AVAssetWriterStatusWriting:
            self._statusbar.showMessage("Error: AVAssetWriter failed to start")
            self._recorder = None
            self._aja_adaptor = None
            return

        # Start writer session
        start_pts = CoreMedia.CMTimeMake(0, 30000)
        self._recorder.writer.startSessionAtSourceTime_(start_pts)
        self._recorder._segment_session_started = True
        self._recorder._segment_start_timestamp = start_pts
        self._recorder.start_time = time.monotonic()
        self._recorder._start_timestamp = start_pts
        self._recorder.started_writing.set()
        self._recorder._write_timecode_sample(start_pts)

        self._aja_rec_frame_num = 0
        self._aja_audio_sample_offset = 0

        # Audio setup
        out_ch = cfg.get("audio_channels", 1)
        self._aja_out_audio_channels = out_ch
        src_ch = header.get("audio_channels", 0)
        if src_ch > 0 and self._recorder.audio_writer_input:
            self._aja_audio_fmt_desc = _aja_create_audio_format_desc(out_ch, 48000)
            self._aja_audio_extract_buf = bytearray(8192 * out_ch * 4)
        else:
            self._aja_audio_fmt_desc = None

        self._recording = True
        self._set_settings_enabled(False)
        self._record_btn.setText("Stop Recording")
        self._record_btn.setStyleSheet("background-color: #cc3333; color: white;")
        self._statusbar.showMessage(f"Recording AJA → {cfg['output']}...")
        self._status_timer.start(500)

    def _stop_recording(self):
        if not self._recording or not self._recorder:
            return

        self._recording = False
        self._status_timer.stop()

        # Stop recorder in a background thread to avoid blocking the GUI.
        # For AJA mode, the preview loop keeps running — only the recorder stops.
        recorder = self._recorder
        def do_stop():
            recorder.stop()
            self._status_signal.stopped.emit()

        threading.Thread(target=do_stop, daemon=True).start()
        self._statusbar.showMessage("Stopping...")

    def _on_recording_stopped(self):
        """Called on the main thread when recording finishes.

        For AVFoundation: the session keeps running, preview resumes.
        For AJA: the preview loop keeps running, adaptor is cleared.
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

        # Clear AJA recording state (preview loop continues)
        self._aja_adaptor = None

        self._recording = False
        if self._aja_check.isChecked():
            self._previewing = self._aja_preview_running
        else:
            self._previewing = self._session is not None and self._session.isRunning()
        self._record_btn.setText("Start Recording")
        self._record_btn.setStyleSheet("")
        self._set_settings_enabled(True)

        if not self._previewing and not self._aja_check.isChecked():
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

    def _on_audio_levels(self, meter_data):
        """Update audio meter from audio callback (runs on the main thread)."""
        self._audio_meter.set_levels_db(meter_data)

    def _set_settings_enabled(self, enabled):
        """Enable/disable all settings controls."""
        for widget in (
            self._video_device_combo, self._audio_device_combo, self._audio_check, self._aja_check,
            self._codec_combo, self._resolution_combo, self._fps_combo,
            self._bitrate_edit, self._container_combo, self._bit_depth_combo,
            self._chroma_combo, self._color_space_combo,
            self._audio_codec_combo, self._audio_bitrate_edit,
            self._audio_sample_rate_combo, self._stereo_check,
            self._output_edit, self._split_duration_edit, self._split_size_edit,
            self._stop_after_edit, self._max_frames_edit,
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


def install_signal_handlers(app, window):
    """Translate terminal signals into a clean Qt shutdown."""
    pending_signal = {"signum": None}

    def request_shutdown(signum, _frame):
        pending_signal["signum"] = signum

    def drain_shutdown_request():
        signum = pending_signal["signum"]
        if signum is None:
            return
        pending_signal["signum"] = None
        app._signal_exit_code = 128 + signum
        if window.isVisible():
            window.close()
        else:
            app.quit()

    poll_timer = QTimer(app)
    poll_timer.setInterval(100)
    poll_timer.timeout.connect(drain_shutdown_request)
    poll_timer.start()

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)
    return poll_timer


def main():
    # Suppress pjcap's quiet-mode print wrapper in GUI context
    import pjcap
    pjcap._quiet = True

    app = QApplication(sys.argv)
    app.setApplicationName("pjcap")
    app._signal_exit_code = 0

    window = PjcapGUI()
    window.show()
    app._signal_poll_timer = install_signal_handlers(app, window)

    exit_code = app.exec_()
    sys.exit(app._signal_exit_code or exit_code)


if __name__ == "__main__":
    main()
