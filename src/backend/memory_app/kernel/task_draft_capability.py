"""Keep legacy approved writes separate from create-only task drafts."""
from dataclasses import replace
from collections.abc import Mapping

from core.ai_tooling.contracts import tool_from_legacy_capability


def task_draft_definition(definition):
    definition = replace(definition, requires_approval=False)
    tool = tool_from_legacy_capability(definition,
        boundary_requirements=('draft_create_only',))
    return replace(definition, tool_definition=tool)


class TaskDraftCapability:
    def __init__(self, *, legacy, drafts, request_loader, division_capabilities, division_binding=None):
        self.legacy, self.drafts = legacy, drafts
        self.request_loader, self.division_capabilities = request_loader, division_capabilities
        self.division_binding = division_binding

    def invoke(self, request):
        frozen = self.request_loader(request['turn_id'])
        if frozen.get('desired_outcome') != 'project.task':
            return self.legacy.invoke(request)
        if 'document.draft.propose' not in self.division_capabilities(frozen):
            raise ValueError('task draft is outside frozen division')
        args = request.get('arguments', {})
        if set(args) not in ({'title', 'markdown'}, {'title', 'markdown', 'final_for'}):
            raise ValueError('task draft only accepts new title and markdown')
        final_for = args.get('final_for')
        if 'final_for' in args and (not isinstance(final_for, str) or not final_for.strip()
                                   or len(final_for) > 2000):
            raise ValueError('task draft final_for must be a nonempty deliverable of at most 2000 characters')
        binding = self.division_binding(frozen) if final_for is not None and self.division_binding else None
        terminate = binding is not None and final_for == binding['deliverable']
        result = self.drafts.create(turn_id=frozen['turn_id'], project=frozen['scope']['project_id'],
            operation=request['operation_id'], title=args['title'], markdown=args['markdown'])
        if terminate:
            result = {**result, 'title': args['title'], 'summary': args['markdown'][:2000],
                      'terminate': True, 'final_for': final_for, 'division_binding': binding}
        return {'summary':args['markdown'][:2000], 'receipt_ref':result['receipt_ref'],
            'payload_ref':None, 'evidence_refs':[f"crp://{self.drafts.documents.namespace_id}/documents/{result['document_id']}.json"],
            'result':result}


def final_draft_decision(request, events, payloads, *, binding, drafts):
    """Complete from a successful operation, never from a claimed tool name."""
    if binding is None or drafts is None:
        return None
    prefix = f"crp://session/{request['turn_id']}/"
    tools = [(index, event) for index, event in enumerate(events)
             if str(event.get('type', '')).startswith('tool.')]
    if not tools or tools[-1][1].get('type') != 'tool.completed':
        return None
    index, event = tools[-1]
    if any(item.get('type') == 'hook.continuation' for item in events[index + 1:]):
        return None
    data = event.get('data', {})
    if (not isinstance(data, Mapping) or event.get('turn_id') != request['turn_id']
            or data.get('capability_id') != 'document.draft.propose'
            or not isinstance(data.get('payload_ref'), str)
            or not isinstance(data.get('receipt_ref'), str)
            or not data['payload_ref'].startswith(prefix)):
        return None
    try:
        result = payloads.get(data['payload_ref'])
    except (KeyError, PermissionError):
        return None
    if (not isinstance(result, Mapping) or result.get('terminate') is not True
            or result.get('division_binding') != binding
            or result.get('final_for') != binding['deliverable']
            or result.get('receipt_ref') != data.get('receipt_ref')):
        return None
    call_id = event.get('correlation', {}).get('tool_call_id')
    intents = [item for item in events if item.get('type') == 'tool.intent.recorded'
               and item.get('turn_id') == request['turn_id']
               and item.get('correlation', {}).get('tool_call_id') == call_id]
    if len(intents) != 1:
        return None
    intent_ref = intents[0].get('data', {}).get('payload_ref')
    if not isinstance(intent_ref, str) or not intent_ref.startswith(prefix):
        return None
    try:
        intent = payloads.get(intent_ref)
    except (KeyError, PermissionError):
        return None
    if (not isinstance(intent, Mapping) or intent.get('turn_id') != request['turn_id']
            or intent.get('capability_id') != 'document.draft.propose'
            or intent.get('invocation_id') != call_id):
        return None
    operation = intent.get('operation_id')
    if not isinstance(operation, str):
        return None
    saved = drafts.get_operation(operation)
    if saved is None:
        return None
    inputs, output = saved.payload.get('inputs'), saved.payload.get('result')
    if (not isinstance(inputs, Mapping) or not isinstance(output, Mapping)
            or data['receipt_ref'] != output.get('receipt_ref')):
        return None
    args = intent.get('arguments')
    if (not isinstance(args, Mapping) or set(args) != {'title', 'markdown', 'final_for'}
            or args['final_for'] != binding['deliverable']
            or inputs != {'turn_id': request['turn_id'], 'project_id': binding['project_id'],
                          'title': args['title'], 'markdown': args['markdown']}
            or any(result.get(key) != output.get(key) for key in ('document_id', 'document_revision', 'receipt_ref'))
            or result.get('title') != inputs['title']
            or result.get('summary') != inputs['markdown'][:2000]):
        return None
    summary = (result['title'] + '\n' + result['summary'])[:2000]
    return {'type': 'complete', 'summary': summary, 'payload_ref': data['payload_ref'],
            'evidence_refs': list(data.get('evidence_refs', []))}
