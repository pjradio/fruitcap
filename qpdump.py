#!/usr/bin/env python3
"""Dump per-frame QP (quantization parameter) values from H.264/H.265 files.

Uses ffmpeg's trace_headers bitstream filter to extract slice-level QP from
the bitstream without full decoding. For frames with multiple slices, the
average slice QP is reported.

Usage:
    ./qpdump.py capture.mp4
    ./qpdump.py --detailed capture.mp4
    ./qpdump.py --csv capture.mp4 > frames.csv
"""

import argparse
import re
import subprocess
import sys


# H.264 slice types: 0=P, 1=B, 2=I (and 5=P, 6=B, 7=I for SI/SP)
H264_SLICE_TYPES = {0: "P", 1: "B", 2: "I", 3: "SP", 4: "SI",
                    5: "P", 6: "B", 7: "I", 8: "SP", 9: "SI"}

# HEVC slice types: 0=B, 1=P, 2=I
HEVC_SLICE_TYPES = {0: "B", 1: "P", 2: "I"}


def detect_codec(path):
    """Detect video codec using ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name", "-of", "csv=p=0", path],
        capture_output=True, text=True
    )
    codec = result.stdout.strip()
    if codec in ("h264", "hevc"):
        return codec
    print(f"Error: unsupported codec '{codec}' (only h264 and hevc supported)",
          file=sys.stderr)
    sys.exit(1)


def get_duration(path):
    """Get video duration in seconds using ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
         "-show_entries", "stream=duration", "-of", "csv=p=0", path],
        capture_output=True, text=True
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        # Fall back to format-level duration (e.g. for MKV)
        result = subprocess.run(
            ["ffprobe", "-v", "quiet",
             "-show_entries", "format=duration", "-of", "csv=p=0", path],
            capture_output=True, text=True
        )
        try:
            return float(result.stdout.strip())
        except ValueError:
            return None


def parse_trace_output(stderr_text, codec):
    """Parse ffmpeg trace_headers output into per-frame QP data."""
    frames = []
    pic_init_qp = 26  # default per spec (pic_init_qp_minus26 = 0)
    current_slice_type = None
    current_slice_qps = []
    current_pkt_size = None
    current_is_key = False

    for line in stderr_text.splitlines():
        # PPS: pic_init_qp_minus26 (H.264) or init_qp_minus26 (HEVC)
        m = re.search(r'(?:pic_init|init)_qp_minus26\s+\S+\s+=\s+(-?\d+)', line)
        if m:
            pic_init_qp = 26 + int(m.group(1))
            continue

        # Slice type
        m = re.search(r'slice_type\s+\S+\s+=\s+(\d+)', line)
        if m:
            st = int(m.group(1))
            if codec == "h264":
                current_slice_type = H264_SLICE_TYPES.get(st, "?")
            else:
                current_slice_type = HEVC_SLICE_TYPES.get(st, "?")
            continue

        # Slice QP delta
        m = re.search(r'slice_qp_delta\s+\S+\s+=\s+(-?\d+)', line)
        if m:
            qp = pic_init_qp + int(m.group(1))
            current_slice_qps.append(qp)
            continue

        # Packet line marks end of a frame/packet
        m = re.search(r'Packet:\s+(\d+)\s+bytes,?\s*(key frame,?)?\s*pts\s+(-?\d+)', line)
        if m:
            pkt_size = int(m.group(1))
            is_key = m.group(2) is not None
            pts = int(m.group(3))

            if current_slice_qps:
                avg_qp = sum(current_slice_qps) / len(current_slice_qps)
                frames.append({
                    "pts": pts,
                    "type": current_slice_type or ("I" if is_key else "?"),
                    "qp": avg_qp,
                    "qp_min": min(current_slice_qps),
                    "qp_max": max(current_slice_qps),
                    "slices": len(current_slice_qps),
                    "size": pkt_size,
                    "key": is_key,
                })
            current_slice_qps = []
            current_slice_type = None
            continue

    # Sort by PTS for display-order output
    frames.sort(key=lambda f: f["pts"])
    return frames


def run_trace(path):
    """Run ffmpeg with trace_headers BSF and return stderr."""
    result = subprocess.run(
        ["ffmpeg", "-v", "verbose", "-i", path,
         "-c:v", "copy", "-bsf:v", "trace_headers",
         "-an", "-f", "null", "-"],
        capture_output=True, text=True
    )
    return result.stderr


def print_table(frames):
    """Print frames as a formatted table."""
    print(f"{'Frame':>6}  {'Type':>4}  {'QP':>5}  {'Size':>10}  {'Bits/pixel':>10}")
    print("-" * 50)
    for i, f in enumerate(frames):
        print(f"{i:6d}  {f['type']:>4}  {f['qp']:5.1f}  "
              f"{f['size']:>10,}  ")


def print_csv(frames):
    """Print frames as CSV."""
    print("frame,type,qp,qp_min,qp_max,slices,size_bytes,keyframe")
    for i, f in enumerate(frames):
        print(f"{i},{f['type']},{f['qp']:.1f},{f['qp_min']},"
              f"{f['qp_max']},{f['slices']},{f['size']},"
              f"{'1' if f['key'] else '0'}")


def format_bitrate(bps):
    """Format bits per second as a human-readable string."""
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.2f} Mbps"
    elif bps >= 1_000:
        return f"{bps / 1_000:.0f} kbps"
    else:
        return f"{bps:.0f} bps"


def print_summary(frames, duration=None):
    """Print QP statistics summary."""
    if not frames:
        print("No frames found.")
        return

    all_qps = [f["qp"] for f in frames]
    total_size = sum(f["size"] for f in frames)

    print(f"Total frames: {len(frames)}")
    size_line = f"Total size:   {total_size:,} bytes ({total_size / 1024 / 1024:.1f} MB)"
    if duration and duration > 0:
        avg_bitrate = (total_size * 8) / duration
        size_line += f"  avg bitrate: {format_bitrate(avg_bitrate)}"
    print(size_line)
    print(f"Overall QP:   min={min(all_qps):.1f}  avg={sum(all_qps)/len(all_qps):.1f}  max={max(all_qps):.1f}")

    for frame_type in ("I", "P", "B"):
        typed = [f for f in frames if f["type"] == frame_type]
        if not typed:
            continue
        qps = [f["qp"] for f in typed]
        sizes = [f["size"] for f in typed]
        print(f"  {frame_type}-frames:  {len(typed):5d}  "
              f"QP min={min(qps):.1f} avg={sum(qps)/len(qps):.1f} max={max(qps):.1f}  "
              f"avg size={sum(sizes)//len(sizes):,} bytes")


def main():
    parser = argparse.ArgumentParser(
        description="Dump per-frame QP values from H.264/H.265 files")
    parser.add_argument("input", help="Input video file")
    parser.add_argument("--csv", action="store_true",
                        help="Output as CSV")
    parser.add_argument("--detailed", action="store_true",
                        help="Show per-frame table with summary")
    args = parser.parse_args()

    codec = detect_codec(args.input)
    duration = get_duration(args.input)
    print(f"Codec: {codec}", file=sys.stderr)
    stderr = run_trace(args.input)
    frames = parse_trace_output(stderr, codec)

    if not frames:
        print("No frames with QP data found.", file=sys.stderr)
        sys.exit(1)

    if args.csv:
        print_csv(frames)
    elif args.detailed:
        print_table(frames)
        print_summary(frames, duration)
    else:
        print_summary(frames, duration)


if __name__ == "__main__":
    main()
