from copy import deepcopy

import pytest

from backend.memory_app.kernel.answer_continuations import decode, encode
from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.v2.agentic_rag import answer_candidates, candidate_id
from backend.memory_app.v2.answer_evidence import build_evidence_catalog
from backend.memory_app.v2.ladder import plan_ladder
from backend.memory_app.v2.policies import override
from backend.memory_app.v2.policies.ladder import _DiscoveryChains, source_identity_v14
from backend.recognition import RecognitionConflict, RecognitionError, WorkScope
from core.ai_kernel.sqlite_store import SQLiteAITurnStore
from core.search_and_recall.evidence_windows import EvidenceWindow
from core.storage_provider import JsonObjectStore, SQLiteStructuredRecordStore


TEMPORARY = {'_discovery_chains', '_discovered_chain', '_chain_candidates'}
QUESTION = 'What did Maya paint?'
CONTENT = 'Maya painted a garden portrait.'


@pytest.fixture
def original(tmp_path):
    root = tmp_path / '.rebuild-data'
    records = SQLiteStructuredRecordStore(root / 'structured-records.sqlite3')
    sources = JsonObjectStore(root)
    scope = WorkScope('local-user', 'alpha')
    value = {'id': 'painting', 'project_id': 'alpha', 'title': 'Painting record',
        'metadata': {'content': CONTENT}}
    sources.write('sources', 'painting', value, expected_revision=0)
    revision = sources.revision('sources', 'painting')
    authority = SourceEgressService(records)
    snapshot = authority.snapshot(scope, [{'type': 'original_source', 'id': 'painting', 'revision': revision}])
    authority.validate_snapshot(scope, snapshot)
    authority.require(snapshot, 'generation')
    row = {'kind': 'source', 'id': 'painting', 'layer': 'L0', 'score': 5.0,
        'scope': scope, 'snapshot': snapshot, 'title': 'Painting record', 'href': '',
        'entry': {'kind': 'source', 'id': 'painting', 'revision': revision, 'content': CONTENT,
            'title': 'Painting record', 'metadata': {}},
        'windows': (EvidenceWindow(0, len(CONTENT), CONTENT),), 'excerpt': CONTENT,
        'match_in': 'content', 'coordinate_space': 'source_content_v1',
        '_other_request_metadata': {'value': 'kept'}, 'vector_score': .8}
    chains = ({'windows': row['windows'], 'excerpt': CONTENT, 'seeds': ()},)
    capability = _DiscoveryChains(QUESTION, source_identity_v14(row), chains)
    row.update(_discovery_chains=capability, _discovered_chain=(capability, 0),
        _chain_candidates=(dict(row),))
    return row, authority, sources, root / 'ai-turns.sqlite3'


@pytest.mark.parametrize('current', [True, False], ids=['current-final-set', 'new-observation'])
def test_final_handoff_clears_only_discovery_runtime_and_freezes_real_source(original, current):
    row, authority, sources, store_path = original
    before, old_source = deepcopy(row), sources.read('sources', 'painting')
    catalog = build_evidence_catalog([row], compose_version='@37')
    plan = {'policy_versions': {'retrieve': '@9', 'ladder': '@15'},
        'chosen': [row] if current else [], '_rag': {'observed': [] if current else [row]}}
    chosen = answer_candidates(plan, [] if current else [candidate_id(row)])
    assert len(chosen) == 1 and type(chosen[0]) is dict and chosen[0] is not row
    assert set(row) - set(chosen[0]) == TEMPORARY
    assert chosen[0] == {key: value for key, value in row.items() if key not in TEMPORARY}
    assert chosen[0]['_other_request_metadata'] == {'value': 'kept'}
    assert chosen[0]['vector_score'] == .8
    assert row == before and sources.read('sources', 'painting') == old_source
    assert sources.revision('sources', 'painting') == row['entry']['revision']
    authority.validate_snapshot(row['scope'], chosen[0]['snapshot'])
    authority.require(chosen[0]['snapshot'], 'generation')
    assert build_evidence_catalog(chosen, compose_version='@37') == catalog
    with pytest.raises(ValueError, match='invalid_answer_continuation_facts'):
        encode(row)
    capsule = encode({'chosen': chosen})
    store = SQLiteAITurnStore(store_path)
    store.claim_turn({'turn_id': 'publication-turn', 'idempotency_key': 'publication-key',
        'session_id': 'publication-session', 'operation_id': 'publication-operation',
        'desired_outcome': 'project.answer', 'scope': {'project_id': 'alpha'},
        'input': {'text': QUESTION}, 'policy_versions': plan['policy_versions']})
    store.get_or_create_immutable_payload('publication-turn', 'publication-candidates-v1', capsule)
    reread = SQLiteAITurnStore(store_path).get_immutable_payload('publication-turn', 'publication-candidates-v1')
    assert decode(reread[1]) == {'chosen': chosen}


@pytest.mark.parametrize('version', ['@1', '@15'])
def test_ordinary_ladder_keeps_source_bytes_and_metadata(original, version):
    row, authority, sources, _ = original
    ordinary = {key: value for key, value in row.items() if key not in TEMPORARY}
    before = deepcopy(ordinary)
    with override(ladder=version):
        result = plan_ladder([ordinary], QUESTION, token_budget=8000)
    assert len(result['chosen']) == 1
    chosen = result['chosen'][0]
    assert chosen['entry'] == ordinary['entry'] and chosen['snapshot'] == ordinary['snapshot']
    assert chosen['windows'] == ordinary['windows'] and chosen['excerpt'] == CONTENT
    assert chosen['coordinate_space'] == 'source_content_v1'
    assert chosen['_other_request_metadata'] == {'value': 'kept'}
    assert ordinary == before and sources.revision('sources', 'painting') == chosen['entry']['revision']
    authority.validate_snapshot(chosen['scope'], chosen['snapshot'])


@pytest.mark.parametrize('change', ['revision', 'privacy'])
def test_source_authority_still_rejects_changed_published_candidate(original, change):
    row, authority, sources, _ = original
    plan = {'policy_versions': {'retrieve': '@9'}, 'chosen': [], '_rag': {'observed': [row]}}
    selected = answer_candidates(plan, [candidate_id(row)])[0]
    if change == 'revision':
        current = sources.read('sources', 'painting')
        sources.write('sources', 'painting', {**current, 'title': 'Later revision'},
            expected_revision=selected['entry']['revision'])
    else:
        authority.set_policy(row['scope'], 'original_source', 'painting', row['entry']['revision'], 0, [])
    with pytest.raises(RecognitionError):
        authority.validate_snapshot(row['scope'], selected['snapshot'])


@pytest.mark.parametrize('selection', ['unknown', 'duplicate', 'too-many'])
def test_explicit_selection_identity_and_limit_stay_mandatory(original, selection):
    row, _, _, _ = original
    identity = candidate_id(row)
    identities = ['unknown'] if selection == 'unknown' else [identity] * (2 if selection == 'duplicate' else 9)
    plan = {'policy_versions': {'retrieve': '@9'}, 'chosen': [], '_rag': {'observed': [row]}}
    with pytest.raises(RecognitionConflict, match='rag_answer_outside_observed'):
        answer_candidates(plan, identities)
