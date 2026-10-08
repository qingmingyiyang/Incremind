import pytest

from backend.memory_app.packet_egress import capture_packet_egress, validate_packet_egress
from backend.memory_app.source_egress import SourceEgressService
from backend.recognition import RecognitionConflict, RecognitionService, WorkScope
from core.storage_provider import SQLiteStructuredRecordStore


class Models:
    def __init__(self, base_url="https://example.test"):
        self.base_url = base_url

    def public(self):
        return {"generation": {"base_url": self.base_url, "allow_remote": True}}


@pytest.fixture
def env(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / "memory.sqlite3")
    service = RecognitionService(records)
    return service, SourceEgressService(records), WorkScope("user", "project")


def _experience(service, scope, content="evidence"):
    return service.stage_experience(scope=scope, content=content)


def _recognition(service, scope, experiences=(), recognitions=(), name=None):
    candidate = service.propose(scope=scope, content="derived", source_experience_ids=experiences,
                                source_recognition_ids=recognitions)
    return service.publish(scope=scope, candidate_id=candidate.id, expected_revision=1,
                           reviewer="user", recognition_id=name)


def _context_packet(recognition):
    return {"kind": "context", "items": [{"id": recognition.id, "revision": recognition.revision}]}


def test_context_requires_frozen_snapshot_and_remote_generation_permission(env):
    service, authority, scope = env
    experience = _experience(service, scope)
    recognition = _recognition(service, scope, (experience,))
    packet = _context_packet(recognition)
    with pytest.raises(RecognitionConflict, match="unavailable"):
        validate_packet_egress(service, Models(), scope, packet)
    # Missing policies allow egress since T1.2; explicitly deny this fixture
    # before testing remote permission rejection and subsequent restoration.
    authority.set_policy(scope, "experience", experience, 1, 0, [])
    packet["source_egress"] = capture_packet_egress(service, scope, packet)
    with pytest.raises(RecognitionConflict, match="authorized"):
        validate_packet_egress(service, Models(), scope, packet)
    authority.set_policy(scope, "experience", experience, 1, 1, ["generation", "embedding", "rerank"])
    packet["source_egress"] = capture_packet_egress(service, scope, packet)
    validate_packet_egress(service, Models(), scope, packet)


def test_context_snapshot_rejects_policy_change_and_wrong_roots(env):
    service, authority, scope = env
    first, second = _experience(service, scope, "first"), _experience(service, scope, "second")
    first_recognition = _recognition(service, scope, (first,), name="first")
    second_recognition = _recognition(service, scope, (second,), name="second")
    authority.set_policy(scope, "experience", first, 1, 0, ["generation", "embedding", "rerank"])
    authority.set_policy(scope, "experience", second, 1, 0, ["generation", "embedding", "rerank"])
    packet = _context_packet(first_recognition)
    packet["source_egress"] = capture_packet_egress(service, scope, packet)
    packet["items"] = [{"id": second_recognition.id, "revision": second_recognition.revision}]
    with pytest.raises(RecognitionConflict, match="roots changed"):
        validate_packet_egress(service, Models(), scope, packet)
    packet = _context_packet(first_recognition)
    packet["source_egress"] = capture_packet_egress(service, scope, packet)
    authority.set_policy(scope, "experience", first, 1, 1, ["generation", "embedding", "rerank"])
    with pytest.raises(RecognitionConflict, match="conflicted"):
        validate_packet_egress(service, Models(), scope, packet)


def test_derived_recognition_inherits_its_experience_permission(env):
    service, authority, scope = env
    experience = _experience(service, scope)
    authority.set_policy(scope, "experience", experience, 1, 0, ["generation", "embedding", "rerank"])
    recognition = _recognition(service, scope, (experience,))
    packet = _context_packet(recognition)
    packet["source_egress"] = capture_packet_egress(service, scope, packet)
    validate_packet_egress(service, Models(), scope, packet)


def test_restructure_captures_every_snapshot_recognition_and_experience(env):
    service, authority, scope = env
    first, second = _experience(service, scope, "first"), _experience(service, scope, "second")
    parent = _recognition(service, scope, (first,), name="parent")
    child = _recognition(service, scope, (second,), (parent.id,), name="child")
    for source_id in (first, second):
        authority.set_policy(scope, "experience", source_id, 1, 0, ["generation", "embedding", "rerank"])
    packet = {"kind": "restructure", "snapshot": {
        "recognitions": [{"id": child.id, "revision": child.revision}, {"id": parent.id, "revision": parent.revision}],
        "experiences": [{"id": second, "revision": 1}, {"id": first, "revision": 1}],
    }}
    packet["source_egress"] = capture_packet_egress(service, scope, packet)
    assert packet["source_egress"]["roots"] == sorted([
        {"type": "experience", "id": first, "revision": 1},
        {"type": "experience", "id": second, "revision": 1},
        {"type": "recognition", "id": child.id, "revision": child.revision},
        {"type": "recognition", "id": parent.id, "revision": parent.revision},
    ], key=lambda item: (item["type"], item["id"]))
    validate_packet_egress(service, Models(), scope, packet)
    packet["snapshot"]["experiences"].pop()
    with pytest.raises(RecognitionConflict, match="roots changed"):
        validate_packet_egress(service, Models(), scope, packet)


def test_local_no_source_packet_remains_compatible(env):
    service, _, scope = env
    packet = {"kind": "context", "items": []}
    assert capture_packet_egress(service, scope, packet) is None
    validate_packet_egress(service, Models("http://127.0.0.1:8080"), scope, packet)
    packet["source_egress"] = {"roots": []}
    with pytest.raises(RecognitionConflict, match="invalid"):
        validate_packet_egress(service, Models("http://127.0.0.1:8080"), scope, packet)


def test_duplicate_packet_source_ids_are_rejected_even_when_revisions_differ(env):
    service, _, scope = env
    experience = _experience(service, scope)
    recognition = _recognition(service, scope, (experience,))
    packet = {"kind": "context", "items": [
        {"id": recognition.id, "revision": recognition.revision},
        {"id": recognition.id, "revision": recognition.revision + 1},
    ]}
    with pytest.raises(RecognitionConflict, match="duplicates"):
        capture_packet_egress(service, scope, packet)


def test_unknown_packet_kind_is_rejected(env):
    service, _, scope = env
    with pytest.raises(RecognitionConflict, match="kind"):
        capture_packet_egress(service, scope, {"kind": "unknown", "items": []})
