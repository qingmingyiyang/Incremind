"""Ordinary historical drafts keep their existing identity without comment proof."""
import pytest

from backend.memory_app.v2.source_sections import (
    SECTIONS, resolve_comment_sources, validate_comment_inputs,
)
from backend.recognition import RecognitionConflict
from tests.memory_app.v2.test_insight_generation import env


def test_plain_comment_inputs_keep_a_retained_revision_after_document_edit(env):
    frozen = resolve_comment_sources(env.records, env.documents, 'alpha', env.doc, revision=1)
    assert frozen == {'document': {'id': env.doc, 'revision': 1}, 'sources': [], 'bindings': []}
    env.documents.save_user_edit(env.doc, markdown='后续人工修订', expected_revision=1)
    validate_comment_inputs(env.records, env.documents, 'alpha', frozen)
    assert env.documents.read(env.doc)['revision'] == 2
    assert frozen['document']['revision'] == 1


@pytest.mark.parametrize('damage', ['revision_missing', 'markdown_missing',
    'revision_bool', 'markdown_bool', 'proof_invalidated', 'proof_corrupt'])
def test_plain_historical_comment_inputs_do_not_bypass_retained_proof_validation(env, damage):
    frozen = resolve_comment_sources(env.records, env.documents, 'alpha', env.doc, revision=1)
    env.documents.save_user_edit(env.doc, markdown='后续人工修订', expected_revision=1)
    with env.records.begin() as tx:
        if damage.startswith(('revision_', 'markdown_')):
            collection = 'document_revisions' if damage.startswith('revision_') else 'document_markdown'
            row = tx.read(collection, f'{env.doc}~r1')
            assert row is not None
            if damage.endswith('_missing'):
                tx.delete(collection, row.object_id, expected_revision=row.revision)
            else:
                tx.put(collection, row.object_id, {**row.payload, 'revision': True},
                    expected_revision=row.revision)
        else:
            proof = tx.list(SECTIONS)[0]
            assert proof.payload['state'] == 'unbound'
            payload = {**proof.payload, 'state': 'invalidated'} if damage == 'proof_invalidated' else {
                **proof.payload, 'unexpected_capture': 'forged'}
            tx.put(SECTIONS, proof.object_id, payload, expected_revision=proof.revision)
        tx.commit()
    with pytest.raises(RecognitionConflict):
        validate_comment_inputs(env.records, env.documents, 'alpha', frozen)
