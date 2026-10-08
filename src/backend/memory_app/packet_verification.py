"""Shared current-recognition fence and clock for packets and Turn results."""

from collections.abc import Mapping, Sequence

from backend.recognition import RecognitionConflict, RecognitionService, WorkScope
from .recall_state import is_recall_excluded


def _verify_packet_current(service: RecognitionService, scope: WorkScope, items: Sequence[object]) -> None:
    for item in items:
        if not isinstance(item, Mapping):
            raise RecognitionConflict("context packet is invalid")
        recognition = service.get_recognition(scope=scope, recognition_id=item.get("id"))
        if recognition is None or not recognition.authorized or recognition.revision != item.get("revision"):
            raise RecognitionConflict("context packet is stale; preview again")
        if is_recall_excluded(service.records, scope, recognition.id):
            raise RecognitionConflict("context packet is excluded from recall; preview again")


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
