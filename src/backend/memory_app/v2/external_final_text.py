import re

from .external_execution import RESULT_KIND, RECEIPT_KIND
from .external_dispatch import DispatchResult
from .external_host import HostLease
from .external_progress import bound_product
from .external_runner import ARCHIVE
from .external_runs import ExternalRuns


KIND = 'external-task-final-text-v1'
_SOURCES = {'claude-code': 'claude.result@1', 'codex': 'codex.last-agent-message@1'}


def validate_final_response(value, executor):
    if (not isinstance(value, dict) or set(value) != {'source', 'text'} or executor not in _SOURCES
            or value['source'] != _SOURCES[executor] or not isinstance(value['text'], str)
            or len(value['text']) > 200000):
        raise ValueError('external_final_text_source_invalid')
    return dict(value)


def _facts(reader, turns, request, product_id, executor, owner_id):
    identity = request['turn_id']
    archive = turns.get_immutable_payload(identity, ARCHIVE)
    native = turns.get_immutable_payload(identity, RESULT_KIND)
    proof = turns.get_immutable_payload(identity, RECEIPT_KIND)
    run = ExternalRuns(reader).read(identity, owner_id=owner_id)
    if (turns.get_request(identity) != request or archive is None or native is None or proof is None
            or archive[1]['request'] != request or archive[1]['owner_id'] != owner_id
            or archive[1]['turn_id'] != identity or archive[1]['launch']['executor'] != executor
            or request['session_id'] != 'session-' + product_id
            or run['executor'] != executor or native[1]['turn_id'] != identity
            or native[1]['owner_id'] != owner_id or native[1]['status'] != run['status']
            or native[1]['exit_code'] != run['exit_code'] or native[1]['usage'] != run['usage']
            or proof[1] != {'turn_id': identity, 'owner_id': owner_id, 'payload_ref': native[0],
                'run_revision': run['revision'], 'status': run['status'], 'effect_certainty': proof[1].get('effect_certainty')}
            or run['status'] not in {'completed', 'failed', 'cancelled', 'timed_out', 'output_limit'}
            or proof[1]['effect_certainty'] not in {'confirmed_applied', 'confirmed_none', 'unknown'}
            or run['status'] == 'completed' and proof[1]['effect_certainty'] != 'confirmed_applied'
            or run['status'] != 'completed' and proof[1]['effect_certainty'] == 'confirmed_applied'):
        raise ValueError('external_final_text_binding_invalid')
    return {'turn_id': identity, 'owner_id': owner_id, 'archive_ref': archive[0], 'result_ref': native[0],
        'receipt_ref': proof[0], 'run_revision': run['revision'], 'product_id': product_id}, run


def capture_final_text(lease, result, *, records, turns, turn_id, owner_id):
    if not isinstance(lease, HostLease):
        raise ValueError('external_final_text_lease_invalid')
    if (not isinstance(result, DispatchResult) or lease._binding()[0] is not records
            or lease.owner_id != owner_id):
        raise ValueError('external_final_text_binding_invalid')
    request = turns.get_request(turn_id)
    product_id = request['session_id'].removeprefix('session-')
    execution = records.read('v2_task_executions', product_id)
    # 旧的直接运行入口没有工作台归属；本侧证明只服务于真实产品任务。
    if execution is None:
        return None
    owner = lease._process_owner
    if owner is None or not owner.closed:
        if result.status == 'completed':
            raise ValueError('external_final_text_owner_not_closed')
        return None
    with records.begin() as tx:
        lease._validate_native_current()
        execution = tx.read('v2_task_executions', product_id)
        product_owner = execution.payload.get('owner') if execution else None
        product = bound_product(tx, product_id=product_id, product_owner=product_owner,
            request=request, executor=lease.plan.executor, turn_id=turn_id)
        binding, run = _facts(tx, turns, request, product_id, lease.plan.executor, owner_id)
        if (lease.accepted_turn != request or lease.owner_id != owner_id or result.status != run['status']
                or result.exit_code != run['exit_code'] or result.usage != run['usage']):
            raise ValueError('external_final_text_binding_invalid')
        response = result.final_response
        if response is not None:
            validate_final_response(response, lease.plan.executor)
            if lease.redact(response['text']) != response['text']:
                raise ValueError('external_final_text_source_invalid')
        proven = result.status == 'completed' and response is not None
        if not owner.closed or lease._process_owner is not owner:
            raise ValueError('external_final_text_owner_not_closed')
        value = {'version': KIND, 'binding': {**binding, 'product_owner': product_owner,
            'execution_revision': execution.revision, 'product_revision': product.revision},
            'state': 'proven' if proven else 'unavailable',
            'source': response['source'] if proven else None, 'text': response['text'] if proven else None}
        lease._validate_native_current()
        return turns.get_or_create_immutable_payload(turn_id, KIND, value)


def qualified_final_text(reader, turns, request, product_id, executor, *, owner_id, product_owner):
    saved = turns.get_immutable_payload(request['turn_id'], KIND)
    if saved is None:
        return None
    value = saved[1]
    if not isinstance(value, dict):
        raise ValueError('external_final_text_binding_invalid')
    binding, run = _facts(reader, turns, request, product_id, executor, owner_id)
    frozen = value.get('binding', {})
    execution = reader.read('v2_task_executions', product_id)
    product = reader.read('v2_turns', product_id)
    if (set(value) != {'version', 'binding', 'state', 'source', 'text'} or value['version'] != KIND
            or not isinstance(frozen, dict)
            or set(frozen) != set(binding) | {'product_owner', 'execution_revision', 'product_revision'}
            or any(frozen[key] != expected for key, expected in binding.items())
            or not isinstance(frozen['product_owner'], str) or not re.fullmatch(r'[0-9a-f]{32}', frozen['product_owner'])
            or product_owner is not None and frozen['product_owner'] != product_owner
            or execution is None or product is None
            or type(frozen['execution_revision']) is not int or not 1 <= frozen['execution_revision'] <= execution.revision
            or type(frozen['product_revision']) is not int or not 1 <= frozen['product_revision'] <= product.revision
            or value['state'] not in {'proven', 'unavailable'}):
        raise ValueError('external_final_text_binding_invalid')
    if value['state'] == 'unavailable':
        if value['source'] is not None or value['text'] is not None:
            raise ValueError('external_final_text_source_invalid')
        return None
    if run['status'] != 'completed':
        raise ValueError('external_final_text_source_invalid')
    return validate_final_response({'source': value['source'], 'text': value['text']}, executor)
