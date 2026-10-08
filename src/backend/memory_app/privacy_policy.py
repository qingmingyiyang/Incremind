"""Global model switches and versioned project privacy shared by v2 callers."""
from __future__ import annotations

from collections.abc import Mapping

from backend.recognition import RecognitionConflict, RecognitionError
from core.storage_provider import SQLiteUnitOfWorkConflict


from .privacy_state import (
    _SCOPES, _STATE, _STATE_ID, is_private_project, privacy_revision,
)
_PURPOSES = ("generation", "embedding", "rerank", "asr", "vision", "search")


def set_private_project(records, project_id, private, expected_revision):
    with records.begin() as tx:
        saved = set_private_project_in_transaction(tx, project_id, private, expected_revision)
        tx.commit()
    return saved


def set_private_project_in_transaction(tx, project_id, private, expected_revision):
    """Enlist a privacy change with the caller's project/settings CAS write."""
    if not isinstance(project_id, str) or not project_id or type(private) is not bool:
        raise RecognitionError("project privacy is invalid")
    if type(expected_revision) is not int or expected_revision < 0:
        raise RecognitionError("project privacy revision is invalid")
    current = tx.read(_SCOPES, project_id)
    if (current.revision if current is not None else 0) != expected_revision:
        raise SQLiteUnitOfWorkConflict("project privacy revision conflicted")
    if is_private_project(tx, project_id) == private:
        return current
    counter = tx.read(_STATE, _STATE_ID)
    next_revision = privacy_revision(tx) + 1
    from .cache_sources import invalidate_sources
    invalidate_sources(tx, project_id=project_id)
    # Retain the false row so removing privacy does not reset its CAS token.
    saved = tx.put(_SCOPES, project_id, {"project_id": project_id, "private": private},
                   expected_revision=expected_revision)
    tx.put(_STATE, _STATE_ID, {"revision": next_revision},
           expected_revision=counter.revision if counter is not None else 0)
    return saved


def egress_allowed(records, models, project_id, purpose) -> bool:
    if purpose not in _PURPOSES:
        raise RecognitionError("model egress purpose is invalid")
    if is_private_project(records, project_id):
        return False
    configuration = models.public()
    model = configuration.get(purpose) if isinstance(configuration, Mapping) else None
    if not isinstance(model, Mapping):
        return False
    return model.get("enabled" if purpose == "asr" else "allow_remote") is True
