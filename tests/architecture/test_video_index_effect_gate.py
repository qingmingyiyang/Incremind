from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_video_index_writes_have_no_private_thread_scheduler() -> None:
    bootstrap = (ROOT / "src/backend/api/bootstrap.py").read_text(encoding="utf-8")
    retrieval = (ROOT / "src/backend/video_summary/infrastructure/agent_memory/retrieval.py").read_text(encoding="utf-8")
    app = (ROOT / "src/backend/api/app.py").read_text(encoding="utf-8")

    assert "Thread(target=self._run" not in bootstrap
    assert "_refresh_series_async" not in retrieval
    assert "bind_effect_runtime(application.state.effect_runtime)" in app
    assert "dispatch_operation(effect.operation_id" not in bootstrap
    assert "admit_in_connection" in bootstrap
    snapshot = retrieval[retrieval.index("def index_authority_snapshot"):retrieval.index("def search", retrieval.index("def index_authority_snapshot"))]
    assert "_build_series_signatures" not in snapshot
    assert "sha1" not in snapshot and "md5" not in snapshot and "blake" not in snapshot
