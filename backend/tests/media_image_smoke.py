"""Exercise the actual restricted worker image's codecs, not only its version.

CI passes this file on stdin to the built image. No database, secret, user
video, network request or permanent file is needed.
"""
import asyncio
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

from app.blog_media_worker import normalize


with TemporaryDirectory(prefix="vecher-image-smoke-") as directory:
    root = Path(directory)
    source, video, thumbnail = (root / name for name in ("source.mp4", "video.mp4", "thumbnail.jpg"))
    subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-y",
        "-f", "lavfi", "-i", "color=c=blue:s=64x48:r=10:d=0.6",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=0.6",
        "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-shortest", "-metadata", "comment=source-metadata-must-be-removed",
        str(source),
    ], check=True, stdin=subprocess.DEVNULL, timeout=30)
    with source.open("rb") as incoming, video.open("w+b") as outgoing, thumbnail.open("w+b") as poster:
        info = asyncio.run(normalize(incoming.fileno(), outgoing.fileno(), poster.fileno()))
        assert info.width == 64 and info.height == 48 and info.has_audio
        assert 400 <= info.duration_ms <= 1200
        assert os.fstat(outgoing.fileno()).st_size > 0
        poster.seek(0)
        assert poster.read(2) == b"\xff\xd8"
    print("MEDIA_WORKER_IMAGE_NORMALIZATION_OK")
