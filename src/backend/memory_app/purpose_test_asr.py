from pathlib import Path
from io import BytesIO
import re
import wave

from backend.api.tokenhub_asr_provider import (GetCloudAsrProviderSettings, TokenHubAudioAssetTranscriber,
    freeze_workbench_asr_binding, server_asr_access, shared_cloud_asr_status, tokenhub_egress_manifest)
from backend.security.provider_egress import ProviderEgressPolicyStore
from backend.shared.secret_detection import redact_secrets
from core.product_core.cloud_asr_provider_settings import TOKENHUB_ASR_SECRET_REF
from .rebuild_storage import build_rebuild_object_store
from .v2.privacy import is_private_project
from .v2.purpose_samples import CHECKS, RegisteredPurposeSample
from .v2.receipt_history import cost_totals, number, usage_view
from .workspace_contracts import _now


def asr_test_configuration(root, container):
    root = Path(root).resolve()
    if container is None or Path(container.root_dir).resolve() != root:
        raise ValueError('purpose_test_asr_owner_unavailable')
    objects, _ = build_rebuild_object_store(root)
    settings = GetCloudAsrProviderSettings(objects).execute()
    available = shared_cloud_asr_status(root)
    access = server_asr_access(root)
    selection = access.public_selection('asr') if access is not None else None
    generation = available['credential_generation'] if available['shared'] else container.secret_store.get_generation(TOKENHUB_ASR_SECRET_REF)
    manifest = tokenhub_egress_manifest(root)
    return {'identity': {'settings_revision': settings.settings_revision, 'secret_generation': generation,
                'server_model_binding': selection['server_model_binding'] if selection is not None else None},
            'enabled': settings.enabled, 'available': available['available'], 'reason': available['reason'],
            'has_api_key': available['available'] if available['shared'] else container.secret_store.has_secret(TOKENHUB_ASR_SECRET_REF),
            'consented': ProviderEgressPolicyStore(root).is_consented(manifest)}


def validate_asr_test(sample, container, *, validate):
    if type(sample) is not RegisteredPurposeSample or sample.purpose != 'asr':
        raise ValueError('purpose_test_sample_invalid')
    validate()
    sample.validate()
    if sample.project_id != 'default' and sample.records.read('v2_projects', sample.project_id) is None:
        raise ValueError('purpose_test_project_unavailable')
    configuration = asr_test_configuration(sample.root, container)
    if is_private_project(sample.records, sample.project_id):
        raise ValueError('purpose_test_remote_disabled')
    if not configuration['enabled']:
        raise ValueError('purpose_test_asr_disabled')
    if not configuration['available']:
        raise ValueError(configuration['reason'])
    if not configuration['has_api_key']:
        raise ValueError('purpose_test_asr_key_unavailable')
    if not configuration['consented']:
        raise ValueError('purpose_test_asr_consent_required')
    return configuration


def run_asr_test(sample, container, *, validate):
    def current():
        return validate_asr_test(sample, container, validate=validate)
    current()
    objects, storage = build_rebuild_object_store(sample.root)
    job_id = 'model-check-' + sample.command_id
    source_id, asset_id = 'model-sample-' + sample.command_id, 'sample-audio-' + sample.command_id
    binding_id = freeze_workbench_asr_binding(runtime_root=sample.root, object_store=objects,
        job_id=job_id, source_id=source_id, project_id=sample.project_id,
        secret_store=container.secret_store, registered_sample=sample)
    with wave.open(BytesIO(sample.raw)) as audio:
        duration = audio.getnframes() / audio.getframerate()
    objects.write('audio_asset_refs', asset_id, {'schema_version': '1.0.0', 'id': asset_id,
        'source_id': source_id, 'audio_asset_ref': f'crp-ref://{storage.namespace_id}/assets/{asset_id}',
        'path': str(sample.path), 'media_type': 'audio/wav', 'source_type': 'audio', 'status': 'available',
        'duration_seconds': duration}, expected_revision=0)
    execution_ref = 'facts:model-purpose-test/' + sample.command_id
    current()
    with sample.records.begin() as tx:
        row = tx.read(CHECKS, sample.command_id)
        tx.put(CHECKS, sample.command_id, {**row.payload, 'asr': {'source_id': source_id,
            'asset_id': asset_id, 'binding_id': binding_id, 'execution_ref': execution_ref}}, expected_revision=row.revision)
        tx.commit()
    provider = TokenHubAudioAssetTranscriber(sample.root, objects, namespace_id=storage.namespace_id,
        secret_store=container.secret_store, validate_wire=current, now=_now(), registered_sample=sample)
    try:
        provider.execute(audio_asset_id=asset_id, execution_ref=execution_ref, binding_id=binding_id)
    except Exception:
        result = read_asr_test(sample.records, sample.root, sample.command_id)
        if result is not None:
            return result
        raise
    result = read_asr_test(sample.records, sample.root, sample.command_id)
    if result is None or result['status'] != 'complete':
        raise ValueError('purpose_test_asr_output_unavailable')
    return result


def read_asr_test(records, root, identity):
    row = records.read(CHECKS, identity)
    if row is None or row.payload.get('purpose') != 'asr' or not isinstance(row.payload.get('asr'), dict):
        return None
    proof, binding = row.payload.get('input_proof'), row.payload['asr']
    objects, _storage = build_rebuild_object_store(root)
    frozen = objects.read('workbench_asr_bindings', binding['binding_id'])
    if (not isinstance(proof, dict) or proof.get('command_id') != identity
            or frozen is None or frozen.get('registered_sample') != proof or frozen.get('project_id') != row.payload['project_id']):
        raise ValueError('purpose_test_asr_proof_unavailable')
    matches = [dispatch for dispatch in objects.list('cloud_asr_dispatches') if TokenHubAudioAssetTranscriber._is_dispatch_for_attempt(
        dispatch, dispatch_id=dispatch.get('id'), audio_asset_id=binding['asset_id'], source_id=binding['source_id'],
        binding_id=binding['binding_id'], execution_ref=binding['execution_ref'])]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError('purpose_test_asr_proof_unavailable')
    dispatch = matches[0]
    result = {'status': 'unknown', 'receipt_kind': 'asr_dispatch', 'receipt_id': dispatch['id'],
        'usage': usage_view(None), 'cost': cost_totals([None]), 'attempt_count': None}
    if dispatch.get('status') == 'completed':
        output = objects.read('media_processing_outputs', dispatch.get('output_id'))
        if not TokenHubAudioAssetTranscriber._is_completed_output_for_dispatch(output, dispatch_id=dispatch['id'],
                output_id=dispatch.get('output_id'), audio_asset_id=binding['asset_id'], source_id=binding['source_id'],
                binding_id=binding['binding_id'], execution_ref=binding['execution_ref'], accept_legacy_binding=False):
            raise ValueError('purpose_test_asr_proof_unavailable')
        result.update(status='complete', text=redact_secrets(output['text']), attempt_count=1,
            reported_total_tokens=number(output['metadata'].get('usage_total_token')))
    elif dispatch.get('status') == 'failed':
        code = dispatch.get('error_code')
        error = code if isinstance(code, str) and re.fullmatch(r'(?:server_model|server_asr)_[a-z0-9_]{1,100}', code) else 'purpose_test_asr_request_failed'
        result.update(status='failed', error=error,
            attempt_count=0 if dispatch.get('transmission') == 'confirmed_none' else None)
    for key in records.list('v2_memory_turn_keys'):
        target = key.payload['identity']
        if (target['kind'] == 'memory.link_suggest' and target['project'] == row.payload['project_id']
                and target.get('purpose') == 'asr' and target['key'] == {
                    'namespace': 'shared-asr-wire-v1', 'dispatch_id': dispatch['id'], 'asset_id': binding['asset_id']}):
            result['turn_id'] = key.object_id
            break
    return result


def dispose_asr_test(sample):
    if type(sample) is not RegisteredPurposeSample or sample.purpose != 'asr':
        raise ValueError('purpose_test_sample_invalid')
    objects, _ = build_rebuild_object_store(sample.root)
    asset_id = 'sample-audio-' + sample.command_id
    with objects.locked('audio_asset_refs', asset_id):
        asset = objects.read('audio_asset_refs', asset_id)
        if asset is None:
            return
        if (asset.get('id') != asset_id or asset.get('source_id') != 'model-sample-' + sample.command_id
                or asset.get('media_type') != 'audio/wav' or Path(asset.get('path', '')).resolve() != sample.path.resolve()):
            raise ValueError('purpose_test_sample_changed')
        objects.delete('audio_asset_refs', asset_id)
        if objects.read('audio_asset_refs', asset_id) is not None:
            raise ValueError('purpose_test_cleanup_unavailable')
