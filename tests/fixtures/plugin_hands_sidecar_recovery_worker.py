from __future__ import annotations

import json
from pathlib import Path
import sys

from core.plugin_hands.durable_lifecycle import PluginHandsLifecycleRecovery
from core.plugin_hands.workspace import PluginHandsWorkspaceManager
from core.storage_provider import SQLiteStructuredRecordStore


def main() -> int:
    root, result_path = (Path(value).resolve() for value in sys.argv[1:3])
    workspace_root = (root / "workspaces").resolve(strict=True)
    recovered = PluginHandsLifecycleRecovery(
        SQLiteStructuredRecordStore(root / "records.sqlite3"),
        now=lambda: "2026-08-26T00:00:01Z",
    ).reconcile(PluginHandsWorkspaceManager(workspace_root))
    record = recovered[0] if len(recovered) == 1 else None
    result_path.write_text(json.dumps({
        "count": len(recovered),
        "state": record.state if record is not None else None,
        "workspace_ref": record.workspace_ref if record is not None else None,
        "workspace_exists": (workspace_root / "lease-sidecar-0001").is_dir(),
    }, separators=(",", ":")), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
