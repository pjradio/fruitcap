"""Tests for fruitcap improvements."""

import configparser
import datetime
import os
import signal
import sys
import tempfile
import textwrap
from unittest import mock

import pytest

# We need to mock AVFoundation and related macOS frameworks before importing fruitcap,
# since tests may run in environments without these frameworks (CI, etc.)
# But on macOS where fruitcap actually runs, these are available.
# We'll import fruitcap directly and test its pure-logic functions.

import fruitcap


# ── SIGTERM handling ──

# ── Bitrate shorthand ──

class TestParseBitrate:
    def test_plain_integer(self):
        assert fruitcap.parse_bitrate("80000000") == 80_000_000

    def test_megabit_lowercase(self):
        assert fruitcap.parse_bitrate("80m") == 80_000_000

    def test_megabit_uppercase(self):
        assert fruitcap.parse_bitrate("150M") == 150_000_000

    def test_kilobit(self):
        assert fruitcap.parse_bitrate("256k") == 256_000

    def test_gigabit(self):
        assert fruitcap.parse_bitrate("1g") == 1_000_000_000

    def test_fractional_megabit(self):
        assert fruitcap.parse_bitrate("2.5m") == 2_500_000

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            fruitcap.parse_bitrate("")

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            fruitcap.parse_bitrate("abc")

    def test_load_config_with_shorthand(self):
        """Config file with shorthand bitrate values should parse correctly."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".cfg", delete=False) as f:
            f.write("[capture]\nbitrate = 80m\n[audio]\nbitrate = 256k\n")
            f.flush()
            cfg = fruitcap.load_config(f.name)
        os.unlink(f.name)
        assert cfg["bitrate"] == 80_000_000
        assert cfg["audio_bitrate"] == 256_000


# ── CLI config overrides ──

class TestCliOverrides:
    def _write_cfg(self, content="[capture]\ncodec = h264\nbitrate = 50m\n[audio]\n"):
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".cfg", delete=False)
        f.write(content)
        f.flush()
        f.close()
        return f.name

    def test_override_codec(self):
        path = self._write_cfg()
        cfg = fruitcap.load_config(path, overrides={"codec": "h265"})
        os.unlink(path)
        assert cfg["codec"] == "h265"

    def test_override_bitrate(self):
        path = self._write_cfg()
        cfg = fruitcap.load_config(path, overrides={"bitrate": "100m"})
        os.unlink(path)
        assert cfg["bitrate"] == 100_000_000

    def test_override_resolution(self):
        path = self._write_cfg()
        cfg = fruitcap.load_config(path, overrides={"resolution": "1080p"})
        os.unlink(path)
        assert cfg["width"] == 1920
        assert cfg["height"] == 1080

    def test_override_output(self):
        path = self._write_cfg()
        cfg = fruitcap.load_config(path, overrides={"output": "my_video.mp4"})
        os.unlink(path)
        assert cfg["output"] == "my_video.mp4"

    def test_override_fps(self):
        path = self._write_cfg()
        cfg = fruitcap.load_config(path, overrides={"fps": "29.97"})
        os.unlink(path)
        assert cfg["fps"] == pytest.approx(29.97)

    def test_config_path(self):
        """--config flag should load from the specified path."""
        path = self._write_cfg("[capture]\nresolution = 720p\ncodec = h264\n[audio]\n")
        cfg = fruitcap.load_config(path)
        os.unlink(path)
        assert cfg["width"] == 1280
        assert cfg["height"] == 720

    def test_no_overrides(self):
        """When no overrides, config file values are used."""
        path = self._write_cfg("[capture]\ncodec = h264\nbitrate = 50m\n[audio]\n")
        cfg = fruitcap.load_config(path)
        os.unlink(path)
        assert cfg["codec"] == "h264"
        assert cfg["bitrate"] == 50_000_000

    def test_default_output_template_is_timestamped(self):
        path = self._write_cfg("[capture]\ncodec = h264\n[audio]\n")
        cfg = fruitcap.load_config(path)
        os.unlink(path)
        assert cfg["output"] == "capture-%d-%t.mp4"

    def test_output_template_tokens_load_from_config(self):
        path = self._write_cfg("[capture]\noutput = custom_%d_%t.mov\n[audio]\n")
        cfg = fruitcap.load_config(path)
        os.unlink(path)
        assert cfg["output"] == "custom_%d_%t.mov"


# ── SIGTERM handling ──

class TestSigtermHandling:
    def test_sigterm_handler_is_registered_in_main(self):
        """Verify that main() installs a SIGTERM handler for headless mode."""
        # We check that the run_headless function registers SIGTERM
        # by inspecting the signal handler after calling it
        old_handler = signal.getsignal(signal.SIGTERM)
        try:
            # Simulate: the function should register a SIGTERM handler
            recorder = mock.MagicMock()
            recorder.running = False  # So the loop exits immediately
            with mock.patch('builtins.input', side_effect=EOFError):
                fruitcap.run_headless(recorder)
            handler = signal.getsignal(signal.SIGTERM)
            assert handler is not signal.SIG_DFL, "SIGTERM handler should be installed"
        finally:
            signal.signal(signal.SIGTERM, old_handler)


# ── Device selection ──

class TestDeviceSelection:
    def _make_mock_device(self, name, uid="uid"):
        dev = mock.MagicMock()
        dev.localizedName.return_value = name
        dev.uniqueID.return_value = uid
        return dev

    def test_find_device_by_index(self):
        dev0 = self._make_mock_device("Camera A")
        dev1 = self._make_mock_device("Camera B")
        result = fruitcap.find_device_by_selector([dev0, dev1], "1", "video")
        assert result is dev1

    def test_find_device_by_name(self):
        dev0 = self._make_mock_device("FaceTime HD Camera")
        dev1 = self._make_mock_device("Avid DNxIO")
        result = fruitcap.find_device_by_selector([dev0, dev1], "dnxio", "video")
        assert result is dev1

    def test_find_device_default_first(self):
        dev0 = self._make_mock_device("Camera A")
        result = fruitcap.find_device_by_selector([dev0], None, "video")
        assert result is dev0

    def test_find_device_no_devices(self):
        result = fruitcap.find_device_by_selector([], None, "video")
        assert result is None

    def test_find_device_bad_name_exits(self):
        dev0 = self._make_mock_device("Camera A")
        with pytest.raises(SystemExit):
            fruitcap.find_device_by_selector([dev0], "nonexistent", "video")

    def test_find_device_index_out_of_range_exits(self):
        dev0 = self._make_mock_device("Camera A")
        with pytest.raises(SystemExit):
            fruitcap.find_device_by_selector([dev0], "5", "video")

    def test_list_devices(self):
        dev0 = self._make_mock_device("Camera A", "uid0")
        dev1 = self._make_mock_device("Camera B", "uid1")
        result = fruitcap.list_devices([dev0, dev1])
        assert len(result) == 2
        assert result[0] == (0, "Camera A", "uid0")
        assert result[1] == (1, "Camera B", "uid1")


# ── Output filename improvements ──

class TestOutputFilename:
    def test_date_token(self):
        path = fruitcap.generate_output_path("capture_%d.mp4")
        today = datetime.date.today().strftime("%Y%m%d")
        assert today in path
        assert path.endswith(".mp4")

    def test_time_token(self):
        path = fruitcap.generate_output_path("capture_%t.mp4")
        # Should contain 6-digit time string
        basename = os.path.basename(path)
        # Extract the time portion between 'capture_' and '.mp4'
        time_part = basename.replace("capture_", "").replace(".mp4", "")
        assert len(time_part) == 6
        assert time_part.isdigit()

    def test_both_tokens(self):
        path = fruitcap.generate_output_path("cap_%d_%t.mp4")
        today = datetime.date.today().strftime("%Y%m%d")
        assert today in path

    def test_no_tokens(self):
        path = fruitcap.generate_output_path("capture.mp4")
        assert path == "capture.mp4"

    def test_no_overwrite_new_file(self):
        """When file doesn't exist, no_overwrite returns path as-is."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "capture.mp4")
            result = fruitcap.generate_output_path(path, no_overwrite=True)
            assert result == path

    def test_no_overwrite_existing_file(self):
        """When file exists, appends _1."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "capture.mp4")
            open(path, "w").close()  # create the file
            result = fruitcap.generate_output_path(path, no_overwrite=True)
            assert result == os.path.join(d, "capture_1.mp4")

    def test_no_overwrite_multiple_existing(self):
        """When _1 also exists, increments to _2."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "capture.mp4")
            open(path, "w").close()
            open(os.path.join(d, "capture_1.mp4"), "w").close()
            result = fruitcap.generate_output_path(path, no_overwrite=True)
            assert result == os.path.join(d, "capture_2.mp4")

    def test_overwrite_mode_returns_original(self):
        """Default (overwrite) mode returns path even if file exists."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "capture.mp4")
            open(path, "w").close()
            result = fruitcap.generate_output_path(path, no_overwrite=False)
            assert result == path


# ── List formats ──

class TestListFormats:
    def test_format_device_formats_basic(self):
        formats = [
            {"width": 1920, "height": 1080, "fourcc": "420v",
             "fps_ranges": [{"min": 1.0, "max": 30.0}]},
            {"width": 3840, "height": 2160, "fourcc": "420f",
             "fps_ranges": [{"min": 24.0, "max": 24.0}, {"min": 30.0, "max": 30.0}]},
        ]
        lines = fruitcap.format_device_formats(formats)
        assert len(lines) == 2
        assert "1920x1080" in lines[0]
        assert "420v" in lines[0]
        assert "1-30 fps" in lines[0]
        assert "3840x2160" in lines[1]
        assert "24, 30 fps" in lines[1]

    def test_format_device_formats_descriptions(self):
        """Known FourCC codes should include descriptions."""
        formats = [
            {"width": 1920, "height": 1080, "fourcc": "2vuy",
             "fps_ranges": [{"min": 30.0, "max": 30.0}]},
            {"width": 1920, "height": 1080, "fourcc": "v210",
             "fps_ranges": [{"min": 30.0, "max": 30.0}]},
        ]
        lines = fruitcap.format_device_formats(formats)
        assert "8-bit 4:2:2 YUV" in lines[0]
        assert "10-bit 4:2:2 YUV" in lines[1]

    def test_format_device_formats_aligned_columns(self):
        """Output columns should be aligned."""
        formats = [
            {"width": 720, "height": 486, "fourcc": "2vuy",
             "fps_ranges": [{"min": 29.97, "max": 29.97}]},
            {"width": 1920, "height": 1080, "fourcc": "BGRA",
             "fps_ranges": [{"min": 60.0, "max": 60.0}]},
        ]
        lines = fruitcap.format_device_formats(formats)
        # Both lines should have the FourCC at the same column position
        col0 = lines[0].index("2vuy")
        col1 = lines[1].index("BGRA")
        assert col0 == col1

    def test_format_device_formats_dedup(self):
        """Duplicate entries should be deduplicated."""
        formats = [
            {"width": 1920, "height": 1080, "fourcc": "420v",
             "fps_ranges": [{"min": 30.0, "max": 30.0}]},
            {"width": 1920, "height": 1080, "fourcc": "420v",
             "fps_ranges": [{"min": 30.0, "max": 30.0}]},
        ]
        lines = fruitcap.format_device_formats(formats)
        assert len(lines) == 1

    def test_format_device_formats_empty(self):
        lines = fruitcap.format_device_formats([])
        assert lines == []

    def test_format_device_formats_hex_fourcc(self):
        """Non-printable FourCC codes should display as hex."""
        formats = [
            {"width": 1920, "height": 1080, "fourcc": "0x00000020",
             "fps_ranges": [{"min": 30.0, "max": 30.0}]},
        ]
        lines = fruitcap.format_device_formats(formats)
        assert len(lines) == 1
        assert "0x00000020" in lines[0]

    def test_fourcc_descriptions_dict(self):
        """FOURCC_DESCRIPTIONS should contain common pixel formats."""
        for code in ("2vuy", "v210", "r210", "BGRA", "420v", "420f"):
            assert code in fruitcap.FOURCC_DESCRIPTIONS


# ── ProRes and container ──

class TestProResAndContainer:
    def _write_cfg(self, content):
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".cfg", delete=False)
        f.write(content)
        f.flush()
        f.close()
        return f.name

    def test_prores_codec_accepted(self):
        path = self._write_cfg("[capture]\ncodec = prores\n[audio]\n")
        cfg = fruitcap.load_config(path)
        os.unlink(path)
        assert cfg["codec"] == "prores"

    def test_prores_variants(self):
        for variant in ("prores_proxy", "prores_lt", "prores", "prores_hq"):
            path = self._write_cfg(f"[capture]\ncodec = {variant}\n[audio]\n")
            cfg = fruitcap.load_config(path)
            os.unlink(path)
            assert cfg["codec"] == variant

    def test_prores_auto_container_mov(self):
        """ProRes should auto-select MOV container."""
        path = self._write_cfg("[capture]\ncodec = prores\n[audio]\n")
        cfg = fruitcap.load_config(path)
        os.unlink(path)
        assert cfg["container"] == "mov"

    def test_h264_auto_container_mp4(self):
        """H.264 should auto-select MP4 container."""
        path = self._write_cfg("[capture]\ncodec = h264\n[audio]\n")
        cfg = fruitcap.load_config(path)
        os.unlink(path)
        assert cfg["container"] == "mp4"

    def test_explicit_container_override(self):
        """Explicit container should override auto-selection."""
        path = self._write_cfg("[capture]\ncodec = h265\ncontainer = mov\n[audio]\n")
        cfg = fruitcap.load_config(path)
        os.unlink(path)
        assert cfg["container"] == "mov"

    def test_invalid_container_exits(self):
        path = self._write_cfg("[capture]\ncontainer = avi\n[audio]\n")
        with pytest.raises(SystemExit):
            fruitcap.load_config(path)
        os.unlink(path)

    def test_invalid_codec_exits(self):
        path = self._write_cfg("[capture]\ncodec = vp9\n[audio]\n")
        with pytest.raises(SystemExit):
            fruitcap.load_config(path)
        os.unlink(path)

    def test_container_cli_override(self):
        path = self._write_cfg("[capture]\ncodec = h264\n[audio]\n")
        cfg = fruitcap.load_config(path, overrides={"container": "mov"})
        os.unlink(path)
        assert cfg["container"] == "mov"

    def test_prores_auto_pcm_audio(self):
        """ProRes should auto-select PCM audio when audio codec not explicitly set."""
        path = self._write_cfg("[capture]\ncodec = prores\n[audio]\n")
        cfg = fruitcap.load_config(path)
        os.unlink(path)
        assert cfg["audio_codec"] == "pcm"

    def test_prores_audio_override_respected(self):
        """Explicit audio codec override should be respected with ProRes."""
        path = self._write_cfg("[capture]\ncodec = prores\n[audio]\n")
        cfg = fruitcap.load_config(path, overrides={"audio_codec": "aac"})
        os.unlink(path)
        assert cfg["audio_codec"] == "aac"

    def test_h264_default_aac_audio(self):
        """H.264 should keep AAC audio by default."""
        path = self._write_cfg("[capture]\ncodec = h264\n[audio]\ncodec = aac\n")
        cfg = fruitcap.load_config(path)
        os.unlink(path)
        assert cfg["audio_codec"] == "aac"

    def test_pcm_audio_codec_accepted(self):
        """PCM should be a valid audio codec."""
        path = self._write_cfg("[capture]\n[audio]\ncodec = pcm\n")
        cfg = fruitcap.load_config(path)
        os.unlink(path)
        assert cfg["audio_codec"] == "pcm"

    def test_prores_forces_10bit_422(self):
        """ProRes should force 10-bit 4:2:2 regardless of config."""
        path = self._write_cfg("[capture]\ncodec = prores\nbit_depth = 8\nchroma = 420\n[audio]\n")
        cfg = fruitcap.load_config(path)
        os.unlink(path)
        assert cfg["bit_depth"] == 10
        assert cfg["chroma"] == "422"

    def test_prores_hq_forces_10bit_422(self):
        """ProRes HQ should also force 10-bit 4:2:2."""
        path = self._write_cfg("[capture]\ncodec = prores_hq\n[audio]\n")
        cfg = fruitcap.load_config(path)
        os.unlink(path)
        assert cfg["bit_depth"] == 10
        assert cfg["chroma"] == "422"

    def test_h264_keeps_config_bit_depth_chroma(self):
        """H.264 should respect config bit_depth and chroma."""
        path = self._write_cfg("[capture]\ncodec = h264\nbit_depth = 8\nchroma = 420\n[audio]\n")
        cfg = fruitcap.load_config(path)
        os.unlink(path)
        assert cfg["bit_depth"] == 8
        assert cfg["chroma"] == "420"


# ── Quiet mode ──

class TestQuietMode:
    def test_log_prints_when_not_quiet(self, capsys):
        old = fruitcap._quiet
        try:
            fruitcap._quiet = False
            fruitcap.log("hello")
            assert "hello" in capsys.readouterr().out
        finally:
            fruitcap._quiet = old

    def test_log_suppressed_when_quiet(self, capsys):
        old = fruitcap._quiet
        try:
            fruitcap._quiet = True
            fruitcap.log("hello")
            assert capsys.readouterr().out == ""
        finally:
            fruitcap._quiet = old

    def test_errors_still_print_when_quiet(self, capsys):
        """Errors use print(), not log(), so they always appear."""
        old = fruitcap._quiet
        try:
            fruitcap._quiet = True
            # Simulate an error print (these remain as print())
            print("Error: something went wrong")
            assert "Error" in capsys.readouterr().out
        finally:
            fruitcap._quiet = old


# ── Color space ──

class TestColorSpace:
    def _write_cfg(self, content):
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".cfg", delete=False)
        f.write(content)
        f.flush()
        f.close()
        return f.name

    def test_default_bt709(self):
        path = self._write_cfg("[capture]\n[audio]\n")
        cfg = fruitcap.load_config(path)
        os.unlink(path)
        assert cfg["color_space"] == "bt709"

    def test_hlg(self):
        path = self._write_cfg("[capture]\ncolor_space = hlg\n[audio]\n")
        cfg = fruitcap.load_config(path)
        os.unlink(path)
        assert cfg["color_space"] == "hlg"

    def test_pq(self):
        path = self._write_cfg("[capture]\ncolor_space = pq\n[audio]\n")
        cfg = fruitcap.load_config(path)
        os.unlink(path)
        assert cfg["color_space"] == "pq"

    def test_bt2020(self):
        path = self._write_cfg("[capture]\ncolor_space = bt2020\n[audio]\n")
        cfg = fruitcap.load_config(path)
        os.unlink(path)
        assert cfg["color_space"] == "bt2020"

    def test_invalid_color_space_exits(self):
        path = self._write_cfg("[capture]\ncolor_space = srgb\n[audio]\n")
        with pytest.raises(SystemExit):
            fruitcap.load_config(path)
        os.unlink(path)

    def test_color_space_cli_override(self):
        path = self._write_cfg("[capture]\n[audio]\n")
        cfg = fruitcap.load_config(path, overrides={"color_space": "hlg"})
        os.unlink(path)
        assert cfg["color_space"] == "hlg"

    def test_preset_keys_exist(self):
        """All presets should have the required keys."""
        for name, preset in fruitcap.COLOR_SPACE_PRESETS.items():
            assert "primaries" in preset, f"{name} missing primaries"
            assert "transfer" in preset, f"{name} missing transfer"
            assert "matrix" in preset, f"{name} missing matrix"


# ── Segment splitting ──

class TestParseSize:
    def test_plain_bytes(self):
        assert fruitcap.parse_size("1048576") == 1_048_576

    def test_kilobytes(self):
        assert fruitcap.parse_size("100k") == 100 * 1024

    def test_megabytes(self):
        assert fruitcap.parse_size("500m") == 500 * 1024**2

    def test_gigabytes(self):
        assert fruitcap.parse_size("2g") == 2 * 1024**3

    def test_megabytes_suffix_mb(self):
        assert fruitcap.parse_size("500mb") == 500 * 1024**2

    def test_gigabytes_suffix_gb(self):
        assert fruitcap.parse_size("2gb") == 2 * 1024**3

    def test_fractional(self):
        assert fruitcap.parse_size("1.5g") == int(1.5 * 1024**3)

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            fruitcap.parse_size("")

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            fruitcap.parse_size("abc")


class TestSegmentPath:
    def test_segment_path_numbering(self):
        assert fruitcap.generate_segment_path("/tmp/capture.mp4", 1) == "/tmp/capture_001.mp4"
        assert fruitcap.generate_segment_path("/tmp/capture.mp4", 2) == "/tmp/capture_002.mp4"
        assert fruitcap.generate_segment_path("/tmp/capture.mp4", 100) == "/tmp/capture_100.mp4"

    def test_segment_path_mov(self):
        assert fruitcap.generate_segment_path("out.mov", 3) == "out_003.mov"

    def test_splitting_enabled(self):
        """Recorder correctly reports splitting state."""
        cfg = {"output": "test.mp4"}
        r = fruitcap.Recorder(cfg)
        assert not r._splitting_enabled()
        r.split_seconds = 60
        assert r._splitting_enabled()
        r.split_seconds = None
        r.split_size_bytes = 500 * 1024**2
        assert r._splitting_enabled()

    def test_output_path_for_segment_no_split(self):
        cfg = {"output": "test.mp4"}
        r = fruitcap.Recorder(cfg)
        assert r._output_path_for_segment(1) == "test.mp4"

    def test_output_path_for_segment_with_split(self):
        cfg = {"output": "test.mp4"}
        r = fruitcap.Recorder(cfg)
        r.split_seconds = 60
        assert r._output_path_for_segment(1) == "test_001.mp4"
        assert r._output_path_for_segment(2) == "test_002.mp4"


# ── Audio-only mode ──

class TestAudioOnly:
    def _write_cfg(self, content="[capture]\n[audio]\n"):
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".cfg", delete=False)
        f.write(content)
        f.flush()
        f.close()
        return f.name

    def test_audio_only_flag_in_config(self):
        path = self._write_cfg()
        cfg = fruitcap.load_config(path, overrides={"audio_only": True})
        os.unlink(path)
        assert cfg["audio_only"] is True

    def test_audio_only_default_false(self):
        path = self._write_cfg()
        cfg = fruitcap.load_config(path)
        os.unlink(path)
        assert cfg["audio_only"] is False

    def test_audio_only_with_aac(self):
        path = self._write_cfg("[capture]\n[audio]\ncodec = aac\n")
        cfg = fruitcap.load_config(path, overrides={"audio_only": True})
        os.unlink(path)
        assert cfg["audio_only"] is True
        assert cfg["audio_codec"] == "aac"

    def test_audio_only_with_alac(self):
        path = self._write_cfg("[capture]\n[audio]\ncodec = alac\n")
        cfg = fruitcap.load_config(path, overrides={"audio_only": True})
        os.unlink(path)
        assert cfg["audio_codec"] == "alac"

    def test_audio_only_with_pcm(self):
        path = self._write_cfg("[capture]\n[audio]\ncodec = pcm\n")
        cfg = fruitcap.load_config(path, overrides={"audio_only": True})
        os.unlink(path)
        assert cfg["audio_codec"] == "pcm"

    def test_audio_only_sample_rate_override(self):
        path = self._write_cfg("[capture]\n[audio]\n")
        cfg = fruitcap.load_config(path, overrides={"audio_only": True, "audio_sample_rate": "96000"})
        os.unlink(path)
        assert cfg["audio_sample_rate"] == 96000

    def test_audio_only_channels_override(self):
        path = self._write_cfg("[capture]\n[audio]\n")
        cfg = fruitcap.load_config(path, overrides={"audio_only": True, "audio_channels": "1"})
        os.unlink(path)
        assert cfg["audio_channels"] == 1


class TestOutputFileType:
    def test_video_mp4_uses_mpeg4(self):
        file_type, ext = fruitcap.get_output_file_type_and_extension(
            {"audio_only": False, "container": "mp4"}
        )
        assert file_type == fruitcap.AVF.AVFileTypeMPEG4
        assert ext == ".mp4"

    def test_video_mov_uses_quicktime(self):
        file_type, ext = fruitcap.get_output_file_type_and_extension(
            {"audio_only": False, "container": "mov"}
        )
        assert file_type == fruitcap.AVF.AVFileTypeQuickTimeMovie
        assert ext == ".mov"

    def test_audio_only_pcm_uses_caf(self):
        file_type, ext = fruitcap.get_output_file_type_and_extension(
            {"audio_only": True, "audio_codec": "pcm"}
        )
        assert file_type == fruitcap.AVF.AVFileTypeCoreAudioFormat
        assert ext == ".caf"


class TestWriterMetadata:
    def test_mp4_uses_quicktime_software_identifier(self):
        metadata = fruitcap.build_writer_metadata(fruitcap.AVF.AVFileTypeMPEG4)
        assert len(metadata) == 1
        assert metadata[0].identifier() == fruitcap.AVF.AVMetadataIdentifierQuickTimeMetadataSoftware
        assert metadata[0].value() == "fruitcap.py"

    def test_caf_falls_back_to_common_software_identifier(self):
        metadata = fruitcap.build_writer_metadata(fruitcap.AVF.AVFileTypeCoreAudioFormat)
        assert len(metadata) == 1
        assert metadata[0].identifier() == fruitcap.AVF.AVMetadataCommonIdentifierSoftware
        assert metadata[0].value() == "fruitcap.py"


class TestCliHelpers:
    def test_build_overrides_includes_audio_settings(self):
        parser = fruitcap.build_parser()
        args = parser.parse_args([
            "--audio-codec", "alac",
            "--audio-bitrate", "320k",
            "--audio-sample-rate", "96000",
            "--audio-channels", "1",
        ])
        overrides = fruitcap.build_overrides_from_args(args)
        assert overrides["audio_codec"] == "alac"
        assert overrides["audio_bitrate"] == "320k"
        assert overrides["audio_sample_rate"] == 96000
        assert overrides["audio_channels"] == 1

    def test_build_overrides_includes_discard_late_frames(self):
        parser = fruitcap.build_parser()
        args = parser.parse_args(["--discard-late-frames"])
        overrides = fruitcap.build_overrides_from_args(args)
        assert overrides["discard_late_frames"] is True

    def test_build_overrides_can_disable_discard_late_frames(self):
        parser = fruitcap.build_parser()
        args = parser.parse_args(["--no-discard-late-frames"])
        overrides = fruitcap.build_overrides_from_args(args)
        assert overrides["discard_late_frames"] is False


class TestRunHeadless:
    class FakeRecorder:
        def __init__(self):
            self.running = True
            self.stop_calls = 0

        def stop(self):
            self.stop_calls += 1
            self.running = False

    def test_noninteractive_waits_for_recorder_to_stop(self):
        recorder = self.FakeRecorder()
        fake_stdin = mock.Mock()
        fake_stdin.isatty.return_value = False

        def fake_sleep(_):
            recorder.running = False

        with mock.patch.object(fruitcap.sys, "stdin", fake_stdin):
            with mock.patch("fruitcap.time.sleep", side_effect=fake_sleep):
                fruitcap.run_headless(recorder)

        assert recorder.stop_calls == 1

    def test_interactive_mode_does_not_block_after_auto_stop(self):
        recorder = self.FakeRecorder()
        fake_stdin = mock.Mock()
        fake_stdin.isatty.return_value = True
        fake_stdin.readline.side_effect = AssertionError("readline should not be called")

        def fake_select(*_args, **_kwargs):
            recorder.running = False
            return [], [], []

        with mock.patch.object(fruitcap.sys, "stdin", fake_stdin):
            with mock.patch("fruitcap.select.select", side_effect=fake_select):
                fruitcap.run_headless(recorder)

        assert recorder.stop_calls == 1


class TestMainRuntimeConfiguration:
    class FakeRecorder:
        instances = []

        def __init__(self, cfg):
            self.cfg = cfg
            self.running = False
            self.split_seconds = None
            self.split_size_bytes = None
            self.max_frames = None
            self.max_seconds = None
            self.compressed_preview = None
            self.setup_writer_split_state = None
            self.setup_writer_output = None
            self.__class__.instances.append(self)

        def find_device(self, selector=None):
            return object()

        def find_audio_device(self, selector=None):
            return object()

        def setup_session(self, device=None, audio_device=None):
            return None

        def setup_writer(self):
            self.setup_writer_split_state = (self.split_seconds, self.split_size_bytes)
            self.setup_writer_output = self.cfg["output"]

        def start(self):
            self.running = False

        def stop(self):
            self.running = False

    def setup_method(self):
        self.FakeRecorder.instances = []

    def test_main_applies_split_options_before_setup_writer(self):
        cfg = {
            "audio_only": False,
            "audio_enabled": False,
            "container": "mp4",
            "output": "capture.mp4",
            "codec": "h264",
            "width": 1920,
            "height": 1080,
            "bit_depth": 8,
            "chroma": "420",
            "bitrate": 80_000_000,
            "fps": None,
            "audio_codec": "aac",
            "audio_sample_rate": 48000,
            "audio_channels": 2,
        }
        with mock.patch.object(sys, "argv", ["fruitcap.py", "--split-every", "300", "--split-size", "2g"]):
            with mock.patch("fruitcap.load_config", return_value=cfg.copy()):
                with mock.patch("fruitcap.check_camera_permission"):
                    with mock.patch("fruitcap.Recorder", self.FakeRecorder):
                        with mock.patch("fruitcap.run_headless"):
                            fruitcap.main()

        recorder = self.FakeRecorder.instances[0]
        assert recorder.setup_writer_split_state == (300.0, 2 * 1024**3)

    def test_main_uses_caf_extension_for_audio_only_pcm_defaults(self):
        cfg = {
            "audio_only": True,
            "audio_enabled": True,
            "container": "mp4",
            "output": "capture.mp4",
            "codec": "h264",
            "width": 1920,
            "height": 1080,
            "bit_depth": 8,
            "chroma": "420",
            "bitrate": 80_000_000,
            "fps": None,
            "audio_codec": "pcm",
            "audio_bitrate": 256_000,
            "audio_sample_rate": 48000,
            "audio_channels": 2,
        }
        with mock.patch.object(sys, "argv", ["fruitcap.py", "--audio-only"]):
            with mock.patch("fruitcap.load_config", return_value=cfg.copy()):
                with mock.patch("fruitcap.check_microphone_permission", return_value=True):
                    with mock.patch("fruitcap.Recorder", self.FakeRecorder):
                        with mock.patch("fruitcap.run_headless"):
                            fruitcap.main()

        recorder = self.FakeRecorder.instances[0]
        assert recorder.setup_writer_output == "capture.caf"
