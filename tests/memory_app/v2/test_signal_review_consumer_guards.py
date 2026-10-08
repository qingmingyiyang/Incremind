"""Real consumer transactions and governed transports retain confirmed source proof."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from backend.memory_app.model_config import ModelConfiguration
from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.v2.consolidation import Consolidation
from backend.memory_app.v2.policies import override
from backend.memory_app.v2.signal_review_feedback import review_corrections, validate_review_corrections
from backend.recognition import RecognitionConflict, WorkScope
from backend.security.secrets import InMemorySecretStore
from tests.memory_app.test_fast_generation import choose
from tests.memory_app.v2.kernel_receipts import requests
from tests.memory_app.v2.test_signal_review_feedback import prepare_confirmed, replace
from tests.memory_app.v2.test_workbench_ask import env, publish, add_document, ask


@pytest.fixture
def confirmed(env):
    return prepare_confirmed(env)


def configured_job(env, event_id, *, text='Synthetic corrected method', on_wire=None):
    calls = []
    def transport(**request):
        messages = '\n'.join(message['content'] for message in request['messages'])
        overview = '最多300字' in messages
        calls.append({'overview': overview})
        if not overview and on_wire is not None:
            on_wire()
        output = {'text': 'Synthetic overview'} if overview else {
            'text': text if '"event_id"' in messages else 'Synthetic independent grouping',
            'conditions': ['Synthetic condition']}
        if not overview and '"event_id"' in messages:
            output.update(event_ids=list(event_id) if isinstance(event_id, (list, tuple)) else [event_id],
                kind='correction')
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(output)}}],
            'usage': {'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5}}
    models = ModelConfiguration(env.records, env.root, InMemorySecretStore(), completion_fn=transport)
    models.update('generation', {'base_url': 'https://synthetic.invalid/v1', 'model': 'main',
        'api_key': 'synthetic-only', 'allow_remote': True, 'enabled': True, 'expected_revision': 0})
    choose(models, 'quick')
    job = Consolidation(env.records, env.service, env.documents, models,
        now=lambda: datetime.now(timezone.utc) + timedelta(days=1))
    return job, models, calls


def run(job):
    with override(trigger='@2', consolidate='@2'):
        return job.run('alpha')


def state(records):
    return {name: records.list(name) for name in ('recognition_candidates', 'v2_insight_patterns',
        'v2_consolidation_inputs', 'v2_insight_evidence_support')}


def test_sql_consumption_failure_rolls_back_candidate_pattern_and_input(confirmed, caplog):
    env, _, _, _, decision = confirmed
    event_id = 'signal-decision:' + decision.object_id
    before = state(env.records)
    originals = {name: env.records.list(name) for name in ('v2_signal_decisions', 'recognitions',
        'recognition_experiences', 'documents', 'workspace_items', 'workspace_ask_receipts')}
    with sqlite3.connect(env.records.database_path) as connection:
        connection.execute("CREATE TRIGGER synthetic_reject_signal_input BEFORE INSERT ON crp_structured_records "
            "WHEN NEW.collection='v2_consolidation_inputs' BEGIN SELECT RAISE(ABORT, 'synthetic_input_refused'); END")
    job, _, calls = configured_job(env, event_id)
    outcome = run(job)
    assert any(not call['overview'] for call in calls)
    assert 'exception_type=IntegrityError' in caplog.text
    assert outcome['new_suggestions'] == 0 and outcome['failed_groups'] >= 1
    assert state(env.records) == before
    assert {name: env.records.list(name) for name in originals} == originals


@pytest.mark.parametrize('change', ['private', 'revision', 'forgotten'])
def test_wire_source_retraction_prevents_candidate_and_consumption(confirmed, change):
    from backend.memory_app.v2.recall_preferences import set_preference
    env, _, recognition, _, decision = confirmed
    captured = review_corrections(env.records, 'alpha')
    assert len(captured) == 1
    before = state(env.records)
    changed = []
    def retract():
        assert not changed
        changed.append(change)
        scope = WorkScope('local-user', 'alpha')
        if change == 'private':
            SourceEgressService(env.records).set_policy(scope, 'recognition', recognition.id,
                recognition.revision, 0, [])
        elif change == 'revision':
            env.service.revise(scope=scope, recognition_id=recognition.id,
                expected_revision=recognition.revision, content='Synthetic revised during transport')
        else:
            set_preference(env.records, scope, recognition.id, recognition_revision=recognition.revision,
                preference_revision=0, state='forgotten')
    job, _, calls = configured_job(env, 'signal-decision:' + decision.object_id, on_wire=retract)
    outcome = run(job)
    assert changed == [change] and sum(not call['overview'] for call in calls) == 1
    assert outcome['new_suggestions'] == 0
    assert state(env.records) == before
    assert env.records.read('v2_signal_decisions', decision.object_id) == decision
    with pytest.raises(RecognitionConflict, match='review_feedback_changed'):
        validate_review_corrections(env.records, captured, project='alpha')


def test_duplicate_other_target_retains_all_review_parents_in_pending_candidate(confirmed):
    env, _, recognition, _, decision = confirmed
    target, _ = publish(env, text='Synthetic duplicate corrected method')
    target_row = env.records.read('recognitions', target.id)
    captured = review_corrections(env.records, 'alpha')
    assert len(captured) == 1 and captured[0]['_source_recognition_ids'] == [recognition.id]
    job, _, _ = configured_job(env, 'signal-decision:' + decision.object_id, text=target.content)
    outcome = run(job)
    assert outcome['new_suggestions'] == 1, outcome
    patterns = env.records.list('v2_insight_patterns')
    assert len(patterns) == 1
    candidate = env.records.read('recognition_candidates', patterns[0].object_id)
    assert candidate.payload['state'] == 'pending' and candidate.payload['content'] == target.content
    assert set(candidate.payload['source_recognition_ids']) == set(captured[0]['_source_recognition_ids'])
    assert set(captured[0]['_source_experience_ids']) <= set(candidate.payload['source_experience_ids'])
    assert env.records.read('recognitions', target.id) == target_row
    assert env.records.list('v2_insight_evidence_support') == ()
    scope = WorkScope('local-user', 'alpha')
    roots = [{'type': kind, 'id': identity, 'revision': env.records.read(collection, identity).revision}
        for field, kind, collection in (('source_experience_ids', 'experience', 'recognition_experiences'),
            ('source_recognition_ids', 'recognition', 'recognitions'))
        for identity in candidate.payload[field]]
    snapshot = SourceEgressService(env.records).snapshot(scope, roots)
    expected = {(node['type'], node['id'], node['source_revision'])
        for node in captured[0]['_source_graph']['nodes'] if node.get('kind') == 'material'}
    actual = {(node['type'], node['id'], node['source_revision']) for node in snapshot['nodes']}
    assert expected <= actual
    # Explicit user confirmation of this synthetic pending candidate preserves all parents.
    published = env.service.publish(scope=scope, candidate_id=candidate.object_id,
        expected_revision=candidate.revision, reviewer='local-user')
    published_snapshot = SourceEgressService(env.records).snapshot(scope,
        [{'type': 'recognition', 'id': published.id, 'revision': published.revision}])
    published_closure = {(node['type'], node['id'], node['source_revision'])
        for node in published_snapshot['nodes']}
    assert expected <= published_closure
    assert env.records.read('recognitions', target.id) == target_row


def test_duplicate_same_target_uses_existing_support_without_changing_recognition(confirmed):
    env, _, recognition, _, decision = confirmed
    before = env.records.read('recognitions', recognition.id)
    candidates = env.records.list('recognition_candidates')
    captured = review_corrections(env.records, 'alpha')
    assert len(captured) == 1
    job, _, _ = configured_job(env, 'signal-decision:' + decision.object_id, text=recognition.content)
    outcome = run(job)
    assert outcome['new_suggestions'] == 1, outcome
    support = env.records.list('v2_insight_evidence_support')
    assert len(support) == 1 and support[0].payload['state'] == 'pending'
    assert set(captured[0]['_source_experience_ids']) <= set(support[0].payload['experience_ids'])
    expected = {(node['type'], node['id'], node['source_revision'])
        for node in captured[0]['_source_graph']['nodes'] if node.get('kind') == 'material'}
    actual = {(node['type'], node['id'], node['source_revision'])
        for snapshot in support[0].payload['snapshots'] for node in snapshot['snapshot']['nodes']}
    assert expected <= actual
    assert env.records.read('recognitions', recognition.id) == before
    assert env.records.list('recognition_candidates') == candidates
    consumed = env.records.list('v2_consolidation_inputs')
    assert len(consumed) == 1 and consumed[0].payload['event_ids'] == ['signal-decision:' + decision.object_id]


@pytest.mark.parametrize('gate', ['enabled', 'allow_remote'])
def test_disabled_generation_has_no_wire_or_consumption(confirmed, gate):
    env, _, _, _, decision = confirmed
    before = state(env.records)
    job, models, calls = configured_job(env, 'signal-decision:' + decision.object_id)
    models.update('generation', {gate: False, 'expected_revision': models.public()['generation']['revision']})
    outcome = run(job)
    assert calls == [] and outcome['new_suggestions'] == 0
    assert state(env.records) == before


@pytest.mark.parametrize('qualification', ['unknown', 'malformed', 'dismiss', 'unconfirmed'])
def test_unqualified_review_never_consumes_decision(env, qualification):
    if qualification == 'unconfirmed':
        document = add_document(env, summary='alpha beta gamma', body='Synthetic unconfirmed source')[0]
        publish(env, doc=document)
        responses = [ask(env, text='alpha beta gamma?', intent='ask') for _ in range(2)]
        assert all(response.status_code == 200 for response in responses)
        assert env.records.list('v2_signal_decisions') == ()
        event_id = 'signal-decision:synthetic-unconfirmed'
    else:
        env, _, _, _, decision = prepare_confirmed(env, action='dismiss' if qualification == 'dismiss' else 'confirm')
        event_id = 'signal-decision:' + decision.object_id
        if qualification != 'dismiss':
            field, value = {'unknown': ('kind', 'synthetic_unknown'),
                'malformed': ('text', 'Synthetic prohibited text')}[qualification]
            replace(env.records, 'v2_signal_decisions', decision, {**decision.payload, field: value})
    before = state(env.records)
    job, _, _ = configured_job(env, event_id)
    outcome = run(job)
    assert outcome['new_suggestions'] == 0 and state(env.records) == before
    assert [request for request in requests(env.records) if request['desired_outcome'] == 'memory.consolidate'] == []


def test_legacy_edit_and_review_duplicate_preserve_both_recognition_parents(confirmed):
    from tests.memory_app.v2.test_consolidation_events import corrected
    from backend.memory_app.v2.consolidation_events import corrections
    env, _, recognition_y, _, decision = confirmed
    _, recognition_x, correction = corrected(env)
    legacy = corrections(env.records, 'alpha')
    reviewed = review_corrections(env.records, 'alpha')
    assert [event['event_id'] for event in legacy] == [correction.object_id]
    assert len(reviewed) == 1
    event_ids = [correction.object_id, 'signal-decision:' + decision.object_id]
    target = env.records.read('recognitions', recognition_y.id)
    job, _, _ = configured_job(env, event_ids, text=recognition_y.content)
    outcome = run(job)
    patterns = [row for row in env.records.list('v2_insight_patterns')
        if set(row.payload.get('event_ids', [])) == set(event_ids)]
    assert len(patterns) == 1, outcome
    candidate = env.records.read('recognition_candidates', patterns[0].object_id)
    assert candidate.payload['state'] == 'pending'
    assert {recognition_x.id, recognition_y.id} <= set(candidate.payload['source_recognition_ids'])
    assert env.records.read('recognitions', recognition_y.id) == target
    assert env.records.list('v2_insight_evidence_support') == ()
    consumed = [row for row in env.records.list('v2_consolidation_inputs')
        if set(row.payload.get('event_ids', [])) == set(event_ids)]
    assert len(consumed) == 1
    roots = [{'type': kind, 'id': identity, 'revision': env.records.read(collection, identity).revision}
        for field, kind, collection in (('source_experience_ids', 'experience', 'recognition_experiences'),
            ('source_recognition_ids', 'recognition', 'recognitions'))
        for identity in candidate.payload[field]]
    snapshot = SourceEgressService(env.records).snapshot(WorkScope('local-user', 'alpha'), roots)
    assert {recognition_x.id, recognition_y.id} <= {
        node['id'] for node in snapshot['nodes'] if node['type'] == 'recognition'}
