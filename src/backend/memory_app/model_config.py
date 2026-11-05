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
from backend.shared.llm.model_capabilities import ModelCapabilities, is_official_terra
from backend.shared.llm.oxr_gateway import OxrImageGateway, is_oxr_endpoint
from backend.shared.llm.model_transport import (
    ModelTransportError, ModelTransportTimeout, ModelInterrupted, OwnedAttemptInterruption,
    complete_with_owned_transport,
)
from backend.shared.llm.model_prices import validate_rates, official_prices
from .model_costs import PRICES, configuration_binding, PriceRecordingSink, BalanceRecordingSink, SharedModelMeteringError
from .model_binding import public_model_configuration, valid_public_configuration_fields
from .local_generation_readiness import inspect_local_generation, retry_loading, LocalGenerationError
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
    if isinstance(error, SharedModelMeteringError):
        failure = ModelConfigurationError(error.public_code)
        failure.status_code, failure.code = error.status_code, error.code
        failure.retryable = False
        return failure
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
        server_model_access=None,
        server_model_balances=None,
    ):
        self.records = records
        self.root = root
        self._local_models_root = Path(local_models_root) if local_models_root is not None else root / 'data' / 'models'
        self.secrets = secrets or build_model_secret_store(root)
        self._gateway_factory = gateway_factory
        self._completion_fn = completion_fn or _load_litellm_completion
        self._internal_local_key_provider = internal_local_key_provider
        self._model_http_client_factory = model_http_client_factory
        self.server_model_access = server_model_access
        self.server_model_balances = server_model_balances
        self._lock = RLock()
        self.subscriptions = subscriptions or ChatGPTSubscriptions(records, self.secrets)
        self._responses = ResponsesCompletion()
        self._embedding_projection = None
        self._embedding_policy_reader = None
        self._provider_connections = None

    def bind_provider_connections(self, registry):
        if (getattr(registry, '_connection_owner', None) is not self
                or registry.records is not self.records or registry.secrets is not self.secrets):
            raise ModelConfigurationError('provider_connection_owner_invalid')
        with self._lock:
            self._provider_connections = registry

    def select_provider_connection(self, identity, **values):
        if self._provider_connections is None:
            raise ModelConfigurationError('provider_connection_owner_invalid')
        from .provider_connection_config import select_provider_connection
        return select_provider_connection(self, self._provider_connections, identity, **values)

    def _connection_status(self, purpose, payload):
        use = self.records.read('v2_provider_connection_uses', purpose)
        if use is None or payload.get('secret_ref') != use.payload['secret_ref']:
            return None
        available = True
        try:
            self._validate_connection(purpose, payload)
        except ModelConfigurationError:
            available = False
        return {'id': use.payload['connection_id'], 'revision': use.payload['connection_revision'], 'available': available}

    def _validate_connection(self, purpose, payload):
        use = self.records.read('v2_provider_connection_uses', purpose)
        if use is None or payload.get('secret_ref') != use.payload['secret_ref']:
            return
        if self._provider_connections is None:
            raise ModelConfigurationError('provider_connection_owner_invalid')
        try:
            self._provider_connections.validate_connection_use(self.records, purpose, payload)
        except ValueError as error:
            raise ModelConfigurationError(str(error)) from None

    def _release_model_secret(self, ref):
        if not ref:
            return
        if self._provider_connections is not None:
            self._provider_connections.release_connection_secret(ref)
        else:
            self.secrets.delete(ref)

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
        self._require_independent('generation')
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
            if self.server_model_access is not None:
                current = self._public_one(purpose)
                if (current.get('provider') != 'local' or current.get('mode') != 'local'
                        or any(configuration.get(key) != current.get(key)
                               for key in ('mode', 'model', 'model_key', 'mode_revision', 'runtime_revision'))):
                    raise ModelConfigurationError('model_price_configuration_changed')
            return {'rates': {'input_per_million': '0', 'output_per_million': '0',
                             'cache_read_per_million': '0'},
                    'source': 'local', 'revision': 0, 'editable': False}
        if self.server_model_access is not None:
            selection = self._server_selection(purpose, public=True)
            if selection['mode'] == 'shared':
                profile = selection['configuration']
                if profile is None or not selection['available']:
                    return {'rates': None, 'source': None, 'revision': 0, 'editable': False}
                binding = configuration.get('server_model_binding')
                if binding != selection['server_model_binding']:
                    raise ModelConfigurationError('server_model_binding_changed')
                return {'rates': profile.get('prices', {}).get(configuration['model']),
                    'source': 'server', 'revision': binding['profile_revision'], 'editable': False}
        instant = at if at is not None else datetime.now(timezone.utc)
        saved = self.records.read(PRICES, purpose)
        revision = saved.revision if saved else 0
        current_binding = configuration_binding(configuration)
        saved_binding = saved.payload.get('configuration') if saved else None
        same_configuration = saved_binding == current_binding
        if (not same_configuration and isinstance(saved_binding, Mapping)
                and current_binding.get('server_model_binding', {}).get('mode') == 'independent'
                and saved_binding.get('server_model_binding', {}).get('mode', 'independent') == 'independent'):
            # 个人价目继续属于原个人配置；中央价目在上方由中央主人独立投影。
            same_configuration = ({key: value for key, value in saved_binding.items() if key != 'server_model_binding'}
                == {key: value for key, value in current_binding.items() if key != 'server_model_binding'})
        if saved and same_configuration:
            return {'rates': dict(saved.payload['rates']), 'source': 'manual', 'revision': revision, 'editable': True}
        known = official_prices(configuration.get('provider', ''), configuration.get('model', ''),
                                configuration.get('base_url', ''))
        rates = known.at(instant) if known is not None else None
        return {'rates': rates, 'source': known.source if rates is not None else None,
                'revision': revision, 'editable': rates is None}

    def update_model_prices(self, purpose, rates, *, expected_revision, expected_configuration_revision):
        self._require_independent(purpose)
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

    def price_wire_sink(self, sink, *, purpose='generation', configuration, max_output_tokens=None, wire_request=None):
        self.require_model_wire_owner(purpose, sink)
        binding = configuration.get('server_model_binding')
        selection = self._server_selection(purpose, public=True)
        shared = (isinstance(binding, Mapping) and binding.get('mode') == 'shared'
            or selection is not None and selection['mode'] == 'shared')
        local = purpose == 'embedding' and configuration.get('provider') == 'local' and configuration.get('mode') == 'local'
        if shared and not local:
            if (self.server_model_access is None or self.server_model_balances is None
                    or self.server_model_balances.models is not self.server_model_access._owner):
                raise ModelConfigurationError('server_model_billing_owner_unavailable')
            if not isinstance(binding, Mapping) or binding.get('mode') != 'shared':
                raise ModelConfigurationError('server_model_binding_changed')
            if (sink is None or not all(isinstance(getattr(sink, key, None), str)
                    for key in ('turn_id', 'model_request_id'))):
                raise ModelConfigurationError('server_model_wire_owner_required')
            priced = PriceRecordingSink(sink, self, purpose, configuration)
            return BalanceRecordingSink(priced, self.server_model_balances, self.server_model_access,
                purpose, configuration, max_output_tokens=max_output_tokens, wire_request=wire_request,
                cost_records=self.records)
        if sink is None or not all(isinstance(getattr(sink, key, None), str)
                                   for key in ('turn_id', 'model_request_id')):
            return sink
        return PriceRecordingSink(sink, self, purpose, configuration)

    def public(self) -> dict:
        with self._lock:
            return {purpose: self._public_one(purpose) for purpose in PURPOSES} | {
                "generation_mode": self.generation_mode(),
            }

    def public_for_purpose(self, purpose: str) -> dict:
        if purpose not in PURPOSES:
            raise ModelConfigurationError('unknown model purpose')
        with self._lock:
            # 委托实际实例，辅助模型仍须核验冻结的 fast 绑定。
            return self._public_one(purpose)

    def _generation_authority_binding(self):
        public = self._public_one('generation')
        configuration = public_model_configuration(public)
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
        server_selection = self._server_selection('generation', public=True)
        if server_selection is not None:
            if server_selection['mode'] == 'shared':
                public = self._public_one('generation')
                configuration = public_model_configuration(public)
                base, mode, subscription = configuration['base_url'], 'api', False
            else:
                configuration['server_model_binding'] = dict(server_selection['server_model_binding'])
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
            if configured:
                selection = self._server_selection('generation', public=True)
                if selection is not None and selection['mode'] == 'shared':
                    configured = bool(selection['available'] and model in selection['configuration']['allowed_models'])
            return {'model': model, 'revision': row.revision if row else 0, 'configured': configured}

    def update_fast_model(self, *, model, expected_revision, expected_generation_revision,
                          expected_mode_revision):
        if (model is not None and (not isinstance(model, str) or not model.strip() or len(model) > 200)
                or any(type(value) is not int or value < 0 for value in
                       (expected_revision, expected_generation_revision, expected_mode_revision))):
            raise ModelConfigurationError('fast_model_invalid')
        model = model.strip() if model is not None else None
        selection = self._server_selection('generation')
        if (model is not None and selection is not None and selection['mode'] == 'shared'
                and model not in selection['configuration']['allowed_models']):
            raise ModelConfigurationError('server_model_not_allowed')
        initial_binding = self._generation_authority_binding()
        if (selection is not None and initial_binding['configuration'].get('server_model_binding')
                != selection['server_model_binding']):
            raise ModelConfigurationError('fast_model_configuration_changed')
        catalog = None
        if model is not None and not (selection and selection['mode'] == 'shared') and self.subscription_selection()['model']:
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
                if (not (selection and selection['mode'] == 'shared') and self.subscription_selection()['model']
                        and (catalog is None or model not in catalog)):
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
                or not valid_public_configuration_fields(binding['parent']['configuration'])):
            raise ModelConfigurationError('fast_model_binding_invalid')
        if binding['model'] is None:
            return self
        return _AuxiliaryGenerationConfiguration(self, binding)

    def _public_one(self, purpose: str) -> dict:
        record = self.records.read("recognition_model_config", purpose)
        payload = dict(record.payload) if record else {}
        selection = self._server_selection(purpose, public=True)
        if selection is not None and selection['mode'] == 'shared':
            profile = selection['configuration']
            public = {'purpose': purpose, **{key: profile[key] if profile else '' for key in ('provider', 'base_url', 'model')},
                'allow_remote': payload.get('allow_remote', False),
                'enabled': payload.get('enabled', False) is True,
                'revision': record.revision if record else 0, 'has_api_key': selection['available'],
                'configured': selection['available'],
                'server_managed': True,
                'allowed_models': list(profile['allowed_models']) if profile else [],
                **({'server_model_binding': dict(selection['server_model_binding'])}
                   if selection['server_model_binding'] is not None else {})}
            if purpose == 'embedding' and self._embedding_projection is not None:
                public = self._embedding_projection(public)
                if public.get('mode') == 'local' and public.get('provider') == 'local':
                    public.pop('server_model_binding', None)
            if purpose == 'vision':
                from .local_image_provider import local_image_status
                choice = self.records.read('v2_vision_mode', 'default')
                local = local_image_status(self.root)
                public.update(mode=choice.payload['mode'] if choice else 'local',
                    mode_revision=choice.revision if choice else 0, local=local)
                if public['mode'] == 'local':
                    public['configured'] = local['status'] == 'ready'
            return public
        connection = self._connection_status(purpose, payload)
        secret_ref = payload.pop("secret_ref", "")
        has_key = bool(secret_ref and self.secrets.has_secret(secret_ref))
        local_selected = purpose == "generation" and self._local_selected() and _valid_local_profile(payload)
        selection = self.subscription_selection() if purpose == "generation" else {}
        if selection.get("model"):
            auth = self.subscriptions.status()
            return self._with_server_binding({"purpose": purpose, "provider": "openai", "base_url": RESOURCE,
                "model": selection["model"], "allow_remote": payload.get("allow_remote", False),
                "revision": record.revision if record else 0, "enabled": True,
                "has_api_key": False, "configured": auth["sharing"] and selection["account_revision"] == auth["revision"],
                "subscription_binding": {"selection": selection["revision"], "account": auth["revision"]}}, purpose)
        public = {
            "purpose": purpose, "provider": "openai", "base_url": "", "model": "",
            "allow_remote": False, **payload,
            "enabled": bool(payload.get("enabled", False)),
            "revision": record.revision if record else 0,
            "has_api_key": has_key,
            "configured": bool(payload.get("model") and payload.get("base_url")
                               and (self._local_generation_readiness().available if local_selected else has_key)
                               and (connection is None or connection['available'])),
        }
        if connection is not None:
            public['provider_connection'] = connection
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
        return self._with_server_binding(public, purpose)

    def _server_selection(self, purpose, *, public=False):
        if self.server_model_access is None:
            return None
        try:
            return (self.server_model_access.public_selection(purpose) if public
                    else self.server_model_access.selection(purpose))
        except ValueError as error:
            raise ModelConfigurationError(str(error)) from None

    def _with_server_binding(self, value, purpose):
        selection = self._server_selection(purpose)
        if selection is not None:
            if selection['mode'] != 'independent':
                raise ModelConfigurationError('server_model_binding_changed')
            value = {**value, 'server_model_binding': dict(selection['server_model_binding'])}
        return value

    def _require_independent(self, purpose):
        selection = self._server_selection(purpose, public=True)
        if selection is not None and selection['mode'] == 'shared':
            raise ModelConfigurationError('server_model_configuration_readonly')

    def require_model_wire_owner(self, purpose, sink):
        selection = self._server_selection(purpose, public=True)
        if selection is None or selection['mode'] != 'shared':
            return
        if purpose == 'embedding':
            public = self._public_one(purpose)
            if public.get('provider') == 'local' and public.get('mode') == 'local':
                return
        if not callable(getattr(sink, 'begin_model_wire_attempt', None)):
            raise ModelConfigurationError('server_model_wire_owner_required')

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
            selection = self._server_selection('vision', public=True)
            if selection is not None and selection['mode'] == 'shared':
                public = self._public_one('vision')
                return {key: public.get(key) for key in
                    ('provider', 'model', 'base_url', 'allow_remote', 'revision', 'mode', 'mode_revision')} | {
                        'has_key': public['has_api_key'],
                        'server_model_binding': public.get('server_model_binding')}
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
            local = self._local_generation_readiness()
            selection = self._server_selection('generation', public=True)
            if selection is not None and selection['mode'] == 'shared':
                choice = self.records.read(_GENERATION_MODE_COLLECTION, _GENERATION_MODE_ID)
                return {'mode': 'api', 'local_enabled': False, 'local_base_url': _DEFAULT_LOCAL_BASE_URL,
                    'local_model': _LOCAL_MODEL, 'local_model_installed': local.installed,
                    'local_readiness': local.public(),
                    'api_configured': selection['available'], 'revision': choice.revision if choice else 0}
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
            connection = self._connection_status('generation', api)
            return {
                "mode": "subscription" if self.subscription_selection()["model"] else mode,
                "local_enabled": local_enabled,
                "local_base_url": local_base_url,
                "local_model": _LOCAL_MODEL,
                "local_model_installed": local.installed,
                "local_readiness": local.public(),
                "api_configured": bool(api.get("base_url") and api.get("model") and api_ref
                                       and self.secrets.has_secret(str(api_ref))
                                       and (connection is None or connection['available'])),
                "revision": choice.revision if choice else 0,
            }

    def _local_model_installed(self) -> bool:
        return self._local_generation_readiness().installed

    def _local_generation_readiness(self):
        return inspect_local_generation(self._local_models_root / _LOCAL_MODEL)

    def _local_selected(self) -> bool:
        selection = self._server_selection('generation', public=True)
        if selection is not None and selection['mode'] == 'shared':
            return False
        choice = self.records.read(_GENERATION_MODE_COLLECTION, _GENERATION_MODE_ID)
        if choice:
            return choice.payload.get("mode") == "local" and choice.payload.get("local_enabled") is True
        current = self.records.read("recognition_model_config", "generation")
        return bool(current and current.payload.get("model") == _LOCAL_MODEL
                    and _is_loopback_endpoint(str(current.payload.get("base_url") or "")))

    def local_generation_selected(self) -> bool:
        with self._lock:
            if self.subscription_selection()["model"]:
                return False
            current = self.records.read("recognition_model_config", "generation")
            return bool(self._local_selected() and current
                        and _valid_local_profile(current.payload))

    def local_generation_allowed(self) -> bool:
        return self.local_generation_selected() and self._local_generation_readiness().available

    def update_generation_mode(
        self, *, mode: str, local_enabled: bool, local_base_url: str,
        expected_revision: int,
    ) -> dict[str, object]:
        self._require_independent('generation')
        if not isinstance(mode, str) or mode not in {"api", "local"} or not isinstance(local_enabled, bool):
            raise ModelConfigurationError("invalid_generation_mode")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            raise ModelConfigurationError("expected_revision must be a nonnegative integer")
        local_base_url = _validated_local_model_base_url(local_base_url)
        if mode == "local" and not local_enabled:
            raise ModelConfigurationError("enable_local_model_before_selection")
        with self._lock:
            choice = self.records.read(_GENERATION_MODE_COLLECTION, _GENERATION_MODE_ID)
            current_choice_revision = choice.revision if choice else 0
            if current_choice_revision != expected_revision:
                raise ModelConfigurationError("generation_mode_revision_conflict")
            if mode == "local":
                try:
                    readiness = self._local_generation_readiness()
                    readiness.require_selection()
                except LocalGenerationError as error:
                    raise ModelConfigurationError(str(error)) from error
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
                if mode == 'local':
                    try:
                        latest = self._local_generation_readiness()
                        latest.require_selection()
                        if latest.identity != readiness.identity:
                            raise LocalGenerationError('local_model_assets_changed')
                    except LocalGenerationError as error:
                        raise ModelConfigurationError(str(error)) from error
                tx.commit()
            if mode == 'local':
                retry_loading(latest)
            return self.generation_mode()

    def update(self, purpose: str, data: dict) -> dict:
        if purpose not in PURPOSES:
            raise ModelConfigurationError("unknown model purpose")
        selection = self._server_selection(purpose, public=True)
        if selection is not None and selection['mode'] == 'shared':
            if set(data) - {'allow_remote', 'enabled', 'expected_revision'}:
                raise ModelConfigurationError('server_model_configuration_readonly')
            if any(type(data[key]) is not bool for key in ('allow_remote', 'enabled') if key in data):
                raise ModelConfigurationError('invalid model configuration')
            revision = data.get('expected_revision')
            if type(revision) is not int or revision < 0:
                raise ModelConfigurationError('expected_revision must be a nonnegative integer')
            with self._lock, self.records.begin() as tx:
                if selection['server_model_binding'] is not None:
                    self.server_model_access.validate_binding(selection['server_model_binding'])
                current = tx.read('recognition_model_config', purpose)
                payload = dict(current.payload) if current else {}
                payload.update({key: data[key] for key in ('allow_remote', 'enabled') if key in data})
                tx.put('recognition_model_config', purpose, payload, expected_revision=revision)
                tx.commit()
            return self._public_one(purpose)
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
                self._validate_connection(purpose, payload)
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
                self._release_model_secret(old_ref)
            return self._public_one(purpose)

    def snapshot(self, purpose: str) -> dict:
        if purpose not in PURPOSES:
            raise ModelConfigurationError("unknown model purpose")
        with self._lock:
            if purpose == 'embedding' and self.server_model_access is not None and self._embedding_projection is not None:
                selected = self._public_one(purpose)
                if selected.get('mode') == 'local' and selected.get('provider') == 'local':
                    if not selected['configured']:
                        raise ModelConfigurationError('model_not_configured')
                    return {**selected, 'api_key': 'local-vector'}
            selection = self._server_selection(purpose)
            if selection is not None and selection['mode'] == 'shared':
                public = self._public_one(purpose)
                binding = selection['server_model_binding']
                if public['server_model_binding'] != binding:
                    raise ModelConfigurationError('server_model_binding_changed')
                try:
                    credential = self.server_model_access.credential(binding)
                except ValueError as error:
                    raise ModelConfigurationError(str(error)) from None
                return {**public, 'api_key': credential.value}
            record = self.records.read("recognition_model_config", purpose)
            if purpose == 'embedding':
                selected = self._public_one(purpose)
                if selected.get('mode') == 'local':
                    if not selected['configured']:
                        raise ModelConfigurationError('model_not_configured')
                    return self._with_server_binding({**selected, 'api_key': 'local-vector'}, purpose)
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
                return self._with_server_binding({**before, "api_key": key}, purpose)
            if not record:
                raise ModelConfigurationError("model_not_configured")
            payload = dict(record.payload)
            self._validate_connection(purpose, payload)
            secret_ref = payload.pop("secret_ref", "")
            local_selected = purpose == "generation" and self._local_selected()
            if local_selected and not _valid_local_profile(payload):
                raise ModelConfigurationError("local_model_configuration_invalid")
            if local_selected:
                try:
                    self._local_generation_readiness().require_available()
                except LocalGenerationError as error:
                    raise ModelConfigurationError(str(error)) from error
            key = "local-model" if local_selected else self.secrets.get_snapshot(secret_ref).value
            if not key or not payload.get("model") or not payload.get("base_url"):
                raise ModelConfigurationError("model_not_configured")
            result = {**payload, "api_key": key, "revision": record.revision}
            if purpose == 'embedding' and self._embedding_projection is not None:
                result.update(mode=selected['mode'], mode_revision=selected['mode_revision'])
                if selected['mode'] == 'server':
                    result['model_key'] = selected['model_key']
            return self._with_server_binding(result, purpose)

    def search(self, query, *, parameters, messages_factory, normalize_results, validate_current, wire_attempt_sink):
        """复用原搜索传输，以当前用途权限和原内核 wire 回执约束它。"""
        self.require_model_wire_owner('search', wire_attempt_sink)
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
            if fresh.get('provider') == 'brave':
                if fresh.get('server_model_binding', {}).get('mode') != 'shared':
                    raise ModelConfigurationError('search_configuration_changed')
            else:
                _validate_generation_egress(fresh)

        current()
        if cfg.get('provider') == 'brave':
            return self._shared_brave_search(query, cfg=cfg, parameters=parameters,
                normalize_results=normalize_results, current=current, wire_attempt_sink=wire_attempt_sink)
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
        except SharedModelMeteringError as error:
            raise _model_request_failure(error) from None
        except Exception:
            raise ModelConfigurationError('search_request_failed') from None
        return results, {'model':cfg['model'], 'configuration_revision':cfg['revision'], 'usage':usage}

    def _shared_brave_search(self, query, *, cfg, parameters, normalize_results, current, wire_attempt_sink):
        from backend.video_summary.infrastructure.litellm_web_search import BraveWebSearchGateway
        request = BraveWebSearchGateway.prepare_request(query, count=parameters['max_results'])
        sink = self.price_wire_sink(wire_attempt_sink, purpose='search', configuration=cfg, wire_request=request)
        attempt = sink.begin_model_wire_attempt()

        def wire():
            current()
            try:
                # 预留已成功且绑定仍有效，才取得中央密钥并发送一次请求。
                fresh = self.snapshot('search')
                results = BraveWebSearchGateway.send(request, api_key=fresh['api_key'],
                    timeout_seconds=parameters['timeout_seconds'], validate_current=current)
                attempt.observe_metering({'completed_requests': 1})
                attempt.succeeded(usage={}, cache_observation=None)
                return results
            except BaseException:
                attempt.failed_transport(error_code='search_provider_request_failed')
                raise
        try:
            results = attempt.invoke_wire(wire)
            current()
            results = normalize_results(results, credentials=(cfg['api_key'],))
            return results, {'model': cfg['model'], 'configuration_revision': cfg['revision'], 'usage': {}}
        except SharedModelMeteringError as error:
            raise _model_request_failure(error) from None
        except (RecognitionConflict, ModelConfigurationError, RunLeaseRevoked):
            raise
        except Exception:
            raise ModelConfigurationError('search_request_failed') from None

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
        self.require_model_wire_owner('generation', wire_attempt_sink)
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
        wire_attempt_sink = self.price_wire_sink(wire_attempt_sink, configuration=cfg, max_output_tokens=max_tokens)
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
                wire_limit = control.limits.get('max_wire_attempts', 3) if retry_policy is not None else 3
                if type(wire_limit) is not int or not 1 <= wire_limit <= 3:
                    raise ModelConfigurationError('model_wire_attempt_budget_invalid')
                text, usage, _ = gateway.complete_structured_with_usage(messages,
                    response_model=response_model, max_tokens=max_tokens, timeout=timeout,
                    retries=0, max_wire_attempts=wire_limit,
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
        self.require_model_wire_owner('vision', wire_attempt_sink)
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
        gateway_factory = (OxrImageGateway if is_oxr_endpoint(cfg['base_url'], cfg['model'])
                           else self._gateway_factory)
        gateway = gateway_factory(provider=cfg['provider'], model=cfg['model'],
            base_url=cfg['base_url'], api_key=None, api_key_provider=lambda: cfg['api_key'],
            egress_guard=guard, egress_purpose='media_image_read',
            egress_categories=('instructions', 'image'),
            completion_fn=_validated_generation_completion(self._completion_fn))
        sink = self.price_wire_sink(wire_attempt_sink, purpose='vision', configuration=cfg, max_output_tokens=max_tokens)
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
        except (RecognitionConflict, ModelConfigurationError, RunLeaseRevoked):
            raise
        except SharedModelMeteringError as error:
            raise _model_request_failure(error) from None
        except Exception:
            raise ModelConfigurationError('vision_request_failed') from None
        return output, {'model': cfg['model'], 'configuration_revision': cfg['revision'],
            'usage': usage, 'context_budget': _gateway_budget_snapshot(gateway)}

    def complete_stream(
        self, messages, *, response_model, max_tokens=1800, validate_current=None, on_delta,
    ):
        _validate_current(validate_current)
        self.require_model_wire_owner('generation', None)
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
        stop_requested: Callable[[], bool] | None = None,
        attempt_interruption: OwnedAttemptInterruption | None = None,
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
        if attempt_interruption is not None and (not isinstance(attempt_interruption,OwnedAttemptInterruption)
                or purpose != 'primary' or retry_policy is None):
            raise ModelConfigurationError('model_attempt_interruption_invalid')
        if provider_store_activation is not None:
            if (type(provider_store_activation) is not ProviderStoreActivation
                    or provider_store_activation.expected_purpose != purpose or retry_policy is None):
                raise ModelConfigurationError('provider_background_adapter_invalid')
            provider_store_activation.validate_current()
        _validate_current(validate_current)
        if stop_requested is not None:
            stop_requested()
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
        private_prefix = []
        output_clock, output_wall = monotonic(), time()
        retry_control = None
        try:
            def validate_stream():
                if stop_requested is not None:
                    stop_requested()
                _checkpoint(execution_control)
                _validate_current(validate_current)
                self._assert_governed_configuration_current(
                    initial_identity=initial_identity, expected_public=route["configuration"])
                if provider_store_activation is not None:
                    provider_store_activation.validate_current()

            options = dict(max_tokens=max_tokens, timeout=remaining_timeout,
                           wire_attempt_sink=self.price_wire_sink(wire_attempt_sink, configuration=cfg,
                               max_output_tokens=max_tokens))
            if retry_policy is not None:
                # The governed caller supplies its frozen get('retry') policy.
                # Missing injection retains the historical single-wire path.
                options['retry_control'] = ModelRetryControl(policy=retry_policy,
                    checkpoint=validate_stream, on_retry=on_retry,
                    owned_client_factory=self._model_http_client_factory,interruption=attempt_interruption)
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
            if response_model is not None and (on_delta is not None or stop_requested is not None):
                def delivered(text):
                    if retry_control is not None:
                        partial_text.append(text)
                    if on_delta is not None:
                        on_delta(text)
                def decoded(value):
                    if on_delta is None:
                        private_prefix[:] = [value['raw']]
                    if observe_decoding is not None:
                        observe_decoding(value)
                text, usage = gateway.stream_structured_with_usage(
                    messages, response_model=response_model, on_delta=delivered,
                    validate_current=validate_stream,
                    field=stream_field if on_delta is not None else None,
                    observe=decoded, **options)
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
            if (stop_requested is not None and stop_requested()
                    and getattr(execution_control, 'cancel_requested', False)
                    and retry_control is not None):
                from backend.shared.llm.model_transport import is_provider_closed_witness
                if is_provider_closed_witness(retry_control.closed_witness):
                    # 停止原因来自本轮用户操作，关闭证明仍由真实传输提供。
                    partial = retry_policy({'kind': 'partial_output', 'text': ''.join(partial_text),
                        'wall_elapsed': time() - output_wall, 'monotonic_elapsed': monotonic() - output_clock})
                    interrupted = ModelInterrupted(partial=partial['partial'] if on_delta is not None else '',
                        interruption='stopped', close_witness=retry_control.closed_witness)
                    interrupted.internal_partial = ''.join(private_prefix)
                    raise interrupted from None
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
            failure = _model_request_failure(error)
            if retry_control is not None:
                from backend.shared.llm.model_transport import is_provider_closed_witness
                if is_provider_closed_witness(retry_control.closed_witness):
                    failure.close_witness = retry_control.closed_witness
            raise failure from None

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
        cfg = self.public_for_purpose("generation")
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
        from .v2.policies import get
        capacity = getattr(get('ladder'), 'context_window', None)
        context_window = capacity(cfg['provider'], cfg['model'], cfg['base_url']) if callable(capacity) else 16000
        return self._gateway_factory(
            provider=cfg["provider"], model=cfg["model"], base_url=cfg["base_url"],
            api_key=None, api_key_provider=lambda: (
                self._internal_local_key_provider(str(cfg['base_url'])) or cfg['api_key']
                if self._internal_local_key_provider is not None and self._local_selected() and _valid_local_profile(cfg)
                else cfg['api_key']),
            egress_guard=self._generation_egress_guard(dict(cfg), validate_current=validate_current),
            egress_purpose="memory_generation",
            egress_categories=("instructions", "source_excerpt"),
            context_window_tokens=context_window, reserved_output_tokens=2000,
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
        if self.owner.server_model_access is not None:
            frozen = value['parent']['configuration'].get('server_model_binding')
            if frozen is None:
                raise ModelConfigurationError('fast_model_configuration_changed')
            try:
                selection = self.owner.server_model_access.validate_binding(frozen)
            except ValueError:
                raise ModelConfigurationError('fast_model_configuration_changed') from None
            if (selection['mode'] == 'shared'
                    and value['model'] not in selection['configuration']['allowed_models']):
                raise ModelConfigurationError('server_model_not_allowed')

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
    if not valid_public_configuration_fields(expected):
        return False
    if ('server_model_binding' in current) != ('server_model_binding' in expected):
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
        snapshot.get('server_model_binding'),
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

    if is_official_terra('openai', str(request.get('model', '')), str(request.get('api_base', ''))):
        from backend.shared.llm.openai_sdk import OpenAIResponsesCompletion
        return OpenAIResponsesCompletion()(**request)
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
