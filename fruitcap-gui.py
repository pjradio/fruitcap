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


class StatusSignal(QObject):
    """Bridge to send stop notifications from capture threads to the Qt main thread."""
    stopped = pyqtSignal()


class FruitcapGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("fruitcap")
        self.setMinimumSize(800, 500)
        self.resize(1000, 800)

        self._recorder = None
        self._session = None
        self._recording = False
        self._previewing = False
        self._status_signal = StatusSignal()
        self._status_signal.stopped.connect(self._on_recording_stopped)

        self._build_ui()
        self._populate_devices()

        # Auto-select matching audio device for the initial video device
        self._auto_select_audio_device()

        # Apply initial codec constraints (h264 default = 8-bit only)
        self._on_codec_changed(self._codec_combo.currentText())
        self._on_audio_codec_changed(self._audio_codec_combo.currentText())

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

        # Left: preview
        self._preview_widget = PreviewWidget()
        splitter.addWidget(self._preview_widget)

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
        self._codec_combo.addItems(["h264", "h265", "prores", "prores_proxy", "prores_lt", "prores_hq"])
        self._codec_combo.setCurrentText("h264")
        self._codec_combo.currentTextChanged.connect(self._on_codec_changed)
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
        self._container_combo.addItems(["auto", "mp4", "mov"])
        video_form.addRow("Container:", self._container_combo)

        self._bit_depth_combo = QComboBox()
        self._bit_depth_combo.addItems(["8", "10"])
        video_form.addRow("Bit depth:", self._bit_depth_combo)

        self._chroma_combo = QComboBox()
        self._chroma_combo.addItems(["420", "422"])
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
        self._audio_codec_combo.addItems(["aac", "alac", "pcm"])
        self._audio_codec_combo.currentTextChanged.connect(self._on_audio_codec_changed)
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
            self._chroma_combo.setCurrentText("422")
            self._bit_depth_combo.setEnabled(False)
            self._chroma_combo.setEnabled(False)
            self._container_combo.setCurrentText("mov")
            self._audio_codec_combo.setCurrentText("pcm")
        elif codec == "h264":
            # H.264 only supports 8-bit 4:2:0 on Apple's hardware encoder
            self._bit_depth_combo.setCurrentText("8")
            self._bit_depth_combo.setEnabled(False)
            self._chroma_combo.setCurrentText("420")
            self._chroma_combo.setEnabled(False)
        else:
            # h265 supports both 8 and 10-bit
            self._bit_depth_combo.setEnabled(True)
            self._chroma_combo.setEnabled(True)

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
            "codec": self._codec_combo.currentText(),
            "resolution": self._resolution_combo.currentText(),
            "container": self._container_combo.currentText(),
            "bit_depth": self._bit_depth_combo.currentText(),
            "chroma": self._chroma_combo.currentText(),
            "color_space": self._color_space_combo.currentText(),
            "bitrate": self._bitrate_edit.text(),
            "output": self._output_edit.text(),
            "audio_codec": self._audio_codec_combo.currentText(),
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
        """Start a preview-only capture session (no recording)."""
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

        self._preview_widget.attach_session(self._session)
        self._session.startRunning()
        self._previewing = True
        self._statusbar.showMessage(f"Preview: {device.localizedName()}")

    def _stop_preview(self):
        """Stop the preview-only session."""
        if self._session and self._previewing:
            self._session.stopRunning()
            self._session = None
            self._previewing = False

    def _toggle_recording(self):
        if self._recording:
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self):
        """Build config, create recorder, and start recording."""
        # Stop preview session — the recorder creates its own
        self._stop_preview()

        try:
            cfg = self._build_config()
        except (ValueError, SystemExit) as e:
            self._statusbar.showMessage(f"Config error: {e}")
            self._start_preview()
            return

        self._recorder = Recorder(cfg)

        device = self._get_selected_video_device()
        audio_device = self._get_selected_audio_device()

        if not device:
            self._statusbar.showMessage("No video device selected")
            self._start_preview()
            return

        self._recorder.setup_session(device, audio_device)
        self._recorder.setup_writer()

        # Attach preview to the recorder's session
        self._preview_widget.attach_session(self._recorder.session)

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
        """Called on the main thread when recording finishes."""
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
        self._record_btn.setText("Start Recording")
        self._record_btn.setStyleSheet("")
        self._set_settings_enabled(True)

        # Restart preview
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
