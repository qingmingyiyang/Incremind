from __future__ import annotations

import asyncio
import argparse
import sys
from threading import Thread
from typing import BinaryIO

import uvicorn


def configure_event_loop_policy() -> None:
    if sys.platform != "win32":
        return
    selector_policy = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if selector_policy is None:
        return
    asyncio.set_event_loop_policy(selector_policy())


def _watch_parent_stdin(server: uvicorn.Server, stream: BinaryIO) -> None:
    """Request normal ASGI shutdown once the owning Electron process disappears."""
    try:
        while stream.read(8192):
            pass
    except (OSError, ValueError):
        pass
    finally:
        # A broken owner pipe has the same liveness meaning as EOF. Fail closed
        # by requesting the ordinary ASGI shutdown path in either case.
        server.should_exit = True


def start_parent_stdin_watchdog(
    server: uvicorn.Server,
    stream: BinaryIO | None = None,
) -> Thread:
    """Start the opt-in parent pipe watchdog without changing ordinary CLI launches."""
    parent_stdin = stream if stream is not None else sys.stdin.buffer
    watchdog = Thread(
        target=_watch_parent_stdin,
        args=(server, parent_stdin),
        name="desktop-parent-stdin-watchdog",
        daemon=True,
    )
    watchdog.start()
    return watchdog


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--parent-stdin-watchdog", action="store_true")
    args = parser.parse_args()

    configure_event_loop_policy()
    if not args.parent_stdin_watchdog:
        uvicorn.run("backend.memory_app.app:app", host=args.host, port=args.port)
        return

    config = uvicorn.Config("backend.memory_app.app:app", host=args.host, port=args.port)
    server = uvicorn.Server(config)
    start_parent_stdin_watchdog(server)
    server.run()


if __name__ == "__main__":
    main()
