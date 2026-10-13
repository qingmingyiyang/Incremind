"""第三版仅扩充无时间原词事件合同，真实领域链只隔离模型传输。"""
import asyncio
from copy import deepcopy
import json

import pytest

from backend.memory_app.v2.material_analysis import ANALYSES, item_analysis, read_document_analysis_sources, render_analysis
from backend.memory_app.v2.policies import ACTIVE, get, override
from backend.memory_app.v2.policies.material_analysis import parse_temporal
from backend.memory_app.v2.policies.material_analysis_fallback import parse_focused
from backend.memory_app.v2.policies.material_analysis_nullable import parse_nullable_temporal
from tests.memory_app.v2.test_material_analysis import (
    runtime, source_env, confirm, Model, PLAN, SOURCE,
    test_actual_material_analysis_extract6_and_comment_consumer_full_chain as material_comment_control,
)


BODY = ('Session date: 2024-03-01\n'
    'Ada completed the report yesterday.\n'
    'Bo described a visit last week.\n'
    'Cy plans a workshop tomorrow.\n'
    'Dee collected field notes.\n'
    'Eli plans a new experiment.')


def event(quote, expression, *, actor='Ada', status='asserted'):
    return {'actors': [actor], 'event_text': quote, 'quote': quote, 'expression': expression,
        'anchor_quote': 'Session date: 2024-03-01' if expression is not None else None,
        'anchor_role': 'record' if expression is not None else 'unknown', 'event_status': status}


def mixed_events():
    return [event('Ada completed the report yesterday.', 'yesterday'),
        event('Bo described a visit last week.', 'last week', actor='Bo'),
        event('Cy plans a workshop tomorrow.', 'tomorrow', actor='Cy', status='planned'),
        event('Dee collected field notes.', None, actor='Dee'),
        event('Eli plans a new experiment.', None, actor='Eli', status='planned')]


def parse(rows, *, source=BODY, offset=0):
    return parse_nullable_temporal(json.dumps({'events': rows}), piece=source, offset=offset,
        original_title='Independent record', full_source=source, source_offset=offset)


def test_nullable_events_preserve_order_state_and_have_no_date_authority(monkeypatch):
    from backend.memory_app.v2.policies import material_analysis as old
    original, calls = old._calculate, []
    def calculate(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)
    monkeypatch.setattr(old, '_calculate', calculate)
    rows = mixed_events()
    result = parse(rows, offset=17)
    expected = parse_temporal(json.dumps({'events': rows[:3]}), piece=BODY, offset=17,
        original_title='Independent record', full_source=BODY, source_offset=17)
    assert result[:3] == expected and len(result) == 5
    assert len(calls) == 6
    assert [row['actors'] for row in result] == [[name] for name in ('Ada', 'Bo', 'Cy', 'Dee', 'Eli')]
    for row, status in zip(result[3:], ('asserted', 'planned'), strict=True):
        assert row['event_status'] == status and row['expression'] is None
        assert row['computed'] is row['record_anchor'] is row['anchor_evidence'] is row['calculation_version'] is None
        assert row['anchor_role'] == 'unknown'
        assert row['event_time'] == {'status': 'unknown', 'precision': 'unknown', 'options': []}
        assert BODY[row['evidence']['start'] - 17:row['evidence']['end'] - 17] == row['evidence']['quote']
    calls.clear()
    assert len(parse(rows[3:])) == 2 and calls == []


@pytest.mark.parametrize('change', [
    {'expression': ''}, {'expression': 0}, {'expression': True}, {'expression': []},
    {'anchor_quote': 'Session date: 2024-03-01'}, {'anchor_role': 'record'},
    {'anchor_role': 'local_explicit'}, {'quote': 'Invented event.'}, {'event_status': 'completed'},
    {'event_status': []}, {'event_status': {}}, {'event_status': None},
    {'actors': [None]}, {'event_text': ''},
])
def test_nullable_invalid_fields_do_not_silently_drop_events(change):
    rows = mixed_events()
    rows[3].update(change)
    with pytest.raises(ValueError):
        parse(rows)


def test_nullable_quote_uniqueness_and_full_source_binding_remain_strict():
    with pytest.raises(ValueError, match='quote_invalid'):
        parse([mixed_events()[3]], source=BODY + '\nDee collected field notes.')
    with pytest.raises(ValueError, match='binding_changed'):
        parse_nullable_temporal(json.dumps({'events': mixed_events()[3:]}), piece=BODY, offset=0,
            original_title='Independent record', full_source='Changed material', source_offset=0)


@pytest.mark.parametrize('quote', [
    'Dee collected field notes yesterday.', 'Dee plans a visit tomorrow.', 'Dee下周计划出行。',
    'On 2024-03-01, Dee collected field notes.', 'Dee collected field notes on March 1, 2024.',
    '2024-03-01 Dee collected field notes.', 'Dee于2024年3月1日收集了笔记。',
])
def test_null_rejects_recognized_direct_time_omissions(quote):
    with pytest.raises(ValueError, match='material_analysis_time_expression_omitted'):
        parse([event(quote, None, actor='Dee')], source=quote)


@pytest.mark.parametrize('source,quote', [
    ('Session date: 2024-03-01\nDee collected field notes.',
        'Session date: 2024-03-01\nDee collected field notes.'),
    ('He wrote: "Dee visited yesterday."\nSol collected field notes.', 'Dee visited yesterday.'),
    ('He wrote: "On 2024-03-01, Dee visited."', 'On 2024-03-01, Dee visited.'),
    ('Dee consulted 2024-03-01 notes and drafted a plan.', 'Dee consulted 2024-03-01 notes and drafted a plan.'),
    ('Dee relied on March 1, 2024 records to draft a plan.', 'Dee relied on March 1, 2024 records to draft a plan.'),
    ("Dee consulted yesterday's notes.", "Dee consulted yesterday's notes."),
    ('Dee参考2024年3月1日的资料整理笔记。', 'Dee参考2024年3月1日的资料整理笔记。'),
    ('Dee收集昨日的笔记。', 'Dee收集昨日的笔记。'),
    ('Dee plans a visit at an unspecified future time.', 'Dee plans a visit at an unspecified future time.'),
])
def test_null_keeps_record_metadata_quoted_history_and_unresolved_context_unknown(source, quote):
    value = parse([event(quote, None, actor='Dee', status='planned')], source=source, offset=23)[0]
    assert value['event_time']['status'] == 'unknown' and value['event_status'] == 'planned'
    assert value['record_anchor'] is value['computed'] is value['calculation_version'] is None


@pytest.mark.parametrize('text', [
    'Mira read an archived report dated 2024-13-01.',
    'Mira checked reference ID 2024-13-01.',
])
def test_null_reference_date_shape_does_not_validate_as_event_date(text):
    value = parse([event(text, None, actor='Mira')], source=text)[0]
    assert value['event_status'] == 'asserted' and value['event_time']['status'] == 'unknown'
    assert value['computed'] is value['record_anchor'] is value['calculation_version'] is None


def test_null_direct_invalid_date_still_fails_calendar_validation():
    text = 'Mira collected field notes on 2024-13-01.'
    with pytest.raises(ValueError, match='invalid_calendar_value'):
        parse([event(text, None, actor='Mira')], source=text)


def test_old_versions_reject_null_and_new_version_changes_only_temporal_contract():
    values = dict(original_title='Independent record', full_text=BODY, body_start=0,
        offset=0, source=BODY, plan=PLAN)
    old, new = get('material_analysis', version='@2'), get('material_analysis', version='@3')
    assert old.focused('analysis', **values) == new.focused('analysis', **values)
    assert old('plan', original_title='Material', source=BODY) == new('plan', original_title='Material', source=BODY)
    assert old.instructions('analysis') == new.instructions('analysis')
    values.pop('plan')
    assert old.focused('temporal', **values) != new.focused('temporal', **values)
    assert old.instructions('temporal') != new.instructions('temporal')
    assert '"expression":"时间原词"' in new.instructions('temporal')
    assert '"expression":null' in new.instructions('temporal')
    raw = json.dumps({'events': mixed_events()[3:]})
    with pytest.raises(ValueError, match='material_analysis_invalid'):
        parse_temporal(raw, piece=BODY, offset=0, original_title='Material')
    compressed = json.dumps({'events': mixed_events()[3:], 'read_requests': []})
    options = dict(operation='temporal', piece=BODY, offset=0, plan=PLAN,
        original_title='Material', full_source=BODY, source_offset=0)
    with pytest.raises(ValueError, match='material_analysis_invalid'):
        parse_focused(compressed, **options)
    assert len(new.parse_focused(compressed, **options)['value']) == 2
    assert ACTIVE['material_analysis'] == '@1' and ACTIVE['extract'] == '@5'


class NullableModel(Model):
    def complete(self, messages, **options):
        raw, metadata = super().complete(messages, **options)
        value = json.loads(raw)
        if 'events' in value:
            value['events'].extend([
                event('Mira collected field notes.', None, actor='Mira'),
                event('Sol plans a workshop.', None, actor='Sol', status='planned')])
        return json.dumps(value, ensure_ascii=False), metadata


def test_v3_actual_domain_confirmation_and_readonly_frozen_replay(runtime):
    source = SOURCE + '\nMira collected field notes.\nSol plans a workshop.'
    runtime.created = asyncio.run(runtime.domains.intake.add_text({'project_id': 'alpha', 'text': source,
        'title': 'Independent nullable record', 'preserve_text': True}))
    runtime.model.__class__ = NullableModel
    with override(material_analysis='@3'):
        result = confirm(runtime)
    assert result['status'] == 'confirmed', result
    row = runtime.records.read('workspace_items', result['id'])
    payload = runtime.records.read(ANALYSES, result['id']).payload
    assert payload['policy_versions'] == {'organize': '@2', 'material_analysis': '@3', 'context_preparation': '@1'}
    assert len(runtime.model.calls) == payload['call_limit'] == 3
    assert len(payload['analysis']['temporal']) == 3
    assert [event['event_status'] for event in payload['analysis']['temporal'][1:]] == ['asserted', 'planned']
    rendered = render_analysis(payload['analysis'])
    assert rendered.count('时间原词：未注明') == 2 and '时间原词：None' not in rendered
    with override(material_analysis='@2'):
        assert item_analysis(runtime.records, row, row.payload['draft']) == payload['analysis']
        sources = read_document_analysis_sources(runtime.records, runtime.documents, 'alpha', result['document_id'])
    assert len(sources['analysis_generation_refs']) == 3
    assert sources['body_sources'][0]['text'] == source and len(runtime.model.calls) == 3
    assert runtime.records.list('recognitions') == ()
    stage_id = next(key for key in payload['stage_keys'] if key.endswith('material-temporal-1'))
    saved = runtime.records.read('workspace_organize_steps', stage_id)
    damaged = deepcopy(saved.payload)
    output = json.loads(damaged['output'])
    output['events'][-1]['anchor_role'] = 'record'
    damaged['output'] = json.dumps(output)
    with runtime.records.begin() as tx:
        tx.put('workspace_organize_steps', stage_id, damaged, expected_revision=saved.revision)
        tx.commit()
    with pytest.raises(ValueError):
        read_document_analysis_sources(runtime.records, runtime.documents, 'alpha', result['document_id'])
    assert len(runtime.model.calls) == 3


def test_v2_actual_publication_remains_readable_with_v3_selected(runtime):
    with override(material_analysis='@2'):
        result = confirm(runtime)
    assert result['status'] == 'confirmed', result
    row = runtime.records.read('workspace_items', result['id'])
    payload = runtime.records.read(ANALYSES, result['id']).payload
    assert payload['policy_versions']['material_analysis'] == '@2'
    calls = deepcopy(runtime.model.calls)
    with override(material_analysis='@3'):
        assert item_analysis(runtime.records, row, row.payload['draft']) == payload['analysis']
        sources = read_document_analysis_sources(runtime.records, runtime.documents, 'alpha', result['document_id'])
    assert len(sources['analysis_generation_refs']) == 3 and runtime.model.calls == calls


def test_v3_compressed_temporal_path_and_frozen_replay():
    from backend.memory_app.v2.material_analysis_fallback import _execute
    from tests.memory_app.v2.test_material_analysis_fallback import SmallWindowModel, long_source
    source = long_source() + '\n\nDee collected field notes.'
    class CompressedNullableModel(SmallWindowModel):
        def complete(self, messages, **options):
            raw, metadata = super().complete(messages, **options)
            values, output = json.loads(messages[-1]['content']), json.loads(raw)
            if 'events' in output and 'Dee collected field notes.' in values['focus']['text']:
                output['events'].append(event('Dee collected field notes.', None, actor='Dee'))
            return json.dumps(output, ensure_ascii=False), metadata
    model = CompressedNullableModel()
    episode = {'item_id': 'nullable', 'project_id': 'alpha', 'owner_revision': 1,
        'policy_versions': {'organize': '@2', 'material_analysis': '@3', 'context_preparation': '@1'},
        'original_title': 'Independent record', 'body_range': {'start': 0, 'end': len(source)},
        'input_limit': 4500, 'output_limit': 4500}
    saved, schedules = {}, {}
    def complete(key, messages, parser):
        raw, _ = model.complete(messages, max_tokens=4500, validate_current=lambda: None)
        saved[key] = (deepcopy(messages), raw)
        return parser(raw)
    def run(adapter, freeze):
        return _execute(episode, source_text=source, complete=adapter, freeze=freeze,
            validate=lambda: None, read_original=lambda ref: source, workers=1)
    result = run(complete, lambda key, value: schedules.update({key: deepcopy(value)}))
    assert result['schedules']['material']['compressed']
    temporal = result['analysis']['temporal']
    assert {row['computed'] for row in temporal if row['expression'] is not None} == {'2024-02-29', '2024-03-03'}
    nulls = [row for row in temporal if row['expression'] is None]
    assert len(nulls) == 1 and nulls[0]['actors'] == ['Dee'] and nulls[0]['event_status'] == 'asserted'
    assert nulls[0]['computed'] is nulls[0]['record_anchor'] is nulls[0]['calculation_version'] is None
    assert nulls[0]['event_time']['status'] == 'unknown'
    count = len(model.calls)
    def replay(key, messages, parser):
        expected, raw = saved[key]
        assert messages == expected
        return parser(raw)
    def frozen(key, value):
        assert value == schedules[key]
    assert run(replay, frozen) == result and len(model.calls) == count


@pytest.mark.parametrize('manifest_change', [None, 'source_revoked'])
def test_v3_extract6_pending_and_comment_source_use_verified_publication(source_env, manifest_change):
    with override(material_analysis='@3'):
        material_comment_control(source_env, manifest_change)
    analyses = source_env.records.list(ANALYSES)
    assert len(analyses) == 1 and analyses[0].payload['policy_versions']['material_analysis'] == '@3'


def test_v3_nullable_publication_extract6_keeps_original_claim_evidence(runtime):
    from backend.memory_app.v2.insight_generation import generate_insights
    from backend.recognition import RecognitionService
    class ExtractNullableModel(NullableModel):
        def complete(self, messages, **options):
            values = json.loads(messages[-1]['content'])
            if 'experiences' not in values:
                return super().complete(messages, **options)
            options['validate_current']()
            self.calls.append((deepcopy(messages), options['max_tokens']))
            assert len([row for row in values['material_analysis']['temporal'] if row['expression'] is None]) == 2
            value = {'complete': True, 'supports': [], 'insights': [{'origin': 'body', 'kind': 'new_method',
                'relation': 'new', 'text': '核对项目进度时，应区分计划和已经发生的行动。',
                'conditions': ['核对项目进度时'], 'target_id': None, 'scope_hint': None,
                'claim_ids': ['claim-1', 'claim-2']}]}
            return json.dumps(value, ensure_ascii=False), {'model': 'fake', 'configuration_revision': 1, 'usage': {}}
    source = SOURCE + '\nMira collected field notes.\nSol plans a workshop.'
    runtime.created = asyncio.run(runtime.domains.intake.add_text({'project_id': 'alpha', 'text': source,
        'title': 'Independent nullable record', 'preserve_text': True}))
    runtime.model.__class__ = ExtractNullableModel
    service = RecognitionService(runtime.records)
    with override(material_analysis='@3', extract='@6'):
        confirmed = confirm(runtime)
        assert confirmed['status'] == 'confirmed', confirmed
        proposals = generate_insights(runtime.model, service, runtime.documents, 'alpha', confirmed['document_id'])
        assert len(proposals) == 1 and proposals[0]['state'] == 'pending'
        assert generate_insights(runtime.model, service, runtime.documents, 'alpha', confirmed['document_id']) == proposals
    assert len(runtime.model.calls) == 4 and runtime.records.list('recognitions') == ()
