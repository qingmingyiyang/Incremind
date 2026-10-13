"""Hy-ASR 校对旁路：原始转写是唯一坐标权威，编辑和模型回执分别保留。"""
from copy import deepcopy
import json
import sqlite3
from pathlib import Path

from core.product_core.asr_lexicon_service import AsrLexiconService

from ..model_config import ModelConfigurationError
from ..turn_routing import CONFIGURATION_FIELDS, _revision
from .context_preparation import prepare_context
from .organize_turns import STEPS
from .policies import get
from .policies.context_preparation import ContextPreparationInput, ContextSpan


EPISODES = 'v2_transcript_correction_episodes'
CURRENT = 'v2_transcript_corrections'
PUBLICATIONS = 'v2_transcript_correction_publications'
_VERSION = '@1'


def _selection(records, root=None):
    if not callable(getattr(records, 'begin', None)):
        from ..transaction_records import TransactionRecords
        records = TransactionRecords(records)
    selection = AsrLexiconService(records, runtime_root=root).select_for_session()
    return {'revision': selection.revision,
        'terms': [{'term': term.term, 'weight': term.weight} for term in selection.terms]}


def _configuration(models):
    freeze = getattr(models, 'freeze_auxiliary_binding', None)
    binding = deepcopy(freeze()) if callable(freeze) else None
    selected = models.for_auxiliary(binding) if binding is not None else models
    def public(owner):
        value = owner.public()['generation']
        return {key: deepcopy(value.get(key)) for key in (*CONFIGURATION_FIELDS, 'subscription_binding')}
    return {'primary': public(models), 'auxiliary': public(selected), 'binding': binding}, selected


def _aggregate(metadata):
    usage = {}
    for meta in metadata:
        for name, value in (meta.get('usage') or {}).items():
            if type(value) is int and value >= 0:
                usage[name] = usage.get(name, 0) + value
    return {'model': metadata[-1].get('model', '') if metadata else '', 'usage': usage}


def merge_metadata(correction, generation):
    result = _aggregate([correction or {}, generation or {}])
    result['model'] = (generation or {}).get('model') or (correction or {}).get('model', '')
    return result


def _execute(binding, *, item_id, budget, complete, validate_current):
    """生成与只读复核共用同一消息构造器，不隐式更换已冻结的提示。"""
    policy = get('transcript_correction', version=_VERSION)
    source = binding['source_text']
    terms = [entry['term'] for entry in binding['lexicon']['terms']]
    input_limit, output_limit = budget
    from backend.shared.llm.message_metadata import _estimate_input_tokens
    if _estimate_input_tokens(policy.messages('', terms)) > input_limit:
        raise ValueError('transcript_correction_capacity_unavailable')
    episode_id = 'correction-' + _revision(binding)
    source_ref = {'item_id': item_id, 'episode_id': episode_id}
    word_ref = {'episode_id': episode_id, 'kind': 'lexicon'}
    span = ContextSpan('transcript', source_ref, 'raw_asr', 0, len(source), source)
    words = json.dumps(terms, ensure_ascii=False)
    word_span = ContextSpan('lexicon', word_ref, 'lexicon', 0, len(words), words)
    request = ContextPreparationInput(task='保守的 ASR 术语局部校对', instructions=policy.instructions,
        original_messages=policy.messages(source, terms), focus=span, fixed=(word_span,),
        surrounding=(span,), input_limit=input_limit, output_limit=output_limit)

    def read_original(ref):
        validate_current()
        if ref == word_ref:
            return words
        if ref == source_ref:
            return source
        raise ValueError('context_preparation_source_invalid')

    prefix = 'asrc-' + episode_id[-16:]
    prepared = prepare_context(request, complete_stage=complete, validate_current=validate_current,
        read_original=read_original, stage_prefix=prefix + '-context')
    edits, uncertainties = [], []
    for number, part in enumerate(prepared.parts, 1):
        def parser(raw, part=part):
            validated = policy.parse(raw, source=source, terms=terms,
                start=part.focus.start, end=part.focus.end)
            return {'edits': validated, 'uncertainties': json.loads(raw)['uncertainties']}
        value = complete(prefix + '-part-' + str(number), part.messages, parser)
        edits.extend(value['edits'])
        uncertainties.extend(value['uncertainties'])
    edits.sort(key=lambda edit: edit['start'])
    return edits, uncertainties, policy.apply(source, edits)


def _safe_failure(error):
    if isinstance(error, ModelConfigurationError):
        if str(error).startswith('model_request_failed') or str(error) in {
                'model_output_incomplete', 'model_output_missing_content', 'model_response_invalid'}:
            return 'correction_model_failed'
    elif (str(error) in {'transcript_correction_invalid', 'transcript_correction_capacity_unavailable'}
            or str(error).startswith('context_preparation_')):
        return str(error)
    raise error


def _turn_database(reader, root):
    if root is not None:
        return Path(root) / '.rebuild-data' / 'ai-turns.sqlite3'
    record_path = Path(reader.database_path)
    directory = record_path.parent / '.rebuild-data' if record_path.name == 'recognition.sqlite3' else record_path.parent
    return directory / 'ai-turns.sqlite3'


def _attempt_refs(connection, turn_id):
    return [{'attempt_id': row[0], 'dispatch_ref': row[1], 'receipt_ref': row[2], 'status': row[3]}
        for row in connection.execute('SELECT attempt_id,dispatch_payload_ref,terminal_receipt_ref,terminal_status '
            'FROM ai_model_attempt_reservations WHERE turn_id=? ORDER BY model_request_id,attempt_number', (turn_id,))]


def _checked(reader, row, payload, *, root=None):
    binding = payload.get('binding', {})
    if (payload.get('item_id') != row.object_id or payload.get('project_id') != row.payload.get('project_id')
            or binding.get('source_text') != row.payload.get('source_text')
            or binding.get('audio_transcription') != row.payload.get('audio_transcription')
            or binding.get('policy') != 'transcript_correction@1'):
        raise ValueError('transcript_correction_binding_changed')
    refs = payload.get('stage_refs', [])
    database = _turn_database(reader, root)
    if refs and not database.is_file():
        raise ValueError('transcript_correction_binding_changed')
    connection = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=5) if refs else None
    checked, metadata, unknown = [], [], []

    def complete(stage, messages, parser):
        key = 'organize-' + row.object_id + '-' + stage
        if len(checked) >= len(refs) or refs[len(checked)]['key'] != key:
            raise ValueError('transcript_correction_binding_changed')
        ref = refs[len(checked)]
        step = reader.read(STEPS, ref['key'])
        if (step is None or step.revision != ref['revision'] or step.payload.get('turn_id') != ref['turn_id']
                or step.payload.get('inputs') != ref['inputs']):
            raise ValueError('transcript_correction_binding_changed')
        expected = {'messages': messages, 'max_tokens': payload['budget'][1], 'project_id': row.payload['project_id'],
            'configuration': {key: binding['configuration']['primary'].get(key) for key in CONFIGURATION_FIELDS}}
        stored = connection.execute('SELECT request_json FROM ai_turns WHERE turn_id=?', (ref['turn_id'],)).fetchone()
        request = json.loads(stored[0]) if stored else None
        events = [json.loads(event[0]) for event in connection.execute(
            'SELECT event_json FROM ai_turn_events WHERE turn_id=? ORDER BY sequence', (ref['turn_id'],))]
        materials = request['privacy']['material_refs'] if request else []
        if (step.payload['inputs'] != expected or request is None
                or request.get('desired_outcome') != 'memory.organize'
                or request.get('input', {}).get('text') != '整理以下原件。\n\n' + json.dumps(messages, ensure_ascii=False)
                or request.get('policy_versions', {}).get('organize', '@1') != '@1'
                or len(materials) != 1 or materials[0].get('type') != 'original_item'
                or materials[0].get('id') != row.object_id or materials[0].get('project_id') != row.payload['project_id']
                or type(materials[0].get('revision')) is not int or not 0 < materials[0]['revision'] <= row.revision
                or not events or events[-1]['type'] not in {'turn.completed', 'turn.failed', 'turn.cancelled'}):
            raise ValueError('transcript_correction_binding_changed')
        if ref.get('attempt_refs') != _attempt_refs(connection, ref['turn_id']):
            raise ValueError('transcript_correction_binding_changed')
        checked.append(ref)
        meta = step.payload.get('metadata', {})
        metadata.append(meta)
        if not meta.get('usage'):
            unknown.append({'stage': key, 'turn_id': ref['turn_id'], 'reason': 'wire_usage_unavailable'})
        if 'output' not in step.payload:
            raise ModelConfigurationError('model_request_failed:organize_result_unavailable')
        if events[-1]['type'] != 'turn.completed' or not any(event['type'] == 'model.completed' for event in events):
            raise ValueError('transcript_correction_binding_changed')
        try:
            result = parser(step.payload['output'])
        except ValueError:
            if step.payload.get('rejected') is not True:
                raise ValueError('transcript_correction_binding_changed') from None
            raise
        if step.payload.get('rejected'):
            raise ValueError('transcript_correction_binding_changed')
        return result

    try:
        if connection:
            connection.execute('BEGIN')
        if payload.get('budget') is None:
            if refs or payload.get('error_code') != 'transcript_correction_capacity_unavailable':
                raise ValueError('transcript_correction_binding_changed')
            edits, uncertainties, edited, state = [], [], binding['source_text'], 'raw_fallback'
        else:
            try:
                edits, uncertainties, edited = _execute(binding, item_id=row.object_id, budget=payload['budget'],
                    complete=complete, validate_current=lambda: None)
                state = 'completed'
            except (ValueError, ModelConfigurationError) as error:
                code = _safe_failure(error)
                if code != payload.get('error_code'):
                    raise ValueError('transcript_correction_binding_changed') from None
                edits, uncertainties, edited, state = [], [], binding['source_text'], 'raw_fallback'
        if (edits != payload.get('validated_edits') or uncertainties != payload.get('uncertainties')
                or edited != payload.get('edited_text') or state != payload.get('state') or checked != refs
                or payload.get('call_count') != len(refs) or payload.get('metadata') != _aggregate(metadata)
                or payload.get('unknown_calls') != unknown
                or payload.get('usage_state') != ('partial' if unknown else 'known')):
            raise ValueError('transcript_correction_binding_changed')
    finally:
        if connection:
            connection.close()
    return deepcopy(payload)


class TranscriptCorrection:
    def __init__(self, organize, *, root):
        self.organize, self.root = organize, root
        self.records = organize.records

    def run(self, *, candidate_flow=False):
        row = self.records.read('workspace_items', self.organize.item_id)
        evidence = row.payload.get('audio_transcription')
        if (row.payload.get('input_kind') not in {'audio', 'video'}
                or not isinstance(evidence, dict) or evidence.get('provider') != 'tokenhub-asr'):
            return None
        lexicon = _selection(self.records, self.root)
        if candidate_flow or not lexicon['terms']:
            self._skip(row, 'skipped_candidate_pipeline' if candidate_flow else 'skipped_empty_lexicon')
            return None
        configuration, selected = _configuration(self.organize.models)
        binding = {'source_text': self.organize.source, 'audio_transcription': deepcopy(evidence),
            'lexicon': lexicon, 'policy': 'transcript_correction@1', 'context_policy': 'context_preparation@1',
            'configuration': configuration}
        episode_id = 'correction-' + _revision(binding)
        self.binding, self.episode_id = binding, episode_id
        self.guard()
        with self.records.begin() as tx:
            self.organize._guard_write(tx)
            saved = tx.read(EPISODES, episode_id)
            if saved is None:
                saved = tx.put(EPISODES, episode_id, {'item_id': self.organize.item_id,
                    'project_id': self.organize.project_id, 'binding': binding, 'state': 'processing',
                    'edited_text': self.organize.source, 'validated_edits': [], 'stage_refs': [],
                    'metadata': {}, 'budget': None, 'draft': None}, expected_revision=0)
            pointer = tx.read(CURRENT, self.organize.item_id)
            tx.put(CURRENT, self.organize.item_id, {'episode_id': episode_id, 'state': saved.payload['state']},
                expected_revision=pointer.revision if pointer else 0)
            tx.commit()
        if saved.payload['state'] != 'processing':
            return _checked(self.records, row, saved.payload, root=self.root)
        self.stage_keys = []
        policy = get('transcript_correction', version=_VERSION)
        source = self.organize.source
        edits, uncertainties, state, error_code = [], [], 'completed', None
        try:
            budget_reader = getattr(selected, 'generation_budget_limits', None)
            if not callable(budget_reader):
                raise ValueError('transcript_correction_capacity_unavailable')
            limits = budget_reader(expected_revision=configuration['auxiliary']['revision'])
            input_limit, output_limit = policy.budget(limits)
            with self.records.begin() as tx:
                self.organize._guard_write(tx)
                current = tx.read(EPISODES, episode_id)
                tx.put(EPISODES, episode_id, {**current.payload, 'budget': [input_limit, output_limit]},
                    expected_revision=current.revision)
                tx.commit()

            def complete(stage, messages, parser):
                self.guard()
                key = 'organize-' + self.organize.item_id + '-' + stage
                self.stage_keys.append(key)
                try:
                    raw, _meta = self.organize.complete(messages, stage=stage, max_tokens=output_limit,
                        validate_current=self.guard, validate_output=parser)
                finally:
                    # 配置改变或网络失败也保留已接受Turn引用，费用未知不伪报为零。
                    refs, metadata, unknown = self._stage_evidence()
                    with self.records.begin() as tx:
                        self.organize._guard_write(tx)
                        current = tx.read(EPISODES, episode_id)
                        tx.put(EPISODES, episode_id, {**current.payload, 'stage_refs': refs,
                            'metadata': _aggregate(metadata), 'unknown_calls': unknown,
                            'usage_state': 'partial' if unknown else 'known'}, expected_revision=current.revision)
                        tx.commit()
                self.guard()
                return parser(raw)
            edits, uncertainties, edited = _execute(binding, item_id=self.organize.item_id,
                budget=[input_limit, output_limit], complete=complete, validate_current=self.guard)
        except (ValueError, ModelConfigurationError) as error:
            error_code = _safe_failure(error)
            edits, uncertainties, edited, state = [], [], source, 'raw_fallback'
        self.guard()
        refs, metadata, unknown = self._stage_evidence()
        with self.records.begin() as tx:
            self.organize._guard_write(tx)
            current = tx.read(EPISODES, episode_id)
            payload = {**current.payload, 'state': state, 'edited_text': edited, 'validated_edits': edits,
                'uncertainties': uncertainties, 'error_code': error_code, 'stage_refs': refs,
                'metadata': _aggregate(metadata), 'call_count': len(refs), 'unknown_calls': unknown,
                'usage_state': 'partial' if unknown else 'known'}
            tx.put(EPISODES, episode_id, payload, expected_revision=current.revision)
            pointer = tx.read(CURRENT, self.organize.item_id)
            if pointer.payload.get('episode_id') != episode_id:
                raise ModelConfigurationError('transcript_correction_binding_changed')
            tx.put(CURRENT, self.organize.item_id, {**pointer.payload, 'state': state}, expected_revision=pointer.revision)
            tx.commit()
        return payload

    def _stage_evidence(self):
        refs, metadata, unknown = [], [], []
        database = self.root / '.rebuild-data' / 'ai-turns.sqlite3'
        for key in dict.fromkeys(getattr(self, 'stage_keys', [])):
            step = self.records.read(STEPS, key)
            if step is None:
                continue
            connection = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
            try:
                attempts = _attempt_refs(connection, step.payload['turn_id'])
            finally:
                connection.close()
            refs.append({'key': key, 'revision': step.revision, 'turn_id': step.payload['turn_id'],
                'inputs': deepcopy(step.payload['inputs']), 'attempt_refs': attempts})
            meta = step.payload.get('metadata', {})
            metadata.append(meta)
            if not meta.get('usage'):
                unknown.append({'stage': key, 'turn_id': step.payload['turn_id'], 'reason': 'wire_usage_unavailable'})
        return refs, metadata, unknown

    def usage_metadata(self):
        return _aggregate(self._stage_evidence()[1])

    def guard(self):
        self.organize.validate_current()
        row = self.records.read('workspace_items', self.organize.item_id)
        from ..workspace_audio import _validated_audio_transcription
        from fastapi import HTTPException
        try:
            evidence = _validated_audio_transcription(row.payload, self.root) if row is not None else None
        except HTTPException:
            raise ModelConfigurationError('audio_transcription_evidence_invalid') from None
        if (row is None or row.payload.get('source_text') != self.binding['source_text']
                or row.payload.get('audio_transcription') != self.binding['audio_transcription']
                or evidence != self.binding['audio_transcription']
                or _selection(self.records, self.root) != self.binding['lexicon']
                or _configuration(self.organize.models)[0] != self.binding['configuration']):
            raise ModelConfigurationError('transcript_correction_binding_changed')

    def _skip(self, row, state):
        with self.records.begin() as tx:
            self.organize._guard_write(tx)
            pointer = tx.read(CURRENT, row.object_id)
            tx.put(CURRENT, row.object_id, {'state': state, 'episode_id': None},
                expected_revision=pointer.revision if pointer else 0)
            tx.commit()


def save_draft(organize, correction, draft, *, root=None):
    if correction is None:
        return
    with organize.records.begin() as tx:
        organize._guard_write(tx)
        pointer = tx.read(CURRENT, organize.item_id)
        current = tx.read(EPISODES, pointer.payload['episode_id'])
        row = tx.read('workspace_items', organize.item_id)
        _checked(tx, row, current.payload, root=root)
        if _selection(tx, root) != current.payload['binding']['lexicon']:
            raise ModelConfigurationError('transcript_correction_binding_changed')
        tx.put(EPISODES, current.object_id, {**current.payload, 'draft': deepcopy(draft)}, expected_revision=current.revision)
        tx.commit()


def reading_messages(correction, system, source, *, start=0, end=None):
    if correction is None or not correction['validated_edits']:
        return [{'role': 'system', 'content': system},
            {'role': 'user', 'content': source[start:len(source) if end is None else end]}]
    end = len(source) if end is None else end
    contained = [edit for edit in correction['validated_edits'] if start <= edit['start'] < edit['end'] <= end]
    policy = get('transcript_correction', version=_VERSION)
    messages = policy.reading_messages(system, source, contained, start=start, end=end)
    from backend.shared.llm.message_metadata import _estimate_input_tokens
    limit = correction['budget'][0]
    if _estimate_input_tokens(messages) > limit:
        messages = policy.compact_reading_messages(system, source, contained, start=start, end=end)
    if _estimate_input_tokens(messages) > limit:
        raise ModelConfigurationError('transcript_organization_budget_insufficient')
    return messages


def ground_reading_draft(draft, source, correction):
    if correction is None or not correction['validated_edits'] or not isinstance(draft, dict):
        return draft
    result = deepcopy(draft)
    for field in ('facts', 'todos'):
        for entry in result.get(field, []):
            evidence = entry.get('evidence', {})
            if entry.get('text') == evidence.get('quote'):
                contained = [edit for edit in correction['validated_edits']
                    if evidence['start'] <= edit['start'] < edit['end'] <= evidence['end']]
                entry['text'] = get('transcript_correction', version=_VERSION).apply(source,
                    contained, start=evidence['start'], end=evidence['end'])
    return result


def item_correction(reader, row, draft, *, root=None, require_current=False):
    if row.payload.get('input_kind') not in {'audio', 'video'}:
        return None
    frozen = reader.read(PUBLICATIONS, 'confirm-' + row.object_id)
    if frozen is not None:
        if frozen.payload.get('draft') != draft:
            raise ValueError('transcript_correction_binding_changed')
        # 人工确认后的词表和模型绑定属于历史证明，后续设置变化不能追溯改写。
        return _checked(reader, row, frozen.payload['correction'], root=root)
    pointer = reader.read(CURRENT, row.object_id)
    if pointer is None or pointer.payload.get('episode_id') is None:
        return None
    current = reader.read(EPISODES, pointer.payload['episode_id'])
    if current is None or current.payload.get('draft') != draft:
        raise ValueError('transcript_correction_binding_changed')
    checked = _checked(reader, row, current.payload, root=root)
    if require_current and _selection(reader, root) != checked['binding']['lexicon']:
        raise ValueError('transcript_correction_binding_changed')
    return checked


def save_reviewed_draft(tx, row, draft, *, root=None):
    current = item_correction(tx, row, row.payload['draft'], root=root, require_current=True)
    if current is None:
        return
    pointer = tx.read(CURRENT, row.object_id)
    episode = tx.read(EPISODES, pointer.payload['episode_id'])
    tx.put(EPISODES, episode.object_id, {**episode.payload, 'draft': deepcopy(draft)}, expected_revision=episode.revision)


def render_correction(correction):
    if correction is None:
        return ''
    text = correction['edited_text']
    fence = '~~~'
    while fence in text:
        fence += '~'
    heading = '校对转写' if correction['state'] == 'completed' else '转写原文（校对未应用）'
    lines = ['\n\n## ' + heading, '', fence, text, fence]
    if correction['validated_edits']:
        lines.extend(['', '术语校对（引用仍回查原始转写）：'])
        for edit in correction['validated_edits']:
            lines.append('- ' + json.dumps(edit['quote'], ensure_ascii=False) + ' → '
                + json.dumps(edit['term'], ensure_ascii=False) + f"（原文 {edit['start']}:{edit['end']}）")
    if correction.get('uncertainties'):
        lines.extend(['', '保留的疑点：', *['- ' + json.dumps(value, ensure_ascii=False)
            for value in correction['uncertainties']]])
    return '\n'.join(lines) + '\n'


def freeze_publication(tx, row, draft, markdown, *, root=None):
    correction = item_correction(tx, row, draft, root=root, require_current=True)
    if correction is None:
        return None
    if render_correction(correction) not in markdown:
        raise ValueError('transcript_correction_publication_missing')
    from ..workspace_audio import _validated_audio_transcription
    if _validated_audio_transcription(row.payload, root) != correction['binding']['audio_transcription']:
        raise ValueError('transcript_correction_binding_changed')
    payload = {'item_id': row.object_id, 'project_id': row.payload['project_id'],
        'draft': deepcopy(draft), 'correction': correction, 'markdown': markdown,
        'document_id': None, 'document_revision': None}
    key = 'confirm-' + row.object_id
    saved = tx.read(PUBLICATIONS, key)
    if saved is not None:
        if saved.payload != payload:
            raise ValueError('transcript_correction_binding_changed')
        return saved
    return tx.put(PUBLICATIONS, key, payload, expected_revision=0)


def correction_source_refs(correction, source_id):
    if correction is None:
        return []
    return [{'source_id': source_id, 'locator': f"text:{edit['start']}:{edit['end']}", 'quote': edit['quote']}
        for edit in correction['validated_edits']]


def commit_publication(tx, row, operation, document, *, root=None):
    frozen = tx.read(PUBLICATIONS, operation['id'])
    if frozen is None:
        return
    if frozen.payload['draft'] != operation['draft'] or frozen.payload['markdown'] != operation['markdown']:
        raise ValueError('transcript_correction_binding_changed')
    _checked(tx, row, frozen.payload['correction'], root=root)
    tx.put(PUBLICATIONS, frozen.object_id, {**frozen.payload, 'document_id': document['id'],
        'document_revision': document['revision']}, expected_revision=frozen.revision)
