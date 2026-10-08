"""Disposable subprocess fixture: terminate after a completed chunk is durable."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests import _path_setup  # noqa: F401

from core.storage_provider import SQLiteStructuredRecordStore
from backend.memory_app.v2.organize_turns import OrganizeTurns
from backend.memory_app.workspace_generation import _complete_chunked_video_draft


def run(root, crash):
    records = SQLiteStructuredRecordStore(root / 'records.sqlite3')
    source = '甲' * 8000
    if records.read('workspace_items','synthetic-video') is None:
        with records.begin() as tx:
            tx.put('workspace_items','synthetic-video',{'id':'synthetic-video',
                'project_id':'alpha','source_text':source},expected_revision=0)
            tx.commit()

    class Transport:
        def complete(self, messages, *, max_tokens, validate_current, timeout_seconds=None):
            validate_current()
            with (root/'calls.jsonl').open('a',encoding='utf8') as stream:
                stream.write(json.dumps(messages[0]['content'],ensure_ascii=False)+'\n')
            if crash == 'during':
                os._exit(23)
            if '当前仅处理全文第' in messages[0]['content']:
                output = {'summary':'片段摘要','topics':[],'facts':[],'todos':[],'uncertainties':[]}
            else:
                output = {'title':'整体标题','summary':'整体摘要','topics':[],
                    'uncertainties':[],'people':[],'dates':[],'suggestions':[], 'fact_ids':[],'todo_ids':[]}
            return json.dumps(output,ensure_ascii=False), {'usage':{'total_tokens':4}}

    transport = Transport()
    if crash == 'terminal':
        from core.ai_kernel import SynchronousAIRuntime
        original = SynchronousAIRuntime._append_turn_terminal
        def stop_before_terminal(self, turn_id, event_type, *args, **kwargs):
            if event_type == 'turn.completed':
                os._exit(26)
            return original(self, turn_id, event_type, *args, **kwargs)
        SynchronousAIRuntime._append_turn_terminal = stop_before_terminal
    organize = OrganizeTurns(root=root,records=records,models=transport,item_id='synthetic-video',
        project_id='alpha',source=source,validate_current=lambda:None)
    if crash == 'crash':
        complete = organize.complete
        def stop_after_output(*args, **kwargs):
            result = complete(*args, **kwargs)
            os._exit(19)
        organize.complete = stop_after_output
    draft, metadata = _complete_chunked_video_draft(source,lambda:None,False,organize=organize)
    (root/'result.json').write_text(json.dumps({'draft':draft,'metadata':metadata},ensure_ascii=False),encoding='utf8')


if __name__ == '__main__':
    run(Path(sys.argv[1]), sys.argv[2])
