from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from backend.api.media_ingress_selection_authority import (
    LegacyMediaIngressDisabled,
    MediaIngressRequestConflict,
    MediaIngressSelectionAuthority,
    MediaIngressSelectionConflict,
)
from core.storage_provider import SQLiteStructuredRecordStore


def _authority(tmp_path) -> MediaIngressSelectionAuthority:
    return MediaIngressSelectionAuthority(
        SQLiteStructuredRecordStore(tmp_path / "jobs.sqlite3")
    )


def test_default_selection_preserves_legacy_without_persisting_a_fake_revision(tmp_path) -> None:
    authority = _authority(tmp_path)

    selection = authority.require_legacy()

    assert selection.revision == 0
    assert selection.mode == "legacy"
    assert selection.persisted is False
    assert authority.current() is None


def test_selection_publish_replays_exact_command_and_supports_revisioned_rollback(
    tmp_path,
) -> None:
    authority = _authority(tmp_path)
    hands = authority.publish(
        "hands", expected_revision=0, command_id="select-hands-0001",
        actor="local-user", created_at="2026-08-26T00:00:00Z",
    )
    replay = authority.publish(
        "hands", expected_revision=0, command_id="select-hands-0001",
        actor="local-user", created_at="2026-08-26T00:01:00Z",
    )

    assert replay == hands
    with pytest.raises(LegacyMediaIngressDisabled) as blocked:
        authority.require_legacy()
    assert blocked.value.selection == hands

    legacy = authority.publish(
        "legacy", expected_revision=1, command_id="rollback-legacy-0002",
        actor="local-user", created_at="2026-08-26T00:02:00Z",
    )
    assert legacy.revision == 2 and legacy.mode == "legacy"
    assert authority.require_legacy() == legacy


def test_selection_rejects_stale_revision_and_command_input_drift(tmp_path) -> None:
    authority = _authority(tmp_path)
    authority.publish(
        "hands", expected_revision=0, command_id="select-hands-0001",
        actor="local-user", created_at="2026-08-26T00:00:00Z",
    )

    with pytest.raises(MediaIngressSelectionConflict, match="expected ingress revision"):
        authority.publish(
            "legacy", expected_revision=0, command_id="stale-mode-0002",
            actor="local-user", created_at="2026-08-26T00:01:00Z",
        )
    with pytest.raises(MediaIngressSelectionConflict, match="immutable input"):
        authority.publish(
            "legacy", expected_revision=0, command_id="select-hands-0001",
            actor="local-user", created_at="2026-08-26T00:02:00Z",
        )


def test_cutover_waits_for_the_admitted_writer_to_drain(tmp_path) -> None:
    authority = _authority(tmp_path)
    writer_started = Event()
    release_writer = Event()
    publication_finished = Event()

    def run_writer() -> None:
        with authority.writer("legacy"):
            writer_started.set()
            assert release_writer.wait(timeout=3)

    def publish_hands() -> None:
        authority.publish(
            "hands", expected_revision=0, command_id="select-hands-0001",
            actor="local-user", created_at="2026-08-26T00:00:00Z",
        )
        publication_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(run_writer)
        assert writer_started.wait(timeout=3)
        publication = pool.submit(publish_hands)
        assert publication_finished.wait(timeout=0.1) is False
        release_writer.set()
        writer.result(timeout=3)
        publication.result(timeout=3)

    assert authority.require_hands().revision == 1


def test_request_binding_replays_exact_input_and_rejects_manifest_drift(tmp_path) -> None:
    authority = _authority(tmp_path)
    selection = authority.publish(
        "hands", expected_revision=0, command_id="select-hands-0001",
        actor="local-user", created_at="2026-08-26T00:00:00Z",
    )
    values = {
        "operation": "admit",
        "request_id": "ingress-admit-0001",
        "project_id": "project-1",
        "input_ref": "crp://default/source-manifests/project-1/source-1",
        "selection": selection,
    }

    with authority.writer("hands"):
        authority.bind_request(**values)
        authority.bind_request(**values)
        with pytest.raises(MediaIngressRequestConflict, match="immutable input"):
            authority.bind_request(
                **{
                    **values,
                    "input_ref": "crp://default/source-manifests/project-1/source-2",
                }
            )
