"""Read the existing project privacy facts without write orchestration."""
from backend.recognition import RecognitionConflict


_SCOPES = "v2_private_scopes"
_STATE = "v2_privacy_state"
_STATE_ID = "default"


def is_private_project(reader, project_id) -> bool:
    if project_id is None:
        return False
    record = reader.read(_SCOPES, project_id)
    if record is None:
        return False
    if (record.payload.get("project_id") != project_id
            or type(record.payload.get("private")) is not bool):
        raise RecognitionConflict("stored project privacy is invalid")
    return record.payload["private"]


def privacy_revision(reader) -> int:
    record = reader.read(_STATE, _STATE_ID)
    if record is None:
        return 0
    revision = record.payload.get("revision")
    if type(revision) is not int or revision < 0:
        raise RecognitionConflict("stored privacy revision is invalid")
    return revision
