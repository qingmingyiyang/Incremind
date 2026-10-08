"""Same raw-target vectors are consumed by the Rust signer."""
import json
from pathlib import Path
from unittest.mock import patch
from starlette.requests import Request
from backend.api.worker_auth import WorkerRequestAuthenticator


def test_shared_rust_signatures_match_asgi_targets():
    vectors = json.loads((Path(__file__).parents[3] / "fixtures/worker-request-vectors.json").read_text())
    for vector in vectors:
        path, _, query = vector["target"].partition("?")
        request = Request({"type": "http", "method": "GET", "path": path, "raw_path": path.encode(), "query_string": query.encode(), "headers": []})
        auth = WorkerRequestAuthenticator("test-key", "b7305067cdef49499fe8c39268ec84f0")
        with patch("backend.api.worker_auth.time.time", return_value=1700000000):
            assert auth.verify(request, vector["token"]), vector["target"]
