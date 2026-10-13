"""第四版只分离相邻完整对话正文的精确拼接，领域链仅隔离模型传输。"""
import asyncio
from copy import deepcopy
import json

import pytest

from backend.memory_app.v2.material_analysis import ANALYSES, item_analysis, read_document_analysis_sources
from backend.memory_app.v2.policies import ACTIVE, get, override
from backend.memory_app.v2.policies.material_analysis import parse_claims
from backend.memory_app.v2.policies.material_analysis_dialogue import parse_dialogue_claims
from tests.memory_app.v2.test_material_analysis import runtime, confirm, PLAN, SOURCE, claim
from tests.memory_app.v2.test_material_analysis_nullable import NullableModel


FIRST = 'Check prerequisites before action.'
SECOND = 'Retain exceptions during review.'
DIALOGUE = '[ref:11] Alex: ' + FIRST + '\n[other-22] Bea: ' + SECOND
JOINED = FIRST + ' ' + SECOND


def output(quote=JOINED):
    row = claim('核对前提与例外。', quote)
    row['actors'] = ['Alex', 'Bea']
    return {'claims': [row]}


def parse(source=DIALOGUE, *, quote=JOINED, offset=31, full_source=None, source_offset=31):
    return parse_dialogue_claims(json.dumps(output(quote), ensure_ascii=False), piece=source,
        offset=offset, plan=PLAN, full_source=full_source, source_offset=source_offset)


def test_adjacent_complete_dialogue_bodies_split_to_unique_real_spans():
    rows = parse()
    original = output()['claims'][0]
    assert {key: value for key, value in rows[0].items() if key != 'evidence'} == {
        key: value for key, value in original.items() if key != 'evidence'}
    assert [proof['quote'] for proof in rows[0]['evidence']] == [FIRST, SECOND]
    for proof in rows[0]['evidence']:
        assert DIALOGUE[proof['start'] - 31:proof['end'] - 31] == proof['quote']
        line_start = DIALOGUE.rfind('\n', 0, proof['start'] - 31) + 1
        assert ': ' in DIALOGUE[line_start:proof['start'] - 31]
    assert len(parse(DIALOGUE.replace('\n', '\r\n'))[0]['evidence']) == 2


@pytest.mark.parametrize('source,quote', [
    (DIALOGUE.replace('\n', '\n\n'), JOINED),
    (DIALOGUE.replace('\n', '\nSession date: 2024-03-01\n'), JOINED),
    (DIALOGUE.replace('\n', '\nOrdinary explanation.\n'), JOINED),
    (DIALOGUE.replace('\n', '\n[ref:12] Cy: Intervening statement.\n'), JOINED),
    (DIALOGUE, 'prerequisites before action. ' + SECOND),
    (DIALOGUE, FIRST + ' Retain exceptions'),
    (DIALOGUE, FIRST + '  ' + SECOND),
    (DIALOGUE.replace('prerequisites before', 'prerequisites  before'), JOINED),
    (DIALOGUE, 'Verify prerequisites before action. ' + SECOND),
    (DIALOGUE, FIRST + ' ... ' + SECOND),
    (DIALOGUE + '\n[ref:33] Cy: ' + FIRST, JOINED),
    (DIALOGUE + '\n' + DIALOGUE, JOINED),
    (FIRST + '\n' + SECOND, JOINED),
    (DIALOGUE.replace('[ref:11] Alex:', '[ref:11]    :'), JOINED),
    (DIALOGUE.replace('[ref:11]', '[ ]'), JOINED),
    ('[ref:11] Alex: Earlier conditions. ' + FIRST + '\n[ref:12] Cy: Intervening statement.\n'
        + '[other-22] Bea: ' + SECOND, JOINED),
])
def test_dialogue_split_rejects_gaps_partial_units_normalization_and_ambiguity(source, quote):
    with pytest.raises(ValueError, match='material_analysis_quote_invalid'):
        parse(source, quote=quote)


@pytest.mark.parametrize('partial', ['missing_header', 'missing_tail', 'mid_line_header'])
def test_dialogue_split_requires_complete_original_units_inside_focus(partial):
    if partial == 'missing_header':
        source = DIALOGUE[DIALOGUE.index(FIRST):]
        offset = 31 + DIALOGUE.index(FIRST)
        full = DIALOGUE
    elif partial == 'missing_tail':
        source, offset, full = DIALOGUE, 31, DIALOGUE + ' Additional words.'
    else:
        source, offset, full = DIALOGUE, 38, 'Prefix ' + DIALOGUE
    with pytest.raises(ValueError, match='material_analysis_quote_invalid'):
        parse(source, offset=offset, full_source=full)


def test_exact_quote_fast_path_and_old_versions_stay_unchanged():
    raw = json.dumps(output(FIRST))
    assert parse_dialogue_claims(raw, piece=DIALOGUE, offset=31, plan=PLAN) == parse_claims(
        raw, piece=DIALOGUE, offset=31, plan=PLAN)
    assert len(parse(FIRST, quote=FIRST)[0]['evidence']) == 1
    with pytest.raises(ValueError, match='material_analysis_quote_invalid'):
        parse_claims(json.dumps(output()), piece=DIALOGUE, offset=31, plan=PLAN)
    old, new = get('material_analysis', version='@3'), get('material_analysis', version='@4')
    for operation in ('plan', 'temporal'):
        assert old.instructions(operation) == new.instructions(operation)
    assert old.instructions('analysis') != new.instructions('analysis')
    options = dict(operation='analysis', piece=DIALOGUE, offset=31, plan=PLAN,
        original_title='Independent dialogue', full_source=DIALOGUE, source_offset=31)
    raw = json.dumps({**output(), 'read_requests': []})
    with pytest.raises(ValueError, match='material_analysis_quote_invalid'):
        old.parse_focused(raw, **options)
    assert len(new.parse_focused(raw, **options)['value'][0]['evidence']) == 2
    assert ACTIVE['material_analysis'] == '@1' and ACTIVE['extract'] == '@5'


def test_discontinuous_evidence_is_accepted_as_separate_exact_quotes():
    source = ('[ref:11] Alex: Earlier conditions. ' + FIRST
        + '\n[ref:12] Cy: Intervening statement.\n[other-22] Bea: ' + SECOND)
    value = output()
    value['claims'][0]['evidence'] = [{'quote': FIRST}, {'quote': SECOND}]
    raw = json.dumps(value)
    assert parse_dialogue_claims(raw, piece=source, offset=31, plan=PLAN) == parse_claims(
        raw, piece=source, offset=31, plan=PLAN)


class DialogueModel(NullableModel):
    def complete(self, messages, **options):
        values = json.loads(messages[-1]['content'])
        if 'experiences' in values:
            options['validate_current']()
            self.calls.append((deepcopy(messages), options['max_tokens']))
            row = values['material_analysis']['claims'][-1]
            assert [proof['quote'] for proof in row['evidence']] == [FIRST, SECOND]
            value = {'complete': True, 'supports': [], 'insights': [{'origin': 'body', 'kind': 'new_method',
                'relation': 'new', 'text': '判断是否执行时应核对前提与例外。', 'conditions': ['判断是否执行时'],
                'target_id': None, 'scope_hint': None, 'claim_ids': [row['id']]}]}
            return json.dumps(value, ensure_ascii=False), {'model': 'fake', 'configuration_revision': 1, 'usage': {}}
        raw, metadata = super().complete(messages, **options)
        value = json.loads(raw)
        if 'claims' in value:
            value['claims'].extend(output()['claims'])
        return json.dumps(value, ensure_ascii=False), metadata


def test_v4_actual_confirmation_readonly_owner_and_extract6_keep_both_original_spans(runtime):
    from backend.memory_app.v2.insight_generation import generate_insights
    from backend.recognition import RecognitionService
    source = SOURCE + '\nMira collected field notes.\nSol plans a workshop.\n' + DIALOGUE
    runtime.created = asyncio.run(runtime.domains.intake.add_text({'project_id': 'alpha', 'text': source,
        'title': 'Independent dialogue record', 'preserve_text': True}))
    runtime.model.__class__ = DialogueModel
    with override(material_analysis='@4', extract='@6'):
        result = confirm(runtime)
        assert result['status'] == 'confirmed', result
        payload = runtime.records.read(ANALYSES, result['id']).payload
        assert payload['policy_versions']['material_analysis'] == '@4'
        assert payload['call_limit'] == len(runtime.model.calls) == 3
        assert [proof['quote'] for proof in payload['analysis']['claims'][-1]['evidence']] == [FIRST, SECOND]
        assert len([row for row in payload['analysis']['temporal'] if row['expression'] is None]) == 2
        service = RecognitionService(runtime.records)
        proposals = generate_insights(runtime.model, service, runtime.documents, 'alpha', result['document_id'])
        assert len(proposals) == 1 and proposals[0]['state'] == 'pending'
        assert generate_insights(runtime.model, service, runtime.documents, 'alpha', result['document_id']) == proposals
    row = runtime.records.read('workspace_items', result['id'])
    with override(material_analysis='@3'):
        assert item_analysis(runtime.records, row, row.payload['draft']) == payload['analysis']
        sources = read_document_analysis_sources(runtime.records, runtime.documents, 'alpha', result['document_id'])
    assert sources['body_sources'][0]['text'] == source and len(sources['analysis_generation_refs']) == 3
    assert len(runtime.model.calls) == 4 and runtime.records.list('recognitions') == ()
    saved = runtime.records.list('v2_material_candidate_evidence')
    assert len(saved) == 1 and saved[0].payload['claim_ids'] == ['claim-4']
    assert [proof['quote'] for proof in saved[0].payload['evidence']] == [FIRST, SECOND]


@pytest.mark.parametrize('prior_version', ['@2', '@3'])
def test_v4_selection_still_reads_prior_publications(runtime, prior_version):
    with override(material_analysis=prior_version):
        result = confirm(runtime)
    assert result['status'] == 'confirmed', result
    with override(material_analysis='@4'):
        read_document_analysis_sources(runtime.records, runtime.documents, 'alpha', result['document_id'])
    assert len(runtime.model.calls) == 3


def test_v4_compressed_analysis_uses_exact_split_and_readonly_replay():
    from backend.memory_app.v2.material_analysis_fallback import _execute
    from tests.memory_app.v2.test_material_analysis_fallback import SmallWindowModel, long_source
    source = long_source() + '\n\n' + DIALOGUE
    class CompressedDialogueModel(SmallWindowModel):
        def complete(self, messages, **options):
            raw, metadata = super().complete(messages, **options)
            values, value = json.loads(messages[-1]['content']), json.loads(raw)
            if 'claims' in value and DIALOGUE in values['focus']['text']:
                value['claims'].extend(output()['claims'])
            return json.dumps(value, ensure_ascii=False), metadata
    model = CompressedDialogueModel()
    episode = {'item_id': 'dialogue', 'project_id': 'alpha', 'owner_revision': 1,
        'policy_versions': {'organize': '@2', 'material_analysis': '@4', 'context_preparation': '@1'},
        'original_title': 'Independent dialogue', 'body_range': {'start': 0, 'end': len(source)},
        'input_limit': 4500, 'output_limit': 4500}
    saved, schedules = {}, {}
    def complete(key, messages, parser):
        raw, _ = model.complete(messages, max_tokens=4500, validate_current=lambda: None)
        saved[key] = deepcopy(messages), raw
        return parser(raw)
    def run(adapter, freeze):
        return _execute(episode, source_text=source, complete=adapter, freeze=freeze,
            validate=lambda: None, read_original=lambda ref: source, workers=1)
    result = run(complete, lambda key, value: schedules.update({key: deepcopy(value)}))
    assert result['schedules']['material']['compressed']
    split = [row for row in result['analysis']['claims'] if row['actors'] == ['Alex', 'Bea']]
    assert len(split) == 1 and [proof['quote'] for proof in split[0]['evidence']] == [FIRST, SECOND]
    count = len(model.calls)
    def replay(key, messages, parser):
        expected, raw = saved[key]
        assert messages == expected
        return parser(raw)
    def frozen(key, value):
        assert value == schedules[key]
    assert run(replay, frozen) == result and len(model.calls) == count
