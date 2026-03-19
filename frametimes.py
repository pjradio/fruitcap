#!/usr/bin/env python3
"""Analyze frame timing in H.264/H.265 MP4 files."""

import argparse
import json
import subprocess
import sys


def run_json(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"Error running {cmd[0]}: {r.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    return json.loads(r.stdout)


def get_container_info(path):
    """Get container-level timing info via ffprobe."""
    data = run_json([
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", "-show_format", path
    ])
    video = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    if not video:
        print("No video stream found.", file=sys.stderr)
        sys.exit(1)
    return data["format"], video


def get_frame_durations(path):
    """Get per-frame PTS via ffprobe and compute durations."""
    data = run_json([
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-select_streams", "v:0",
        "-show_entries", "frame=pts_time,duration_time,pkt_duration_time",
        path
    ])
    frames = data.get("frames", [])
    if not frames:
        print("No frames found.", file=sys.stderr)
        sys.exit(1)

    # Try per-frame duration first, fall back to PTS deltas
    durations = []
    for f in frames:
        d = f.get("duration_time") or f.get("pkt_duration_time")
        if d:
            durations.append(float(d))

    if durations:
        return durations, len(frames), "frame duration"

    # Fall back to PTS deltas
    pts = []
    for f in frames:
        t = f.get("pts_time")
        if t is not None:
            pts.append(float(t))
    pts.sort()

    if len(pts) < 2:
        print("Not enough PTS values to compute durations.", file=sys.stderr)
        sys.exit(1)

    durations = [pts[i+1] - pts[i] for i in range(len(pts) - 1)]
    return durations, len(frames), "PTS delta"


def get_mediainfo(path):
    """Get mediainfo summary for the video track."""
    r = subprocess.run(
        ["mediainfo", "--Output=JSON", path],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        return None
    data = json.loads(r.stdout)
    tracks = data.get("media", {}).get("track", [])
    return next((t for t in tracks if t.get("@type") == "Video"), None)


def format_duration(seconds):
    """Format duration in ms and fps."""
    ms = seconds * 1000
    fps = 1.0 / seconds if seconds > 0 else 0
    return f"{ms:8.3f} ms  ({fps:7.3f} fps)"


def main():
    parser = argparse.ArgumentParser(description="Analyze frame timing in MP4 files")
    parser.add_argument("file", help="Input MP4 file")
    args = parser.parse_args()

    fmt, video = get_container_info(args.file)

    # Container / stream info
    codec = video.get("codec_name", "?").upper()
    profile = video.get("profile", "")
    width = video.get("width", "?")
    height = video.get("height", "?")
    r_fps = video.get("r_frame_rate", "")
    avg_fps = video.get("avg_frame_rate", "")
    tb = video.get("codec_time_base") or video.get("time_base", "")
    duration = float(fmt.get("duration", 0))
    nb_frames = video.get("nb_frames", "?")

    def eval_frac(s):
        if "/" in s:
            n, d = s.split("/")
            return float(n) / float(d) if float(d) != 0 else 0
        return float(s) if s else 0

    print(f"\n{'=' * 60}")
    print(f"  {args.file}")
    print(f"{'=' * 60}")
    print(f"  Codec:          {codec} {profile}")
    print(f"  Resolution:     {width}x{height}")
    print(f"  Duration:       {duration:.3f} s")
    print(f"  Frame count:    {nb_frames}")
    print(f"  r_frame_rate:   {r_fps:16s}  ({eval_frac(r_fps):.3f} fps)")
    print(f"  avg_frame_rate: {avg_fps:16s}  ({eval_frac(avg_fps):.3f} fps)")
    print(f"  time_base:      {tb}")

    # mediainfo extras
    mi = get_mediainfo(args.file)
    if mi:
        mi_fps = mi.get("FrameRate", "")
        mi_mode = mi.get("FrameRate_Mode", "")
        mi_min = mi.get("FrameRate_Minimum", "")
        mi_max = mi.get("FrameRate_Maximum", "")
        print(f"\n  mediainfo:")
        print(f"    FrameRate:      {mi_fps} fps ({mi_mode})")
        if mi_min or mi_max:
            print(f"    FrameRate range: {mi_min} – {mi_max} fps")

    # Per-frame analysis
    durations, n_frames, method = get_frame_durations(args.file)

    mn = min(durations)
    mx = max(durations)
    avg = sum(durations) / len(durations)

    print(f"\n  Frame durations (from {method}, {len(durations)} intervals):")
    print(f"    Min: {format_duration(mn)}")
    print(f"    Avg: {format_duration(avg)}")
    print(f"    Max: {format_duration(mx)}")

    # Jitter
    jitter = mx - mn
    print(f"    Jitter (max-min): {jitter*1000:.3f} ms")

    # Distribution: bucket frame durations
    if len(set(f"{d:.6f}" for d in durations)) <= 20:
        print(f"\n  Duration distribution:")
        buckets = {}
        for d in durations:
            key = f"{d*1000:.3f}"
            buckets[key] = buckets.get(key, 0) + 1
        for k, v in sorted(buckets.items(), key=lambda x: float(x[0])):
            fps = 1000.0 / float(k) if float(k) > 0 else 0
            pct = v / len(durations) * 100
            bar = "#" * max(1, int(pct / 2))
            print(f"    {k:>10s} ms ({fps:7.3f} fps): {v:5d} ({pct:5.1f}%) {bar}")
    else:
        print(f"\n  Duration distribution (top 10 of {len(set(f'{d:.6f}' for d in durations))} unique values):")
        buckets = {}
        for d in durations:
            key = f"{d*1000:.3f}"
            buckets[key] = buckets.get(key, 0) + 1
        for k, v in sorted(buckets.items(), key=lambda x: -x[1])[:10]:
            fps = 1000.0 / float(k) if float(k) > 0 else 0
            pct = v / len(durations) * 100
            print(f"    {k:>10s} ms ({fps:7.3f} fps): {v:5d} ({pct:5.1f}%)")

    print()


if __name__ == "__main__":
    main()
