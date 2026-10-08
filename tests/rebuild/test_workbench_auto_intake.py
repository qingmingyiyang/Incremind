from __future__ import annotations

from pathlib import Path

from core.ingestion_core import ObjectStoreSourceRegistrar
from core.job_runner import ObjectStoreJobRepository
from core.product_core import (
    OrchestrateWorkbenchAutoIntake,
    ServeWorkbenchAutoIntakeEndpoint,
    serialize_workbench_auto_intake_item,
    serialize_workbench_auto_intake_result,
)
from core.storage_provider import JsonObjectStore


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _orchestrator(
    store: JsonObjectStore,
    *,
    fetch_html: str = "<html><body><p>个人 AI 记忆工作台 资料库 记忆 四层</p></body></html>",
    namespace_id: str = "default",
) -> OrchestrateWorkbenchAutoIntake:
    return OrchestrateWorkbenchAutoIntake(
        object_store=store,
        source_registrar=ObjectStoreSourceRegistrar(store, namespace_id=namespace_id),
        job_repository=ObjectStoreJobRepository(store),
        fetch_url=lambda url: fetch_html,
        namespace_id=namespace_id,
    )


def _source_ids(store: JsonObjectStore) -> list[str]:
    return [str(item.get("id")) for item in store.list("sources") if item.get("id")]


def test_direct_question_when_not_adding_to_knowledge_base(tmp_path: Path) -> None:
    """阶段 1.5：add_to_knowledge_base=False 不再走独立 direct_question 分支。

    所有输入都进入 L0 + Memory Router。legacy 字段保留兼容但不再改变行为。
    """
    store = _store(tmp_path)
    orchestrator = _orchestrator(store)

    result = orchestrator.execute(
        content="什么是四层记忆",
        add_to_knowledge_base=False,  # legacy 字段，阶段 1.5 后被忽略
    )

    # 即使 add_to_knowledge_base=False，也创建 Source（进入 L0）
    assert result.status == "accepted"
    assert result.job_id.startswith("job-intake-")
    assert len(result.items) == 1
    # memory_event 必须存在，且识别为 question
    assert result.memory_event is not None
    assert result.memory_event["memory_event_type"] == "question"
    assert _source_ids(store) != [], "阶段 1.5：所有输入都进入 L0"


def test_webpage_link_auto_intake_creates_source_and_job(tmp_path: Path) -> None:
    store = _store(tmp_path)
    orchestrator = _orchestrator(store)

    result = orchestrator.execute(
        content="https://example.com/article",
        add_to_knowledge_base=True,
    )

    assert result.status == "accepted"
    assert result.job_id.startswith("job-intake-")
    assert len(result.items) == 1
    item = result.items[0]
    assert item.source_id.startswith("source-")
    assert item.input_type == "webpage"
    assert item.workflow == "link_auto_organization"
    assert item.content_read_status == "completed"
    assert item.structure_status == "completed"
    assert item.series_status == "confirmed"
    assert item.series_confidence >= 0.82
    assert item.needs_user_confirmation is False
    assert item.auto_organization["memory_publication_state"] == "not_published"
    payload = serialize_workbench_auto_intake_result(result)
    assert payload["items"][0]["progression_mode"] == "auto"
    assert payload["items"][0]["progression_reason"] == "deterministic"

    source = store.read("sources", item.source_id)
    assert source is not None
    assert source.get("type") == "link"
    content_read = store.read("source_content_reads", f"content-read-{item.source_id}")
    assert content_read is not None
    assert content_read.get("status") == "completed"
    structure = store.read("source_structures", f"structure-{item.source_id}")
    assert structure is not None
    assignment = store.read("source_series_assignments", f"series-assignment-{item.source_id}")
    assert assignment is not None
    assert assignment.get("confirmed_by") == "system"
    assert assignment.get("reason")
    tag_index_record_count = len(store.list("tag_index"))
    assert tag_index_record_count > 0, "auto intake must index paragraph tags"
    assert item.auto_organization["tag_index_status"] == "indexed"


def test_bookmark_collection_multi_link_creates_child_jobs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    orchestrator = _orchestrator(store)

    result = orchestrator.execute(
        content="https://example.com/a\nhttps://example.com/b",
        add_to_knowledge_base=True,
    )

    assert result.status == "accepted"
    assert len(result.items) == 2
    source_ids = {item.source_id for item in result.items}
    assert len(source_ids) == 2, "each child link must get its own Source"
    for item in result.items:
        assert item.input_type == "webpage"
        assert item.content_read_status == "completed"
        assert item.structure_status == "completed"
        assert item.series_status == "confirmed"
    assert all(not item.needs_user_confirmation for item in result.items)
    all_sources = store.list("sources")
    link_sources = [s for s in all_sources if s.get("type") == "link"]
    assert len(link_sources) == 2
    collection_sources = [s for s in all_sources if s.get("type") == "collection"]
    assert len(collection_sources) == 1


def test_text_auto_intake_structures_and_assigns_series(tmp_path: Path) -> None:
    store = _store(tmp_path)
    orchestrator = _orchestrator(store)

    result = orchestrator.execute(
        content="个人 AI 记忆工作台的资料库需要长期记忆和四层结构。",
        add_to_knowledge_base=True,
    )

    assert result.status == "accepted"
    assert len(result.items) == 1
    item = result.items[0]
    assert item.input_type == "direct_idea"
    assert item.workflow == "text_auto_organization"
    assert item.content_read_status == "completed"
    assert item.structure_status == "completed"
    assert item.series_status == "confirmed"
    assert item.series_name == "个人 AI 记忆工作台"
    structure = store.read("source_structures", f"structure-{item.source_id}")
    assert structure is not None
    assert len(structure.get("paragraph_tags", [])) > 0


def test_low_confidence_file_returns_needs_confirmation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    orchestrator = _orchestrator(store)

    result = orchestrator.execute(
        media_type="application/x-unknown",
        file_name="mystery.dat",
        add_to_knowledge_base=True,
    )

    assert result.status == "needs_confirmation"
    assert len(result.items) == 1
    item = result.items[0]
    assert item.status == "needs_confirmation"
    assert item.needs_user_confirmation is True
    payload = serialize_workbench_auto_intake_result(result)
    assert payload["items"][0]["progression_mode"] == "ask"
    assert payload["items"][0]["progression_reason"] == "material_ambiguity"
    assert item.source_id == ""
    assert _source_ids(store) == [], "low confidence intake must not persist a Source"


def test_file_upload_captures_reference_without_local_path(tmp_path: Path) -> None:
    store = _store(tmp_path)
    orchestrator = _orchestrator(store)

    result = orchestrator.execute(
        media_type="application/pdf",
        file_name="report.pdf",
        add_to_knowledge_base=True,
    )

    assert result.status == "accepted"
    assert len(result.items) == 1
    item = result.items[0]
    assert item.input_type == "pdf"
    assert item.status == "needs_extractor"
    assert item.next_step == "await_document_extractor"
    serialized = serialize_workbench_auto_intake_item(item)
    assert serialized["progression_mode"] == "ask"
    assert serialized["progression_reason"] == "permission_expansion"
    encoded = str(serialized)
    assert not __import__("re").search(r"sk-[A-Za-z0-9_-]{8,}", encoded), "no API key material in response"
    assert not __import__("re").search(r"[A-Za-z]:\\\\", encoded), "no Windows absolute path in response"
    assert item.auto_organization["path_policy"] == "no_os_absolute_path_in_response"


def test_endpoint_serves_auto_intake_accepted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    orchestrator = _orchestrator(store)
    endpoint = ServeWorkbenchAutoIntakeEndpoint()

    response = endpoint.execute(
        method="POST",
        path="/api/rebuild/workbench/auto-intake",
        body={
            "content": "个人 AI 记忆工作台 资料库 记忆",
            "add_to_knowledge_base": True,
        },
        orchestrate=orchestrator.execute,
    )

    assert response.status_code == 201
    assert response.body["status"] == "accepted"
    assert len(response.body["items"]) == 1
    assert response.body["items"][0]["input_type"] == "direct_idea"
    assert response.body["next_ui"] == "library_job_status"


def test_endpoint_serves_direct_question(tmp_path: Path) -> None:
    """阶段 1.5：add_to_knowledge_base=False 不再走 direct_question 分支。

    legacy 字段保留兼容，但所有输入都进入 L0 + Memory Router。
    提问内容也会创建 Source 并被识别为 question memory_event_type。
    """
    store = _store(tmp_path)
    orchestrator = _orchestrator(store)
    endpoint = ServeWorkbenchAutoIntakeEndpoint()

    response = endpoint.execute(
        method="POST",
        path="/api/rebuild/workbench/auto-intake",
        body={
            "content": "如何使用资料库",
            "add_to_knowledge_base": False,  # legacy 字段，阶段 1.5 后被忽略
        },
        orchestrate=orchestrator.execute,
    )

    # 提问内容也进入 L0，status=accepted，HTTP 201
    assert response.status_code == 201
    assert response.body["status"] == "accepted"
    # direct_question_hint 不再返回（legacy 字段已被忽略）
    assert response.body["direct_question_hint"] is None
    # memory_event 必须存在，识别为 question
    assert response.body["memory_event"] is not None
    assert response.body["memory_event"]["memory_event_type"] == "question"


def test_endpoint_rejects_wrong_method(tmp_path: Path) -> None:
    store = _store(tmp_path)
    orchestrator = _orchestrator(store)
    endpoint = ServeWorkbenchAutoIntakeEndpoint()

    response = endpoint.execute(
        method="GET",
        path="/api/rebuild/workbench/auto-intake",
        body={},
        orchestrate=orchestrator.execute,
    )

    assert response.status_code == 405


def test_response_contains_no_secret_or_local_path(tmp_path: Path) -> None:
    store = _store(tmp_path)
    orchestrator = _orchestrator(store)
    endpoint = ServeWorkbenchAutoIntakeEndpoint()

    response = endpoint.execute(
        method="POST",
        path="/api/rebuild/workbench/auto-intake",
        body={
            "content": "个人 AI 记忆工作台 资料库 记忆",
            "add_to_knowledge_base": True,
        },
        orchestrate=orchestrator.execute,
    )

    encoded = str(response.body)
    assert not __import__("re").search(r"sk-[A-Za-z0-9_-]{8,}", encoded), "no API key material in response"
    assert not __import__("re").search(r"[A-Za-z]:\\\\", encoded), "no Windows absolute path in response"
    assert "password=" not in encoded.lower()


def test_web_content_read_failure_returns_needs_confirmation(tmp_path: Path) -> None:
    store = _store(tmp_path)

    def empty_fetch(url: str) -> str:
        return ""

    orchestrator = OrchestrateWorkbenchAutoIntake(
        object_store=store,
        source_registrar=ObjectStoreSourceRegistrar(store, namespace_id="default"),
        job_repository=ObjectStoreJobRepository(store),
        fetch_url=empty_fetch,
        namespace_id="default",
    )

    result = orchestrator.execute(
        content="https://example.com/down",
        add_to_knowledge_base=True,
    )

    assert result.status == "accepted"
    assert len(result.items) == 1
    item = result.items[0]
    assert item.status == "needs_confirmation"
    assert item.needs_user_confirmation is True
    assert "read_error" in item.auto_organization
    payload = serialize_workbench_auto_intake_result(result)
    assert payload["items"][0]["progression_mode"] == "ask"
    assert payload["items"][0]["progression_reason"] == "material_ambiguity"


def test_web_content_read_exception_returns_failed_item(tmp_path: Path) -> None:
    store = _store(tmp_path)

    def failing_fetch(url: str) -> str:
        raise RuntimeError("network unreachable")

    orchestrator = OrchestrateWorkbenchAutoIntake(
        object_store=store,
        source_registrar=ObjectStoreSourceRegistrar(store, namespace_id="default"),
        job_repository=ObjectStoreJobRepository(store),
        fetch_url=failing_fetch,
        namespace_id="default",
    )

    result = orchestrator.execute(
        content="https://example.com/down",
        add_to_knowledge_base=True,
    )

    assert result.status == "accepted"
    assert len(result.items) == 1
    item = result.items[0]
    assert item.status == "failed"
    assert "error" in item.auto_organization


def test_serializer_roundtrip_is_stable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    orchestrator = _orchestrator(store)

    result = orchestrator.execute(
        content="个人 AI 记忆工作台 资料库 记忆",
        add_to_knowledge_base=True,
    )

    payload = serialize_workbench_auto_intake_result(result)
    assert payload["status"] == "accepted"
    assert isinstance(payload["items"], list)
    assert payload["items"][0]["source_id"] == result.items[0].source_id
    assert payload["classification"]["input_type"] == "direct_idea"
