from backend.api.retention_effect_runtime import register_retention_effect_handlers
from core.effect_log import build_effect_runtime


def test_retention_v2_handlers_and_recovery_are_registered(tmp_path) -> None:
    runtime = build_effect_runtime(
        tmp_path / ".rebuild-data" / "jobs.sqlite3",
        owner_id="retention-registration-test",
    )

    register_retention_effect_handlers(tmp_path, runtime)

    assert "source_retention_purge" in runtime.handlers.kinds()
    assert "original_asset_retention_purge" in runtime.handlers.kinds()
    assert "source_retention_purge" in runtime.recoveries.kinds()
    assert "original_asset_retention_purge" in runtime.recoveries.kinds()
