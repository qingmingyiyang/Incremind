from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal


# 业务可消费的 task use keys（与前端 TASK_MODEL_USES 对齐）
TASK_MODEL_USE_KEYS: tuple[str, ...] = (
    "lightweight",
    "intakeMain",
    "default",
    "memory",
    "search",
    "embed",
    "vision",
    "asr",
)

TaskModelUseKey = Literal[
    "lightweight",
    "intakeMain",
    "default",
    "memory",
    "search",
    "embed",
    "vision",
    "asr",
]


@dataclass(frozen=True, slots=True)
class ResolvedModelProfile:
    """业务流程消费 task_model_map 后解析出的 model_profile 引用。

    不包含 API key / cookie / authorization 等敏感字段——这些只存于本地
    secret registry，不进入 DeveloperStudio config，也不进入本结构。
    """

    use_key: str
    profile_id: str | None
    provider_id: str | None
    model_name: str | None
    available: bool

    def to_ref(self) -> dict[str, object]:
        """返回可安全写入 structure_record / auto_organization 的引用字典。"""
        ref: dict[str, object] = {"use_key": self.use_key}
        if self.profile_id is not None:
            ref["profile_id"] = self.profile_id
        if self.provider_id is not None:
            ref["provider_id"] = self.provider_id
        if self.model_name is not None:
            ref["model_name"] = self.model_name
        ref["available"] = self.available
        return ref


@dataclass(frozen=True, slots=True)
class TaskModelMapResolution:
    use_key: str
    profile: ResolvedModelProfile | None

    @property
    def available(self) -> bool:
        return self.profile is not None and self.profile.available

    def to_ref(self) -> dict[str, object] | None:
        if self.profile is None:
            return None
        return self.profile.to_ref()


class TaskModelMapResolver:
    """把只读旧 task_model_map 解析为历史追踪用的 model_profile 引用。

    task_model_map 形如：
        {
            "memory": {"profile_id": "mp-memory-main", "provider_id": "deepseek", "model_name": "deepseek-chat"},
            "asr": {"profile_id": "mp-asr-local", "provider_id": "local-command-asr", "model_name": "whisper-large-v3"},
            ...
        }

    本 use case 不访问 ObjectStore（不读取 model_profiles 详情），只做轻量解析；
    业务流程可以拿 to_ref() 写入 structure_record / auto_organization 的
    model_profile_ref 字段，保留旧配置声称的模型信息。该信息不证明实际
    Provider调用，也不能作为Model Route运行证据。
    """

    def __init__(self, task_model_map: Mapping[str, object]) -> None:
        if not isinstance(task_model_map, Mapping):
            raise TypeError("task_model_map must be a Mapping")
        self._task_model_map = task_model_map

    def resolve(self, use_key: str) -> TaskModelMapResolution:
        if not isinstance(use_key, str) or not use_key.strip():
            raise ValueError("use_key is required")
        entry = self._task_model_map.get(use_key)
        if not isinstance(entry, Mapping):
            return TaskModelMapResolution(use_key=use_key, profile=None)
        profile = _resolve_profile(use_key, entry)
        return TaskModelMapResolution(use_key=use_key, profile=profile)

    def resolve_many(self, use_keys: tuple[str, ...]) -> tuple[TaskModelMapResolution, ...]:
        return tuple(self.resolve(key) for key in use_keys)

    def refs_for(self, use_keys: tuple[str, ...]) -> tuple[dict[str, object], ...]:
        """返回非 None 的 to_ref() 列表，用于写入 record 的 model_profile_refs。"""
        refs: list[dict[str, object]] = []
        for resolution in self.resolve_many(use_keys):
            ref = resolution.to_ref()
            if ref is not None:
                refs.append(ref)
        return tuple(refs)


def _resolve_profile(use_key: str, entry: Mapping[str, object]) -> ResolvedModelProfile:
    profile_id = _optional_str(entry.get("profile_id"))
    provider_id = _optional_str(entry.get("provider_id"))
    model_name = _optional_str(entry.get("model_name"))
    # 显式 available 标记，缺省时按 profile_id 是否存在判定
    explicit_available = entry.get("available")
    if isinstance(explicit_available, bool):
        available = explicit_available
    else:
        available = profile_id is not None or provider_id is not None or model_name is not None
    return ResolvedModelProfile(
        use_key=use_key,
        profile_id=profile_id,
        provider_id=provider_id,
        model_name=model_name,
        available=available,
    )


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None
