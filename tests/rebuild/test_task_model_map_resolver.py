"""TaskModelMapResolver 单元测试 — 3.13 验收第三条（task_model_map 业务消费）。"""

from __future__ import annotations

import pytest

from core.product_core.task_model_map_resolver import (
    ResolvedModelProfile,
    TaskModelMapResolution,
    TaskModelMapResolver,
)


def test_resolve_returns_profile_when_entry_exists() -> None:
    resolver = TaskModelMapResolver(
        {
            "memory": {
                "profile_id": "mp-memory-main",
                "provider_id": "deepseek",
                "model_name": "deepseek-chat",
            }
        }
    )

    resolution = resolver.resolve("memory")

    assert resolution.available is True
    assert resolution.profile is not None
    assert resolution.profile.profile_id == "mp-memory-main"
    assert resolution.profile.provider_id == "deepseek"
    assert resolution.profile.model_name == "deepseek-chat"
    assert resolution.profile.available is True


def test_resolve_returns_none_when_use_key_missing() -> None:
    resolver = TaskModelMapResolver({"memory": {"profile_id": "mp-memory-main"}})

    resolution = resolver.resolve("asr")

    assert resolution.available is False
    assert resolution.profile is None
    assert resolution.to_ref() is None


def test_resolve_returns_none_when_entry_not_mapping() -> None:
    resolver = TaskModelMapResolver({"memory": "not-a-mapping"})

    resolution = resolver.resolve("memory")

    assert resolution.available is False
    assert resolution.profile is None


def test_to_ref_omits_none_fields() -> None:
    resolver = TaskModelMapResolver(
        {"lightweight": {"profile_id": "mp-light", "available": True}}
    )

    resolution = resolver.resolve("lightweight")

    ref = resolution.to_ref()
    assert ref == {
        "use_key": "lightweight",
        "profile_id": "mp-light",
        "available": True,
    }
    # 不应包含 provider_id / model_name（值为 None）
    assert "provider_id" not in ref
    assert "model_name" not in ref


def test_available_defaults_to_profile_id_presence() -> None:
    # 缺省 available 时，profile_id 存在则 available=True
    resolver = TaskModelMapResolver({"default": {"profile_id": "mp-default"}})

    resolution = resolver.resolve("default")

    assert resolution.available is True
    assert resolution.profile.available is True

    # profile_id 缺失时 available=False
    resolver_empty = TaskModelMapResolver({"default": {}})
    empty_resolution = resolver_empty.resolve("default")

    assert empty_resolution.available is False
    assert empty_resolution.profile.available is False


def test_explicit_available_false_overrides_profile_id_presence() -> None:
    resolver = TaskModelMapResolver(
        {"asr": {"profile_id": "mp-asr-local", "available": False}}
    )

    resolution = resolver.resolve("asr")

    assert resolution.profile is not None
    assert resolution.profile.profile_id == "mp-asr-local"
    assert resolution.profile.available is False
    assert resolution.available is False


def test_refs_for_returns_only_non_none_refs() -> None:
    resolver = TaskModelMapResolver(
        {
            "memory": {"profile_id": "mp-memory"},
            "asr": {"profile_id": "mp-asr"},
            # 缺失 intakeMain / lightweight
        }
    )

    refs = resolver.refs_for(("intakeMain", "memory", "lightweight", "asr"))

    assert len(refs) == 2
    use_keys = {ref["use_key"] for ref in refs}
    assert use_keys == {"memory", "asr"}


def test_refs_for_empty_task_model_map_returns_empty_tuple() -> None:
    resolver = TaskModelMapResolver({})

    refs = resolver.refs_for(("memory", "asr"))

    assert refs == ()


def test_resolve_many_returns_tuple_in_order() -> None:
    resolver = TaskModelMapResolver(
        {
            "memory": {"profile_id": "mp-memory"},
            "asr": {"profile_id": "mp-asr"},
        }
    )

    resolutions = resolver.resolve_many(("asr", "memory"))

    assert len(resolutions) == 2
    assert resolutions[0].use_key == "asr"
    assert resolutions[1].use_key == "memory"
    assert all(r.available for r in resolutions)


def test_resolver_rejects_non_mapping_input() -> None:
    with pytest.raises(TypeError):
        TaskModelMapResolver(["not", "a", "mapping"])  # type: ignore[arg-type]


def test_resolver_rejects_empty_use_key() -> None:
    resolver = TaskModelMapResolver({"memory": {"profile_id": "mp-memory"}})

    with pytest.raises(ValueError):
        resolver.resolve("")


def test_to_ref_does_not_include_sensitive_fields() -> None:
    # task_model_map 已被 SaveDeveloperStudioConfig._reject_sensitive_material
    # 拒绝 api_key/cookie/authorization，但 Resolver 也应只输出非敏感字段。
    # 这里模拟一个「假设 api_key 漏过」的场景，验证 to_ref 不输出它。
    resolver = TaskModelMapResolver(
        {
            "memory": {
                "profile_id": "mp-memory",
                "provider_id": "deepseek",
                "model_name": "deepseek-chat",
                "api_key": "sk-leaked-token-1234567",  # 假设漏过
            }
        }
    )

    resolution = resolver.resolve("memory")
    ref = resolution.to_ref()

    # to_ref 只输出 use_key / profile_id / provider_id / model_name / available
    assert "api_key" not in ref
    assert "cookie" not in ref
    assert "authorization" not in ref
    assert ref["profile_id"] == "mp-memory"
    assert ref["provider_id"] == "deepseek"
    assert ref["model_name"] == "deepseek-chat"


def test_resolved_model_profile_to_ref_includes_all_available_fields() -> None:
    profile = ResolvedModelProfile(
        use_key="vision",
        profile_id="mp-vision",
        provider_id="local-vision",
        model_name="qwen-vl",
        available=True,
    )

    ref = profile.to_ref()

    assert ref == {
        "use_key": "vision",
        "profile_id": "mp-vision",
        "provider_id": "local-vision",
        "model_name": "qwen-vl",
        "available": True,
    }


def test_task_model_map_resolution_available_property_handles_none_profile() -> None:
    resolution = TaskModelMapResolution(use_key="asr", profile=None)

    assert resolution.available is False
    assert resolution.to_ref() is None
