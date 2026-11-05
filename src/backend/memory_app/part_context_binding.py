"""为已冻结请求绑定原多部分上下文，不引入 v2 编排依赖。"""
COLLECTION = 'v2_part_contexts'


def prepare_context_refs(request, context):
    if context is None:
        return
    own = {
        'kind':'session_event', 'object_id':request['turn_id'],
        'uri':f"crp://default/{COLLECTION}/{request['turn_id']}"}
    if own in request['input']['refs']:
        if any(ref not in request['input']['refs'] for ref in context['refs']):
            raise ValueError('part_context_references_changed')
        return
    request['input']['refs'] = [*request['input']['refs'],*context['refs'],own]


def bind_context(records, request, context):
    if context is None:
        return
    prepare_context_refs(request,context)
    refs = request['input']['refs']
    with records.begin() as tx:
        tx.put(COLLECTION, request['turn_id'], {'project_id':context['project_id'],
            'input_refs':refs, 'context':context}, expected_revision=0)
        tx.commit()
