from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from backend.security import (
    NetworkEgressProfileConflict,
    NetworkEgressProfileError,
    NetworkEgressProfileStore,
)


def configure(
    runtime_root: Path,
    *,
    mode: str,
    expected_revision: int,
    address: str | None = None,
    port: int | None = None,
    confirm_enable: bool = False,
) -> dict[str, object]:
    try:
        snapshot = NetworkEgressProfileStore(runtime_root).update(
            mode=mode,
            literal_address=address,
            port=port,
            confirm_enable=confirm_enable,
            expected_revision=expected_revision,
        )
    except NetworkEgressProfileConflict:
        return {"status": "conflict", "error_code": "revision_conflict"}
    except NetworkEgressProfileError:
        return {"status": "input_invalid", "error_code": "network_profile_invalid"}
    return {
        "status": "updated",
        "mode": snapshot.profile.mode,
        "revision": snapshot.store_revision,
        "allowed_capabilities": list(snapshot.profile.allowed_capabilities),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Configure the local non-secret governed network egress profile"
    )
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--mode", choices=("direct", "loopback_http_connect"), required=True)
    parser.add_argument("--expected-revision", type=int, required=True)
    parser.add_argument("--address")
    parser.add_argument("--port", type=int)
    parser.add_argument("--confirm-enable", action="store_true")
    args = parser.parse_args()
    result = configure(
        args.runtime_root,
        mode=args.mode,
        expected_revision=args.expected_revision,
        address=args.address,
        port=args.port,
        confirm_enable=args.confirm_enable,
    )
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["status"] == "updated" else 2


if __name__ == "__main__":
    raise SystemExit(main())
