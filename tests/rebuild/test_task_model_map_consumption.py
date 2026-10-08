"""3.13 验收第三条集成测试：task_model_map 能被 structuring / auto-intake 消费。

验证：
- StructureSourceContent 接收 task_model_map，写入 structure_record.model_profile_refs。
- OrchestrateWorkbenchAutoIntake 接收 task_model_map，写入 item.auto_organization.model_profile_refs。
- task_model_map 为 None 或空时向后兼容（不破坏既有调用）。
- sensitive field 保存被拒绝（验收第一条，已有测试，这里只验证 task_model_map 不引入敏感泄露）。
- ASR 模型缺失时 workflow blocked reason 明确（验收第二条，已有测试，这里不重复）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.product_core.source_structuring import StructureSourceContent
from core.product_core.task_model_map_resolver import TaskModelMapResolver
from core.storage_provider import JsonObjectStore, ObjectStorePort


@pytest.fixture
def object_store(tmp_path: Path) -> ObjectStorePort:
    # ObjectStorePort 是 typing.Protocol，不能直接实例化。
    # 使用具体实现 JsonObjectStore（参考 test_source_content_read.py 的 _store 帮助函数）。
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _seed_completed_content_read(
    store: ObjectStorePort,
    *,
    source_id: str = "source-text-1",
    text: str = "这是一个测试文本，关于个人 AI 记忆工作台。",
) -> str:
    """写入 source + completed source_content_read，返回 content_read_id。"""
    store.write(
        "sources",
        source_id,
        {
            "id": source_id,
            "type": "text",
            "title": "测试 Source",
            "storage_uri": f"crp://default/sources/{source_id}",
            "metadata": {"project_id": "project-test"},
        },
        expected_revision=None,
    )
    content_read_id = f"content-read-{source_id}"
    store.write(
        "source_content_reads",
        content_read_id,
        {
            "id": content_read_id,
            "source_id": source_id,
            "status": "completed",
            "text": text,
            "media_type": "text/plain",
        },
        expected_revision=None,
    )
    return content_read_id


def test_structuring_writes_model_profile_refs_when_task_model_map_provided(
    object_store: ObjectStorePort,
) -> None:
    content_read_id = _seed_completed_content_read(object_store)
    task_model_map = {
        "memory": {
            "profile_id": "mp-memory-main",
            "provider_id": "deepseek",
            "model_name": "deepseek-chat",
        },
        "lightweight": {
            "profile_id": "mp-light",
            "available": True,
        },
        # asr / intakeMain 缺失
    }

    result = StructureSourceContent(
        object_store,
        namespace_id="default",
    ).execute(
        source_id="source-text-1",
        content_read_id=content_read_id,
        task_model_map=task_model_map,
    )

    assert result.status == "completed"
    # structure_record 应包含 model_profile_refs
    structure = object_store.read("source_structures", "structure-source-text-1")
    assert structure is not None
    refs = structure.get("model_profile_refs")
    assert isinstance(refs, list)
    # structuring 消费 memory + lightweight 两个 use key
    use_keys = {ref["use_key"] for ref in refs}
    assert "memory" in use_keys
    assert "lightweight" in use_keys
    # 缺失的 asr / intakeMain 不应出现
    assert "asr" not in use_keys
    assert "intakeMain" not in use_keys

    memory_ref = next(ref for ref in refs if ref["use_key"] == "memory")
    assert memory_ref["profile_id"] == "mp-memory-main"
    assert memory_ref["provider_id"] == "deepseek"
    assert memory_ref["model_name"] == "deepseek-chat"
    assert memory_ref["available"] is True


def test_structuring_backward_compatible_when_task_model_map_is_none(
    object_store: ObjectStorePort,
) -> None:
    content_read_id = _seed_completed_content_read(object_store)

    result = StructureSourceContent(
        object_store,
        namespace_id="default",
    ).execute(
        source_id="source-text-1",
        content_read_id=content_read_id,
        task_model_map=None,
    )

    assert result.status == "completed"
    structure = object_store.read("source_structures", "structure-source-text-1")
    assert structure is not None
    # task_model_map 为 None 时 model_profile_refs 应为空列表（向后兼容）
    refs = structure.get("model_profile_refs")
    assert refs == []


def test_structuring_backward_compatible_when_task_model_map_is_empty(
    object_store: ObjectStorePort,
) -> None:
    content_read_id = _seed_completed_content_read(object_store)

    result = StructureSourceContent(
        object_store,
        namespace_id="default",
    ).execute(
        source_id="source-text-1",
        content_read_id=content_read_id,
        task_model_map={},
    )

    assert result.status == "completed"
    structure = object_store.read("source_structures", "structure-source-text-1")
    assert structure is not None
    assert structure.get("model_profile_refs") == []


def test_structuring_model_profile_refs_do_not_leak_sensitive_fields(
    object_store: ObjectStorePort,
) -> None:
    """即使 task_model_map 假设漏过敏感字段，Resolver.to_ref() 也不应输出它们。"""
    content_read_id = _seed_completed_content_read(object_store)
    # 假设 _reject_sensitive_material 没拦住（实际会拦），验证 Resolver 仍不输出
    task_model_map = {
        "memory": {
            "profile_id": "mp-memory",
            "api_key": "sk-leaked-token-1234567",
            "cookie": "session=abc",
            "authorization": "Bearer xyz",
        }
    }

    StructureSourceContent(object_store, namespace_id="default").execute(
        source_id="source-text-1",
        content_read_id=content_read_id,
        task_model_map=task_model_map,
    )

    structure = object_store.read("source_structures", "structure-source-text-1")
    assert structure is not None
    refs_text = str(structure.get("model_profile_refs", [])).lower()
    for forbidden in ("sk-", "api_key", "cookie", "authorization", "bearer", "password", "token"):
        assert forbidden not in refs_text, f"model_profile_refs leaked: {forbidden}"


# ── auto-intake 接入测试 ──


def _make_orchestrator(
    store: ObjectStorePort,
    *,
    task_model_map=None,
):
    """构造一个最小可用的 OrchestrateWorkbenchAutoIntake，不触发实际 media workflow。"""
    from core.product_core.workbench_auto_intake import OrchestrateWorkbenchAutoIntake
    from core.ingestion_core import SourceRegistrarPort
    from core.job_runner import JobRepositoryPort

    class _StubRegistrar(SourceRegistrarPort):
        def register_source(self, **kwargs):
            raise NotImplementedError

    class _StubJobRepo(JobRepositoryPort):
        def create_job(self, **kwargs):
            raise NotImplementedError

        def update_job(self, **kwargs):
            raise NotImplementedError

        def list_jobs(self, **kwargs):
            return []

    return OrchestrateWorkbenchAutoIntake(
        object_store=store,
        source_registrar=_StubRegistrar(),
        job_repository=_StubJobRepo(),
        fetch_url=lambda url: "",
        namespace_id="default",
        task_model_map=task_model_map,
    )


def test_auto_intake_resolver_method_returns_refs_for_intake_keys(
    object_store: ObjectStorePort,
) -> None:
    task_model_map = {
        "intakeMain": {"profile_id": "mp-intake", "provider_id": "deepseek"},
        "memory": {"profile_id": "mp-memory"},
        "lightweight": {"profile_id": "mp-light"},
        # asr 缺失
    }

    orchestrator = _make_orchestrator(object_store, task_model_map=task_model_map)

    refs = orchestrator._resolve_model_profile_refs()

    use_keys = {ref["use_key"] for ref in refs}
    assert "intakeMain" in use_keys
    assert "memory" in use_keys
    assert "lightweight" in use_keys
    # asr 不在消费列表里（auto-intake 只消费 intakeMain/memory/lightweight）
    assert "asr" not in use_keys


def test_auto_intake_resolver_returns_empty_when_task_model_map_is_none(
    object_store: ObjectStorePort,
) -> None:
    orchestrator = _make_orchestrator(object_store, task_model_map=None)

    refs = orchestrator._resolve_model_profile_refs()

    assert refs == ()


def test_auto_intake_resolver_returns_empty_when_task_model_map_is_empty(
    object_store: ObjectStorePort,
) -> None:
    orchestrator = _make_orchestrator(object_store, task_model_map={})

    refs = orchestrator._resolve_model_profile_refs()

    assert refs == ()


def test_auto_intake_resolver_does_not_leak_sensitive_fields(
    object_store: ObjectStorePort,
) -> None:
    task_model_map = {
        "intakeMain": {
            "profile_id": "mp-intake",
            "api_key": "sk-leaked-token-1234567",
            "cookie": "session=abc",
        }
    }

    orchestrator = _make_orchestrator(object_store, task_model_map=task_model_map)

    refs = orchestrator._resolve_model_profile_refs()
    refs_text = str(refs).lower()

    for forbidden in ("sk-", "api_key", "cookie", "authorization", "bearer", "password", "token"):
        assert forbidden not in refs_text, f"auto-intake refs leaked: {forbidden}"
