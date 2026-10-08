from __future__ import annotations

import time
from pathlib import Path


STALE_STREAM_PART_AGE_SECONDS = 10 * 60
MAX_STREAM_PARTS_PER_STARTUP = 256


def cleanup_stale_streamed_asset_parts(
    root_dir: Path,
    *,
    now: float | None = None,
    max_age_seconds: int = STALE_STREAM_PART_AGE_SECONDS,
) -> tuple[str, ...]:
    incoming = root_dir / "library" / "assets" / "originals" / ".incoming"
    if not incoming.exists():
        return ()
    cutoff = (time.time() if now is None else now) - max_age_seconds
    removed: list[str] = []
    for part in sorted(incoming.glob("file-grant-*.part"))[:MAX_STREAM_PARTS_PER_STARTUP]:
        try:
            if part.is_file() and part.stat().st_mtime <= cutoff:
                part.unlink()
                removed.append(part.name)
        except OSError:
            continue
    return tuple(removed)
