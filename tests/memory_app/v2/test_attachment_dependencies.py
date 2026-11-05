import asyncio
from time import monotonic

import pytest
from fastapi import HTTPException
from core.storage_provider import SQLiteStructuredRecordStore
from backend.memory_app.v2.attachments import OriginalReadiness, attachment_ids, expand_parts
from backend.memory_app.v2.route import RoutePart


def test_attachment_context_policy_preserves_the_frozen_prefix_without_active_switch():
    from backend.memory_app.v2.policies import ACTIVE, get
    from backend.memory_app.v2.part_context import ATTACHMENT_PREFIX
    expected = '本次附件的原文证据。成果用给定的原件标题和链接引用实际采用的原件：\n'
    assert get('attachment_context', version='@1')() == ATTACHMENT_PREFIX == expected
    assert 'attachment_context' not in ACTIVE


@pytest.mark.parametrize('body', [{'item_ids': []}, {'item_ids': ['']}, {'item_ids': [1]},
    {'item_ids': ['a'] * 11}, {'item_ids': 'a'}, {'item_ids': ['a'], 'item_id': None}])
def test_attachment_fields_reject_invalid_shape(body):
    with pytest.raises(HTTPException) as failure:
        attachment_ids(body)
    assert failure.value.status_code == 400 and failure.value.detail == 'invalid_turn_fields'


def test_expansion_remaps_all_dependencies_and_keeps_model_plan():
    original = [RoutePart(intent='remember', span=''), RoutePart(intent='ask', span='问题', depends_on=[0]),
                RoutePart(intent='do', span='成果', depends_on=[0])]
    expanded = expand_parts(original, [str(index) for index in range(10)])
    assert len(expanded) == 12
    assert expanded[-1][0].depends_on == list(range(10))
    assert [item for _, item in expanded[:10]] == [str(index) for index in range(10)]
    assert original[1].depends_on == [0]


@pytest.mark.parametrize('terminal', ['ready', 'failed', 'empty', 'timeout'])
def test_original_wait_uses_committed_state_and_cleans_notifications(tmp_path, terminal):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    with records.begin() as tx:
        tx.put('workspace_items', 'one', {'project_id': 'alpha', 'status': 'processing', 'source_text': ''}, expected_revision=0)
        tx.commit()
    async def scenario():
        readiness = OriginalReadiness()
        waiter = asyncio.create_task(readiness.wait(records, 'alpha', 'one', monotonic() + (0.2 if terminal == 'timeout' else 5)))
        await asyncio.sleep(0)
        if terminal != 'timeout':
            assert len(readiness.waiters['alpha', 'one']) == 1
        if terminal != 'timeout':
            with records.begin() as tx:
                row = tx.read('workspace_items', 'one')
                tx.put('workspace_items', 'one', {**row.payload, 'status': 'failed' if terminal == 'failed' else 'ready',
                       'source_text': '实际原文' if terminal == 'ready' else ''}, expected_revision=row.revision)
                tx.commit()
            readiness.notify('alpha', 'one')
        if terminal == 'ready':
            await waiter
        else:
            with pytest.raises(HTTPException) as failure:
                await waiter
            assert failure.value.detail == ('dependency_timeout' if terminal == 'timeout' else 'dependency_failed')
        assert readiness.waiters == {}
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['receipt', 'inactive'])
def test_staged_original_observes_authoritative_remember_failure(tmp_path, failure):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    with records.begin() as tx:
        tx.put('workspace_items', 'one', {'project_id': 'alpha', 'status': 'staged', 'source_text': ''}, expected_revision=0)
        tx.put('v2_turns', 'remember-one', {'receipt': {'remember': {'state': 'failed' if failure == 'receipt' else 'processing'}}}, expected_revision=0)
        tx.commit()
    async def scenario():
        readiness = OriginalReadiness()
        with pytest.raises(HTTPException) as error:
            await readiness.wait(records, 'alpha', 'one', monotonic() + 900,
                turn_id='remember-one', active=lambda: failure != 'inactive')
        assert error.value.detail == 'dependency_failed'
        assert readiness.waiters == {}
    asyncio.run(scenario())
