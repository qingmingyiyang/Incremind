from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from core.companion_core import (
    CompanionConflict,
    CompanionMemoryBridge,
    CompanionMemoryBridgeError,
    CompanionRepository,
)
from core.memory_core import ObjectStoreMemoryCandidateRepository
from core.search_and_recall import LibrarySearchHit, LibrarySearchResult
from core.storage_provider import JsonObjectStore


NOW = datetime(2026, 7, 23, 4, 0, tzinfo=timezone.utc)


class Search:
    def __init__(self, hits=(), *, reason=None, error=None):
        self.hits = tuple(hits)
        self.reason = reason
        self.error = error
        self.calls = []

    def search(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return LibrarySearchResult(
            status="ready" if self.hits else "empty", backend="sqlite_fts5",
            query_text=kwargs["query"], hits=self.hits, total=len(self.hits),
            index_stale=False, reason=self.reason,
        )


def setup_bridge(tmp_path, *, search=None, published=None, published_list=None):
    repo = CompanionRepository.at_data_root(tmp_path, now=lambda: NOW)
    repo.initialize()
    repo.create_session(
        session_id="session:memory", context_epoch=1, prompt_revision=1,
        profile_revision=1, started_at=NOW.isoformat(),
    )
    message = repo.append_message(
        message_id="message:user:memory", request_id="request:memory",
        session_id="session:memory", context_epoch=1, role="user", status="completed",
        content="我喜欢在清晨喝乌龙茶。", created_at=NOW.isoformat(), provider_mode="none",
    )
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library", namespace_id="default")
    published = published or {}
    bridge = CompanionMemoryBridge(
        repo, candidates=ObjectStoreMemoryCandidateRepository(store), search=search or Search(),
        published_lookup=lambda layer, object_id: published.get((layer, object_id)),
        published_list=published_list,
        candidate_delete=lambda candidate_id: store.delete("memory_candidates", candidate_id),
        now=lambda: NOW, today=lambda: date(2026, 7, 23),
        local_timezone=lambda: timezone(timedelta(hours=8)),
    )
    return repo, store, bridge, message


def test_message_proposal_is_pending_review_idempotent_and_dependency_bound(tmp_path) -> None:
    repo, store, bridge, message = setup_bridge(tmp_path)
    first = bridge.propose(message.message_id)
    second = bridge.propose(message.message_id)
    assert first.status == "pending_review" and first.replayed is False
    assert second.candidate_id == first.candidate_id and second.replayed is True
    candidate = store.read("memory_candidates", first.candidate_id)
    assert candidate["proposed_content"] == message.content
    assert candidate["review"] == {
        "requires_user_confirmation": True, "auto_promote_allowed": False,
        "reason": "用户从陪伴历史显式提议，等待人工审阅。",
        "reviewed_by": None, "reviewed_at": None,
    }
    history = repo.list_history(limit=10).items[0]
    assert history.dependency_kinds == ("candidate",)


def test_new_candidate_is_compensated_when_sqlite_dependency_binding_fails(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, store, bridge, message = setup_bridge(tmp_path)
    monkeypatch.setattr(
        repo,
        "register_message_dependency",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("sqlite unavailable")),
    )

    with pytest.raises(CompanionMemoryBridgeError, match="candidate was compensated"):
        bridge.propose(message.message_id)

    assert store.list("memory_candidates") == ()
    assert repo.list_history(limit=10).items[0].dependency_count == 0


@pytest.mark.parametrize("compensation", ["rejected", "failed"])
def test_failed_candidate_compensation_keeps_recoverable_evidence_and_retry_repairs_dependency(
    tmp_path, monkeypatch: pytest.MonkeyPatch, compensation: str,
) -> None:
    repo, store, bridge, message = setup_bridge(tmp_path)
    register = repo.register_message_dependency
    monkeypatch.setattr(
        repo,
        "register_message_dependency",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("sqlite unavailable")),
    )
    bridge.candidate_delete = (
        (lambda _candidate_id: False)
        if compensation == "rejected"
        else (lambda _candidate_id: (_ for _ in ()).throw(OSError("object store unavailable")))
    )

    with pytest.raises(CompanionMemoryBridgeError, match="retry is required"):
        bridge.propose(message.message_id)

    candidates = store.list("memory_candidates")
    assert len(candidates) == 1
    monkeypatch.setattr(repo, "register_message_dependency", register)
    repaired = bridge.propose(message.message_id)
    assert repaired.candidate_id == candidates[0]["id"]
    assert repaired.replayed is True
    assert repo.list_history(limit=10).items[0].dependency_kinds == ("candidate",)


def test_message_proposal_rejects_bridge_from_another_project(tmp_path) -> None:
    repo, store, _bridge, message = setup_bridge(tmp_path)
    wrong_project = CompanionMemoryBridge(
        repo,
        candidates=ObjectStoreMemoryCandidateRepository(store),
        search=Search(),
        project_id="project-alpha",
    )

    with pytest.raises(CompanionConflict, match="project conflicts"):
        wrong_project.propose(message.message_id)
    assert store.list("memory_candidates") == ()


def test_deleted_or_noncompleted_message_cannot_be_proposed(tmp_path) -> None:
    repo, _store, bridge, _message = setup_bridge(tmp_path)
    cancelled = repo.append_message(
        message_id="message:assistant:cancelled", request_id="request:cancelled",
        session_id="session:memory", context_epoch=1, role="assistant", status="cancelled",
        content="", created_at=NOW.isoformat(), provider_mode="none",
    )
    with pytest.raises(CompanionMemoryBridgeError, match="not eligible"):
        bridge.propose(cancelled.message_id)
    with pytest.raises(CompanionMemoryBridgeError, match="not found"):
        bridge.propose("message:missing")


def test_recall_only_injects_current_published_trusted_memory(tmp_path) -> None:
    hit = LibrarySearchHit(
        object_id="atom-tea", layer="l1_atom", content="御主喜欢清晨喝乌龙茶。",
        source_refs=("message:user:memory#companion:message",),
        trust_status="user_confirmed", score=0.91, backend="sqlite_fts5",
    )
    published = {("l1_atom", "atom-tea"): {
        "id": "atom-tea", "confidence": 0.9, "trust_status": "user_confirmed",
        "source_refs": [{"source_id": "message:user:memory", "locator": "companion:message"}],
    }}
    _repo, _store, bridge, _message = setup_bridge(tmp_path, search=Search((hit,)), published=published)
    result = bridge.recall("早上喝什么")
    assert result.status == "recalled" and result.backend == "sqlite_fts5"
    assert result.context == ({"memory_id": "atom-tea", "text": "御主喜欢清晨喝乌龙茶。"},)
    assert result.selected == ({
        "memory_id": "atom-tea", "source_id": "message:user:memory", "score": 0.91, "rank": 1,
    },)


def test_topic_discussion_keeps_lexical_publication_filters_and_scope(tmp_path) -> None:
    hit = LibrarySearchHit(
        object_id="atom-build", layer="l1_atom", content="构建缓存已经分层。",
        source_refs=("source-build#char:0-12",), trust_status="user_confirmed",
        score=0.88, backend="sqlite_fts5",
    )
    search = Search((hit,))
    published = {("l1_atom", "atom-build"): {
        "id": "atom-build", "confidence": 0.9, "trust_status": "user_confirmed",
        "source_refs": [{"source_id": "source-build", "locator": "char:0-12"}],
    }}
    _repo, _store, bridge, _message = setup_bridge(tmp_path, search=search, published=published)

    result = bridge.recall("Windows 构建分层缓存", review_scope="memory_topic_discussion")

    assert search.calls[0]["query"] == "Windows 构建分层缓存"
    assert search.calls[0]["trust_statuses"] == ("user_confirmed", "trusted")
    assert result.review_scope == "memory_topic_discussion"
    assert [item["memory_id"] for item in result.selected] == ["atom-build"]


def test_topic_discussion_anchors_the_exact_published_memory_before_related_results(tmp_path) -> None:
    related = LibrarySearchHit(
        object_id="atom-related", layer="l1_atom", content="相关但不是用户点开的记忆。",
        source_refs=("source-related#char:0-12",), trust_status="user_confirmed",
        score=0.95, backend="sqlite_fts5",
    )
    published = {
        ("l1_atom", "atom-selected"): {
            "id": "atom-selected", "title": "重复标题", "content": "用户实际点开的记忆。",
            "project_id": "default", "confidence": 0.9, "trust_status": "user_confirmed",
            "source_refs": [{"source_id": "source-selected", "locator": "char:0-12"}],
        },
        ("l1_atom", "atom-related"): {
            "id": "atom-related", "content": "相关但不是用户点开的记忆。",
            "project_id": "default", "confidence": 0.9, "trust_status": "user_confirmed",
            "source_refs": [{"source_id": "source-related", "locator": "char:0-12"}],
        },
    }
    _repo, _store, bridge, _message = setup_bridge(
        tmp_path, search=Search((related,)), published=published,
    )

    result = bridge.recall(
        "重复标题", review_scope="memory_topic_discussion", target_memory_id="atom-selected",
    )

    assert result.backend == "publication_authority"
    assert [item["memory_id"] for item in result.selected] == ["atom-selected", "atom-related"]
    assert result.context[0]["text"] == "重复标题\n用户实际点开的记忆。"


def test_topic_discussion_fails_closed_when_anchor_is_not_currently_published(tmp_path) -> None:
    _repo, _store, bridge, _message = setup_bridge(
        tmp_path, search=Search((LibrarySearchHit(
            object_id="atom-similar", layer="l1_atom", content="相似标题",
            source_refs=("source-similar#char:0-4",), trust_status="user_confirmed",
            score=0.99, backend="sqlite_fts5",
        ),)), published={},
    )

    result = bridge.recall(
        "重复标题", review_scope="memory_topic_discussion", target_memory_id="atom-withdrawn",
    )

    assert result.status == "empty"
    assert result.selected == ()
    assert result.dropped_reasons == ("not_published",)


def test_today_review_reads_current_publication_authority_without_keyword_overlap(tmp_path) -> None:
    items = {
        "l1_atom": ({
            "id": "atom-today", "content": "用户把产品架构改成了分层缓存。",
            "confidence": 0.9, "trust_status": "user_confirmed",
            "source_refs": [{"source_id": "source-architecture", "locator": "char:0-20"}],
            "created_at": "2025-01-01T01:00:00+08:00",
            "updated_at": "2026-07-23T01:00:00+08:00",
        }, {
            "id": "atom-old", "content": "用户去年完成了旧版迁移。",
            "confidence": 0.9, "trust_status": "user_confirmed",
            "source_refs": [{"source_id": "source-old", "locator": "char:0-20"}],
            "published_at": "2026-07-10T01:00:00+08:00",
        }),
    }
    search = Search(error=AssertionError("review scope must not depend on lexical search"))
    _repo, _store, bridge, _message = setup_bridge(
        tmp_path, search=search,
        published_list=lambda layer: items.get(layer, ()),
    )

    result = bridge.recall("请回顾今天", review_scope="today_memory_review")

    assert search.calls == []
    assert result.status == "recalled" and result.backend == "publication_authority"
    assert result.context == ({
        "memory_id": "atom-today", "published_date": "2026-07-23",
        "text": "用户把产品架构改成了分层缓存。",
    },)
    assert result.selected == ({
        "memory_id": "atom-today", "source_id": "source-architecture", "score": 1.0, "rank": 1,
    },)
    assert result.review_scope == "today_memory_review"


def test_weekly_review_is_bounded_sorted_and_drops_untrusted_or_sourceless_memory(tmp_path) -> None:
    items = {
        "l1_atom": (
            {"id": "atom-new", "content": "第二条", "confidence": 0.9, "trust_status": "user_confirmed", "source_refs": [{"source_id": "source-new", "locator": "x"}], "published_at": "2026-07-23T02:00:00+08:00"},
            {"id": "atom-mid", "content": "第一条", "confidence": 0.9, "trust_status": "trusted", "source_refs": [{"source_id": "source-mid", "locator": "x"}], "published_at": "2026-07-18T02:00:00+08:00"},
            {"id": "atom-too-old", "content": "范围外", "confidence": 0.9, "trust_status": "user_confirmed", "source_refs": [{"source_id": "source-old", "locator": "x"}], "published_at": "2026-07-16T23:59:59+08:00"},
            {"id": "atom-untrusted", "content": "未确认", "confidence": 0.9, "trust_status": "system_generated", "source_refs": [{"source_id": "source-untrusted", "locator": "x"}], "published_at": "2026-07-22T02:00:00+08:00"},
            {"id": "atom-no-source", "content": "无来源", "confidence": 0.9, "trust_status": "user_confirmed", "source_refs": [], "published_at": "2026-07-22T01:00:00+08:00"},
            {"id": "atom-other-project", "project_id": "private-project", "content": "跨项目正文", "confidence": 0.9, "trust_status": "user_confirmed", "source_refs": [{"source_id": "source-other", "locator": "x"}], "published_at": "2026-07-23T03:00:00+08:00"},
        ),
    }
    _repo, _store, bridge, _message = setup_bridge(
        tmp_path, published_list=lambda layer: items.get(layer, ()),
    )

    result = bridge.recall("七日回顾", review_scope="weekly_memory_review")

    assert [item["memory_id"] for item in result.context] == ["atom-new", "atom-mid"]
    assert [item["rank"] for item in result.selected] == [1, 2]
    assert set(result.dropped_reasons) >= {"outside_review_window", "outside_project", "untrusted", "missing_source"}


def test_review_fails_closed_when_publication_authority_is_unavailable(tmp_path) -> None:
    _repo, _store, bridge, _message = setup_bridge(tmp_path, published_list=None)
    result = bridge.recall("今天回顾", review_scope="today_memory_review")
    assert result.status == "degraded"
    assert result.backend == "unavailable"
    assert result.context == () and result.selected == ()
    assert result.dropped_reasons == ("publication_authority_unavailable",)


@pytest.mark.parametrize(
    ("current", "score", "reason"),
    [
        (None, 0.9, "not_published"),
        ({"id": "atom", "confidence": 0.4}, 0.9, "low_confidence"),
        ({"id": "atom", "confidence": 0.9, "conflict": {"status": "open"}}, 0.9, "relationship_conflict"),
        ({"id": "atom", "confidence": 0.9, "status": "rolled_back"}, 0.9, "withdrawn"),
        ({"id": "atom", "confidence": 0.9}, 0.2, "low_score"),
    ],
)
def test_unpublished_withdrawn_conflicted_or_weak_hits_are_not_injected(tmp_path, current, score, reason) -> None:
    hit = LibrarySearchHit(
        object_id="atom", layer="l1_atom", content="CANARY MEMORY",
        source_refs=("message:user:memory#companion:message",),
        trust_status="user_confirmed", score=score, backend="sqlite_fts5",
    )
    published = {} if current is None else {("l1_atom", "atom"): current}
    _repo, _store, bridge, _message = setup_bridge(tmp_path, search=Search((hit,)), published=published)
    result = bridge.recall("canary")
    assert result.context == () and result.selected == () and reason in result.dropped_reasons


def test_index_failure_degrades_without_exposing_exception(tmp_path) -> None:
    _repo, _store, bridge, _message = setup_bridge(
        tmp_path, search=Search(error=OSError("C:/private/index.sqlite3 CANARY")),
    )
    result = bridge.recall("CANARY QUERY")
    assert result.status == "degraded"
    assert result.dropped_reasons == ("index_unavailable",)
    assert "CANARY" not in repr(result)


def test_topic_discussion_keeps_scope_when_index_is_unavailable(tmp_path) -> None:
    _repo, _store, bridge, _message = setup_bridge(
        tmp_path, search=Search(error=OSError("C:/private/index.sqlite3 CANARY")),
    )

    result = bridge.recall("Windows 构建分层缓存", review_scope="memory_topic_discussion")

    assert result.status == "degraded"
    assert result.review_scope == "memory_topic_discussion"
    assert result.dropped_reasons == ("index_unavailable",)
