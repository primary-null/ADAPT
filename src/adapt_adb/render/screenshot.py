"""Capture and scale device screenshots via `adb exec-out screencap -p`."""

from __future__ import annotations

import base64
import io
import subprocess
from pathlib import Path
from typing import Any

from PIL import Image


def scale_png_to_jpeg(
    png_bytes: bytes,
    max_dimension: int = 1024,
    quality: int = 80,
) -> tuple[bytes, int, int, int]:
    """Decode raw PNG bytes, scale longest side to <= max_dimension, and encode as JPEG.

    Returns (jpeg_bytes, width, height, clamped_max_dimension).
    """
    clamped_max = max(256, min(int(max_dimension), 1280))
    clamped_quality = max(40, min(int(quality), 90))

    with Image.open(io.BytesIO(png_bytes)) as img:
        rgb = img.convert("RGB")
        width, height = rgb.size
        longest = max(width, height)
        if longest > clamped_max:
            scale = clamped_max / float(longest)
            new_width = max(1, round(width * scale))
            new_height = max(1, round(height * scale))
            resample = getattr(Image, "Resampling", Image).LANCZOS
            rgb = rgb.resize((new_width, new_height), resample)
        else:
            new_width, new_height = width, height

        out = io.BytesIO()
        rgb.save(out, format="JPEG", quality=clamped_quality)
        return out.getvalue(), new_width, new_height, clamped_max


def capture_screenshot_jpeg(
    adb_path: Path,
    serial: str,
    max_dimension: int = 1024,
    quality: int = 80,
    runner: Any = subprocess.run,
) -> tuple[str, dict[str, int]]:
    """Run `adb -s <serial> exec-out screencap -p` in binary mode and scale to JPEG.

    Returns (base64_jpeg_string, metadata_dict).
    """
    if not serial:
        raise ValueError("Target device serial is required for screenshot.")

    proc = runner(
        [str(adb_path), "-s", serial, "exec-out", "screencap", "-p"],
        capture_output=True,
        text=False,
        check=False,
        timeout=25,
    )
    raw_out = proc.stdout if isinstance(proc.stdout, bytes) else (proc.stdout or "").encode("latin1")
    if proc.returncode != 0 or not raw_out:
        err_bytes = proc.stderr if isinstance(proc.stderr, bytes) else (proc.stderr or "").encode("utf-8", errors="replace")
        err_msg = err_bytes.decode("utf-8", errors="replace").strip() or "screencap failed"
        raise RuntimeError(err_msg)

    jpeg_bytes, width, height, clamped_max = scale_png_to_jpeg(
        raw_out,
        max_dimension=max_dimension,
        quality=quality,
    )
    b64_data = base64.b64encode(jpeg_bytes).decode("ascii")
    meta = {
        "width": width,
        "height": height,
        "max_dimension": clamped_max,
    }
    return b64_data, meta
