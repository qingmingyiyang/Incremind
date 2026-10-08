from pathlib import Path
import time

from backend.api.bootstrap import _WorkspaceIndexRefresher
from backend.video_summary.infrastructure.in_memory_progress_tracker import InMemoryProgressTracker
from core.effect_log import EffectState, build_effect_runtime
from core.product_core.video_knowledge_index_effect_admission import EFFECT_KIND
from core.product_core.video_knowledge_index_effect_execution import RECEIPT_TABLE


def _refresher(calls: list[tuple[str, ...]]) -> _WorkspaceIndexRefresher:
    return _WorkspaceIndexRefresher(
        refresh_all=lambda: calls.append(("full_rebuild",)),
        upsert_video=lambda series_id, video_id: calls.append(("upsert_video", series_id, video_id)),
        delete_video=lambda series_id, video_id: calls.append(("delete_video", series_id, video_id)),
        delete_series=lambda series_id: calls.append(("delete_series", series_id)),
        progress_tracker=InMemoryProgressTracker(),
    )


def test_index_refresh_is_persisted_and_dispatched_only_by_core_runner(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []
    runtime = build_effect_runtime(tmp_path / "effects.sqlite3", owner_id="core")
    refresher = _refresher(calls)
    refresher.bind_effect_runtime(runtime)

    refresher.upsert_video("series1", "video1")

    with runtime.log._connect() as connection:
        effect_id = connection.execute(
            "SELECT operation_id FROM effect WHERE kind=?", (EFFECT_KIND,),
        ).fetchone()[0]
    effect = runtime.log.get(effect_id)
    assert effect.state is EffectState.PLANNED
    runtime.dispatch_planned(now=int(time.time()))
    assert runtime.log.get(effect_id).state is EffectState.SETTLED_OK
    assert calls == [("upsert_video", "series1", "video1")]
    with runtime.log._connect() as connection:
        assert connection.execute(f"SELECT COUNT(*) FROM {RECEIPT_TABLE}").fetchone()[0] == 1


def test_full_rebuild_is_registered_with_the_core_runtime(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []
    runtime = build_effect_runtime(tmp_path / "effects.sqlite3", owner_id="core")
    refresher = _refresher(calls)
    refresher.bind_effect_runtime(runtime)

    refresher.refresh_all()
    with runtime.log._connect() as connection:
        effect_id = connection.execute(
            "SELECT operation_id FROM effect WHERE kind=?", (EFFECT_KIND,),
        ).fetchone()[0]
    assert runtime.log.get(effect_id).state is EffectState.PLANNED
    runtime.dispatch_planned(now=int(time.time()))
    assert runtime.log.get(effect_id).state is EffectState.SETTLED_OK
    assert calls == [("full_rebuild",)]


def test_same_snapshot_coalesces_and_encoded_identifiers_round_trip(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []
    runtime = build_effect_runtime(tmp_path / "effects.sqlite3", owner_id="core")
    refresher = _refresher(calls)
    refresher.bind_effect_runtime(runtime)

    refresher.upsert_video("系列 一", "视频/二")
    refresher.upsert_video("系列 一", "视频/二")
    with runtime.log._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM effect WHERE kind=?", (EFFECT_KIND,)).fetchone()[0] == 1
    runtime.dispatch_planned(now=int(time.time()))
    assert calls == [("upsert_video", "系列 一", "视频/二")]


def test_changed_authority_snapshot_plans_a_new_effect(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []
    revision = {"value": "one"}
    def snapshot(*_args):
        value = revision["value"]
        return {"source_revision": f"source-{value}", "workspace_revision": f"workspace-{value}", "embedding_profile_revision": "embedding-v5", "lancedb_schema_revision": "lancedb-v5", "index_generation": f"generation-{value}"}
    refresher = _refresher(calls)
    refresher._authority_snapshot = snapshot
    runtime = build_effect_runtime(tmp_path / "effects.sqlite3", owner_id="core")
    refresher.bind_effect_runtime(runtime)
    refresher.upsert_video("series", "video")
    revision["value"] = "two"
    refresher.upsert_video("series", "video")
    with runtime.log._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM effect WHERE kind=?", (EFFECT_KIND,)).fetchone()[0] == 2


def test_two_long_targets_use_short_mapped_identity(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []
    runtime = build_effect_runtime(tmp_path / "effects.sqlite3", owner_id="core")
    refresher = _refresher(calls)
    refresher.bind_effect_runtime(runtime)
    refresher.upsert_video("a" * 60, "b" * 60)
    with runtime.log._connect() as connection:
        request = connection.execute("SELECT request_json FROM video_knowledge_index_effect_request").fetchone()[0]
    assert '"id":"index-1"' in request and '"index_generation":"generation-1"' in request
