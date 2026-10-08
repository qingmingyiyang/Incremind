from __future__ import annotations

from pathlib import Path

from core.product_core import (
    GetLibraryActivityOverview,
    serialize_library_activity_overview,
)
from core.storage_provider import JsonObjectStore


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _write_source(store: JsonObjectStore, source_id: str, created_at: str, title: str = "资料") -> None:
    store.write(
        "sources",
        source_id,
        {
            "schema_version": "1.0.0",
            "id": source_id,
            "type": "text",
            "title": title,
            "storage_uri": f"crp://default/sources/{source_id}.json",
            "media_type": "text/plain",
            "capture_mode": "manual",
            "processing_state": "captured",
            "content_hash": "sha256",
            "size_bytes": 0,
            "created_at": created_at,
            "metadata": {},
        },
        expected_revision=None,
    )


def _reader(store: JsonObjectStore):
    from core.composition import ObjectStoreLibraryOverviewReader

    return ObjectStoreLibraryOverviewReader(store)


def test_activity_overview_returns_year_days_for_requested_year(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_source(store, "source-2026-a", "2026-06-15T10:00:00+08:00", "六月资料")
    _write_source(store, "source-2026-b", "2026-12-31T23:00:00+08:00", "年末资料")
    _write_source(store, "source-2025-a", "2025-03-01T10:00:00+08:00", "去年资料")

    overview = GetLibraryActivityOverview(
        _reader(store),
        namespace_id="default",
    ).execute(year=2026)

    assert overview.status == "ready"
    assert len(overview.year_days) == 365, "2026 is not a leap year"
    june_15 = next(day for day in overview.year_days if day["date"] == "2026-06-15")
    assert june_15["count"] == 1
    dec_31 = next(day for day in overview.year_days if day["date"] == "2026-12-31")
    assert dec_31["count"] == 1
    jan_01 = next(day for day in overview.year_days if day["date"] == "2026-01-01")
    assert jan_01["count"] == 0
    assert 2026 in overview.years
    assert 2025 in overview.years


def test_activity_type_counts_confirmed_video_link_as_video(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_source(store, "source-video-link", "2026-09-24T10:00:00+08:00")
    source = store.read("sources", "source-video-link")
    store.write("sources", "source-video-link", {
        **source, "media_type": "text/uri-list",
        "metadata": {"content_kind": "video", "platform": "bilibili"},
    }, expected_revision=store.revision("sources", "source-video-link"))
    overview = GetLibraryActivityOverview(_reader(store), today=__import__("datetime").date(2026, 9, 24)).execute()
    assert overview.by_type == ({"label": "视频", "count": 1},)


def test_activity_overview_leap_year_returns_366_days(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_source(store, "source-2024-a", "2024-02-29T10:00:00+08:00", "闰年资料")

    overview = GetLibraryActivityOverview(
        _reader(store),
        namespace_id="default",
    ).execute(year=2024)

    assert len(overview.year_days) == 366, "2024 is a leap year"
    feb_29 = next(day for day in overview.year_days if day["date"] == "2024-02-29")
    assert feb_29["count"] == 1


def test_activity_overview_defaults_to_current_year_when_year_omitted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_source(store, "source-now", "2026-07-03T10:00:00+08:00", "今天资料")

    overview = GetLibraryActivityOverview(
        _reader(store),
        namespace_id="default",
        today=__import__("datetime").date(2026, 7, 3),
    ).execute()

    assert len(overview.year_days) == 365
    assert overview.year_days[0]["date"] == "2026-01-01"
    assert overview.year_days[-1]["date"] == "2026-12-31"


def test_activity_overview_invalid_year_falls_back_to_current(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_source(store, "source-now", "2026-07-03T10:00:00+08:00", "今天资料")

    overview = GetLibraryActivityOverview(
        _reader(store),
        namespace_id="default",
        today=__import__("datetime").date(2026, 7, 3),
    ).execute(year=-1)

    assert len(overview.year_days) == 365
    assert overview.year_days[0]["date"].startswith("2026-")


def test_activity_overview_serializes_year_days(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_source(store, "source-2026-a", "2026-06-15T10:00:00+08:00", "六月资料")

    overview = GetLibraryActivityOverview(
        _reader(store),
        namespace_id="default",
    ).execute(year=2026)
    payload = serialize_library_activity_overview(overview)

    assert payload["status"] == "ready"
    assert isinstance(payload["year_days"], list)
    assert len(payload["year_days"]) == 365
    assert payload["year_days"][0] == {"date": "2026-01-01", "count": 0, "month": 1, "day": 1}


def test_activity_overview_empty_store_returns_year_days(tmp_path: Path) -> None:
    store = _store(tmp_path)

    overview = GetLibraryActivityOverview(
        _reader(store),
        namespace_id="default",
        today=__import__("datetime").date(2026, 7, 3),
    ).execute(year=2026)

    assert overview.status == "empty"
    assert len(overview.year_days) == 365
    assert all(day["count"] == 0 for day in overview.year_days)


def test_activity_overview_separates_activity_candidates_and_published_memory(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_source(store, "source-today", "2026-07-03T08:00:00+08:00")
    for candidate_id, status in (("candidate-pending", "pending_review"), ("candidate-rejected", "rejected")):
        store.write(
            "memory_candidates",
            candidate_id,
            {
                "id": candidate_id,
                "status": status,
                "target_layer": "atom",
                "proposed_content": "测试候选正文不得进入脉搏投影",
                "source_refs": [],
                "created_at": "2026-07-03T09:00:00+08:00",
            },
            expected_revision=None,
        )
    for collection, object_id, layer, created_at in (
        ("memory_atoms", "atom-today", "atom", "2026-07-03T10:00:00+08:00"),
        ("memory_scenarios", "scenario-recent", "scenario", "2026-06-28T10:00:00+08:00"),
        ("project_skills", "skill-old", "project_skill", "2026-06-25T10:00:00+08:00"),
    ):
        store.write(
            collection,
            object_id,
            {
                "id": object_id,
                "layer": layer,
                "trust_status": "user_confirmed",
                "created_at": created_at,
            },
            expected_revision=None,
        )

    overview = GetLibraryActivityOverview(
        _reader(store),
        namespace_id="default",
        today=__import__("datetime").date(2026, 7, 3),
    ).execute()

    assert overview.recent_days[-1]["count"] == 4
    assert overview.counts["pending_memory_candidates"] == 1
    assert overview.counts["published_memories"] == 3
    assert overview.counts["today_published_memories"] == 1
    assert overview.counts["recent_7d_published_memories"] == 2


def test_published_memory_date_prefers_confirmation_update_over_old_draft_creation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write(
        "memory_atoms",
        "atom-confirmed-today",
        {
            "id": "atom-confirmed-today", "layer": "atom", "trust_status": "user_confirmed",
            "created_at": "2025-01-01T10:00:00+08:00",
            "updated_at": "2026-08-11T17:00:00+00:00",
        },
        expected_revision=None,
    )
    overview = GetLibraryActivityOverview(
        _reader(store), namespace_id="default",
        today=__import__("datetime").date(2026, 8, 12),
    ).execute()
    assert overview.counts["today_published_memories"] == 1
    assert overview.counts["recent_7d_published_memories"] == 1
