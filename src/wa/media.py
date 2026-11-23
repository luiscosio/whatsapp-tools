"""Prepare an attachment for a live WhatsApp send."""

import subprocess
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from .session import WhatsAppError

MAX_MEDIA_BYTES = 16 * 1024 * 1024
MAX_VIDEO_BYTES = 15 * 1024 * 1024


@contextmanager
def prepared_media(path: Path | None):
    """Re-encode a video that's over WhatsApp's limit to 720p."""
    if path is None or path.stat().st_size <= MAX_VIDEO_BYTES:
        yield path
        return
    if path.suffix.lower() not in (".mp4", ".mov", ".webm"):
        if path.stat().st_size > MAX_MEDIA_BYTES:
            raise WhatsAppError(f"{path.name} exceeds the media size limit")
        yield path
        return
    seconds = float(
        subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    kbps = int(MAX_VIDEO_BYTES * 8 / 1000 / seconds * 0.85) - 96
    with TemporaryDirectory(prefix="wa-media-") as directory:
        out = Path(directory) / (path.stem + ".mp4")
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-i",
                str(path),
                "-vf",
                "scale=-2:720",
                "-c:v",
                "libx264",
                "-b:v",
                f"{max(kbps, 300)}k",
                "-maxrate",
                f"{max(kbps, 300)}k",
                "-bufsize",
                f"{2 * max(kbps, 300)}k",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                "-movflags",
                "+faststart",
                str(out),
            ],
            check=True,
        )
        if out.stat().st_size > MAX_MEDIA_BYTES:
            raise WhatsAppError("Compressed video exceeds the media size limit")
        yield out
