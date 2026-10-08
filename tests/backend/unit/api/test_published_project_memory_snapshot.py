from __future__ import annotations

import pytest

from backend.api.published_project_memory_snapshot import (
    PublishedProjectMemorySnapshotAuthority,
    PublishedProjectMemorySnapshotError,
    published_project_memory_entry_from_payload,
)
from core.aggregate_repository_factory import (
    MemoryPublicationAuthorityResolution,
    ProjectSkillRepositoryResolution,
)
from core.ai_kernel.payload_store import InMemoryTurnPayloadStore
from core.memory_core import ObjectStoreMemoryStore
from core.storage_provider import JsonObjectStore


PROJECT_ID = "project-alpha"


class _Skills:
    def __init__(self, skill: dict[str, object] | None) -> None:
        self.skill = skill

    def load(self, project_id: str) -> dict[str, object] | None:
        assert project_id == PROJECT_ID
        return self.skill


class _Factory:
    def __init__(self, store: JsonObjectStore, skills: _Skills) -> None:
        self.json_store = store
        self._skills = skills
        self.memory_identity = "json:memory-v1"
        self.skill_identity = "json:skill-v1"

    def memory_publication_authority_resolution(self) -> MemoryPublicationAuthorityResolution:
        return MemoryPublicationAuthorityResolution(None, self.memory_identity)

    def project_skill_repository_resolution(self) -> ProjectSkillRepositoryResolution:
        return ProjectSkillRepositoryResolution(self._skills, self.skill_identity)  # type: ignore[arg-type]


def test_freezes_redacted_same_project_published_memory_in_deterministic_budget_order(tmp_path) -> None:
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    memory = ObjectStoreMemoryStore(store)
    memory.publish("atom", _atom("atom-z", revision=2, content="电话 13800138000"))
    memory.publish("atom", _atom("atom-a", revision=2, content="早期事实"))
    memory.publish("scenario", _scenario("scenario-a", revision=1, atom_ids=["atom-a", "atom-z"]))
    memory.publish("series_memory", _series("series-a", revision=1))
    payloads = InMemoryTurnPayloadStore()
    authority = _authority(store, _skill(), payloads=payloads)

    snapshot = authority.acquire(_request(), project_id=PROJECT_ID, profile_id="profile-a", profile_revision=3)

    assert [(entry["manifest_kind"], entry["object_id"]) for entry in snapshot.payload["selected"]] == [
        ("project_skill", "skill-alpha"),
        ("memory_r1", "atom-a"),
        ("memory_r1", "atom-z"),
    ]
    assert {
        (entry["kind"], entry["object_id"], entry["reason"])
        for entry in snapshot.payload["excluded"]
    } >= {
        ("l3_series_memory", "series-a", "tool_recall_required"),
        ("l2_scenario", "scenario-a", "tool_recall_required"),
    }
    assert "13800138000" not in str(snapshot.payload)
    selected_item = snapshot.payload["selected"][-1]
    child = published_project_memory_entry_from_payload(payloads.get(selected_item["payload_ref"]))
    assert child["kind"] == "memory_r1"
    assert child["revision"] == "2"
    assert child["markdown"] == "电话 [REDACTED]"
    assert snapshot.payload["selected_context_bytes"] <= snapshot.payload["context_budget_bytes"]
    assert snapshot.payload["memory_authority_identity"] == "json:memory-v1"
    assert snapshot.payload["project_skill_authority_identity"] == "json:skill-v1"
    assert "memory_r1-atom-z-r2" in selected_item["payload_ref"]


def test_excludes_stale_untrusted_conflicted_cross_project_and_budget_overflow(tmp_path) -> None:
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    memory = ObjectStoreMemoryStore(store)
    memory.publish("atom", _atom("atom-stale", stale=True))
    memory.publish("atom", _atom("atom-untrusted", trust_status="imported_unverified"))
    memory.publish("atom", _atom("atom-conflict", conflict_status="open"))
    memory.publish("atom", _atom("atom-other", project_id="project-other"))
    memory.publish("atom", _atom("atom-large", content="内容" * 300))
    memory.publish(
        "scenario",
        _scenario(
            "scenario-links", revision=1,
            atom_ids=["atom-stale", "atom-untrusted", "atom-conflict", "atom-other", "atom-large"],
            trust_status="imported_unverified",
        ),
    )
    authority = _authority(store, _skill())

    snapshot = authority.acquire(
        _request(max_context_bytes=100), project_id=PROJECT_ID, profile_id="profile-a", profile_revision=3,
    )

    exclusions = {(entry["object_id"], entry["reason"]) for entry in snapshot.payload["excluded"]}
    assert {
        ("atom-stale", "stale"),
        ("atom-untrusted", "untrusted"),
        ("atom-conflict", "conflict"),
        ("atom-other", "cross_project"),
        ("atom-large", "budget"),
    } <= exclusions
    assert all(entry["object_id"] != "atom-large" for entry in snapshot.payload["selected"])


def test_replay_uses_immutable_payload_and_fails_closed_when_authority_identity_changes(tmp_path) -> None:
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    memory = ObjectStoreMemoryStore(store)
    memory.publish("atom", _atom("atom-a"))
    payloads = InMemoryTurnPayloadStore()
    factory = _Factory(store, _Skills(_skill()))
    authority = PublishedProjectMemorySnapshotAuthority(factory=factory, payloads=payloads)  # type: ignore[arg-type]

    first = authority.acquire(_request(), project_id=PROJECT_ID, profile_id="profile-a", profile_revision=3)
    memory.publish("atom", _atom("atom-new"))
    replay = authority.acquire(_request(), project_id=PROJECT_ID, profile_id="profile-a", profile_revision=3)

    assert replay.payload_ref == first.payload_ref
    assert replay.payload == first.payload
    factory.memory_identity = "sqlite:structured-records-v1"
    with pytest.raises(PublishedProjectMemorySnapshotError, match="authority drifted"):
        authority.acquire(_request(), project_id=PROJECT_ID, profile_id="profile-a", profile_revision=3)


def test_entry_validator_rejects_unredacted_sensitive_content(tmp_path) -> None:
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    authority = _authority(store, _skill())
    snapshot = authority.acquire(_request(), project_id=PROJECT_ID, profile_id="profile-a", profile_revision=3)
    selected = snapshot.payload["selected"][0]
    tampered = {
        "schema_version": "1.0.0", "kind": "project_skill", "project_id": PROJECT_ID,
        "object_id": selected["object_id"], "revision": str(selected["revision"]),
        "trust_status": "trusted", "markdown": "token=abcdefghijk",
    }
    with pytest.raises(PublishedProjectMemorySnapshotError, match="sensitive content"):
        published_project_memory_entry_from_payload(tampered)


@pytest.mark.parametrize(
    "unsafe",
    [r"F:\Chriptmas_OS\secret.txt", r"\\server\share\secret.txt", "/etc/passwd"],
)
def test_snapshot_rejects_absolute_paths_before_persisting(tmp_path, unsafe: str) -> None:
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    payloads = InMemoryTurnPayloadStore()
    skill = _skill()
    skill["purpose"] = unsafe
    authority = _authority(store, skill, payloads=payloads)

    with pytest.raises(PublishedProjectMemorySnapshotError, match="unsafe"):
        authority.acquire(_request(), project_id=PROJECT_ID, profile_id="profile-a", profile_revision=3)
    assert payloads.get_immutable_payload("turn-alpha", authority.snapshot_kind) is None


def _authority(
    store: JsonObjectStore, skill: dict[str, object], *, payloads: InMemoryTurnPayloadStore | None = None,
) -> PublishedProjectMemorySnapshotAuthority:
    return PublishedProjectMemorySnapshotAuthority(
        factory=_Factory(store, _Skills(skill)),  # type: ignore[arg-type]
        payloads=payloads or InMemoryTurnPayloadStore(),
    )


def _request(*, max_context_bytes: int = 4096) -> dict[str, object]:
    return {"turn_id": "turn-alpha", "context_policy": {"max_context_bytes": max_context_bytes}}


def _skill() -> dict[str, object]:
    return {
        "id": "skill-alpha", "project_id": PROJECT_ID, "revision": 1, "status": "active",
        "trust_status": "trusted", "conflict": {"status": "none"}, "purpose": "研究项目上下文",
        "output_rules": [{"rule": "优先引用已发布证据", "source_refs": [{"source_id": "source-a", "locator": "char:0-1"}]}],
        "source_refs": [{"source_id": "source-a", "locator": "char:0-1"}],
    }


def _atom(
    object_id: str, *, project_id: str = PROJECT_ID, revision: int = 1, content: str = "已发布事实",
    stale: bool = False, trust_status: str = "trusted", conflict_status: str | None = None,
) -> dict[str, object]:
    item: dict[str, object] = {
        "id": object_id, "project_id": project_id, "revision": revision, "content": content,
        "atom_type": "fact", "stale": stale, "trust_status": trust_status,
        "source_refs": [{"source_id": "source-a", "locator": "char:0-1", "quote": "private quote"}],
    }
    if conflict_status is not None:
        item["conflict_status"] = conflict_status
    return item


def _scenario(
    object_id: str, *, revision: int, atom_ids: list[str] | None = None,
    trust_status: str = "trusted",
) -> dict[str, object]:
    return {
        "id": object_id, "project_id": PROJECT_ID, "revision": revision, "summary": "阶段摘要",
        "atom_ids": atom_ids or ["atom-a"], "trust_status": trust_status,
        "source_refs": [{"source_id": "source-a", "locator": "char:0-1"}],
    }


def _series(object_id: str, *, revision: int) -> dict[str, object]:
    return {
        "id": object_id, "revision": revision, "overview": "系列总览", "scenario_ids": ["scenario-a"],
        "project_ids": [PROJECT_ID], "trust_status": "trusted",
        "source_refs": [{"source_id": "source-a", "locator": "char:0-1"}],
    }
