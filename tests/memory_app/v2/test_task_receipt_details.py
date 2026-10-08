from backend.memory_app.v2.task_receipt_details import task_receipt_details


def test_details_project_actual_tools_and_recalled_text_without_arguments():
    events = [{'type':'tool.completed', 'correlation':{'tool_call_id':'call-one'},
        'data':{'capability_id':'memory.recall','payload_ref':'crp://session/turn-a/result',
                'arguments':{'credential':'must-not-project'}}}]
    details = task_receipt_details('turn-a', events, lambda ref:[
        {'title':'本项目资料','content':'可核查的正文','api_key':'must-not-project'},
        {'content':'我的偏好'}])
    assert details == {'tools':[{'id':'call-one','capability_id':'memory.recall','state':'done'}],
                       'recalled':[{'title':'本项目资料','text':'可核查的正文'}, {'title':'认识','text':'我的偏好'}]}
    assert 'must-not-project' not in str(details)


def test_details_do_not_load_other_turn_payload():
    events = [{'type':'tool.completed', 'correlation':{'tool_call_id':'call'},
        'data':{'capability_id':'memory.recall','payload_ref':'crp://session/other/result'}}]
    def forbidden(ref):
        raise AssertionError('foreign payload must not load')
    assert task_receipt_details('turn-a',events,forbidden)['recalled'] == []
