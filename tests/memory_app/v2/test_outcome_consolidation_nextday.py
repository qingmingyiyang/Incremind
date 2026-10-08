"""Ordinary consolidation cannot reinterpret an edited product Root's birth."""
from datetime import timedelta
import asyncio

import pytest

from backend.memory_app.v2.consolidation_events import consumer_outcomes
from backend.memory_app.v2.insights import source_documents
from backend.recognition import WorkScope
from core.document_engine.ports import DocumentDraft
from tests.memory_app.v2.kernel_receipts import requests
from tests.memory_app.v2.test_outcome_consolidation_consumer import (
    do_env, scenario, completed, edit, adjust, redo, wait_product, NEW, PROJECT,
    consumer, confirm,
)


@pytest.mark.parametrize('confirmed', [False, True], ids=['pending', 'confirmed'])
def test_next_day_edited_root_and_indirect_memory_are_not_ordinary_inputs(scenario, confirmed):
    env = scenario
    old, turn = completed(env)
    edit(env, turn)
    adjust(env, old['turn']['id'])
    env.summary = NEW
    response = redo(env, old, revision=2)
    assert response.status_code == 200, response.text
    new_turn = wait_product(env, response.json())
    assert new_turn['receipt']['do']['state'] == 'done'
    old_document = turn['receipt']['do']['document_id']
    new_document = new_turn['receipt']['do']['document_id']
    assert old_document != new_document
    assert env.documents.read(old_document)['revision'] == 2
    assert env.documents.read(new_document)['revision'] == 1
    facts = env.records.list('v2_outcome_corrections')
    assert {row.payload['kind'] for row in facts} == {'outcome_edit', 'division_adjust', 'outcome_redo'}
    assert len(facts) == 3 and len(env.models.calls) == 5
    # Original generated documents use June's default clock; a real open supplies
    # the ordinary collector's existing seven-day use qualification.
    opened = env.client.post('/api/v2/usage/open', json={
        'project_id': PROJECT, 'kind': 'document', 'id': new_document})
    assert opened.status_code == 204, opened.text
    assert env.records.read('v2_usage_document', new_document).payload['events'][-1]['kind'] == 'open'

    job, wires = consumer(env)
    first = job.run(PROJECT)
    assert first['new_suggestions'] == 1 and first['failed_groups'] == 0
    pattern, = env.records.list('v2_insight_patterns')
    candidate = env.records.read('recognition_candidates', pattern.object_id)
    assert candidate.payload['state'] == 'pending'
    experiences = candidate.payload['source_experience_ids']
    assert len(experiences) == 2
    scope = WorkScope('local-user', PROJECT)
    # The indirect ordinary path really contains two Roots, so len(docs)<2 cannot hide it.
    assert source_documents(env.records, scope, experiences, []) == {old_document, new_document}
    for identity in experiences:
        experience = env.records.read('recognition_experiences', identity)
        assert experience.payload['provenance']['kind'] == 'model_generated_artifact'
        document, = [ref for ref in experience.payload['provenance']['source_refs'] if ref['type'] == 'document']
        assert document['revision'] == 1
    indirect_id = confirm(env, candidate).id if confirmed else candidate.object_id
    consumed, = env.records.list('v2_consolidation_inputs')
    assert set(consumed.payload['event_ids']) == {'outcome:' + row.object_id for row in facts}
    frozen_before = [request for request in requests(env.records)
        if request['desired_outcome'] == 'memory.consolidate']
    assert len(frozen_before) == 1
    preserved = {collection: env.records.list(collection) for collection in (
        'v2_outcome_corrections', 'v2_consolidation_inputs', 'v2_insight_patterns',
        'recognition_candidates', 'recognitions', 'recognition_experiences', 'documents',
    )}
    history = [env.documents.markdown(old_document, revision=revision) for revision in (1, 2)]

    next_day = job.now() + timedelta(days=1)
    job.now = lambda: next_day
    assert consumer_outcomes(env.records, PROJECT, now=next_day.isoformat()) == []
    wire_count = len(wires)
    second = job.run(PROJECT)
    assert second['status'] == 'completed' and second['failed_groups'] == 0
    assert second['new_suggestions'] == 0
    rows = job._collect(PROJECT)
    assert old_document not in {row.object_id for row in rows}
    assert indirect_id not in {row.object_id for row in rows}
    assert all(old_document not in row.documents for row in rows)
    assert new_document in {row.object_id for row in rows if row.kind == 'document'}
    assert [request for request in requests(env.records)
        if request['desired_outcome'] == 'memory.consolidate'] == frozen_before
    assert not any('"outcomes"' in message['content']
        for wire in wires[wire_count:] for message in wire['messages'])
    for collection, before in preserved.items():
        assert env.records.list(collection) == before
    assert [env.documents.markdown(old_document, revision=revision) for revision in (1, 2)] == history

    # A normal document and the untouched new Root keep their original default collection behavior.
    item = asyncio.run(env.state.workspace_domains.intake.add_text({
        'project_id': PROJECT, 'text': '普通独立原文'}))
    ordinary = env.documents.create(DocumentDraft(title='普通整理稿', document_type='notes',
        markdown='普通独立正文', source_refs=({'source_id': item['id'],
            'locator': 'workspace://' + item['id']},), project_id=PROJECT))['id']
    opened = env.client.post('/api/v2/usage/open', json={
        'project_id': PROJECT, 'kind': 'document', 'id': ordinary})
    assert opened.status_code == 204, opened.text
    assert env.records.read('v2_usage_document', ordinary).payload['events'][-1]['kind'] == 'open'
    rows = job._collect(PROJECT)
    assert {ordinary, new_document} <= {row.object_id for row in rows if row.kind == 'document'}
    assert all(old_document not in row.documents for row in rows)
