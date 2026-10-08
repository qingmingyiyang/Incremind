"""Recall priority is independent of a recognition's truth and content revision."""
from backend.recognition import RecognitionConflict, RecognitionError
from datetime import datetime, timezone

COLLECTION = "recognition_recall_preferences"


def preference(records, scope, recognition_id):
    record = records.read(COLLECTION, recognition_id)
    if record is None:
        return {"recall_state": "normal", "recall_preference_revision": 0}
    if record.payload.get("project_id") != scope.project_id or record.payload.get("user_id") != scope.user_id:
        raise RecognitionConflict("recall preference is unavailable in this project")
    return {"recall_state": record.payload["state"], "recall_preference_revision": record.revision}


def annotate(records, scope, entries):
    return [{**entry, **preference(records, scope, entry["id"])} for entry in entries]


def is_recall_excluded(records, scope, recognition_id):
    """Only forgotten preferences remove recall; cooling preserves selection."""
    return preference(records, scope, recognition_id)["recall_state"] == "forgotten"
