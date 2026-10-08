from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, field
import logging
import re
from time import monotonic, time
from threading import RLock
from types import MappingProxyType
from urllib.parse import urlsplit
from uuid import uuid4
from pydantic import BaseModel

from backend.recognition import RecognitionConflict
from backend.security.secrets import build_model_secret_store, SecretStore
from backend.shared.llm.litellm_gateway import LiteLLMCompletionGateway, ModelRetryControl, _model_transport_failure
from backend.shared.llm.json_mode import StructuredResponseDecodeError
from backend.shared.llm.openai_responses import (ResponsesCompletion, ResponsesError, ProviderBackgroundOptions,
    ProviderResumeOnlyOptions, ProviderResponseCursor)
from backend.shared.llm.model_capabilities import ModelCapabilities
from backend.shared.llm.model_transport import ModelTransportError, ModelTransportTimeout, ModelInterrupted, complete_with_owned_transport
from backend.shared.llm.model_prices import validate_rates, official_prices
from .model_costs import PRICES, configuration_binding, PriceRecordingSink
from .chatgpt_subscription import ChatGPTSubscriptions, SubscriptionError, RESOURCE, PROFILES, PROFILE
from core.ai_kernel.event_store import RunLeaseRevoked
from core.storage_provider.sqlite_uow import SQLiteStructuredRecordStore


PURPOSES = ("generation", "embedding", "rerank", "vision", "search")
_GENERATION_MODE_COLLECTION = "recognition_generation_mode"
_GENERATION_MODE_ID = "default"
_FAST_MODEL_COLLECTION = "v2_generation_fast_model"
_LOCAL_MODEL = "qwen2.5-1.5b-instruct"
_DEFAULT_LOCAL_BASE_URL = "http://127.0.0.1:8001/local-model/v1"
_GOVERNED_CONFIGURATION_FIELDS = (
    "purpose", "provider", "base_url", "model", "allow_remote", "revision",
    "configured", "has_api_key",
)
_ROUTING_DIGEST = re.compile(r"[a-f0-9]{64}")
_LOG = logging.getLogger(__name__)


class ModelConfigurationError(ValueError):
    pass


class ModelResponseDecodeError(ModelConfigurationError):
    """保留已完成 wire 的安全解码分类，不携带原响应或异常信息。"""
    def __init__(self, error):
        super().__init__('model_request_failed: check endpoint, model and credentials')
        self.decode_code = error.code
        self.completed_wire = True
        self.usage = dict(error.usage)


def _model_request_failure(error, *, include_status=True):
    status = getattr(error, "status_code", None)
    suffix = f" (HTTP {status})" if include_status and isinstance(status, int) and 100 <= status <= 599 else ""
    if type(error) is StructuredResponseDecodeError and error.completed_wire is True:
        return ModelResponseDecodeError(error)
    failure = ModelConfigurationError("model_request_failed" + suffix + ": check endpoint, model and credentials")
    if isinstance(error, ResponsesError):
        # Only our bounded, sanitized protocol facts cross this boundary. Raw
        # provider messages, bodies and credentials never become error details.
        for name in ("status_code", "code", "category", "output_started", "retryable"):
            setattr(failure, name, getattr(error, name))
    return failure


@dataclass(frozen=True)
class ProviderStoreActivation:
    """An internal adapter choice; the original Turn still owns every wire."""
    adapter: ResponsesCompletion
    validate_current: Callable[[], None]
    expected_purpose: str = 'primary'
    resume_source: Mapping[str, object] | None = field(default=None, repr=False)

    def __post_init__(self):
        if (not isinstance(self.adapter, ResponsesCompletion)
                or self.adapter.background_resume_capable is not True
                or not callable(self.validate_current)
                or type(self.expected_purpose) is not str
                or self.expected_purpose not in {'primary', 'aux'}):
            raise ModelConfigurationError('provider_background_adapter_invalid')
        if self.resume_source is not None:
            source = self.resume_source
            try:
                if (self.expected_purpose != 'primary' or not isinstance(source, Mapping)
                        or set(source) != {'attempt_id', 'dispatch_ref', 'terminal_ref', 'checkpoint_ref', 'cursor'}
                        or any(type(source[key]) is not str or not source[key] for key in
                            ('attempt_id', 'dispatch_ref', 'terminal_ref', 'checkpoint_ref'))
                        or not isinstance(source['cursor'], Mapping)
                        or set(source['cursor']) != {'response_id', 'sequence_number'}):
                    raise ValueError('invalid resume data')
                cursor = ProviderResponseCursor(**source['cursor'])
            except (ValueError, TypeError, KeyError) as error:
                raise ModelConfigurationError('provider_resume_source_invalid') from error
            # 只冻结数据；原产品主人仍须先完成闭合胶囊和动作 CAS。
            object.__setattr__(self, 'resume_source', MappingProxyType({**source,
                'cursor': MappingProxyType({'response_id': cursor.response_id, 'sequence_number': cursor.sequence_number})}))


class _BackgroundWireSink:
    """Observe through the actual Handle while its original Handler is active."""
    def __init__(self, sink):
        self.sink, self.handle = sink, None

    def begin_model_wire_attempt(self):
        handle = self.sink.begin_model_wire_attempt()
        self.handle = handle
        if not callable(getattr(handle, 'observe_provider_checkpoint', None)):
            def reject():
                handle.failed_transport(error_code='ai.provider_checkpoint_owner_unavailable')
                raise ModelConfigurationError('provider_checkpoint_owner_unavailable')
            handle.invoke_wire(reject)
        return handle

    def observe(self, cursor):
        if self.handle is None:
            raise ModelConfigurationError('provider_checkpoint_owner_unavailable')
        self.handle.observe_provider_checkpoint(cursor)


class _GenerationEgressLease:
    """Satisfy the legacy gateway's per-wire completion contract.

    The workbench configuration is its own authority boundary: it stores the
    user's remote consent with the exact endpoint/model revision.  Reusing the
    old provider registry guard here would bind this independent configuration
    to an unrelated active-provider manifest and make the settings page appear
    saved while silently denying its own requests.
    """

    def finish(self, status: str, *, error_code: str | None = None) -> None:
        del status, error_code


class ModelConfiguration:
    """Keep public configuration in SQLite and keys in the platform secret store."""

    def __init__(
        self,
        records: SQLiteStructuredRecordStore,
        root: Path,
        secrets: SecretStore | None = None,
        gateway_factory: Callable[..., LiteLLMCompletionGateway] = LiteLLMCompletionGateway,
        completion_fn: Callable[..., object] | None = None,
        subscriptions: ChatGPTSubscriptions | None = None,
        internal_local_key_provider: Callable[[str], str | None] | None = None,
        local_models_root: Path | None = None,
        model_http_client_factory: Callable[[], object] | None = None,
    ):
        self.records = records
        self.root = root
        self._local_models_root = Path(local_models_root) if local_models_root is not None else root / 'data' / 'models'
        self.secrets = secrets or build_model_secret_store(root)
        self._gateway_factory = gateway_factory
        self._completion_fn = completion_fn or _load_litellm_completion
        self._internal_local_key_provider = internal_local_key_provider
        self._model_http_client_factory = model_http_client_factory
        self._lock = RLock()
        self.subscriptions = subscriptions or ChatGPTSubscriptions(records, self.secrets)
        self._responses = ResponsesCompletion()
        self._embedding_projection = None
        self._embedding_policy_reader = None

    def bind_embedding(self, projection, policy_reader):
        if not callable(projection) or not callable(policy_reader):
            raise ModelConfigurationError('embedding_binding_invalid')
        with self._lock:
            self._embedding_projection = projection
            self._embedding_policy_reader = policy_reader

    def embedding_policy(self):
        if self._embedding_policy_reader is None:
            raise ModelConfigurationError('embedding_binding_required')
        return dict(self._embedding_policy_reader())

    def subscription_selection(self) -> dict:
        row = self.records.read("v2_subscription_selection", "default")
        mode = self.records.read(_GENERATION_MODE_COLLECTION, _GENERATION_MODE_ID)
        active = bool(row and row.payload.get("model") and row.payload.get("mode_revision") == (mode.revision if mode else 0))
        return {"provider": "chatgpt", "model": row.payload["model"] if active else None,
                "revision": row.revision if row else 0, "account_revision": row.payload.get("account_revision") if active else None}

    def select_subscription(self, *, model: str | None, expected_revision: int,
                            allow_remote: bool | None = None, expected_generation_revision: int | None = None) -> dict:
        if model is not None and (not isinstance(model, str) or not model or len(model) > 200):
            raise ModelConfigurationError("subscription_model_invalid")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ModelConfigurationError("subscription_revision_invalid")
        if allow_remote is not None and (type(allow_remote) is not bool or type(expected_generation_revision) is not int):
            raise ModelConfigurationError("subscription_consent_invalid")
        auth = self.subscriptions.status()
        consent_only = allow_remote is not None and self.subscription_selection()["model"] == model
        if model is not None and not consent_only and model not in {r["id"] for r in self.subscriptions.models()}:
            raise ModelConfigurationError("subscription_model_unavailable")
        with self._lock, self.records.begin() as tx:
            current_auth = tx.read(PROFILES, PROFILE)
            if (current_auth.revision if current_auth else 0) != auth["revision"]:
                raise ModelConfigurationError("subscription_account_changed")
            mode = tx.read(_GENERATION_MODE_COLLECTION, _GENERATION_MODE_ID)
            generation = tx.read("recognition_model_config", "generation")
            payload = dict(generation.payload) if generation else {}
            mode_revision = mode.revision if mode else 0
            # Model selection is a mode switch, using the existing saved API
            # profile. Local installation and its profile remain recoverable.
            if model is not None and mode and mode.payload.get("mode") == "local":
                payload = dict(mode.payload.get("api_config") or {})
                saved = tx.put(_GENERATION_MODE_COLLECTION, _GENERATION_MODE_ID,
                    {**mode.payload, "mode": "api"}, expected_revision=mode.revision)
                mode_revision = saved.revision
            if allow_remote is not None:
                if (generation.revision if generation else 0) != expected_generation_revision:
                    raise ModelConfigurationError("subscription_consent_revision_conflict")
                payload["allow_remote"] = allow_remote
            if payload != (generation.payload if generation else {}):
                tx.put("recognition_model_config", "generation", payload,
                    expected_revision=generation.revision if generation else 0)
            tx.put("v2_subscription_selection", "default", {"model": model,
                "mode_revision": mode_revision, "account_revision": self.subscription_selection()["account_revision"] if consent_only else auth["revision"]}, expected_revision=expected_revision)
            tx.commit()
        return self.subscription_selection()

    def close(self):
        self.subscriptions.close()

    def model_prices(self, purpose, *, configuration=None, at=None):
        if purpose not in PURPOSES:
            raise ModelConfigurationError('unknown model purpose')
        configuration = configuration if configuration is not None else self._public_one(purpose)
        if purpose == 'embedding' and configuration.get('provider') == 'local':
            return {'rates': {'input_per_million': '0', 'output_per_million': '0',
                             'cache_read_per_million': '0'},
                    'source': 'local', 'revision': 0, 'editable': False}
        instant = at if at is not None else datetime.now(timezone.utc)
        saved = self.records.read(PRICES, purpose)
        revision = saved.revision if saved else 0
        if saved and saved.payload.get('configuration') == configuration_binding(configuration):
            return {'rates': dict(saved.payload['rates']), 'source': 'manual', 'revision': revision, 'editable': True}
        known = official_prices(configuration.get('provider', ''), configuration.get('model', ''),
                                configuration.get('base_url', ''))
        rates = known.at(instant) if known is not None else None
        return {'rates': rates, 'source': known.source if rates is not None else None,
                'revision': revision, 'editable': rates is None}

    def update_model_prices(self, purpose, rates, *, expected_revision, expected_configuration_revision):
        if purpose not in PURPOSES or any(type(value) is not int or value < 0 for value in
                                         (expected_revision, expected_configuration_revision)):
            raise ModelConfigurationError('model_price_invalid')
        try:
            rates = validate_rates(rates)
        except ValueError:
            raise ModelConfigurationError('model_price_invalid') from None
        with self._lock, self.records.begin() as tx:
            current = tx.read('recognition_model_config', purpose)
            if (current.revision if current else 0) != expected_configuration_revision:
                raise ModelConfigurationError('model_price_configuration_changed')
            configuration = self._public_one(purpose)
            known = official_prices(configuration.get('provider', ''), configuration.get('model', ''),
                                    configuration.get('base_url', ''))
            if known is not None and known.at(datetime.now(timezone.utc)) is not None:
                raise ModelConfigurationError('model_price_official_readonly')
            tx.put(PRICES, purpose, {'configuration': configuration_binding(configuration), 'rates': rates},
                   expected_revision=expected_revision)
            tx.commit()
        return self.model_prices(purpose)

    def price_wire_sink(self, sink, *, purpose='generation', configuration):
        if sink is None or not all(isinstance(getattr(sink, key, None), str)
                                   for key in ('turn_id', 'model_request_id')):
            return sink
        return PriceRecordingSink(sink, self, purpose, configuration)

    def public(self) -> dict:
        with self._lock:
            return {purpose: self._public_one(purpose) for purpose in PURPOSES} | {
                "generation_mode": self.generation_mode(),
            }

    def _generation_authority_binding(self):
        public = self._public_one('generation')
        configuration = {key: public.get(key) for key in _GOVERNED_CONFIGURATION_FIELDS}
        if public.get('subscription_binding'):
            configuration['subscription_binding'] = dict(public['subscription_binding'])
        return {'configuration': configuration, 'mode_revision': self.generation_mode()['revision']}

    def _provider_store_selection(self, reader):
        """Read protocol identity using the caller's transaction, without model locks or keys."""
        generation = reader.read('recognition_model_config', 'generation')
        choice = reader.read(_GENERATION_MODE_COLLECTION, _GENERATION_MODE_ID)
        selection = reader.read('v2_subscription_selection', 'default')
        payload = generation.payload if generation else {}
        base = payload.get('base_url', '')
        mode_revision = choice.revision if choice else 0
        mode = choice.payload.get('mode') if choice else (
            'local' if _is_loopback_endpoint(base) else 'api')
        subscription = bool(selection and selection.payload.get('model')
            and selection.payload.get('mode_revision') == mode_revision)
        has_key = bool(payload.get('secret_ref') and self.secrets.has_secret(payload['secret_ref']))
        configuration = {'purpose': 'generation', 'provider': payload.get('provider', 'openai'),
            'base_url': base, 'model': payload.get('model', ''),
            'allow_remote': payload.get('allow_remote', False),
            'revision': generation.revision if generation else 0,
            'configured': bool(payload.get('model') and base and has_key), 'has_api_key': has_key}
        binding = {'configuration': configuration, 'mode_revision': mode_revision}
        adapter = None
        if (mode == 'api' and not subscription and configuration['configured']
                and payload.get('enabled') is True and configuration['provider'] == 'openai'):
            candidate = self._completion_fn
            # This official API's Responses protocol is documented. A proxy or
            # a model name cannot establish the same background capability.
            if candidate is _load_litellm_completion and base.rstrip('/') == 'https://api.openai.com/v1':
                candidate = ResponsesCompletion(api_base=base,
                    capabilities=ModelCapabilities(background_resume=True))
            if (isinstance(candidate, ResponsesCompletion) and candidate.background_resume_capable
                    and candidate.api_base == base.rstrip('/')):
                adapter = candidate
        return binding, adapter

    def provider_store_capability(self, *, reader=None):
        binding, adapter = self._provider_store_selection(self.records if reader is None else reader)
        return {'available': adapter is not None, 'binding': binding}

    def provider_store_adapter(self, *, reader=None):
        """Return an explicitly capable adapter; do not replace the default loader."""
        return self._provider_store_selection(self.records if reader is None else reader)[1]

    def fast_model(self):
        with self._lock:
            row = self.records.read(_FAST_MODEL_COLLECTION, 'default')
            model = row.payload.get('model') if row else None
            configured = bool(model and row.payload.get('binding') == self._generation_authority_binding())
            return {'model': model, 'revision': row.revision if row else 0, 'configured': configured}

    def update_fast_model(self, *, model, expected_revision, expected_generation_revision,
                          expected_mode_revision):
        if (model is not None and (not isinstance(model, str) or not model.strip() or len(model) > 200)
                or any(type(value) is not int or value < 0 for value in
                       (expected_revision, expected_generation_revision, expected_mode_revision))):
            raise ModelConfigurationError('fast_model_invalid')
        model = model.strip() if model is not None else None
        initial_binding = self._generation_authority_binding()
        catalog = None
        if model is not None and self.subscription_selection()['model']:
            catalog = {entry['id'] for entry in self.subscriptions.models()}
        with self._lock, self.records.begin() as tx:
            generation = tx.read('recognition_model_config', 'generation')
            mode = tx.read(_GENERATION_MODE_COLLECTION, _GENERATION_MODE_ID)
            if ((generation.revision if generation else 0) != expected_generation_revision
                    or (mode.revision if mode else 0) != expected_mode_revision):
                raise ModelConfigurationError('fast_model_configuration_changed')
            binding = self._generation_authority_binding()
            if binding != initial_binding:
                raise ModelConfigurationError('fast_model_configuration_changed')
            if model is not None:
                if not binding['configuration']['configured']:
                    raise ModelConfigurationError('model_not_configured')
                if self._local_selected() and model != _LOCAL_MODEL:
                    raise ModelConfigurationError('fast_local_model_unavailable')
                if self.subscription_selection()['model'] and (catalog is None or model not in catalog):
                    raise ModelConfigurationError('subscription_model_unavailable')
            tx.put(_FAST_MODEL_COLLECTION, 'default', {'model': model,
                'binding': binding if model is not None else None}, expected_revision=expected_revision)
            tx.commit()
        return self.fast_model()

    def freeze_auxiliary_binding(self):
        with self._lock:
            choice = self.fast_model()
            return {'model': choice['model'] if choice['configured'] else None,
                'selection_revision': choice['revision'], 'parent': self._generation_authority_binding()}

    def for_auxiliary(self, binding):
        if (not isinstance(binding, Mapping) or set(binding) != {'model', 'selection_revision', 'parent'}
                or type(binding['selection_revision']) is not int or binding['selection_revision'] < 0
                or not isinstance(binding['parent'], Mapping)
                or set(binding['parent']) != {'configuration', 'mode_revision'}
                or type(binding['parent']['mode_revision']) is not int
                or not isinstance(binding['parent']['configuration'], Mapping)
                or set(binding['parent']['configuration']) not in (
                    set(_GOVERNED_CONFIGURATION_FIELDS), set(_GOVERNED_CONFIGURATION_FIELDS) | {'subscription_binding'})):
            raise ModelConfigurationError('fast_model_binding_invalid')
        if binding['model'] is None:
            return self
        return _AuxiliaryGenerationConfiguration(self, binding)

    def _public_one(self, purpose: str) -> dict:
        record = self.records.read("recognition_model_config", purpose)
        payload = dict(record.payload) if record else {}
        secret_ref = payload.pop("secret_ref", "")
        has_key = bool(secret_ref and self.secrets.has_secret(secret_ref))
        local_selected = purpose == "generation" and self._local_selected() and _valid_local_profile(payload)
        selection = self.subscription_selection() if purpose == "generation" else {}
        if selection.get("model"):
            auth = self.subscriptions.status()
            return {"purpose": purpose, "provider": "openai", "base_url": RESOURCE,
                "model": selection["model"], "allow_remote": payload.get("allow_remote", False),
                "revision": record.revision if record else 0, "enabled": True,
                "has_api_key": False, "configured": auth["sharing"] and selection["account_revision"] == auth["revision"],
                "subscription_binding": {"selection": selection["revision"], "account": auth["revision"]}}
        public = {
            "purpose": purpose, "provider": "openai", "base_url": "", "model": "",
            "allow_remote": False, **payload,
            "enabled": bool(payload.get("enabled", False)),
            "revision": record.revision if record else 0,
            "has_api_key": has_key,
            "configured": bool(payload.get("model") and payload.get("base_url")
                               and (has_key or (local_selected and self._local_model_installed()))),
        }
        if purpose == 'embedding':
            if self._embedding_projection is not None:
                public = self._embedding_projection(public)
        if purpose == 'vision':
            from .local_image_provider import local_image_status
            choice = self.records.read('v2_vision_mode', 'default')
            local = local_image_status(self.root)
            mode = choice.payload['mode'] if choice else 'local'
            public.update(mode=mode, mode_revision=choice.revision if choice else 0, local=local)
            if mode == 'local':
                public['configured'] = local['status'] == 'ready'
        return public

    def update_vision_mode(self, *, mode, expected_revision):
        if (not isinstance(mode, str) or mode not in {'local', 'remote'}
                or type(expected_revision) is not int or expected_revision < 0):
            raise ModelConfigurationError('vision_mode_invalid')
        with self._lock, self.records.begin() as tx:
            tx.put('v2_vision_mode', 'default', {'mode': mode}, expected_revision=expected_revision)
            tx.commit()
        return self._public_one('vision')

    def vision_binding(self):
        """Internal identity only; never expose the secret handle in a read model."""
        with self._lock:
            row = self.records.read('recognition_model_config', 'vision')
            mode = self.records.read('v2_vision_mode', 'default')
            payload = dict(row.payload) if row else {}
            return {key: payload.get(key) for key in
                ('provider', 'model', 'base_url', 'allow_remote', 'secret_ref')} | {
                    'revision': row.revision if row else 0,
                    'mode': mode.payload['mode'] if mode else 'local',
                    'mode_revision': mode.revision if mode else 0,
                    'has_key': bool(payload.get('secret_ref') and self.secrets.has_secret(payload['secret_ref']))}

    def generation_mode(self) -> dict[str, object]:
        """Expose the actual selection without exposing either profile's key reference."""
        with self._lock:
            choice = self.records.read(_GENERATION_MODE_COLLECTION, _GENERATION_MODE_ID)
            current = self.records.read("recognition_model_config", "generation")
            current_payload = dict(current.payload) if current else {}
            if choice:
                stored = dict(choice.payload)
                mode = stored.get("mode")
                local_enabled = stored.get("local_enabled") is True
                api = stored.get("api_config") if isinstance(stored.get("api_config"), Mapping) else {}
                local_base_url = str(stored.get("local_base_url") or _DEFAULT_LOCAL_BASE_URL)
            else:
                mode = "local" if _is_loopback_endpoint(str(current_payload.get("base_url") or "")) else "api"
                local_enabled = mode == "local" and bool(current_payload.get("model"))
                api = current_payload if mode == "api" else {}
                local_base_url = str(current_payload.get("base_url") or _DEFAULT_LOCAL_BASE_URL) if mode == "local" else _DEFAULT_LOCAL_BASE_URL
            api_ref = api.get("secret_ref") if isinstance(api, Mapping) else None
            return {
                "mode": "subscription" if self.subscription_selection()["model"] else mode,
                "local_enabled": local_enabled,
                "local_base_url": local_base_url,
                "local_model": _LOCAL_MODEL,
                "local_model_installed": self._local_model_installed(),
                "api_configured": bool(api.get("base_url") and api.get("model") and api_ref
                                       and self.secrets.has_secret(str(api_ref))),
                "revision": choice.revision if choice else 0,
            }

    def _local_model_installed(self) -> bool:
        return (self._local_models_root / _LOCAL_MODEL / "model.safetensors").is_file()

    def _local_selected(self) -> bool:
        choice = self.records.read(_GENERATION_MODE_COLLECTION, _GENERATION_MODE_ID)
        if choice:
            return choice.payload.get("mode") == "local" and choice.payload.get("local_enabled") is True
        current = self.records.read("recognition_model_config", "generation")
        return bool(current and current.payload.get("model") == _LOCAL_MODEL
                    and _is_loopback_endpoint(str(current.payload.get("base_url") or "")))

    def local_generation_allowed(self) -> bool:
        with self._lock:
            if self.subscription_selection()["model"]:
                return False
            current = self.records.read("recognition_model_config", "generation")
            return bool(self._local_selected() and current
                        and _valid_local_profile(current.payload) and self._local_model_installed())

    def update_generation_mode(
        self, *, mode: str, local_enabled: bool, local_base_url: str,
        expected_revision: int,
    ) -> dict[str, object]:
        if mode not in {"api", "local"} or not isinstance(local_enabled, bool):
            raise ModelConfigurationError("invalid_generation_mode")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            raise ModelConfigurationError("expected_revision must be a nonnegative integer")
        local_base_url = _validated_local_model_base_url(local_base_url)
        if mode == "local" and not local_enabled:
            raise ModelConfigurationError("enable_local_model_before_selection")
        if mode == "local" and not self._local_model_installed():
            raise ModelConfigurationError("local_model_not_installed")
        with self._lock:
            choice = self.records.read(_GENERATION_MODE_COLLECTION, _GENERATION_MODE_ID)
            current_choice_revision = choice.revision if choice else 0
            if current_choice_revision != expected_revision:
                raise ModelConfigurationError("generation_mode_revision_conflict")
            generation = self.records.read("recognition_model_config", "generation")
            current = dict(generation.payload) if generation else {}
            if choice:
                previous_mode = choice.payload.get("mode")
                saved_api = choice.payload.get("api_config")
                api_config = dict(saved_api) if isinstance(saved_api, Mapping) else {}
            else:
                previous_mode = "local" if _is_loopback_endpoint(str(current.get("base_url") or "")) else "api"
                api_config = current if previous_mode == "api" else {}
            if previous_mode == "api":
                api_config = current
            local_config = {
                "provider": "openai", "base_url": local_base_url,
                "model": _LOCAL_MODEL, "allow_remote": False,
                "enabled": True, "secret_ref": "",
            }
            target = local_config if mode == "local" else api_config
            selection = {
                "schema_version": "1.0.0", "id": _GENERATION_MODE_ID,
                "mode": mode, "local_enabled": local_enabled,
                "local_base_url": local_base_url, "api_config": api_config,
            }
            with self.records.begin() as tx:
                if target != current:
                    tx.put("recognition_model_config", "generation", target,
                           expected_revision=generation.revision if generation else 0)
                tx.put(_GENERATION_MODE_COLLECTION, _GENERATION_MODE_ID, selection,
                       expected_revision=current_choice_revision)
                tx.commit()
            return self.generation_mode()

    def update(self, purpose: str, data: dict) -> dict:
        if purpose not in PURPOSES:
            raise ModelConfigurationError("unknown model purpose")
        revision = data.get("expected_revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ModelConfigurationError("expected_revision must be a nonnegative integer")
        with self._lock:
            choice = self.records.read(_GENERATION_MODE_COLLECTION, _GENERATION_MODE_ID) if purpose == "generation" else None
            if purpose == "generation" and self.subscription_selection()["model"]:
                raise ModelConfigurationError("select_api_before_editing_generation")
            if choice and choice.payload.get("mode") == "local":
                raise ModelConfigurationError("select_api_before_editing_generation")
            current = self.records.read("recognition_model_config", purpose)
            previous = dict(current.payload) if current else {}
            payload = {key: data.get(key, previous.get(key, default)) for key, default in (
                ("provider", "openai"), ("base_url", ""), ("model", ""), ("allow_remote", False), ("enabled", False),
            )}
            for key in ("provider", "base_url", "model"):
                if not isinstance(payload[key], str) or len(payload[key]) > 2048:
                    raise ModelConfigurationError("invalid model configuration")
                payload[key] = payload[key].strip()
            if payload["provider"] != "openai":
                raise ModelConfigurationError("use an OpenAI-compatible endpoint for this first version")
            if not isinstance(payload["allow_remote"], bool):
                raise ModelConfigurationError("allow_remote must be boolean")
            if not isinstance(payload["enabled"], bool):
                raise ModelConfigurationError("enabled must be boolean")
            if payload["base_url"]:
                url = urlsplit(payload["base_url"])
                if url.username or url.password or url.query or url.fragment or not url.hostname:
                    raise ModelConfigurationError("endpoint must not contain credentials, query or fragment")
                local = url.hostname in {"localhost", "127.0.0.1", "::1"}
                if url.scheme != "https" and not (local and url.scheme == "http"):
                    raise ModelConfigurationError("remote endpoint requires HTTPS")
                if choice and local:
                    raise ModelConfigurationError("select_local_model_in_generation_mode")
            new_key = data.get("api_key", "")
            if not isinstance(new_key, str) or len(new_key) > 8192:
                raise ModelConfigurationError("invalid API key")
            old_ref = previous.get("secret_ref", "")
            ref = old_ref
            if data.get("clear_api_key") is True:
                ref = ""
            if new_key.strip():
                ref = "recognition-" + uuid4().hex
                self.secrets.set(ref, new_key)
            payload["secret_ref"] = ref
            try:
                with self.records.begin() as tx:
                    tx.put("recognition_model_config", purpose, payload,
                           expected_revision=revision)
                    if choice:
                        tx.put(_GENERATION_MODE_COLLECTION, _GENERATION_MODE_ID,
                               {**choice.payload, "api_config": payload},
                               expected_revision=choice.revision)
                    tx.commit()
            except Exception:
                if ref and ref != old_ref:
                    self.secrets.delete(ref)
                raise
            if old_ref and ref != old_ref:
                self.secrets.delete(old_ref)
            return self._public_one(purpose)

    def snapshot(self, purpose: str) -> dict:
        if purpose not in PURPOSES:
            raise ModelConfigurationError("unknown model purpose")
        with self._lock:
            record = self.records.read("recognition_model_config", purpose)
            if purpose == 'embedding':
                selected = self._public_one(purpose)
                if selected.get('mode') == 'local':
                    if not selected['configured']:
                        raise ModelConfigurationError('model_not_configured')
                    return {**selected, 'api_key': 'local-vector'}
            if purpose == "generation" and self.subscription_selection()["model"]:
                before = self._public_one(purpose)
                if not before["configured"]:
                    raise ModelConfigurationError("subscription_selection_required")
                try:
                    key = self.subscriptions.token()
                except SubscriptionError as error:
                    raise ModelConfigurationError(error.code) from None
                if before != self._public_one(purpose):
                    raise ModelConfigurationError("model_configuration_changed_before_request")
                return {**before, "api_key": key}
            if not record:
                raise ModelConfigurationError("model_not_configured")
            payload = dict(record.payload)
            secret_ref = payload.pop("secret_ref", "")
            local_selected = purpose == "generation" and self._local_selected()
            if local_selected and not _valid_local_profile(payload):
                raise ModelConfigurationError("local_model_configuration_invalid")
            key = "local-model" if local_selected and self._local_model_installed() else self.secrets.get_snapshot(secret_ref).value
            if not key or not payload.get("model") or not payload.get("base_url"):
                raise ModelConfigurationError("model_not_configured")
            result = {**payload, "api_key": key, "revision": record.revision}
            if purpose == 'embedding' and self._embedding_projection is not None:
                result.update(mode=selected['mode'], mode_revision=selected['mode_revision'])
            return result

    def search(self, query, *, parameters, messages_factory, normalize_results, validate_current, wire_attempt_sink):
        """复用原搜索传输，以当前用途权限和原内核 wire 回执约束它。"""
        from backend.video_summary.infrastructure.litellm_web_search import LiteLLMNativeWebSearchGateway
        from backend.shared.llm.message_metadata import _extract_normalized_usage
        from backend.shared.llm.litellm_gateway import _extract_prompt_cache_observation

        if not callable(getattr(wire_attempt_sink, 'begin_model_wire_attempt', None)):
            raise ModelConfigurationError('search_kernel_receipt_required')
        _validate_current(validate_current)
        cfg = self.snapshot('search')
        expected = _generation_snapshot_identity(cfg)

        def current():
            _validate_current(validate_current)
            fresh = self.snapshot('search')
            if (_generation_snapshot_identity(fresh) != expected or fresh.get('enabled') is not True
                    or fresh.get('allow_remote') is not True):
                raise ModelConfigurationError('search_configuration_changed')
            _validate_generation_egress(fresh)

        current()
        sink = self.price_wire_sink(wire_attempt_sink, purpose='search', configuration=cfg)
        completion = _validated_generation_completion(self._completion_fn)
        usage = {}

        def observed(**request):
            current()
            attempt = sink.begin_model_wire_attempt()
            def wire():
                nonlocal usage
                try:
                    response = completion(**request)
                    usage = _extract_normalized_usage(response)
                    attempt.succeeded(usage=usage, cache_observation=_extract_prompt_cache_observation(response))
                    return response
                except BaseException:
                    attempt.failed_transport(error_code='search_provider_request_failed')
                    raise
            response = attempt.invoke_wire(wire)
            current()
            return response

        def guard(purpose, categories, payload_bytes):
            # 旧传输名称只在这一已核配置接点映射到新 search 用途。
            if (purpose != 'web_search' or categories != ('instructions', 'source_excerpt')
                    or type(payload_bytes) is not int or payload_bytes < 0):
                raise ModelConfigurationError('search_egress_policy_mismatch')
            current()
            return _GenerationEgressLease()

        gateway = LiteLLMNativeWebSearchGateway(provider=cfg['provider'], model=cfg['model'],
            base_url=cfg['base_url'], api_key_provider=lambda:cfg['api_key'],
            search_context_size=parameters['search_context_size'], completion_fn=observed,
            egress_guard=guard, messages_factory=messages_factory)
        try:
            results = gateway.search(query, max_results=parameters['max_results'],
                timeout_seconds=parameters['timeout_seconds'])
            current()
            results = normalize_results(results, credentials=(cfg['api_key'],))
            if not results:
                raise ModelConfigurationError('search_no_usable_evidence')
        except (RecognitionConflict, ModelConfigurationError, RunLeaseRevoked):
            raise
        except Exception:
            raise ModelConfigurationError('search_request_failed') from None
        return results, {'model':cfg['model'], 'configuration_revision':cfg['revision'], 'usage':usage}

    def complete(
        self,
        messages: list[dict],
        *,
        max_tokens: int = 1800,
        validate_current: Callable[[], None] | None = None,
        timeout_seconds: int | None = None,
        response_model: type[BaseModel] | None = None,
        wire_attempt_sink: object | None = None,
        retry_policy: Callable[[Mapping[str, object]], Mapping[str, object]] | None = None,
    ) -> tuple[str, dict]:
        """Complete a generation request while retaining source authority.

        ``validate_current`` is supplied by the memory workbench for a frozen
        source-egress snapshot.  Check it before work, at the gateway's actual
        wire boundary, and after a provider response so a revoked source can
        neither be sent nor have a raced response accepted.
        """
        _validate_current(validate_current)
        cfg = self.snapshot("generation")
        if retry_policy is not None and not callable(retry_policy):
            raise ModelConfigurationError("model_retry_policy_invalid")
        expected = _generation_snapshot_identity(cfg) if retry_policy is not None else None
        def validate_retry():
            _validate_current(validate_current)
            if _generation_snapshot_identity(self.snapshot("generation")) != expected:
                raise ModelConfigurationError("model_configuration_changed_before_request")
        gateway = self._generation_gateway(cfg,
            validate_current=validate_retry if retry_policy is not None else validate_current)
        wire_attempt_sink = self.price_wire_sink(wire_attempt_sink, configuration=cfg)
        try:
            timeout = timeout_seconds if timeout_seconds is not None else (
                180 if _is_loopback_endpoint(str(cfg["base_url"])) else 60
            )
            options = {'wire_attempt_sink': wire_attempt_sink} if wire_attempt_sink is not None else {}
            if retry_policy is not None:
                _validate_wire_attempt_sink(wire_attempt_sink)
                control = ModelRetryControl(policy=retry_policy, checkpoint=validate_retry,
                    owned_client_factory=self._model_http_client_factory)
                if timeout_seconds is None:
                    timeout = float(control.limits['total_timeout'])
                options['retry_control'] = control
            if response_model is None:
                text, usage, _ = gateway.complete_text_with_usage(messages, max_tokens=max_tokens, timeout=timeout,
                    **options)
            else:
                text, usage, _ = gateway.complete_structured_with_usage(messages,
                    response_model=response_model, max_tokens=max_tokens, timeout=timeout,
                    retries=0, max_wire_attempts=3,
                    **options)
            _validate_current(validate_current)
            if retry_policy is not None:
                validate_retry()
        except RecognitionConflict:
            # Source authority changes are optimistic-concurrency conflicts,
            # not provider failures.  Preserve their public identity for the
            # API layer and never wrap them with a model transport message.
            raise
        except ModelConfigurationError:
            # These are local, fixed policy/result codes.  They contain no
            # provider payload and let the workbench distinguish an incomplete
            # output from a transport failure.
            raise
        except Exception as error:
            # Provider exceptions can contain request details. Keep them out of API/log responses.
            raise _model_request_failure(error) from None
        return text, {"model": cfg["model"], "configuration_revision": cfg["revision"], "usage": usage,
                      "context_budget": _gateway_budget_snapshot(gateway)}

    def complete_structured(
        self, messages: list[dict], *, response_model: type[BaseModel], max_tokens: int = 1800,
        validate_current: Callable[[], None] | None = None, timeout_seconds: int | None = None,
        wire_attempt_sink: object | None = None,
        retry_policy: Callable[[Mapping[str, object]], Mapping[str, object]] | None = None,
    ) -> tuple[BaseModel, dict]:
        return self.complete(messages, response_model=response_model, max_tokens=max_tokens,
            validate_current=validate_current, timeout_seconds=timeout_seconds,
            **({'wire_attempt_sink': wire_attempt_sink} if wire_attempt_sink is not None else {}),
            **({'retry_policy': retry_policy} if retry_policy is not None else {}))

    def complete_vision(self, messages, *, response_model, max_tokens=4000,
                        validate_current=None, wire_attempt_sink=None, timeout_seconds=None, retry_policy=None):
        """Use the existing gateway with independent, current vision consent."""
        _validate_current(validate_current)
        public = self._public_one('vision')
        if public['mode'] != 'remote' or public['allow_remote'] is not True:
            raise ModelConfigurationError('vision_remote_disabled')
        cfg = self.snapshot('vision')
        expected = _generation_snapshot_identity(cfg)
        mode_revision = public['mode_revision']

        def validate():
            _validate_current(validate_current)
            current = self._public_one('vision')
            if (current['mode'] != 'remote' or current['allow_remote'] is not True
                    or current['mode_revision'] != mode_revision):
                raise ModelConfigurationError('vision_remote_disabled')
            if _generation_snapshot_identity(self.snapshot('vision')) != expected:
                raise ModelConfigurationError('vision_configuration_changed')
            _validate_generation_egress(cfg)

        def guard(purpose, categories, estimated_bytes):
            del purpose, categories, estimated_bytes
            validate()
            return _GenerationEgressLease()

        validate()
        gateway = self._gateway_factory(provider=cfg['provider'], model=cfg['model'],
            base_url=cfg['base_url'], api_key=None, api_key_provider=lambda: cfg['api_key'],
            egress_guard=guard, egress_purpose='media_image_read',
            egress_categories=('instructions', 'image'),
            completion_fn=_validated_generation_completion(self._completion_fn))
        sink = self.price_wire_sink(wire_attempt_sink, purpose='vision', configuration=cfg)
        try:
            options = {'wire_attempt_sink': sink} if sink is not None else {}
            timeout = 60 if timeout_seconds is None else timeout_seconds
            if retry_policy is not None:
                if not callable(retry_policy):
                    raise ModelConfigurationError('model_retry_policy_invalid')
                _validate_wire_attempt_sink(sink)
                control = ModelRetryControl(policy=retry_policy, checkpoint=validate,
                    owned_client_factory=self._model_http_client_factory)
                if timeout_seconds is None:
                    timeout = float(control.limits['total_timeout'])
                options['retry_control'] = control
            output, usage, _ = gateway.complete_structured_with_usage(messages,
                response_model=response_model, max_tokens=max_tokens, timeout=timeout,
                retries=0, max_wire_attempts=1,
                **options)
            validate()
        except (RecognitionConflict, ModelConfigurationError):
            raise
        except Exception:
            raise ModelConfigurationError('vision_request_failed') from None
        return output, {'model': cfg['model'], 'configuration_revision': cfg['revision'],
            'usage': usage, 'context_budget': _gateway_budget_snapshot(gateway)}

    def complete_stream(
        self, messages, *, response_model, max_tokens=1800, validate_current=None, on_delta,
    ):
        _validate_current(validate_current)
        cfg = self.snapshot("generation")
        expected = _generation_snapshot_identity(cfg)
        def validate():
            _validate_current(validate_current)
            if _generation_snapshot_identity(self.snapshot("generation")) != expected:
                raise ModelConfigurationError("model_configuration_changed_before_request")
        gateway = self._generation_gateway(cfg, validate_current=validate)
        try:
            output, usage = gateway.stream_structured_with_usage(messages,
                response_model=response_model, max_tokens=max_tokens, validate_current=validate,
                on_delta=on_delta, timeout=180 if _is_loopback_endpoint(str(cfg["base_url"])) else 60)
            validate()
        except (RecognitionConflict, ModelConfigurationError):
            raise
        except Exception as error:
            raise _model_request_failure(error, include_status=False) from None
        return output, {"model": cfg["model"], "configuration_revision": cfg["revision"], "usage": usage,
                        "context_budget": _gateway_budget_snapshot(gateway)}

    def complete_governed(
        self,
        messages: list[dict],
        *,
        routing_snapshot: Mapping[str, object],
        execution_control: object,
        metadata_sink: object,
        wire_attempt_sink: object,
        max_tokens: int = 1800,
        validate_current: Callable[[], None] | None = None,
        response_model: type[BaseModel] | None = None,
        on_delta: Callable[[str], None] | None = None,
        purpose: str = "primary",
        timeout_seconds: float | None = None,
        retry_policy: Callable[[Mapping[str, object]], Mapping[str, object]] | None = None,
        on_retry: Callable[[Mapping[str, object]], None] | None = None,
        stream_field: str = 'answer',
        observe_decoding=None,
        provider_store_activation: ProviderStoreActivation | None = None,
    ) -> tuple[str | BaseModel, dict]:
        """Complete one legacy Turn-governed request through this app's key authority.

        The Turn owns routing and wire receipts while this configuration owns its
        encrypted credential.  Only the public, key-free configuration is frozen
        into the Turn snapshot; the exact key-bearing snapshot is read again at
        the egress boundary and after the provider returns.
        """

        # A source-egress snapshot is part of the caller's authority. Check it
        # before this Turn has any chance to start a model attempt, at the
        # legacy gateway's wire boundary, and once more before its response can
        # be recorded as a completed call.
        if purpose not in {"primary", "aux"}:
            raise ModelConfigurationError("model_call_purpose_invalid")
        if retry_policy is not None and not callable(retry_policy):
            raise ModelConfigurationError("model_retry_policy_invalid")
        if provider_store_activation is not None:
            if (type(provider_store_activation) is not ProviderStoreActivation
                    or provider_store_activation.expected_purpose != purpose or retry_policy is None):
                raise ModelConfigurationError('provider_background_adapter_invalid')
            provider_store_activation.validate_current()
        _validate_current(validate_current)
        _checkpoint(execution_control)
        _validate_wire_attempt_sink(wire_attempt_sink)
        cfg = self.snapshot("generation")
        public = self._public_one("generation")
        route = _validate_governed_routing_snapshot(routing_snapshot, public)
        _validate_generation_egress(cfg)
        initial_identity = _generation_snapshot_identity(cfg)
        self._assert_governed_configuration_current(
            initial_identity=initial_identity,
            expected_public=route["configuration"],
        )

        _metadata_routed(metadata_sink, route, purpose=purpose)
        _checkpoint(execution_control)
        _metadata_started(metadata_sink, cfg)
        started = True
        try:
            _checkpoint(execution_control)
        except BaseException:
            _metadata_failed(metadata_sink, started=started)
            raise

        try:
            if retry_policy is None:
                remaining_timeout = _remaining_timeout_seconds(
                    execution_control, local=_is_loopback_endpoint(str(cfg["base_url"])))
            else:
                remaining = getattr(execution_control, 'remaining_timeout_ms', None)
                if type(remaining) is not int or remaining <= 0:
                    raise ModelConfigurationError('model_execution_timeout_invalid')
                remaining_timeout = min(remaining / 1000, float(retry_policy({'kind': 'limits'})['total_timeout']))
            if timeout_seconds is not None:
                if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
                    raise ModelConfigurationError("model_execution_timeout_invalid")
                remaining_timeout = min(remaining_timeout, timeout_seconds)
            gateway = (self._generation_gateway(cfg, validate_current=validate_current)
                if provider_store_activation is None else None)
        except BaseException:
            _metadata_failed(metadata_sink, started=started)
            raise
        partial_text = []
        output_clock, output_wall = monotonic(), time()
        retry_control = None
        try:
            def validate_stream():
                _checkpoint(execution_control)
                _validate_current(validate_current)
                self._assert_governed_configuration_current(
                    initial_identity=initial_identity, expected_public=route["configuration"])
                if provider_store_activation is not None:
                    provider_store_activation.validate_current()

            options = dict(max_tokens=max_tokens, timeout=remaining_timeout,
                           wire_attempt_sink=self.price_wire_sink(wire_attempt_sink, configuration=cfg))
            if retry_policy is not None:
                # The governed caller supplies its frozen get('retry') policy.
                # Missing injection retains the historical single-wire path.
                options['retry_control'] = ModelRetryControl(policy=retry_policy,
                    checkpoint=validate_stream, on_retry=on_retry,
                    owned_client_factory=self._model_http_client_factory)
                retry_control = options['retry_control']
            if provider_store_activation is not None:
                sink = _BackgroundWireSink(options['wire_attempt_sink'])
                options['wire_attempt_sink'] = sink
                def resume_read(value):
                    validate_stream()
                    return value['remaining'] > 0
                background_options = ProviderBackgroundOptions(checkpoint=validate_stream,
                    resume=resume_read, observe=sink.observe)
                if provider_store_activation.resume_source is not None:
                    source = provider_store_activation.resume_source
                    cursor = ProviderResponseCursor(**source['cursor'])
                    def validate_resume():
                        validate_stream()
                        try:
                            if sink.handle is None or not callable(getattr(sink.handle, 'bind_provider_resume', None)):
                                raise ModelConfigurationError('provider_resume_owner_unavailable')
                            # 原 Handler 内重读的规范 source 必须与这个 typed cursor 完全相同。
                            sink.handle.bind_provider_resume(source)
                        except RunLeaseRevoked:
                            raise
                        except Exception as error:
                            if sink.handle is not None:
                                sink.handle.failed_transport(error_code='ai.provider_resume_binding_invalid')
                            raise ModelConfigurationError('provider_resume_binding_invalid') from error
                    background_options = ProviderResumeOnlyOptions(checkpoint=validate_resume,
                        resume=resume_read, observe=sink.observe, cursor=cursor)
                gateway = self._generation_gateway(cfg, validate_current=validate_stream,
                    background_options=background_options,
                    background_adapter=provider_store_activation.adapter)
            if on_delta is not None and response_model is not None:
                def delivered(text):
                    if retry_control is not None:
                        partial_text.append(text)
                    on_delta(text)
                text, usage = gateway.stream_structured_with_usage(
                    messages, response_model=response_model, on_delta=delivered,
                    validate_current=validate_stream,
                    **({'field': stream_field} if stream_field != 'answer' else {}),
                    **({'observe': observe_decoding} if observe_decoding is not None else {}), **options)
                cache_observation = {}
            elif response_model is not None:
                text, usage, cache_observation = gateway.complete_structured_with_usage(
                    messages, response_model=response_model, retries=0, max_wire_attempts=1 if purpose == "aux" else 3, **options)
            else:
                text, usage, cache_observation = gateway.complete_text_with_usage(messages, **options)
        except (RecognitionConflict, ModelConfigurationError):
            _metadata_failed(metadata_sink, started=started)
            raise
        except BaseException as error:
            _metadata_failed(metadata_sink, started=started)
            if _preserve_turn_control_error(error, execution_control):
                raise
            if not isinstance(error, Exception):
                raise
            if (retry_control is not None and retry_control.phase == 'body' and partial_text
                    and _model_transport_failure(error, datetime.now(timezone.utc))[0]
                    in {'connection', 'server', 'timeout', 'stalled', 'rate_limit'}):
                validate_stream()
                partial = retry_policy({'kind': 'partial_output', 'text': ''.join(partial_text),
                    'wall_elapsed': time() - output_wall, 'monotonic_elapsed': monotonic() - output_clock})
                raise ModelInterrupted(**partial, close_witness=retry_control.closed_witness) from None
            raise _model_request_failure(error) from None

        try:
            _validate_current(validate_current)
            _checkpoint(execution_control)
            self._assert_governed_configuration_current(
                initial_identity=initial_identity,
                expected_public=route["configuration"],
            )
            if provider_store_activation is not None:
                provider_store_activation.validate_current()
        except BaseException:
            _metadata_failed(metadata_sink, started=started)
            raise
        if cache_observation:
            _metadata_cache_observed(metadata_sink, cache_observation)
        _metadata_completed(metadata_sink, usage)
        return text, {
            "model": cfg["model"],
            "configuration_revision": cfg["revision"],
            "usage": usage,
            "context_budget": _gateway_budget_snapshot(gateway),
        }

    def _assert_governed_configuration_current(
        self,
        *,
        initial_identity: tuple[object, ...],
        expected_public: Mapping[str, object],
    ) -> None:
        try:
            current = self.snapshot("generation")
        except ModelConfigurationError:
            raise ModelConfigurationError("model_configuration_changed_during_request") from None
        if _generation_snapshot_identity(current) != initial_identity:
            raise ModelConfigurationError("model_configuration_changed_during_request")
        current_public = self._public_one("generation")
        if not _same_governed_public_configuration(current_public, expected_public):
            raise ModelConfigurationError("model_configuration_changed_during_request")

    def generation_budget_limits(self, *, expected_revision, max_tokens=None):
        """Project current gateway limits only when the frozen packet matches."""
        cfg = self.public()["generation"]
        if not cfg.get("configured") or cfg["revision"] != expected_revision:
            return None
        gateway = self._generation_gateway(cfg)
        reader = getattr(gateway, "input_budget_limits", None)
        return reader(max_tokens=max_tokens) if callable(reader) else None

    def _generation_gateway(
        self,
        cfg: Mapping[str, object],
        *,
        validate_current: Callable[[], None] | None = None,
        background_options: ProviderBackgroundOptions | None = None,
        background_adapter: ResponsesCompletion | None = None,
    ) -> LiteLLMCompletionGateway:
        native = self._responses if cfg.get('subscription_binding') else self._completion_fn
        transport_options = ({'capabilities': ModelCapabilities(structured_modes=('prompt',))}
            if cfg.get('subscription_binding') else {})
        if background_options is not None:
            if background_adapter is not None:
                native = background_adapter
            if (cfg.get('subscription_binding') or not isinstance(native, ResponsesCompletion)
                    or not native.background_resume_capable
                    or native.api_base != str(cfg['base_url']).rstrip('/')):
                raise ValueError('provider_background_adapter_invalid')
            transport_options = {'capabilities': native.capabilities,
                'background_options': background_options}
        return self._gateway_factory(
            provider=cfg["provider"], model=cfg["model"], base_url=cfg["base_url"],
            api_key=None, api_key_provider=lambda: (
                self._internal_local_key_provider(str(cfg['base_url'])) or cfg['api_key']
                if self._internal_local_key_provider is not None and self._local_selected() and _valid_local_profile(cfg)
                else cfg['api_key']),
            egress_guard=self._generation_egress_guard(dict(cfg), validate_current=validate_current),
            egress_purpose="memory_generation",
            egress_categories=("instructions", "source_excerpt"),
            context_window_tokens=16000, reserved_output_tokens=2000,
            completion_fn=_validated_generation_completion(native,
                preserve_responses=background_options is not None),
            **transport_options,
        )

    def _generation_egress_guard(
        self,
        snapshot: dict,
        *,
        validate_current: Callable[[], None] | None = None,
    ) -> Callable[[str, tuple[str, ...], int], _GenerationEgressLease]:
        """Authorize one legacy-gateway wire attempt against an exact snapshot.

        The gateway calls this after it has constructed the request and before
        its completion function is invoked.  The second read closes the gap
        between saving the settings and sending a request: a changed endpoint,
        model, consent flag, key, or revision cannot use the old snapshot.
        """

        expected = _generation_snapshot_identity(snapshot)

        def guard(purpose: str, categories: tuple[str, ...], payload_bytes: int) -> _GenerationEgressLease:
            if purpose != "memory_generation" or categories != ("instructions", "source_excerpt"):
                raise ModelConfigurationError("model_egress_policy_mismatch")
            if isinstance(payload_bytes, bool) or not isinstance(payload_bytes, int) or payload_bytes < 0:
                raise ModelConfigurationError("model_egress_payload_invalid")
            _validate_generation_egress(snapshot)
            try:
                current = self.snapshot("generation")
            except ModelConfigurationError:
                raise ModelConfigurationError("model_configuration_changed_before_request") from None
            if _generation_snapshot_identity(current) != expected:
                raise ModelConfigurationError("model_configuration_changed_before_request")
            _validate_generation_egress(current)
            _validate_current(validate_current)
            return _GenerationEgressLease()

        return guard


class _AuxiliaryGenerationConfiguration(ModelConfiguration):
    """One frozen model choice over the original key and permission authority."""

    def __init__(self, owner, binding):
        self.owner, self.binding = owner, binding
        self._validate_binding()

    def __getattr__(self, name):
        return getattr(self.owner, name)

    def _validate_binding(self):
        value = self.binding
        if (not isinstance(value, Mapping) or set(value) != {'model', 'selection_revision', 'parent'}
                or not isinstance(value['model'], str) or not value['model']
                or type(value['selection_revision']) is not int or value['selection_revision'] < 1):
            raise ModelConfigurationError('fast_model_binding_invalid')
        row = self.records.read(_FAST_MODEL_COLLECTION, 'default')
        if (row is None or row.revision != value['selection_revision']
                or row.payload != {'model': value['model'], 'binding': value['parent']}
                or self.owner._generation_authority_binding() != value['parent']):
            raise ModelConfigurationError('fast_model_configuration_changed')

    def _public_one(self, purpose):
        public = self.owner._public_one(purpose)
        if purpose == 'generation':
            self._validate_binding()
            public = {**public, 'model': self.binding['model']}
        return public

    def public(self):
        public = self.owner.public()
        return {**public, 'generation': self._public_one('generation')}

    def snapshot(self, purpose):
        snapshot = self.owner.snapshot(purpose)
        if purpose == 'generation':
            self._validate_binding()
            snapshot = {**snapshot, 'model': self.binding['model']}
        return snapshot


def _validate_governed_routing_snapshot(
    snapshot: Mapping[str, object], current_public: Mapping[str, object],
) -> dict[str, object]:
    if not isinstance(snapshot, Mapping) or set(snapshot) != {
        "payload_ref", "revision", "prompt_cache_scope_identity", "configuration", "execution_location",
    }:
        raise ModelConfigurationError("model_routing_snapshot_invalid")
    payload_ref = snapshot.get("payload_ref")
    revision = snapshot.get("revision")
    prompt_cache_scope_identity = snapshot.get("prompt_cache_scope_identity")
    configuration = snapshot.get("configuration")
    execution_location = snapshot.get("execution_location")
    if (
        not isinstance(payload_ref, str)
        or not payload_ref.startswith("crp://session/")
        or not isinstance(revision, str)
        or not _ROUTING_DIGEST.fullmatch(revision)
        or not isinstance(prompt_cache_scope_identity, str)
        or not _ROUTING_DIGEST.fullmatch(prompt_cache_scope_identity)
        or not isinstance(configuration, Mapping)
        or not _same_governed_public_configuration(current_public, configuration)
        or execution_location not in {"remote", "local_loopback"}
    ):
        raise ModelConfigurationError("model_routing_snapshot_invalid")
    expected_location = _generation_execution_location(str(current_public["base_url"]))
    if execution_location != expected_location:
        raise ModelConfigurationError("model_routing_snapshot_invalid")
    return {
        "payload_ref": payload_ref,
        "revision": revision,
        "prompt_cache_scope_identity": prompt_cache_scope_identity,
        "configuration": dict(configuration),
        "execution_location": execution_location,
    }


def _same_governed_public_configuration(
    current: Mapping[str, object], expected: Mapping[str, object],
) -> bool:
    if not isinstance(expected, Mapping) or set(expected) not in (
        set(_GOVERNED_CONFIGURATION_FIELDS), set(_GOVERNED_CONFIGURATION_FIELDS) | {"subscription_binding"}
    ):
        return False
    for field in expected:
        actual = current.get(field)
        proposed = expected.get(field)
        if type(actual) is not type(proposed) or actual != proposed:
            return False
    return True


def _generation_execution_location(base_url: str) -> str:
    try:
        parsed = urlsplit(base_url)
    except ValueError:
        raise ModelConfigurationError("model_routing_snapshot_invalid") from None
    host = parsed.hostname.lower() if parsed.hostname else ""
    if host in {"localhost", "127.0.0.1", "::1"}:
        return "local_loopback"
    return "remote"


def _checkpoint(control: object) -> None:
    checkpoint = getattr(control, "checkpoint", None)
    if not callable(checkpoint):
        raise ModelConfigurationError("model_execution_control_invalid")
    checkpoint()


def _remaining_timeout_seconds(control: object, *, local: bool = False) -> float:
    remaining = getattr(control, "remaining_timeout_ms", None)
    if not isinstance(remaining, int) or isinstance(remaining, bool) or remaining <= 0:
        raise ModelConfigurationError("model_execution_timeout_invalid")
    return min(180.0 if local else 60.0, remaining / 1000)


def _is_loopback_endpoint(base_url: str) -> bool:
    host = urlsplit(base_url).hostname
    return host in {"localhost", "127.0.0.1", "::1"}


def _validated_local_model_base_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise ModelConfigurationError("invalid_local_model_endpoint")
    clean = value.strip().rstrip("/")
    try:
        url = urlsplit(clean)
        valid = (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
                 and url.port is not None and url.path == "/local-model/v1"
                 and not url.username and not url.password and not url.query and not url.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise ModelConfigurationError("invalid_local_model_endpoint")
    return clean


def _valid_local_profile(payload: Mapping[str, object]) -> bool:
    if payload.get("model") != _LOCAL_MODEL or payload.get("allow_remote") is not False:
        return False
    try:
        _validated_local_model_base_url(payload.get("base_url"))
    except ModelConfigurationError:
        return False
    return True


def _preserve_turn_control_error(error: BaseException, control: object) -> bool:
    """Keep Turn cancellation/lease semantics distinct from provider failures."""

    if isinstance(error, RunLeaseRevoked):
        return True
    try:
        _checkpoint(control)
    except BaseException as stopped:
        # A provider exception may contain request data. Preserve cancellation
        # identity without attaching that provider traceback as its public cause.
        raise stopped from None
    return False


def _validate_wire_attempt_sink(sink: object) -> None:
    if not callable(getattr(sink, "begin_model_wire_attempt", None)):
        raise ModelConfigurationError("model_wire_attempt_sink_invalid")


def _metadata_routed(sink: object, route: Mapping[str, object], *, purpose: str = "primary") -> None:
    method = getattr(sink, "model_call_routed", None)
    if not callable(method):
        raise ModelConfigurationError("model_metadata_sink_invalid")
    method(
        snapshot_ref=route["payload_ref"],
        snapshot_revision=route["revision"],
        prompt_cache_scope_identity=route["prompt_cache_scope_identity"],
        provider=route["configuration"]["provider"],
        model=route["configuration"]["model"],
        execution_location=route["execution_location"],
        purpose=purpose,
    )


def _metadata_started(sink: object, cfg: Mapping[str, object]) -> None:
    method = getattr(sink, "model_call_started", None)
    if not callable(method):
        raise ModelConfigurationError("model_metadata_sink_invalid")
    method(provider=cfg["provider"], model=cfg["model"])


def _metadata_completed(sink: object, usage: Mapping[str, int]) -> None:
    method = getattr(sink, "model_call_completed", None)
    if not callable(method):
        raise ModelConfigurationError("model_metadata_sink_invalid")
    method(usage=usage)


def _metadata_cache_observed(sink: object, observation: Mapping[str, int]) -> None:
    method = getattr(sink, "model_call_cache_observed", None)
    if not callable(method):
        raise ModelConfigurationError("model_metadata_sink_invalid")
    method(observation=observation)


def _metadata_failed(sink: object, *, started: bool) -> None:
    if not started:
        return
    method = getattr(sink, "model_call_failed", None)
    if not callable(method):
        raise ModelConfigurationError("model_metadata_sink_invalid")
    method()


def _generation_snapshot_identity(snapshot: dict) -> tuple[object, ...]:
    return (
        snapshot.get("revision"),
        snapshot.get("provider"),
        snapshot.get("base_url"),
        snapshot.get("model"),
        snapshot.get("allow_remote"),
        snapshot.get("subscription_binding", snapshot.get("api_key")),
    )


def _gateway_budget_snapshot(gateway):
    reader = getattr(gateway, "input_budget_snapshot", None)
    return reader() if callable(reader) else None


def _validate_generation_egress(snapshot: dict) -> None:
    """Validate the saved destination again at the boundary before wiring."""

    base_url = snapshot.get("base_url")
    model = snapshot.get("model")
    if snapshot.get("provider") != "openai" or not isinstance(base_url, str) or not base_url.strip() or not isinstance(model, str) or not model.strip():
        raise ModelConfigurationError("model_egress_not_configured")
    try:
        parsed = urlsplit(base_url)
    except ValueError:
        raise ModelConfigurationError("model_egress_endpoint_invalid") from None
    host = parsed.hostname.lower() if parsed.hostname else ""
    if parsed.username or parsed.password or parsed.query or parsed.fragment or not host:
        raise ModelConfigurationError("model_egress_endpoint_invalid")
    is_loopback = host in {"localhost", "127.0.0.1", "::1"}
    if is_loopback:
        if parsed.scheme not in {"http", "https"}:
            raise ModelConfigurationError("model_egress_endpoint_invalid")
        return
    if parsed.scheme != "https" or snapshot.get("allow_remote") is not True:
        raise ModelConfigurationError("model_egress_remote_not_consented")


def _validate_current(validate_current: Callable[[], None] | None) -> None:
    if validate_current is not None:
        validate_current()


def _load_litellm_completion(**request: object) -> object:
    """Use the same LiteLLM transport the legacy gateway loads by default."""

    try:
        from litellm import completion
    except ModuleNotFoundError as error:
        raise RuntimeError("缺少 litellm 依赖，无法调用模型。") from error
    return complete_with_owned_transport(completion, request)


class _ValidatedResponsesCompletion(ResponsesCompletion):
    """Keep the proven adapter identity while applying the existing validator."""

    def __init__(self, native, complete):
        super().__init__(client=native.client, api_base=native.api_base,
            capabilities=native.capabilities)
        self._validated_complete = complete

    def __call__(self, **request):
        return self._validated_complete(**request)


def _validated_generation_completion(completion_fn: Callable[..., object], *,
        preserve_responses=False) -> Callable[..., object]:
    """Reject known incomplete provider responses before the gateway exposes text."""

    def complete(**request: object) -> object:
        response = completion_fn(**request)
        if request.get("stream"):
            retry_enabled = isinstance(request.get('timeout'), ModelTransportTimeout)
            if retry_enabled:
                response = request['timeout'].bind_completion(response)
            return _validated_generation_stream(response, retry_enabled=retry_enabled)
        _validate_generation_response(response)
        return response

    if preserve_responses:
        if not isinstance(completion_fn, ResponsesCompletion) or not completion_fn.background_resume_capable:
            raise ValueError('provider_background_adapter_invalid')
        return _ValidatedResponsesCompletion(completion_fn, complete)
    return complete


def _validated_generation_stream(stream, *, retry_enabled=False):
    finished, failure = False, None
    try:
        for chunk in stream:
            for choice in _lookup(chunk, "choices") or ():
                reason = _lookup(choice, "finish_reason")
                if reason is not None:
                    if reason != "stop":
                        raise ModelConfigurationError("model_output_incomplete")
                    finished = True
            yield chunk
        if not finished:
            if retry_enabled:
                raise ConnectionError('provider stream ended before its terminal event')
            raise ModelConfigurationError("model_output_incomplete")
    except BaseException as error:
        failure = error
        raise
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                if not retry_enabled:
                    raise
                if failure is not None and not isinstance(failure, Exception):
                    raise failure from None
                raise ModelTransportError('provider_close_failed') from None
        elif retry_enabled and failure is not None and isinstance(failure, Exception):
            raise ModelTransportError('provider_close_failed') from None


def _validate_generation_response(response: object) -> None:
    choices = _lookup(response, "choices")
    if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes)) or not choices:
        raise ModelConfigurationError("model_response_invalid")
    first = choices[0]
    finish_reason = _lookup(first, "finish_reason")
    if isinstance(finish_reason, str) and finish_reason.strip() and finish_reason.strip().lower() != "stop":
        reason = finish_reason.strip().lower()
        safe_reason = reason if reason in {"length", "content_filter", "tool_calls", "function_call"} else "other"
        raw_tokens = _lookup(_lookup(response, "usage"), "completion_tokens")
        tokens = raw_tokens if type(raw_tokens) is int and 0 <= raw_tokens <= 1_000_000 else None
        _LOG.warning("generation response incomplete reason=%s completion_tokens=%s", safe_reason, tokens)
        raise ModelConfigurationError("model_output_incomplete")
    message = _lookup(first, "message")
    content = _lookup(message, "content")
    if not isinstance(content, str) or not content.strip():
        raise ModelConfigurationError("model_output_missing_content")


def _lookup(value: object, key: str) -> object:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)
