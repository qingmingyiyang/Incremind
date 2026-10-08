from __future__ import annotations

import pytest

from backend.api.agent_message_authority import (
    AgentMessagePayloadAuthority,
    AgentMessagePayloadAuthorityError,
)
from core.ai_kernel import InMemoryTurnPayloadStore


SENDER = "turn-sender-001"
RECIPIENT = "turn-recipient-001"


class _StaticReader:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def get(self, _payload_ref: str) -> object:
        return self._payload


def _authority(payloads: InMemoryTurnPayloadStore) -> AgentMessagePayloadAuthority:
    return AgentMessagePayloadAuthority(payload_reader=payloads, immutable_payloads=payloads)


def _copy(
    authority: AgentMessagePayloadAuthority,
    source_ref: str,
    *,
    message_id: str = "message-001",
    kind: str = "result",
) -> str:
    return authority.copy_for_recipient(
        sender_turn_id=SENDER,
        recipient_turn_id=RECIPIENT,
        source_payload_ref=source_ref,
        message_id=message_id,
        kind=kind,
    )


def test_copies_sender_owned_public_payload_to_recipient_immutable_scope() -> None:
    payloads = InMemoryTurnPayloadStore()
    source = {"summary": "three public findings", "items": ["one", "two"]}
    source_ref = payloads.put(SENDER, "public-result", source)

    copied = _copy(_authority(payloads), source_ref)

    assert copied.startswith(f"crp://session/{RECIPIENT}/agent-message/message-001/result/")
    assert payloads.get(copied) == source
    assert copied != source_ref
    immutable = payloads.get_immutable_payload(RECIPIENT, "agent-message/message-001/result")
    assert immutable is not None and immutable[0] == copied and immutable[1] == source


def test_rejects_cross_turn_payload_reference_forgery() -> None:
    payloads = InMemoryTurnPayloadStore()
    foreign = payloads.put("turn-foreign-001", "public-result", {"summary": "foreign"})

    with pytest.raises(AgentMessagePayloadAuthorityError, match="crossed Turn"):
        _copy(_authority(payloads), foreign)


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("secret-note", {"summary": "not public"}),
        ("public-result", {"api_key": "redacted"}),
        ("public-result", {"nested": {"hidden_context": "private"}}),
        ("public-result", {"path": "C:\\private\\note.txt"}),
        ("public-result", {"ref": "crp://session/turn-sender-001/context-manifest/private"}),
        ("public-result", {"ref": "crp://session/turn-sender-001/public-result/source-002"}),
    ],
)
def test_rejects_sensitive_fields_paths_and_protected_references(kind: str, payload: object) -> None:
    payloads = InMemoryTurnPayloadStore()
    source_ref = f"crp://session/{SENDER}/{kind}/source-001"
    authority = AgentMessagePayloadAuthority(
        payload_reader=_StaticReader(payload), immutable_payloads=payloads,
    )

    with pytest.raises(AgentMessagePayloadAuthorityError):
        _copy(authority, source_ref)


def test_rejects_depth_and_size_limits() -> None:
    payloads = InMemoryTurnPayloadStore()
    nested: object = "leaf"
    for _ in range(7):
        nested = {"item": nested}
    depth_ref = payloads.put(SENDER, "public-result", nested)
    with pytest.raises(AgentMessagePayloadAuthorityError, match="depth"):
        _copy(_authority(payloads), depth_ref)

    size_ref = payloads.put(SENDER, "public-result", {"summary": "x" * (17 * 1024)})
    with pytest.raises(AgentMessagePayloadAuthorityError, match="size"):
        _copy(_authority(payloads), size_ref, message_id="message-002")


@pytest.mark.parametrize(
    "path_like",
    [
        "folder/report.txt",
        "folder/../private.txt",
        "folder/.. /private.txt",
        "file:///C:/private/report.txt",
        "folder\\report.txt",
        ". /private.txt",
    ],
)
def test_rejects_relative_and_file_uri_path_forms(path_like: str) -> None:
    payloads = InMemoryTurnPayloadStore()
    authority = AgentMessagePayloadAuthority(
        payload_reader=_StaticReader({"summary": path_like}), immutable_payloads=payloads,
    )

    with pytest.raises(AgentMessagePayloadAuthorityError, match="protected reference"):
        _copy(authority, f"crp://session/{SENDER}/public-result/source-path")


def test_keeps_an_ordinary_chinese_sentence_that_is_not_a_path() -> None:
    payloads = InMemoryTurnPayloadStore()
    sentence = "今天先整理三条公开发现，明天再核对结果。"
    authority = AgentMessagePayloadAuthority(
        payload_reader=_StaticReader({"summary": sentence}), immutable_payloads=payloads,
    )

    copied = _copy(authority, f"crp://session/{SENDER}/public-result/source-sentence")

    assert payloads.get(copied) == {"summary": sentence}


def test_replay_is_idempotent_but_different_content_collides() -> None:
    payloads = InMemoryTurnPayloadStore()
    authority = _authority(payloads)
    first = payloads.put(SENDER, "public-result", {"summary": "first"})

    copied = _copy(authority, first)
    assert _copy(authority, first) == copied

    different = payloads.put(SENDER, "public-result", {"summary": "different"})
    with pytest.raises(AgentMessagePayloadAuthorityError, match="identity conflicts"):
        _copy(authority, different)


def test_recipient_copy_is_immutable_after_source_payload_changes() -> None:
    payloads = InMemoryTurnPayloadStore()
    source = {"summary": "original", "items": ["a"]}
    source_ref = payloads.put(SENDER, "public-result", source)
    copied = _copy(_authority(payloads), source_ref)

    source["summary"] = "mutated outside store"
    source["items"].append("b")

    assert payloads.get(copied) == {"summary": "original", "items": ["a"]}
