from collections.abc import Mapping
from pathlib import Path

from pydantic import RootModel

from backend.recognition import RecognitionConflict, WorkScope
from backend.shared.server_resources import RESOURCE_POOL
from core.product_core.cloud_asr_provider_settings import GetCloudAsrProviderSettings
from ..model_config import ModelConfiguration
from ..model_costs import BalanceRecordingSink
from ..original_sources import original, source_store
from ..source_egress import SourceEgressService
from .memory_turn import MemoryTurn
from .policies.shared_metering import get


BINDINGS = 'v2_asr_shared_bindings'


def asr_wire_owner(runtime_root):
    resources = RESOURCE_POOL.get()
    factory = getattr(resources, 'server_asr_wire_owner_for', None)
    if not callable(factory):
        raise RecognitionConflict('server_asr_wire_owner_unavailable')
    owner = factory(Path(runtime_root).resolve())
    if not isinstance(owner, tuple) or len(owner) != 2:
        raise RecognitionConflict('server_asr_wire_owner_unavailable')
    records, models = owner
    access = getattr(models, 'server_model_access', None)
    balances = getattr(models, 'server_model_balances', None)
    root = Path(runtime_root).resolve()
    if (not isinstance(models, ModelConfiguration) or models.records is not records
            or models.root.resolve() != root or access is None or access.user_root != root
            or balances is None or balances.models is not access._owner):
        raise RecognitionConflict('server_asr_wire_owner_unavailable')
    return records, models


def shared_asr_configuration(runtime_root):
    records, models = asr_wire_owner(runtime_root)
    selection = models.server_model_access.selection('asr')
    profile = selection['configuration']
    if (selection['mode'] != 'shared' or profile is None
            or not get(version='@3').asr_configuration(**{key: profile[key] for key in ('provider', 'base_url', 'model')})):
        raise RecognitionConflict('server_asr_protocol_unavailable')
    with models.server_model_access._owner.records.begin_read() as reader:
        price = models.server_model_access._owner.metering_price(reader, profile['profile_id'], profile['model'])
    if price['price']['unit'] != 'audio_seconds':
        raise RecognitionConflict('server_asr_price_unavailable')
    return records, models, selection


def frozen_asr_mode(runtime_root, binding_id):
    records, models = asr_wire_owner(runtime_root)
    frozen = records.read(BINDINGS, binding_id)
    access = models.server_model_access
    if frozen is not None:
        access.validate_binding(frozen.payload['binding'])
        return 'shared'
    # 历史版本不允许共享转写，无共享旁路的旧绑定保持独立凭据归属。
    if access.selection('asr')['mode'] != 'independent':
        raise RecognitionConflict('server_asr_admission_mode_changed')
    return 'independent'


def freeze_shared_asr_binding(runtime_root, object_store, *, binding_id, source_id, project_id, job_id, manifest_id):
    records, models, selection = shared_asr_configuration(runtime_root)
    scope = WorkScope('local-user', project_id)
    if source_store(records).read('sources', source_id) is not None:
        material = {'type': 'original_source', 'id': source_id, 'project_id': project_id,
            'revision': original(records, scope, 'original_source', source_id).revision}
    else:
        matches = [row for row in records.list('workspace_items')
            if row.payload.get('processing_run_id') == job_id and row.payload.get('project_id') == project_id
            and row.payload.get('status') == 'processing']
        if len(matches) != 1:
            raise RecognitionConflict('server_asr_original_unavailable')
        material = {'type': 'original_item', 'id': matches[0].object_id,
            'revision': matches[0].revision, 'project_id': project_id}
    settings = GetCloudAsrProviderSettings(object_store).execute()
    authority = SourceEgressService(records)
    snapshot = authority.snapshot(scope, [{key: material[key] for key in ('type', 'id', 'revision')}])
    authority.require(snapshot, 'asr')
    payload = {'binding': selection['server_model_binding'], 'material': material,
        'settings_revision': settings.settings_revision, 'manifest_id': manifest_id,
        'source_id': source_id, 'project_id': project_id, 'source_snapshot': snapshot}
    with records.begin() as tx:
        prior = tx.read(BINDINGS, binding_id)
        if prior is None:
            tx.put(BINDINGS, binding_id, payload, expected_revision=0)
        elif prior.payload != payload:
            raise RecognitionConflict('server_asr_binding_changed')
        tx.commit()
    return payload


def freeze_shared_asr_sample(runtime_root, object_store, *, binding_id, source_id, project_id, manifest_id, registered_sample):
    from .purpose_samples import RegisteredPurposeSample
    from .privacy import is_private_project
    records, models, selection = shared_asr_configuration(runtime_root)
    if (type(registered_sample) is not RegisteredPurposeSample or registered_sample.records is not records
            or registered_sample.root != Path(runtime_root).resolve() or registered_sample.project_id != project_id
            or registered_sample.purpose != 'asr' or is_private_project(records, project_id)
            or source_id != 'model-sample-' + registered_sample.command_id):
        raise RecognitionConflict('server_asr_sample_unavailable')
    registered_sample.validate()
    settings = GetCloudAsrProviderSettings(object_store).execute()
    payload = {'kind': 'purpose_test_sample', 'registered_sample': registered_sample.descriptor,
        'binding': selection['server_model_binding'], 'settings_revision': settings.settings_revision,
        'manifest_id': manifest_id, 'source_id': source_id, 'project_id': project_id}
    with records.begin() as tx:
        old = tx.read(BINDINGS, binding_id)
        if old is None:
            tx.put(BINDINGS, binding_id, payload, expected_revision=0)
        elif old.payload != payload:
            raise RecognitionConflict('server_asr_binding_changed')
        tx.commit()
    return payload


class _AsrModelsView:
    def __init__(self, models, object_store, frozen):
        self.models, self.object_store, self.frozen = models, object_store, frozen

    def __getattr__(self, name):
        return getattr(self.models, name)

    def route(self):
        settings = GetCloudAsrProviderSettings(self.object_store).execute()
        selection = self.models.server_model_access.validate_binding(self.frozen['binding'])
        profile = selection['configuration']
        if not settings.enabled or settings.settings_revision != self.frozen['settings_revision']:
            raise RecognitionConflict('server_asr_settings_changed')
        return {'purpose': 'asr', **{key: profile[key] for key in ('provider', 'model', 'base_url')},
            'revision': settings.settings_revision, 'enabled': settings.enabled, 'allow_remote': settings.enabled,
            'configured': True, 'server_model_binding': dict(self.frozen['binding'])}

    def public(self):
        return {**self.models.public(), 'asr': self.route()}


def run_shared_asr_wire(*, runtime_root, records, models, project_id, source_id, asset_id,
                        binding_id, object_store, request, validate_wire, invoke, dispatch_id, registered_sample=None):
    saved = records.read(BINDINGS, binding_id)
    if saved is None:
        raise RecognitionConflict('server_asr_binding_unavailable')
    frozen = saved.payload
    if frozen['source_id'] != source_id or frozen['project_id'] != project_id:
        raise RecognitionConflict('server_asr_binding_changed')
    sample = frozen.get('kind') == 'purpose_test_sample'
    if sample:
        from .purpose_samples import RegisteredPurposeSample
        if (type(registered_sample) is not RegisteredPurposeSample or registered_sample.records is not records
                or registered_sample.root != Path(runtime_root).resolve() or registered_sample.project_id != project_id
                or registered_sample.purpose != 'asr' or registered_sample.descriptor != frozen.get('registered_sample')
                or source_id != 'model-sample-' + registered_sample.command_id
                or asset_id != 'sample-audio-' + registered_sample.command_id):
            raise RecognitionConflict('server_asr_sample_unavailable')
    elif registered_sample is not None or frozen.get('kind') is not None:
        raise RecognitionConflict('server_asr_binding_changed')
    view = _AsrModelsView(models, object_store, frozen)

    def validate():
        from backend.api.tokenhub_asr_provider import tokenhub_egress_manifest
        from backend.security.provider_egress import ProviderEgressPolicyStore
        current_records, current_models = asr_wire_owner(runtime_root)
        if current_records is not records or current_models is not models:
            raise RecognitionConflict('server_asr_wire_owner_changed')
        if records.read(BINDINGS, binding_id) != saved:
            raise RecognitionConflict('server_asr_binding_changed')
        if sample:
            registered_sample.validate()
            registered_sample.require_remote(view)
        else:
            authority = SourceEgressService(records)
            authority.validate_snapshot(WorkScope('local-user', project_id), frozen['source_snapshot'])
            authority.require(frozen['source_snapshot'], 'asr')
        manifest = tokenhub_egress_manifest(runtime_root)
        if (manifest.manifest_id != frozen['manifest_id']
                or not ProviderEgressPolicyStore(runtime_root).is_consented(manifest)):
            raise RecognitionConflict('server_asr_consent_changed')
        view.route()
        validate_wire()

    arguments = dict(kind='memory.link_suggest', project=project_id,
        key={'namespace': 'shared-asr-wire-v1', 'dispatch_id': dispatch_id, 'asset_id': asset_id},
        materials=[] if sample else [frozen['material']], validate=validate, purpose='asr')
    if sample:
        from ..kernel.memory_turn import MemoryTurn as KernelMemoryTurn
        from .privacy import egress_allowed
        turn = KernelMemoryTurn(records, view, **arguments, freeze_request=registered_sample.freeze,
            validate_request=registered_sample.validate_request, remote_allowed=egress_allowed)
    else:
        turn = MemoryTurn(records, view, **arguments)

    def execute(control, current):
        current()
        if sample:
            registered_sample.publish(turn.request, view)
        sink = BalanceRecordingSink(control, models.server_model_balances, models.server_model_access,
            'asr', view.route(), wire_request=request, cost_records=records)
        attempt = sink.begin_model_wire_attempt()

        def wire():
            try:
                current()
                credential = models.server_model_access.credential(frozen['binding'])
                status, response = invoke(credential.value, attempt.mark_transmission_started)
                if not 200 <= status < 300:
                    raise RecognitionConflict('server_asr_http_failed')
                attempt.observe_metering(response)
                attempt.succeeded(usage={}, cache_observation=None)
                return response
            except BaseException as error:
                if not attempt.transmission_started:
                    raw = getattr(error, 'public_code', str(error))
                    code = raw if isinstance(raw, str) and raw.startswith(('server_model_', 'server_asr_')) else (
                        'server_asr_source_changed' if isinstance(error, RecognitionConflict)
                        or raw == 'remote_processing_target_changed' else
                        'server_asr_owner_changed' if raw in {'user_unauthorized', 'user_disabled'} else 'shared_asr_not_sent')
                    attempt.confirmed_none(error_code=code, error=error)
                    if code in {'server_asr_source_changed', 'server_asr_owner_changed'}:
                        raise RecognitionConflict(code) from None
                else:
                    attempt.failed_transport(error_code='shared_asr_request_failed')
                raise
        response = attempt.invoke_wire(wire)
        current()
        if not isinstance(response, Mapping):
            raise RecognitionConflict('server_asr_response_invalid')
        return RootModel[dict](dict(response)), {'model': view.route()['model'], 'usage': {}}

    output, _ = turn.generate([], response_model=RootModel[dict], max_tokens=0, invoke=execute)
    return 200, output.root
