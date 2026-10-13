"""校对原文不改写；模型替身只替换传输，SQLite 和 Turn 保持真实实现。"""
import json
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from backend.memory_app.v2.policies import get
from tests.memory_app.governed_model_fixture import GovernedModel


class CorrectionTransport(GovernedModel):
    def __init__(self):
        self.calls = []
        self.invalid = False
        self.before_return = None
        self.network_failure = False
        self.revision = 1
        self.remote = False

    def public(self):
        result = super().public()
        result['generation']['revision'] = self.revision
        if self.remote:
            result['generation'].update(base_url='https://model.example.test/v1', allow_remote=True, enabled=True)
        return result

    def generation_budget_limits(self, *, expected_revision, max_tokens=None):
        assert expected_revision == self.public()['generation']['revision']
        return {'window': 200000, 'reserve': 7000}

    def complete(self, messages, *, max_tokens, validate_current=None, **options):
        validate_current()
        self.calls.append(deepcopy(messages))
        if '录音转写术语校对员' in messages[0]['content']:
            if self.network_failure:
                raise RuntimeError('synthetic transport unavailable')
            value = {'edits': [{'quote': 'Incre mind', 'term': 'Incremind',
                'unambiguous': not self.invalid, 'reason': '产品名'}], 'uncertainties': ['同音人名保留原文。']}
        else:
            if 'reading_view' in messages[-1]['content']:
                values = json.loads(messages[-1]['content'])
                assert values['raw'] == 'I use Incre mind for notes.'
                assert values['reading_view'] == 'I use Incremind for notes.'
            value = {'title': '录音', 'summary': 'Incremind notes', 'topics': [],
                'facts': [{'text': 'I use Incremind for notes.', 'evidence': {'quote': 'I use Incre mind for notes.'}}],
                'todos': [], 'uncertainties': [], 'people': [], 'dates': [], 'suggestions': []}
        if self.before_return:
            self.before_return()
        return json.dumps(value, ensure_ascii=False), {'model': 'fake', 'usage': {'input_tokens': 7, 'output_tokens': 3}}


@pytest.fixture
def correction_env(tmp_path, monkeypatch):
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(tmp_path))
    from fastapi import FastAPI
    from backend.memory_app.workspace import install_workspace_routes
    from backend.memory_app.workspace_audio import _audio_original_identity
    from backend.api.rebuild_storage_runtime import build_rebuild_object_store
    from backend.recognition import RecognitionService
    from core.document_engine import SQLiteDocumentRepository
    from core.storage_provider import SQLiteStructuredRecordStore
    from core.product_core.asr_lexicon_service import AsrLexiconService
    root = tmp_path
    records = SQLiteStructuredRecordStore(root / '.rebuild-data' / 'records.sqlite3')
    documents = SQLiteDocumentRepository(records, namespace_id='default')
    model = CorrectionTransport()
    domains = install_workspace_routes(FastAPI(), runtime_root=root, records=records,
        models=model, documents=documents, service=RecognitionService(records))
    raw = 'I use Incre mind for notes.'
    created = asyncio.run(domains.intake.add_text({'text': raw, 'project_id': 'alpha', 'preserve_text': True}))
    original = root / 'retained.ogg'
    original.write_bytes(b'original-audio-fixture')
    store, storage = build_rebuild_object_store(root)
    run, source, asset, output = 'asr-run', 'source-asr-run', 'audio-asset-asr-run', 'asr-output'
    binding_id = f'workbench-asr-{run}-{source}'
    store.write('workbench_asr_bindings', binding_id, {'project_id': 'alpha', 'source_id': source,
        'remote_processing': True, 'provider': 'tokenhub-asr'}, expected_revision=0)
    store.write('audio_asset_refs', asset, {'source_id': source}, expected_revision=0)
    output_ref = 'crp://default/media-processing-outputs/asr-output'
    store.write('media_processing_outputs', output, {'id': output, 'ref': output_ref, 'source_id': source,
        'source_type': 'audio', 'output_kind': 'transcript', 'status': 'completed',
        'provider': 'tokenhub-asr', 'text': raw, 'metadata': {'audio_asset_id': asset}}, expected_revision=0)
    checkpoint = {'schema_version': 1, 'project_id': 'alpha', 'item_id': created['id'], 'run_id': run,
        'store_kind': 'rebuild', 'namespace_id': storage.namespace_id, 'binding_id': binding_id,
        'source_id': source, 'audio_asset_id': asset, 'output_id': output, 'output_ref': output_ref,
        'provider': 'tokenhub-asr', 'original_identity': _audio_original_identity(original), 'text': raw}
    with records.begin() as tx:
        item = tx.read('workspace_items', created['id'])
        tx.put('workspace_items', created['id'], {**item.payload, 'input_kind': 'audio', 'source_text': raw,
            'original_path': str(original), 'original_name': 'retained.ogg', 'audio_transcription': checkpoint},
            expected_revision=item.revision)
        tx.commit()
    lexicon = AsrLexiconService(records, runtime_root=root)
    lexicon.import_text(text='Incremind', expected_revision=0, preview=False)
    return SimpleNamespace(root=root, records=records, model=model, domains=domains, raw=raw,
        created=created, checkpoint=checkpoint, lexicon=lexicon, documents=documents)


def process(env):
    return asyncio.run(env.domains.intake.process(env.created['id'], {'project_id': 'alpha'}))


def episode(env):
    from backend.memory_app.v2.transcript_correction import EPISODES, CURRENT
    pointer = env.records.read(CURRENT, env.created['id']).payload
    return env.records.read(EPISODES, pointer['episode_id'])


def test_default_intake_uses_corrected_reading_but_keeps_raw_source_and_real_receipts(correction_env):
    env = correction_env
    ready = process(env)
    assert ready['status'] == 'ready', ready
    saved = env.records.read('workspace_items', ready['id']).payload
    assert saved['source_text'] == env.raw
    assert saved['audio_transcription'] == env.checkpoint
    assert saved['draft']['facts'][0]['text'] == 'I use Incremind for notes.'
    assert saved['draft']['facts'][0]['evidence']['quote'] == env.raw
    assert saved['model_usage'] == {'input_tokens': 14, 'output_tokens': 6}
    corrected = episode(env).payload
    assert corrected['edited_text'] == 'I use Incremind for notes.'
    assert corrected['call_count'] == 1
    assert corrected['uncertainties'] == ['同音人名保留原文。']
    assert len(env.model.calls) == 2
    step_ref = corrected['stage_refs'][0]
    from core.ai_kernel.sqlite_store import SQLiteAITurnStore
    turns = SQLiteAITurnStore(env.root / '.rebuild-data' / 'ai-turns.sqlite3')
    assert turns.get_request(step_ref['turn_id'])['desired_outcome'] == 'memory.organize'
    assert turns.events_after(step_ref['turn_id'])[-1]['type'] == 'turn.completed'


def test_invalid_model_edit_falls_back_atomically_and_retains_paid_receipt(correction_env):
    env = correction_env
    env.model.invalid = True
    ready = process(env)
    assert ready['status'] == 'ready', ready
    corrected = episode(env).payload
    assert corrected['state'] == 'raw_fallback'
    assert corrected['validated_edits'] == []
    assert corrected['edited_text'] == env.raw
    assert corrected['metadata']['usage'] == {'input_tokens': 7, 'output_tokens': 3}
    assert env.records.read('workspace_items', ready['id']).payload['model_usage'] == {'input_tokens': 14, 'output_tokens': 6}


def test_confirm_freezes_corrected_markdown_and_raw_source_after_later_vocabulary_change(correction_env):
    env = correction_env
    ready = process(env)
    assert ready['status'] == 'ready'
    markdown = env.domains.review.image_markdown(ready['id'], 'alpha', ready['draft'])
    assert '## 校对转写' in markdown and 'I use Incremind for notes.' in markdown
    assert '保留的疑点' in markdown
    confirmed = asyncio.run(env.domains.review.confirm(ready['id'], {'project_id': 'alpha',
        'expected_revision': ready['revision']}))
    assert confirmed['status'] == 'confirmed'
    env.lexicon.import_text(text='new vocabulary', expected_revision=env.lexicon.current_revision(), preview=False)
    assert env.domains.review.image_markdown(ready['id'], 'alpha', confirmed['draft']) == markdown
    assert env.documents.markdown(confirmed['document_id']) == markdown
    source = env.domains.confirmations.source_store.read('sources', confirmed['source_id'])
    assert source['metadata']['content_snapshot'] == env.raw
    assert any(ref.get('quote') == 'Incre mind' for ref in env.documents.read(confirmed['document_id'])['source_refs'])


def test_unconfirmed_vocabulary_change_rejects_old_correction_publication(correction_env):
    env = correction_env
    ready = process(env)
    env.lexicon.import_text(text='another term', expected_revision=env.lexicon.current_revision(), preview=False)
    from fastapi import HTTPException
    with pytest.raises(HTTPException, match='transcript_correction_binding_changed'):
        asyncio.run(env.domains.review.confirm(ready['id'], {'project_id': 'alpha', 'expected_revision': ready['revision']}))
    from backend.memory_app.v2.transcript_correction import PUBLICATIONS
    assert env.records.read(PUBLICATIONS, 'confirm-' + ready['id']) is None
    assert env.records.read('workspace_items', ready['id']).payload['status'] == 'ready'


@pytest.mark.parametrize('target', ['sidecar', 'step'])
def test_sidecar_edit_tampering_is_rejected_by_actual_model_output_replay(correction_env, target):
    env = correction_env
    ready = process(env)
    from backend.memory_app.v2.transcript_correction import EPISODES
    current = episode(env)
    with env.records.begin() as tx:
        if target == 'sidecar':
            forged = deepcopy(current.payload)
            forged['validated_edits'] = []
            forged['edited_text'] = env.raw
            tx.put(EPISODES, current.object_id, forged, expected_revision=current.revision)
        else:
            from backend.memory_app.v2.organize_turns import STEPS
            step = tx.read(STEPS, current.payload['stage_refs'][0]['key'])
            tx.put(STEPS, step.object_id, {**step.payload,
                'output': json.dumps({'edits': [], 'uncertainties': []})}, expected_revision=step.revision)
        tx.commit()
    with pytest.raises(ValueError, match='transcript_correction_binding_changed'):
        env.domains.review.image_markdown(ready['id'], 'alpha', ready['draft'])


@pytest.mark.parametrize('drift', ['vocabulary', 'configuration', 'original', 'privacy'])
def test_mid_call_drift_refuses_old_edits_and_keeps_real_paid_attempt(correction_env, drift):
    env = correction_env
    if drift == 'privacy':
        env.model.remote = True
    def change():
        env.model.before_return = None
        if drift == 'vocabulary':
            env.lexicon.import_text(text='another term', expected_revision=env.lexicon.current_revision(), preview=False)
        elif drift == 'configuration':
            env.model.revision += 1
        elif drift == 'privacy':
            from backend.memory_app.v2.privacy import set_private_project
            set_private_project(env.records, 'alpha', True, 0)
        else:
            (env.root / 'retained.ogg').write_bytes(b'changed-original')
    env.model.before_return = change
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        process(env)
    assert error.value.status_code == 409
    saved = env.records.read('workspace_items', env.created['id']).payload
    assert saved['status'] == 'failed' and saved['draft'] is None
    assert saved['source_text'] == env.raw
    assert len(env.model.calls) == 1
    correction = episode(env).payload
    assert correction['stage_refs'][0]['attempt_refs']


def test_network_failure_retains_unknown_usage_and_attempt_refs(correction_env):
    env = correction_env
    env.model.network_failure = True
    ready = process(env)
    assert ready['status'] == 'ready', ready
    correction = episode(env).payload
    assert correction['state'] == 'raw_fallback'
    assert correction['usage_state'] == 'partial'
    assert len(correction['unknown_calls']) == 1
    assert correction['stage_refs'][0]['attempt_refs'][0]['receipt_ref']
    assert correction['metadata']['usage'] == {}
    assert env.records.read('workspace_items', ready['id']).payload['model_usage'] == {'input_tokens': 7, 'output_tokens': 3}


def test_empty_lexicon_adds_no_correction_model_call(correction_env):
    env = correction_env
    for term in env.lexicon.list_terms():
        env.lexicon.retire(term.term)
    ready = process(env)
    assert ready['status'] == 'ready'
    assert len(env.model.calls) == 1
    from backend.memory_app.v2.transcript_correction import CURRENT
    assert env.records.read(CURRENT, ready['id']).payload['state'] == 'skipped_empty_lexicon'


def test_reading_view_uses_compact_edits_inside_actual_budget_and_rejects_unfit_raw():
    from backend.memory_app.v2.transcript_correction import reading_messages
    from backend.shared.llm.message_metadata import _estimate_input_tokens
    from backend.memory_app.model_config import ModelConfigurationError
    raw = 'Incre mind ' + ('This is retained raw evidence. ' * 300)
    correction = {'validated_edits': [{'start': 0, 'end': 10, 'quote': 'Incre mind', 'term': 'Incremind'}],
        'budget': [100000, 7000]}
    policy = get('transcript_correction', version='@1')
    full = policy.reading_messages('Organize', raw, correction['validated_edits'])
    compact = policy.compact_reading_messages('Organize', raw, correction['validated_edits'])
    limit = _estimate_input_tokens(compact)
    assert _estimate_input_tokens(full) > limit
    correction['budget'][0] = limit
    result = reading_messages(correction, 'Organize', raw)
    assert json.loads(result[-1]['content'])['term_edits'][0]['term'] == 'Incremind'
    assert json.loads(result[-1]['content'])['raw'] == raw
    assert _estimate_input_tokens(result) <= limit
    correction['budget'][0] = limit - 1
    with pytest.raises(ModelConfigurationError, match='transcript_organization_budget_insufficient'):
        reading_messages(correction, 'Organize', raw)


def test_vocabulary_change_reuses_raw_asr_checkpoint_but_creates_new_correction_episode(correction_env, monkeypatch):
    env = correction_env
    original_complete = env.model.complete
    fail = [True]
    def complete(messages, **options):
        if fail[0] and '录音转写术语校对员' not in messages[0]['content']:
            fail[0] = False
            raise RuntimeError('synthetic organization failure')
        return original_complete(messages, **options)
    env.model.complete = complete
    asr_calls = []
    def no_retranscription(*args, **kwargs):
        asr_calls.append(True)
        raise AssertionError('retained raw ASR must be reused')
    monkeypatch.setattr('backend.memory_app.workspace_audio._transcribe_output', no_retranscription)
    first = process(env)
    assert first['status'] == 'failed'
    first_episode = episode(env).object_id
    env.lexicon.import_text(text='new term', expected_revision=env.lexicon.current_revision(), preview=False)
    asyncio.run(env.domains.items.retry(env.created['id'], {'project_id': 'alpha'}))
    second = process(env)
    assert second['status'] == 'ready', second
    assert episode(env).object_id != first_episode
    assert asr_calls == []
    assert env.records.read('workspace_items', env.created['id']).payload['audio_transcription'] == env.checkpoint
    from backend.memory_app.v2.transcript_correction import EPISODES
    assert len(env.records.list(EPISODES)) == 2


@pytest.mark.parametrize('kind,candidate', [('text', False), ('local-native', False), ('audio', True)])
def test_unsupported_and_candidate_paths_do_not_invoke_correction(correction_env, kind, candidate):
    env = correction_env
    from backend.memory_app.v2.organize_turns import OrganizeTurns
    from backend.memory_app.v2.transcript_correction import TranscriptCorrection, CURRENT
    if kind != 'audio':
        with env.records.begin() as tx:
            row = tx.read('workspace_items', env.created['id'])
            payload = deepcopy(row.payload)
            if kind == 'local-native':
                payload['audio_transcription']['provider'] = 'local-faster-whisper'
            else:
                payload['input_kind'] = 'text'
            tx.put('workspace_items', row.object_id, payload, expected_revision=row.revision)
            tx.commit()
    organize = OrganizeTurns(root=env.root, records=env.records, models=env.model, item_id=env.created['id'],
        project_id='alpha', source=env.raw, validate_current=lambda: None)
    assert TranscriptCorrection(organize, root=env.root).run(candidate_flow=candidate) is None
    assert env.model.calls == []
    if candidate:
        assert env.records.read(CURRENT, env.created['id']).payload['state'] == 'skipped_candidate_pipeline'


def test_long_video_intake_passes_corrected_reading_to_chunks_and_keeps_absolute_raw_quotes(correction_env):
    env = correction_env
    from backend.api.rebuild_storage_runtime import build_rebuild_object_store
    raw = (env.raw + '\n' + ''.join(f'Paragraph {index} retains unique original evidence and discussion.\n'
        for index in range(110))).strip()
    store, _ = build_rebuild_object_store(env.root)
    output = store.read('media_processing_outputs', env.checkpoint['output_id'])
    store.write('media_processing_outputs', env.checkpoint['output_id'], {**output, 'text': raw},
        expected_revision=store.revision('media_processing_outputs', env.checkpoint['output_id']))
    with env.records.begin() as tx:
        row = tx.read('workspace_items', env.created['id'])
        tx.put('workspace_items', row.object_id, {**row.payload, 'input_kind': 'video', 'source_text': raw,
            'audio_transcription': {**env.checkpoint, 'text': raw}}, expected_revision=row.revision)
        tx.commit()
    original = env.model.complete
    chunk_views = []
    def complete(messages, **options):
        if '录音转写术语校对员' in messages[0]['content']:
            return original(messages, **options)
        options['validate_current']()
        env.model.calls.append(deepcopy(messages))
        if '当前仅处理全文第' in messages[0]['content']:
            value = json.loads(messages[-1]['content'])
            chunk_views.append(value)
            quote = value['raw'].split('\n')[0]
            result = {'summary': 'chunk', 'topics': [], 'facts': [{'text': quote, 'evidence': {'quote': quote}}],
                'todos': [], 'uncertainties': []}
        else:
            value = json.loads(messages[-1]['content'])
            result = {'title': 'video', 'summary': 'Incremind video', 'topics': [], 'people': [], 'dates': [],
                'suggestions': [], 'uncertainties': [], 'fact_ids': [value['candidates'][0]['id']], 'todo_ids': []}
        return json.dumps(result), {'model': 'fake', 'usage': {'input_tokens': 7, 'output_tokens': 3}}
    env.model.complete = complete
    ready = process(env)
    assert ready['status'] == 'ready', ready
    assert len(chunk_views) >= 2
    assert chunk_views[0]['reading_view'].startswith('I use Incremind for notes.')
    saved = env.records.read('workspace_items', ready['id']).payload
    assert saved['source_text'] == raw
    assert saved['draft']['facts'][0]['text'] == 'I use Incremind for notes.'
    evidence = saved['draft']['facts'][0]['evidence']
    assert raw[evidence['start']:evidence['end']] == evidence['quote'] == env.raw
    assert saved['model_usage'] == {'input_tokens': len(env.model.calls) * 7, 'output_tokens': len(env.model.calls) * 3}
    assert env.domains.review.image_markdown(ready['id'], 'alpha', ready['draft']).count('## 校对转写') == 1


def test_human_reviewed_draft_preserves_correction_proof_and_confirmed_view(correction_env):
    env = correction_env
    ready = process(env)
    body = deepcopy(ready['draft'])
    body.update(title='人工核对后的录音', project_id='alpha', expected_revision=ready['revision'])
    reviewed = asyncio.run(env.domains.review.save_draft(ready['id'], body))
    assert reviewed['title'] == '人工核对后的录音'
    confirmed = asyncio.run(env.domains.review.confirm(ready['id'], {'project_id': 'alpha',
        'expected_revision': reviewed['revision']}))
    assert confirmed['status'] == 'confirmed'
    assert '## 校对转写' in env.documents.markdown(confirmed['document_id'])


def test_correction_preflight_uses_frozen_aux_capacity_not_primary_window(correction_env):
    env = correction_env
    from backend.memory_app.v2.organize_turns import OrganizeTurns
    from backend.memory_app.v2.transcript_correction import TranscriptCorrection
    selected = SimpleNamespace(public=env.model.public,
        generation_budget_limits=lambda **kwargs: {'window': 500, 'reserve': 200})
    env.model.freeze_auxiliary_binding = lambda: {'test_aux': 'small'}
    env.model.for_auxiliary = lambda binding: selected
    env.model.generation_budget_limits = lambda **kwargs: pytest.fail('primary capacity is not the selected aux capacity')
    organize = OrganizeTurns(root=env.root, records=env.records, models=env.model, item_id=env.created['id'],
        project_id='alpha', source=env.raw, validate_current=lambda: None)
    correction = TranscriptCorrection(organize, root=env.root).run()
    assert correction['state'] == 'raw_fallback'
    assert correction['error_code'] == 'transcript_correction_capacity_unavailable'
    assert correction['call_count'] == 0
    assert env.model.calls == []


def test_inline_confirmation_branch_freezes_correction_before_later_vocabulary_change(correction_env):
    env = correction_env
    ready = process(env)
    env.domains.review.confirmations = None
    confirmed = asyncio.run(env.domains.review.confirm(ready['id'], {'project_id': 'alpha',
        'expected_revision': ready['revision']}))
    assert confirmed['status'] == 'confirmed'
    from backend.memory_app.v2.transcript_correction import PUBLICATIONS
    publication = env.records.read(PUBLICATIONS, 'confirm-' + ready['id']).payload
    assert publication['document_id'] == confirmed['document_id']
    env.lexicon.import_text(text='later term', expected_revision=env.lexicon.current_revision(), preview=False)
    markdown = env.documents.markdown(confirmed['document_id'])
    assert '## 校对转写' in markdown
    assert env.domains.review.image_markdown(ready['id'], 'alpha', confirmed['draft']) == markdown
    assert any(ref.get('quote') == 'Incre mind' for ref in env.documents.read(confirmed['document_id'])['source_refs'])


def test_policy_applies_only_validated_local_terms_and_keeps_raw_coordinates():
    policy = get('transcript_correction', version='@1')
    source = '英克瑞明支持录音。2026年不改日期。'
    edits = policy.parse(json.dumps({'edits': [{'quote': '英克瑞明', 'term': '英克瑞明德',
        'unambiguous': True, 'reason': '根据上下文识别产品名'}], 'uncertainties': []}, ensure_ascii=False),
        source=source, terms=['英克瑞明德'], start=0, end=len(source))
    assert edits == [{'start': 0, 'end': 4, 'quote': '英克瑞明', 'term': '英克瑞明德',
        'reason': '根据上下文识别产品名'}]
    assert policy.apply(source, edits) == '英克瑞明德支持录音。2026年不改日期。'
    assert source == '英克瑞明支持录音。2026年不改日期。'


@pytest.mark.parametrize('quote,term,extra', [
    ('2026', '2027', {}), ('不要', '需要', {}), ('三月', '四月', {}),
    ('英克瑞明', '其他产品', {}), ('英克瑞明', '英克瑞明德', {'unambiguous': False}),
    ('英克瑞明德', '英克瑞明', {}),
])
def test_policy_rejects_protected_ambiguous_or_already_correct_changes(quote, term, extra):
    policy = get('transcript_correction', version='@1')
    source = '2026年三月不要改动英克瑞明德。英克瑞明支持录音。'
    value = {'edits': [{'quote': quote, 'term': term, 'unambiguous': True, 'reason': '判断', **extra}],
        'uncertainties': []}
    with pytest.raises(ValueError, match='transcript_correction_invalid'):
        policy.parse(json.dumps(value, ensure_ascii=False), source=source,
            terms=['英克瑞明德', '英克瑞明', '2027', '需要', '四月'], start=0, end=len(source))


def test_policy_rejects_overlapping_batch_instead_of_partial_application():
    policy = get('transcript_correction', version='@1')
    source = '英克瑞明支持录音。'
    value = {'edits': [{'quote': '英克瑞明', 'term': '英克瑞明德', 'unambiguous': True, 'reason': '产品'},
        {'quote': '克瑞明', 'term': '克瑞明德', 'unambiguous': True, 'reason': '产品'}], 'uncertainties': []}
    with pytest.raises(ValueError, match='transcript_correction_invalid'):
        policy.parse(json.dumps(value, ensure_ascii=False), source=source,
            terms=['英克瑞明德', '克瑞明德'], start=0, end=len(source))


def test_policy_accepts_internal_spaces_for_real_product_term():
    policy = get('transcript_correction', version='@1')
    raw = 'I use Incre mind for notes.'
    value = {'edits': [{'quote': 'Incre mind', 'term': 'Incremind', 'unambiguous': True,
        'reason': 'Product name in the notes context'}], 'uncertainties': []}
    edits = policy.parse(json.dumps(value), source=raw, terms=['Incremind'], start=0, end=len(raw))
    assert policy.apply(raw, edits) == 'I use Incremind for notes.'
