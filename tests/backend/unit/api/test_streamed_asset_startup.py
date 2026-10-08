from __future__ import annotations

import os
import subprocess
import sys

from backend.api.streamed_asset_startup import cleanup_stale_streamed_asset_parts


def test_startup_removes_only_stale_bounded_stream_parts(tmp_path) -> None:
    incoming = tmp_path / "library" / "assets" / "originals" / ".incoming"
    incoming.mkdir(parents=True)
    stale = incoming / ("file-grant-" + "a" * 43 + ".part")
    fresh = incoming / ("file-grant-" + "b" * 43 + ".part")
    unrelated = incoming / "keep.txt"
    stale.write_bytes(b"partial-after-process-kill")
    fresh.write_bytes(b"active")
    unrelated.write_text("keep", encoding="utf-8")
    os.utime(stale, (100, 100))
    os.utime(fresh, (950, 950))

    removed = cleanup_stale_streamed_asset_parts(tmp_path, now=1000, max_age_seconds=100)

    assert removed == (stale.name,)
    assert not stale.exists()
    assert fresh.read_bytes() == b"active"
    assert unrelated.read_text(encoding="utf-8") == "keep"


def test_startup_recovers_partial_left_by_interrupted_process(tmp_path) -> None:
    incoming = tmp_path / "library" / "assets" / "originals" / ".incoming"
    stale = incoming / ("file-grant-" + "c" * 43 + ".part")
    script = (
        "import os, pathlib, sys; "
        "path = pathlib.Path(sys.argv[1]); "
        "path.parent.mkdir(parents=True); "
        "path.write_bytes(b'interrupted-stream'); "
        "os.utime(path, (100, 100)); "
        "os._exit(77)"
    )

    interrupted = subprocess.run(
        [sys.executable, "-c", script, str(stale)],
        check=False,
        capture_output=True,
        timeout=10,
    )

    assert interrupted.returncode == 77
    assert stale.read_bytes() == b"interrupted-stream"
    removed = cleanup_stale_streamed_asset_parts(tmp_path, now=1000, max_age_seconds=100)
    assert removed == (stale.name,)
    assert not stale.exists()
