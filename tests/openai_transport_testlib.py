from __future__ import annotations

import json
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Lock, Thread
from time import sleep


class OpenAITransportFixture:
    """Real loopback OpenAI-compatible transport shared by integration Gates."""

    def __init__(
        self,
        *,
        expected_model: str,
        fault: int | str | None,
        block_first_request: bool = False,
        faults_by_ordinal: dict[int, int | str | None] | None = None,
    ) -> None:
        self.expected_model = expected_model
        self.fault = fault
        self.requests: list[dict[str, object]] = []
        self.bodies: list[dict[str, object]] = []
        self.first_request_started = Event()
        self.release_first_request = Event()
        self._request_lock = Lock()
        self._block_first_request = block_first_request
        self._faults_by_ordinal = dict(faults_by_ordinal or {})
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                size = int(self.headers["Content-Length"])
                payload = json.loads(self.rfile.read(size))
                model = str(payload["model"])
                with fixture._request_lock:
                    ordinal = len(fixture.requests) + 1
                    fixture.bodies.append(payload)
                    fixture.requests.append({
                        "model": model,
                        "path": self.path,
                        "ordinal": ordinal,
                        "has_authorization": "Authorization" in self.headers,
                    })
                if fixture._block_first_request and ordinal == 1:
                    fixture.first_request_started.set()
                    if not fixture.release_first_request.wait(timeout=10):
                        self._respond(504, {"error": {"message": "fixture release timed out"}})
                        return
                fault = fixture._faults_by_ordinal.get(ordinal, fixture.fault)
                if model != fixture.expected_model:
                    self._respond(400, {"error": {"message": "unexpected model"}})
                elif fault is None:
                    self._respond(200, {
                        "choices": [{"message": {"content": "{\"answer\": \"fallback\"}"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    })
                elif fault == "timeout":
                    sleep(0.5)
                    self._respond(200, {"choices": [{"message": {"content": "late"}}]})
                elif fault == "drop":
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    self.connection.close()
                elif fault == "redirect":
                    self.send_response(302)
                    self.send_header("Location", "https://example.invalid/never-follow")
                    self.end_headers()
                else:
                    self._respond(int(fault), {"error": {"message": "fixture fault"}})

            def _respond(self, status: int, body: dict[str, object]) -> None:
                encoded = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                try:
                    self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    pass

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
