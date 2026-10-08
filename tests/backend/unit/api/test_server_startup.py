from __future__ import annotations

import asyncio
import os
import sys
import unittest
from unittest.mock import patch

from backend.api.server import configure_event_loop_policy, main, start_parent_stdin_watchdog


class ServerStartupTests(unittest.TestCase):
    def test_configures_windows_selector_event_loop_policy_when_available(self) -> None:
        policy = object()
        original_policy = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
        asyncio.WindowsSelectorEventLoopPolicy = lambda: policy  # type: ignore[attr-defined]
        try:
            with patch("backend.api.server.sys.platform", "win32"), patch(
                "backend.api.server.asyncio.set_event_loop_policy"
            ) as set_policy:
                configure_event_loop_policy()

            set_policy.assert_called_once_with(policy)
        finally:
            if original_policy is None:
                delattr(asyncio, "WindowsSelectorEventLoopPolicy")
            else:
                asyncio.WindowsSelectorEventLoopPolicy = original_policy  # type: ignore[attr-defined]

    def test_does_not_change_event_loop_policy_on_non_windows(self) -> None:
        with patch("backend.api.server.sys.platform", "linux"), patch(
            "backend.api.server.asyncio.set_event_loop_policy"
        ) as set_policy:
            configure_event_loop_policy()

        set_policy.assert_not_called()

    def test_parent_stdin_watchdog_requests_graceful_exit_on_eof(self) -> None:
        class Server:
            should_exit = False

        reader_fd, writer_fd = os.pipe()
        try:
            with os.fdopen(reader_fd, "rb", closefd=True) as reader:
                server = Server()
                watchdog = start_parent_stdin_watchdog(server, reader)
                self.assertTrue(watchdog.is_alive())
                os.close(writer_fd)
                writer_fd = -1
                watchdog.join(timeout=1)

            self.assertFalse(watchdog.is_alive())
            self.assertTrue(server.should_exit)
        finally:
            if writer_fd != -1:
                os.close(writer_fd)

    def test_parent_stdin_watchdog_fails_closed_when_owner_pipe_breaks(self) -> None:
        class Server:
            should_exit = False

        class BrokenPipe:
            def read(self, _size: int) -> bytes:
                raise OSError("owner pipe broke")

        server = Server()
        watchdog = start_parent_stdin_watchdog(server, BrokenPipe())  # type: ignore[arg-type]
        watchdog.join(timeout=1)

        self.assertFalse(watchdog.is_alive())
        self.assertTrue(server.should_exit)

    def test_parent_watchdog_cli_flag_uses_server_instance_without_changing_default_launch(self) -> None:
        sentinel_server = unittest.mock.MagicMock()
        with patch("backend.api.server.configure_event_loop_policy"), patch(
            "backend.api.server.uvicorn.Config"
        ) as config, patch("backend.api.server.uvicorn.Server", return_value=sentinel_server) as server_factory, patch(
            "backend.api.server.start_parent_stdin_watchdog"
        ) as watchdog, patch.object(sys, "argv", ["server", "--host", "127.0.0.1", "--port", "9010", "--parent-stdin-watchdog"]):
            main()

        config.assert_called_once_with("backend.api.app:app", host="127.0.0.1", port=9010)
        server_factory.assert_called_once()
        watchdog.assert_called_once_with(sentinel_server)
        sentinel_server.run.assert_called_once_with()

    def test_default_cli_launch_does_not_install_parent_watchdog(self) -> None:
        with patch("backend.api.server.configure_event_loop_policy"), patch(
            "backend.api.server.uvicorn.run"
        ) as run, patch("backend.api.server.start_parent_stdin_watchdog") as watchdog, patch.object(
            sys, "argv", ["server", "--host", "127.0.0.1", "--port", "9011"]
        ):
            main()

        run.assert_called_once_with("backend.api.app:app", host="127.0.0.1", port=9011)
        watchdog.assert_not_called()


if __name__ == "__main__":
    unittest.main()
