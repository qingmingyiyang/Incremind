"""Real retrieval owner and source revisions; no model transport is configured."""
from types import SimpleNamespace

import pytest

from backend.memory_app.v2.policies import override
from backend.recognition import RecognitionError
from core.document_engine import DocumentDraft
from core.storage_provider import JsonObjectStore
from tests.memory_app.v2.test_source_retrieval_index import query_for


@pytest.fixture
def initial_query(tmp_path):
    store = JsonObjectStore(tmp_path / '.rebuild-data')
    store.write('sources', 'orion-source', {'id': 'orion-source', 'project_id': 'alpha',
        'title': 'Synthetic Orion', 'metadata': {'content': 'Orion calibration original is amber.'}},
        expected_revision=0)
    query = query_for(tmp_path, store)
    query.models = SimpleNamespace(public=lambda: {}, generation_budget_limits=lambda **_: {
        'window': 32000, 'reserve': 512})
    query.documents.create(DocumentDraft(title='Orion calibration', document_type='legacy-material',
        markdown='# Orion\n\n## 摘要\nOrion calibration summary.\n\n## 正文\nOrion calibration body.',
        source_refs=({'source_id': 'orion-source', 'locator': 'text:0:36'},), project_id='alpha'))
    return query


def test_initial_probe_is_upper_only_and_remainder_only_collects_l0(initial_query, monkeypatch):
    query = initial_query
    calls, original = [], query._collect_candidates

    def observed(*args, **kwargs):
        calls.append(kwargs.get('only_layers'))
        return original(*args, **kwargs)

    monkeypatch.setattr(query, '_collect_candidates', observed)
    with override(retrieve='@10'):
        initial = query.collect_initial_candidates('alpha', 'Orion calibration')
        assert initial['candidates']
        assert {row['layer'] for row in initial['candidates']} <= {'L3', 'L2', 'L1'}
        complete = query.collect_initial_remainder('alpha', 'Orion calibration', initial)
        assert any(row['layer'] == 'L0' for row in complete['candidates'])
        query.validate_initial_candidates('alpha', 'Orion calibration', complete)
    assert calls == [('L3', 'L2', 'L1'), ('L0',)]


@pytest.mark.parametrize('project,question,scene', [('other', 'Orion calibration', None),
    ('alpha', 'different question', None), ('alpha', 'Orion calibration', 'other')])
def test_probe_reuse_requires_same_scope_and_question(initial_query, project, question, scene):
    with override(retrieve='@10'):
        initial = initial_query.collect_initial_candidates('alpha', 'Orion calibration')
        with pytest.raises(RecognitionError, match='initial_memory_binding_changed'):
            initial_query.collect_initial_remainder(project, question, initial, scene=scene)


def test_cached_upper_sources_are_revalidated_before_remainder(initial_query):
    query = initial_query
    with override(retrieve='@10'):
        initial = query.collect_initial_candidates('alpha', 'Orion calibration')
        saved = query.source_store.read('sources', 'orion-source')
        query.source_store.write('sources', 'orion-source', {**saved,
            'metadata': {'content': 'Orion revised source.'}}, expected_revision=1)
        with pytest.raises(RecognitionError):
            query.collect_initial_remainder('alpha', 'Orion calibration', initial)


def test_initial_probe_is_not_available_to_default_policy(initial_query):
    with pytest.raises(ValueError, match='initial_memory_policy_required'):
        initial_query.collect_initial_candidates('alpha', 'Orion calibration')
