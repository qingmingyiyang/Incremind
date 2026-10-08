from __future__ import annotations

import pytest

from core.storage_provider import JsonObjectStore
from core.source_processing import (
    SourceManifestArtifactError,
    SourceManifestArtifactRepository,
    SourceManifestCodec,
    SourceManifestIdentityConflict,
)


def manifest(namespace: str = "team-a", project: str = "project-a"):
    source_id = "manifest-a"
    return SourceManifestCodec.decode({
        "schema_version": "1.0.0", "source_id": source_id,
        "source_ref": f"crp://{namespace}/sources/{project}/source-a",
        "platform": "xiaohongshu", "input_identity": "share text", "resolver_revision": "1", "normalizer_revision": "1", "content_kind": "text",
        "body": {"kind": "text", "text": "body", "source_ref": None}, "metadata": {"nested": ["immutable"]},
        "permission": {"decision": "granted", "evidence_refs": ["crp://evidence/permission"]}, "provenance_refs": ["crp://provenance/source"],
        "assets": [{"asset_id": "text-1", "ordinal": 0, "kind": "text", "media_type": "text/plain", "role": "body", "locator": None, "source_ref": "crp://sources/text-1", "relations": [], "evidence_refs": ["crp://evidence/text-1"]}],
    })


def repository(tmp_path) -> SourceManifestArtifactRepository:
    return SourceManifestArtifactRepository(
        JsonObjectStore(tmp_path / "objects", namespace_id="test"), namespace_id="test"
    )


def test_first_write_is_immutable_and_exact_replay_returns_the_same_artifact(tmp_path) -> None:
    repo = repository(tmp_path)
    first = repo.put(project_id="project-a", manifest_id="manifest-a-r1", manifest=manifest())
    replay = repo.put(project_id="project-a", manifest_id="manifest-a-r1", manifest=manifest())
    assert first == replay
    assert first.public_ref == "crp://test/source-manifests/projects/project-a/manifest-a-r1"
    assert first.revision == "r1"


def test_legacy_artifact_replays_without_implicit_schema_writeback(tmp_path) -> None:
    repo = repository(tmp_path)
    legacy = manifest()
    assert legacy.schema_version == "1.1.0"
    raw_legacy = {
        "namespace_id": "test", "project_id": "project-a", "manifest_id": "manifest-a-r1",
        "public_ref": repo.public_ref(project_id="project-a", manifest_id="manifest-a-r1"),
        "revision": "r1", "manifest": {
            "schema_version": "1.0.0", "source_id": "manifest-a",
            "source_ref": "crp://team-a/sources/project-a/source-a", "platform": "xiaohongshu",
            "input_identity": "share text", "resolver_revision": "1", "normalizer_revision": "1",
            "content_kind": "text", "body": {"kind": "text", "text": "body", "source_ref": None},
            "metadata": {"nested": ["immutable"]},
            "permission": {"decision": "granted", "evidence_refs": ["crp://evidence/permission"]},
            "provenance_refs": ["crp://provenance/source"],
            "assets": [{"asset_id": "text-1", "ordinal": 0, "kind": "text", "media_type": "text/plain", "role": "body", "locator": None, "source_ref": "crp://sources/text-1", "relations": [], "evidence_refs": ["crp://evidence/text-1"]}],
        },
    }
    repo._object_store.write("source_manifests", "project-a~manifest-a-r1", raw_legacy, expected_revision=0)  # type: ignore[attr-defined]
    replay = repo.put(project_id="project-a", manifest_id="manifest-a-r1", manifest=legacy)
    assert replay.manifest.schema_version == "1.1.0"
    assert replay.manifest.credential_binding is None
    assert repo._object_store.read("source_manifests", "project-a~manifest-a-r1") == raw_legacy  # type: ignore[attr-defined]


def test_different_payload_for_the_same_manifest_identity_conflicts(tmp_path) -> None:
    repo = repository(tmp_path)
    repo.put(project_id="project-a", manifest_id="manifest-a-r1", manifest=manifest())
    changed = SourceManifestCodec.encode(manifest())
    changed["metadata"] = {"changed": True}
    with pytest.raises(SourceManifestIdentityConflict):
        repo.put(project_id="project-a", manifest_id="manifest-a-r1", manifest=SourceManifestCodec.decode(changed))


def test_public_ref_and_manifest_scope_must_match(tmp_path) -> None:
    repo = repository(tmp_path)
    with pytest.raises(SourceManifestArtifactError):
        repo.put(project_id="project-a", manifest_id="bad/id", manifest=manifest())
    with pytest.raises(SourceManifestArtifactError):
        repo.public_ref(project_id="project-a", manifest_id="")


def test_repository_namespace_must_match_the_physical_object_store(tmp_path) -> None:
    store = JsonObjectStore(tmp_path / "objects", namespace_id="physical")
    with pytest.raises(SourceManifestArtifactError, match="does not match"):
        SourceManifestArtifactRepository(store, namespace_id="public")
    assert store.collection_names() == ()


def test_resolver_only_accepts_scoped_source_manifest_references_and_never_urls(tmp_path) -> None:
    repo = repository(tmp_path)
    persisted = repo.put(project_id="project-a", manifest_id="manifest-a-r1", manifest=manifest())
    assert repo.resolve_source_ref(source_ref=persisted.public_ref, project_id="project-a") == persisted
    for source_ref in ("https://example.test/source", "crp://test/source-manifests/projects/other/manifest-a-r1"):
        with pytest.raises(SourceManifestArtifactError):
            repo.resolve_source_ref(source_ref=source_ref, project_id="project-a")


def test_missing_or_corrupt_artifact_fails_closed_without_other_authority_writes(tmp_path) -> None:
    store = JsonObjectStore(tmp_path / "objects", namespace_id="test")
    repo = SourceManifestArtifactRepository(store, namespace_id="test")
    missing_ref = repo.public_ref(
        project_id="project-a", manifest_id="missing-r1"
    )
    with pytest.raises(SourceManifestArtifactError, match="not found"):
        repo.resolve_source_ref(
            source_ref=missing_ref,
            project_id="project-a",
        )

    corrupt_ref = repo.public_ref(
        project_id="project-a", manifest_id="corrupt-r1"
    )
    store.write(
        "source_manifests",
        "project-a~corrupt-r1",
        {
            "namespace_id": "test",
            "project_id": "project-a",
            "manifest_id": "corrupt-r1",
            "public_ref": corrupt_ref,
            "revision": "r1",
            "manifest": {"schema_version": "1.0.0"},
        },
        expected_revision=0,
    )
    with pytest.raises(SourceManifestArtifactError, match="codec validation"):
        repo.resolve_source_ref(
            source_ref=corrupt_ref,
            project_id="project-a",
        )
    assert set(store.collection_names()) == {"source_manifests"}
