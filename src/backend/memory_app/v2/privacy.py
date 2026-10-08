"""Global model switches and versioned project privacy shared by v2 callers."""
from __future__ import annotations

from collections.abc import Mapping

from backend.recognition import RecognitionConflict, RecognitionError
from core.storage_provider import SQLiteUnitOfWorkConflict
from ..original_sources import resolve_turn_material


from ..privacy_policy import (
    _SCOPES, _STATE, _STATE_ID, _PURPOSES, is_private_project, privacy_revision,
    set_private_project, set_private_project_in_transaction, egress_allowed,
)


def external_egress_allowed(reader, project_id, client):
    """Use the independent external-agent preference and existing privacy facts."""
    from backend.recognition import WorkScope
    from .external_agent_settings import external_agent_settings

    if not isinstance(client, str) or client not in {'claude', 'codex'}:
        return False
    if not isinstance(project_id, str) or not project_id:
        return False
    try:
        WorkScope('local-user', project_id)
    except RecognitionError:
        return False
    settings = external_agent_settings(reader)
    return (settings['allow_remote'] is True and settings['clients'][client] is True
            and not is_private_project(reader, project_id))


def external_catalog_allowed(reader, client):
    """Admit only the global directory call; no project or body is permitted."""
    from .external_agent_settings import external_agent_settings

    if not isinstance(client, str) or client not in {'claude', 'codex'}:
        return False
    settings = external_agent_settings(reader)
    return settings['allow_remote'] is True and settings['clients'][client] is True


def freeze_turn_materials(records, models, project_id, materials, *, authority, local_only=False, purpose="generation"):
    """Filter typed domain material before a caller loads any model input text.

    Descriptors contain only a typed identity, scope and expected revision.
    References and source roots are derived from the existing domain authority.
    Explicit local processing may retain private material; it cannot acquire
    remote authority later. No authorization result is cached across requests.
    """
    from copy import deepcopy
    from backend.recognition import WorkScope

    if purpose not in _PURPOSES:
        raise RecognitionError("model egress purpose is invalid")
    revision = privacy_revision(records)
    allowed, excluded, snapshots, material_refs = [], [], [], []
    for item in materials:
        own_project = item.get("project_id")
        if own_project not in {project_id, "me"}:
            raise RecognitionConflict("turn material is outside the requested scope")
        scope = WorkScope("local-user", own_project)
        resolved, roots = resolve_turn_material(records, scope, item)
        snapshot = authority.snapshot(scope, roots)
        if item["type"] == "original_source":
            node = next((node for node in snapshot["nodes"]
                         if node["type"] == "original_source" and node["id"] == item["id"]), None)
            if (node is None or node["source_revision"] != resolved["revision"]
                    or node.get("incarnation") != resolved["payload"]["_original_incarnation"]):
                raise RecognitionConflict("turn original incarnation conflicted")
        if not local_only:
            try:
                authority.require(snapshot, purpose)
            except RecognitionConflict:
                excluded.append({"kind": resolved["ref"]["kind"], "object_id": item["id"],
                                 "project_id": own_project})
                continue
        allowed.append(resolved)
        material_refs.append(deepcopy(item))
        snapshots.append(snapshot)
    if privacy_revision(records) != revision:
        raise RecognitionConflict("privacy changed while freezing turn inputs")
    remote = not local_only and egress_allowed(records, models, project_id, purpose)
    return allowed, {
        "mode": "remote_allowed" if remote else "local_only", "allow_remote": remote,
        "pii": "possible", "consent_refs": [f"crp://default/model-settings/{purpose}"] if remote else [],
        "retention": "session", "privacy_revision": revision,
        "excluded_refs": sorted(excluded, key=lambda r: (r["project_id"], r["kind"], r["object_id"])),
        "source_snapshots": snapshots,
        "material_refs": material_refs,
    }
