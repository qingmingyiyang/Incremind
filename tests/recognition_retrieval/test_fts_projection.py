from __future__ import annotations

import backend.recognition_retrieval.service as service
from backend.recognition_retrieval import RecognitionRetrievalError, retrieve


def _entry(item_id: str, content: str, **overrides: object) -> dict[str, object]:
    return {
        "id": item_id,
        "revision": 1,
        "current_revision": 1,
        "project_id": "project-a",
        "content": content,
        "status": "published",
        "authorized": True,
        "source_refs": [f"experience:{item_id}"],
        **overrides,
    }


def test_request_fts_projection_keeps_chinese_and_english_keyword_baseline(monkeypatch) -> None:
    entries = [
        _entry("both", "网页 web 工作台保留真实业务闭环。"),
        _entry("other", "网页界面采用浅色圆角卡片。"),
        _entry("english-only", "web workbench uses an independent draft pane."),
    ]

    projected = retrieve("project-a", "网页 web", entries)

    def python_candidates(accepted, _query_tokens):
        return {entry.id for entry in accepted}

    monkeypatch.setattr(service, "_fts_keyword_candidate_ids", python_candidates)
    baseline = retrieve("project-a", "网页 web", entries)

    assert [hit.id for hit in projected.hits] == [hit.id for hit in baseline.hits] == ["both"]
    assert [hit.keyword_score for hit in projected.hits] == [hit.keyword_score for hit in baseline.hits] == [1.0]
    assert projected.trace["keyword"]["backend"] == "sqlite_fts5"
    assert projected.trace["keyword"]["status"] == "used"


def test_fts_projection_only_receives_current_authorized_scope_entries() -> None:
    result = retrieve(
        "project-a",
        "网页优先",
        [
            _entry("current", "网页优先，桌面封装后置。"),
            _entry("other-project", "网页优先。", project_id="project-b"),
            _entry("unauthorized", "网页优先。", authorized=False),
            _entry("stale", "网页优先。", revision=1, current_revision=2),
        ],
    )

    assert [hit.id for hit in result.hits] == ["current"]
    assert result.trace["candidate_count"] == 1
    assert result.trace["excluded"] == {"other_project": 1, "unauthorized": 1, "stale": 1, "invalid": 0}
    assert result.trace["keyword"]["candidate_ids"] == ["current"]


def test_fts_error_falls_back_to_python_keyword_baseline(monkeypatch) -> None:
    entries = [_entry("current", "网页优先，桌面封装后置。")]

    def unavailable(*_args, **_kwargs):
        raise RecognitionRetrievalError("sqlite_fts5_unavailable")

    monkeypatch.setattr(service, "_fts_keyword_candidate_ids", unavailable)
    result = retrieve("project-a", "网页优先", entries)

    assert [hit.id for hit in result.hits] == ["current"]
    assert result.trace["keyword"]["status"] == "degraded"
    assert result.trace["keyword"]["backend"] == "python"
    assert result.trace["keyword"]["reason"] == "sqlite_fts5_unavailable"
