#!/usr/bin/env python3
"""List all VideoToolbox encoders available on this system."""

import ctypes
import objc

vt = ctypes.cdll.LoadLibrary(
    "/System/Library/Frameworks/VideoToolbox.framework/VideoToolbox"
)

encoders_ptr = ctypes.c_void_p()
status = vt.VTCopyVideoEncoderList(None, ctypes.byref(encoders_ptr))
if status != 0:
    print(f"VTCopyVideoEncoderList failed with status {status}")
    raise SystemExit(1)

arr = objc.objc_object(c_void_p=encoders_ptr)

print(f"{'FourCC':<8} {'Codec':<28} {'Encoder'}")
print("-" * 72)
for enc in arr:
    codec_type = enc.get("CodecType", 0)
    if isinstance(codec_type, int) and codec_type > 0xFF:
        fourcc = codec_type.to_bytes(4, "big").decode("ascii", errors="replace")
    else:
        fourcc = str(codec_type)
    codec_name = enc.get("CodecName", "?")
    encoder_name = enc.get("EncoderName", "?")
    print(f"{fourcc:<8} {codec_name:<28} {encoder_name}")

print(f"\n{len(arr)} encoders found.")
