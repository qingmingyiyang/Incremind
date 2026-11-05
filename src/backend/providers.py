from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import re
from threading import RLock
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from backend.shared.filesystem import atomic_write_text
from backend.shared.server_resources import RESOURCE_POOL
from core.product_core.model_dispatch_authority import model_dispatch_authority_fence


_PROVIDER_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
CONNECTIONS = 'v2_provider_connections'
CONNECTION_USES = 'v2_provider_connection_uses'
CONNECTION_OPERATIONS = 'v2_provider_connection_operations'


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ProviderRegistry:
    """Stores non-secret provider metadata in the global library area."""

    def __init__(self, root_dir: Path, *, connection_owner=None) -> None:
        self._root_dir = Path(root_dir).resolve()
        self._path = root_dir / "library" / "global" / "providers" / "providers.json"
        self._lock = RLock()
        self._connection_owner = connection_owner
        if connection_owner is not None:
            from backend.memory_app.model_config import ModelConfiguration
            if (not isinstance(connection_owner, ModelConfiguration)
                    or Path(connection_owner.root).resolve() != self._root_dir):
                raise ValueError('provider_connection_owner_invalid')
            self.records, self.secrets = connection_owner.records, connection_owner.secrets
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

    def _connections_owner(self):
        owner = self._connection_owner
        if (owner is None or Path(owner.root).resolve() != self._root_dir
                or owner.records is not self.records or owner.secrets is not self.secrets):
            raise ValueError('provider_connection_owner_invalid')
        return owner

    @staticmethod
    def validate_connection(values):
        from backend.memory_app.model_config import _validate_generation_egress
        if (not isinstance(values, dict) or set(values) != {'name', 'base_url', 'models'}
                or not isinstance(values['name'], str) or not 1 <= len(values['name'].strip()) <= 80
                or not isinstance(values['base_url'], str) or len(values['base_url']) > 2048
                or not isinstance(values['models'], list) or not 1 <= len(values['models']) <= 64
                or any(not isinstance(model, str) or not 1 <= len(model.strip()) <= 200 for model in values['models'])):
            raise ValueError('provider_connection_invalid')
        models = list(dict.fromkeys(model.strip() for model in values['models']))
        base = values['base_url'].strip()
        try:
            _validate_generation_egress({'provider': 'openai', 'base_url': base,
                'model': models[0], 'allow_remote': True})
            if urlsplit(base).hostname in {'localhost', '127.0.0.1', '::1'}:
                raise ValueError('loopback')
        except ValueError:
            raise ValueError('provider_connection_endpoint_invalid') from None
        return {'name': values['name'].strip(), 'provider': 'openai', 'base_url': base, 'models': models}

    def connections(self):
        owner = self._connections_owner()
        with owner._lock, self.records.begin_read() as reader:
            return [self._connection_view(reader, row) for row in reader.list(CONNECTIONS)]

    def _connection_view(self, reader, row):
        value = row.payload
        return {'id': row.object_id, 'revision': row.revision,
            **{key: value[key] for key in ('name', 'provider', 'base_url', 'models', 'enabled')},
            'has_api_key': bool(value.get('secret_ref') and self.secrets.has_secret(value['secret_ref'])),
            'last_operation_id': value.get('operation_id'),
            'uses': [use.object_id for use in reader.list(CONNECTION_USES)
                if use.payload['connection_id'] == row.object_id],
            'updated_at': value['updated_at']}

    def save_connection(self, identity, values, *, api_key, expected_revision, operation_id=None):
        owner = self._connections_owner()
        operation_id = operation_id or 'save-' + uuid4().hex
        if (not isinstance(identity, str) or not _PROVIDER_ID.fullmatch(identity)
                or type(expected_revision) is not int or expected_revision < 0
                or not isinstance(operation_id, str) or not _PROVIDER_ID.fullmatch(operation_id)
                or not isinstance(api_key, str) or len(api_key) > 8192):
            raise ValueError('provider_connection_invalid')
        normalized = self.validate_connection(values)
        with owner._lock, self._lock:
            previous = self.records.read(CONNECTIONS, identity)
            if self.records.read(CONNECTION_OPERATIONS, operation_id) is not None:
                raise ValueError('provider_connection_operation_already_recorded')
            old_ref = previous.payload.get('secret_ref', '') if previous else ''
            ref = old_ref
            if api_key.strip():
                ref = 'provider-connection-' + uuid4().hex
                self.secrets.set(ref, api_key)
            try:
                if not ref or not self.secrets.has_secret(ref):
                    raise ValueError('provider_connection_key_required')
                # 换地址必须明确提供新凭据，避免把旧服务的密钥交给新地址。
                if previous and normalized['base_url'] != previous.payload['base_url'] and ref == old_ref:
                    raise ValueError('provider_connection_new_key_required')
                with self.records.begin() as tx:
                    if tx.read(CONNECTION_OPERATIONS, operation_id) is not None:
                        raise ValueError('provider_connection_operation_already_recorded')
                    saved = tx.put(CONNECTIONS, identity, {**normalized, 'enabled': True,
                        'secret_ref': ref, 'updated_at': _now(), 'operation_id': operation_id}, expected_revision=expected_revision)
                    result = self._connection_view(tx, saved)
                    tx.put(CONNECTION_OPERATIONS, operation_id, {'status': 'accepted', 'action': 'save',
                        'connection_id': identity, 'result_revision': saved.revision, 'at': _now()}, expected_revision=0)
                    tx.commit()
            except BaseException:
                if ref and ref != old_ref:
                    self.secrets.delete(ref)
                raise
            self.release_connection_secret(old_ref)
            return result

    def disconnect_connection(self, identity, *, expected_revision, operation_id=None):
        owner = self._connections_owner()
        operation_id = operation_id or 'disconnect-' + uuid4().hex
        if (type(expected_revision) is not int or expected_revision < 1
                or not isinstance(operation_id, str) or not _PROVIDER_ID.fullmatch(operation_id)):
            raise ValueError('provider_connection_invalid')
        with owner._lock, self._lock:
            with self.records.begin() as tx:
                if tx.read(CONNECTION_OPERATIONS, operation_id) is not None:
                    raise ValueError('provider_connection_operation_already_recorded')
                row = tx.read(CONNECTIONS, identity)
                if row is None:
                    raise ValueError('provider_connection_not_found')
                saved = tx.put(CONNECTIONS, identity, {**row.payload, 'enabled': False,
                    'secret_ref': '', 'updated_at': _now(), 'operation_id': operation_id}, expected_revision=expected_revision)
                result = self._connection_view(tx, saved)
                tx.put(CONNECTION_OPERATIONS, operation_id, {'status': 'accepted', 'action': 'disconnect',
                    'connection_id': identity, 'result_revision': saved.revision, 'at': _now()}, expected_revision=0)
                tx.commit()
            self.release_connection_secret(row.payload.get('secret_ref', ''))
            return result

    def connection_operation(self, operation_id, *, cancel=False):
        owner = self._connections_owner()
        if not isinstance(operation_id, str) or not _PROVIDER_ID.fullmatch(operation_id):
            raise ValueError('provider_connection_invalid')
        with owner._lock, self._lock, self.records.begin() as tx:
            row = tx.read(CONNECTION_OPERATIONS, operation_id)
            if row is None and cancel:
                row = tx.put(CONNECTION_OPERATIONS, operation_id,
                    {'status': 'cancelled', 'action': None, 'connection_id': None, 'result_revision': None, 'at': _now()},
                    expected_revision=0)
                tx.commit()
            return {'operation_id': operation_id, **row.payload} if row else {'operation_id': operation_id, 'status': 'not_recorded'}

    def stage_connection_selection(self, tx, operation_id, *, identity, purpose, configuration_revision, connection_revision):
        self._connections_owner()
        if not isinstance(operation_id, str) or not _PROVIDER_ID.fullmatch(operation_id):
            raise ValueError('provider_connection_invalid')
        if tx.read(CONNECTION_OPERATIONS, operation_id) is not None:
            raise ValueError('provider_connection_operation_already_recorded')
        tx.put(CONNECTION_OPERATIONS, operation_id, {'status': 'accepted', 'action': 'selection',
            'connection_id': identity, 'result_revision': configuration_revision, 'purpose': purpose,
            'connection_revision': connection_revision, 'at': _now()}, expected_revision=0)

    def connection_snapshot(self, reader, identity, expected_revision):
        self._connections_owner()
        if (not isinstance(identity, str) or not _PROVIDER_ID.fullmatch(identity)
                or type(expected_revision) is not int or expected_revision < 1):
            raise ValueError('provider_connection_invalid')
        row = reader.read(CONNECTIONS, identity)
        if row is None or row.revision != expected_revision:
            raise ValueError('provider_connection_changed')
        value = row.payload
        if not value['enabled'] or not value.get('secret_ref') or not self.secrets.has_secret(value['secret_ref']):
            raise ValueError('provider_connection_disconnected')
        return row, self.secrets.get_generation(value['secret_ref'])

    def validate_connection_use(self, reader, purpose, configuration):
        self._connections_owner()
        use = reader.read(CONNECTION_USES, purpose)
        if use is None:
            return
        binding = use.payload
        # 本机和订阅保留原方式；重新采用 API 档案时才再次核对连接资格。
        if configuration.get('secret_ref') != binding['secret_ref']:
            return
        if any(configuration.get(key) != binding[key] for key in ('provider', 'base_url', 'model')):
            raise ValueError('provider_connection_changed')
        row, generation = self.connection_snapshot(reader, binding['connection_id'], binding['connection_revision'])
        if (generation != binding['secret_generation'] or row.payload['secret_ref'] != binding['secret_ref']
                or binding['model'] not in row.payload['models']):
            raise ValueError('provider_connection_changed')

    def release_connection_secret(self, ref):
        owner = self._connections_owner()
        if not ref:
            return
        with owner._lock, self._lock, self.records.begin_read() as reader:
            referenced = any(row.payload.get('secret_ref') == ref for collection in
                (CONNECTIONS, 'recognition_model_config') for row in reader.list(collection))
            choice = reader.read('recognition_generation_mode', 'default')
            archived = choice.payload.get('api_config') if choice else None
            if referenced or isinstance(archived, dict) and archived.get('secret_ref') == ref:
                return
            self.secrets.delete(ref)

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
