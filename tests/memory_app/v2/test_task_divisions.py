from backend.memory_app.v2.task_divisions import TaskDivisions
from core.storage_provider import SQLiteStructuredRecordStore


def test_samples_reuse_governed_vector_ranking_and_cache(tmp_path, monkeypatch):
    from tests.memory_app.v2.test_insight_links import VectorModel
    from backend.memory_app.retrieval_models import ConfiguredTransport
    calls = []
    def wire(transport, *, endpoint, payload):
        transport._check_current()
        calls.append(payload['input'])
        return {'data':[{'index':i,'embedding':[1.0,0.0]} for i,_ in enumerate(payload['input'])],
                'usage':{'prompt_tokens':5}}
    monkeypatch.setattr(ConfiguredTransport, 'post_json', wire)
    records = SQLiteStructuredRecordStore(tmp_path/'records.sqlite3')
    samples = TaskDivisions(records, models=VectorModel())
    item = {'goal':'规划','deliverable':'文稿','capabilities':['memory.recall'],'depends_on':[]}
    samples.complete('turn-old',project='alpha',text='咖啡馆经营策划',items=[item],outcome='done')
    assert samples.similar('alpha','饮品门店商业方案')[0]['source_turn_id'] == 'turn-old'
    assert calls
    initial_calls = len(calls)
    assert samples.similar('alpha','饮品门店商业方案')[0]['source_turn_id'] == 'turn-old'
    assert len(calls) == initial_calls + 1
    assert calls[-1] == ['饮品门店商业方案'], 'candidate vector must come from the existing cache'
    samples.similar('alpha','饮品门店商业方案')
    assert len(calls) == initial_calls + 1, 'the identical query wire result must then be replayed locally'
    assert records.list('v2_memory_turn_keys')
    assert not records.list('v2_egress_receipts')


def test_samples_are_bounded_adjustable_and_deletable(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path/'records.sqlite3')
    samples = TaskDivisions(records)
    item = {'goal':'整理', 'deliverable':'方案', 'capabilities':['memory.recall'], 'depends_on':[]}
    for index in range(5):
        samples.complete(f'turn-sample-{index}', project='alpha', text='整理研究方案',
                         items=[item], outcome='failed' if index == 0 else 'done')
    assert len(samples.similar('alpha', '整理研究方案')) == 3
    samples.adjust('turn-sample-4', project='alpha', items=[{**item,'goal':'调整目标'}], expected_revision=1)
    selected = samples.similar('alpha', '整理研究方案')
    assert selected[0]['source_turn_id'] == 'turn-sample-4'
    assert selected[0]['adjusted'] is True
    assert all(row['outcome'] != 'failed' for row in selected)
    samples.remove('turn-sample-4', project='alpha', expected_revision=2)
    assert all(row['source_turn_id'] != 'turn-sample-4' for row in samples.similar('alpha','整理研究方案'))
    assert samples.similar('alpha','完全不同的食谱') == []
