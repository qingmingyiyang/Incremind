from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import re
from threading import Lock
from typing import Any

from backend.shared.filesystem import atomic_write_text
from backend.shared.server_resources import RESOURCE_POOL
from core.product_core.model_dispatch_authority import model_dispatch_authority_fence


_PROVIDER_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ProviderRegistry:
    """Stores non-secret provider metadata in the global library area."""

    def __init__(self, root_dir: Path) -> None:
        self._root_dir = Path(root_dir).resolve()
        self._path = root_dir / "library" / "global" / "providers" / "providers.json"
        self._lock = Lock()
        resources = RESOURCE_POOL.get()
        if resources is None:
            self._mutation_attribution = None
        else:
            factory = resources.provider_file_attribution_factory
            if not callable(factory):
                raise ValueError('server_provider_attribution_factory_unconfigured')
            self._mutation_attribution = factory(root_dir, 'providers')

    def list(self, *, fallback: dict[str, Any]) -> list[dict[str, Any]]:
        with model_dispatch_authority_fence(self._root_dir), self._lock:
            payload = self._read()
            providers = payload.get("providers", [])
            if not providers:
                provider = self._normalize({**fallback, "provider_id": "openai", "is_active": True})
                payload = {"active_provider_id": "openai", "providers": [provider]}
                self._write(payload)
                return [{**provider, "is_active": True}]
            active_id = str(payload.get("active_provider_id") or providers[0]["provider_id"])
            return [{**item, "is_active": item["provider_id"] == active_id} for item in providers]

    def list_readonly(self, *, fallback: dict[str, Any]) -> list[dict[str, Any]]:
        """Returns registry records without bootstrapping persistent defaults."""
        with model_dispatch_authority_fence(self._root_dir), self._lock:
            payload = self._read()
            providers = payload.get("providers", [])
            if not providers:
                provider = self._normalize({**fallback, "provider_id": "openai", "is_active": True})
                return [{**provider, "is_active": True}]
            active_id = str(payload.get("active_provider_id") or providers[0]["provider_id"])
            return [{**item, "is_active": item["provider_id"] == active_id} for item in providers]

    def get(self, provider_id: str, *, fallback: dict[str, Any]) -> dict[str, Any]:
        for item in self.list(fallback=fallback):
            if item["provider_id"] == provider_id:
                return item
        raise KeyError(provider_id)

    def get_readonly(self, provider_id: str, *, fallback: dict[str, Any]) -> dict[str, Any]:
        for item in self.list_readonly(fallback=fallback):
            if item["provider_id"] == provider_id:
                return item
        raise KeyError(provider_id)

    def create(self, values: dict[str, Any], *, fallback: dict[str, Any]) -> dict[str, Any]:
        with model_dispatch_authority_fence(self._root_dir), self._lock:
            payload = self._ensure_payload(fallback)
            item = self._normalize(values)
            if any(current["provider_id"] == item["provider_id"] for current in payload["providers"]):
                raise ValueError("供应商标识已存在。")
            payload["providers"].append(item)
            self._write(payload)
            return {**item, "is_active": False}

    def update(self, provider_id: str, values: dict[str, Any], *, fallback: dict[str, Any]) -> dict[str, Any]:
        with model_dispatch_authority_fence(self._root_dir), self._lock:
            payload = self._ensure_payload(fallback)
            for index, current in enumerate(payload["providers"]):
                if current["provider_id"] != provider_id:
                    continue
                next_item = self._normalize(
                    {
                        **current,
                        **values,
                        "provider_id": provider_id,
                        "created_at": current["created_at"],
                        "updated_at": _now(),
                    }
                )
                payload["providers"][index] = next_item
                self._write(payload)
                return {**next_item, "is_active": payload["active_provider_id"] == provider_id}
        raise KeyError(provider_id)

    def activate(self, provider_id: str, *, fallback: dict[str, Any]) -> dict[str, Any]:
        with model_dispatch_authority_fence(self._root_dir), self._lock:
            payload = self._ensure_payload(fallback)
            for item in payload["providers"]:
                if item["provider_id"] == provider_id:
                    payload["active_provider_id"] = provider_id
                    self._write(payload)
                    return {**item, "is_active": True}
        raise KeyError(provider_id)

    def delete(self, provider_id: str, *, fallback: dict[str, Any]) -> None:
        with model_dispatch_authority_fence(self._root_dir), self._lock:
            payload = self._ensure_payload(fallback)
            if payload["active_provider_id"] == provider_id:
                raise ValueError("当前启用的供应商不能删除，请先切换供应商。")
            next_items = [item for item in payload["providers"] if item["provider_id"] != provider_id]
            if len(next_items) == len(payload["providers"]):
                raise KeyError(provider_id)
            payload["providers"] = next_items
            self._write(payload)

    def _ensure_payload(self, fallback: dict[str, Any]) -> dict[str, Any]:
        payload = self._read()
        if payload.get("providers"):
            return payload
        provider = self._normalize({**fallback, "provider_id": "openai", "is_active": True})
        return {"active_provider_id": "openai", "providers": [provider]}

    def _read(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("供应商注册表损坏，无法安全读取。") from error
        return payload if isinstance(payload, dict) else {}

    def _write(self, payload: dict[str, Any]) -> None:
        if self._mutation_attribution is not None:
            payload = self._mutation_attribution.json(payload,self._read())
        self._path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self._path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    @staticmethod
    def _normalize(values: dict[str, Any]) -> dict[str, Any]:
        provider_id = str(values.get("provider_id", "")).strip().lower()
        if not _PROVIDER_ID.fullmatch(provider_id):
            raise ValueError("供应商标识仅支持小写字母、数字、连字符和下划线，最长 64 位。")
        name = str(values.get("name") or provider_id).strip()
        if not name:
            raise ValueError("供应商名称不能为空。")
        base_url = str(values.get("base_url", "")).strip().rstrip("/")
        if base_url and not base_url.startswith(("http://", "https://")):
            raise ValueError("模型服务地址必须包含 http:// 或 https://。")
        api_path = "/" + str(values.get("api_path") or "chat/completions").strip().strip("/")
        models = sorted({str(item).strip() for item in values.get("models", []) if str(item).strip()})
        model = str(values.get("model", "")).strip()
        if model and model not in models:
            models.append(model)
        created_at = str(values.get("created_at") or _now())
        return {
            "provider_id": provider_id,
            "name": name[:80],
            "llm_provider": str(values.get("llm_provider") or "openai").strip(),
            "base_url": base_url,
            "api_path": api_path,
            "model": model,
            "models": models,
            "enabled": bool(values.get("enabled", True)),
            "created_at": created_at,
            "updated_at": str(values.get("updated_at") or created_at),
        }
