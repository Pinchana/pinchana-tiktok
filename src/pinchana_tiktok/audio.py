"""Raise quiet TikTok audio to a consistent level without re-encoding video."""

import asyncio
import json
import logging
import math
from pathlib import Path

logger = logging.getLogger(__name__)
TARGET_LUFS = -14.0
TRUE_PEAK_DB = -1.0


async def _run(arguments: list[str], timeout: float = 90) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-nostdin", "-nostats", *arguments,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.communicate()
        raise
    return process.returncode, stderr.decode(errors="replace")


async def measure_loudness(source: Path) -> dict | None:
    code, output = await _run([
        "-hide_banner", "-i", str(source), "-map", "0:a:0", "-vn",
        "-af", f"loudnorm=I={TARGET_LUFS}:TP={TRUE_PEAK_DB}:LRA=11:print_format=json",
        "-f", "null", "-",
    ])
    if code:
        return None  # Silent/no-audio posts and unsupported files stay unchanged.
    try:
        data, _ = json.JSONDecoder().raw_decode(output[output.rfind("{"):])
        names = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
        if not all(math.isfinite(float(data[name])) for name in names):
            return None
        return data
    except (ValueError, KeyError, TypeError):
        return None


async def normalize_video_audio(source: Path) -> Path:
    output = source.with_name(f".{source.stem}.audio-normalized{source.suffix}")
    try:
        measured = await measure_loudness(source)
        if measured is None or float(measured["input_i"]) >= TARGET_LUFS - 0.5:
            return source
        parameters = ":".join([
            f"I={TARGET_LUFS}", f"TP={TRUE_PEAK_DB}", "LRA=11",
            f"measured_I={measured['input_i']}", f"measured_TP={measured['input_tp']}",
            f"measured_LRA={measured['input_lra']}", f"measured_thresh={measured['input_thresh']}",
            f"offset={measured['target_offset']}", "linear=true",
        ])
        code, _ = await _run([
            "-y", "-loglevel", "error", "-i", str(source),
            "-map", "0:v:0", "-map", "0:a:0", "-map_metadata", "0",
            "-c:v", "copy", "-af", "loudnorm=" + parameters,
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", str(output),
        ])
        if code or not output.is_file() or not output.stat().st_size:
            logger.warning("TikTok audio normalization failed; retaining original media")
            return source
        output.replace(source)
        logger.info("TikTok quiet audio normalized input_lufs=%s target_lufs=%s video=copy",
                    measured["input_i"], TARGET_LUFS)
    except (OSError, TimeoutError):
        logger.warning("TikTok audio normalization unavailable; retaining original media")
    finally:
        output.unlink(missing_ok=True)
    return source
