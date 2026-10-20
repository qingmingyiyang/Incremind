"""Product-layer rebuild storage composition with security adapters bound."""
from __future__ import annotations

from pathlib import Path

from backend.security.audited_records import audit_records
from backend.security.user_context import json_attribution
from backend.shared.rebuild_storage import build_rebuild_object_store as _build


def build_rebuild_object_store(runtime_root: Path, *, repository_root: Path | None = None):
    return _build(
        runtime_root,
        repository_root=repository_root,
        audit_records=audit_records,
        json_attribution=json_attribution,
    )
