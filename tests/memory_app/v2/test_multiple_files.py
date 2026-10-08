from tests.memory_app.v2.test_workbench_remember import env, post, wait


def test_multiple_files_keep_distinct_originals_and_one_thread(env):
    thread_id = None
    receipts = []
    for name, content in [('one.txt', '原文证据第一份'), ('two.md', '原文证据第二份')]:
        response = env.http.post('/api/v2/workbench/files', data={'project_id': 'alpha'},
            files={'file': (name, content.encode(), 'text/plain')})
        assert response.status_code == 200, response.text
        item = response.json()
        result = post(env, text='', item_id=item['id'], **({'thread_id': thread_id} if thread_id else {}))
        thread_id = result['thread_id']
        receipt = wait(env, result)['receipt']['remember']
        assert receipt['state'] == 'done'
        row = env.records.read('workspace_items', item['id'])
        assert row.payload['source_text'] == content
        assert row.payload['status'] == 'confirmed'
        assert receipt['document_id'] == row.payload['document_id']
        assert receipt['verified'] is False
        assert all(insight['state'] == 'pending' for insight in receipt['insights'])
        receipts.append(receipt)
    assert receipts[0]['item_id'] != receipts[1]['item_id']
    assert receipts[0]['document_id'] != receipts[1]['document_id']
    saved = env.http.get(f'/api/v2/workbench/threads/{thread_id}?project_id=alpha').json()
    assert len(saved['turns']) == 2
    assert {turn['receipt']['remember']['item_id'] for turn in saved['turns']} == {row['item_id'] for row in receipts}
    assert env.records.list('recognitions') == ()
