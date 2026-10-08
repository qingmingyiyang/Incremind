"""已准入宿主的内部执行接点；不授予外发或操作权限。"""
from copy import deepcopy
import json

from core.ai_kernel.dispatcher import ToolProviderFailure
from .external_dispatch import dispatch_external
from .external_host import HostLease
from .external_runs import ExternalRuns
from .external_process import ProcessCleanupError


RESULT_KIND = 'external-task-result-v1'
RECEIPT_KIND = 'external-task-receipt-v1'
_TERMINAL = {'completed', 'failed', 'cancelled', 'timed_out', 'output_limit'}


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _failure(code, started):
    return ToolProviderFailure(code, effect_certainty='unknown' if started else 'confirmed_none')


def _dto(body, payload_ref, receipt_ref):
    return {'summary':'外部任务完成', 'payload_ref':payload_ref, 'receipt_ref':receipt_ref, 'evidence_refs':[]}


def execute_external(lease, context, *, records, turns, turn_id, owner_id,
        event_sink=None, output_limit=1048576, before_launch=None):
    """借用 caller 管理的 lease；全部进程已关闭后才保存终态。"""
    started = False
    reserved = False
    execution_active = False
    may_have_started = False
    try:
        if (not isinstance(lease, HostLease) or lease.owner_id != owner_id
                or type(output_limit) is not int or output_limit < 1
                or lease.accepted_turn.get('turn_id') != turn_id
                or _bytes(lease.accepted_turn) != _bytes(turns.get_request(turn_id))):
            raise _failure('external_task_binding_invalid', False)
        limit = lease.execution_limit(records=records, owner_id=owner_id)
        runs = ExternalRuns(records, limit=limit)
        plan = lease.plan
        reservation = runs.reserve(turn_id, owner_id=owner_id, executor=plan.executor,
            adapter_version=plan.adapter_version, cli_version=plan.cli_version,
            preset=plan.preset, workspace=plan.cwd)
        row = reservation.run
        if not reservation.reservation_created:
            # 旧占位不能证明未启动；缺任一真实终态材料均拒绝重启。
            started = True
            result = turns.get_immutable_payload(turn_id, RESULT_KIND)
            receipt = turns.get_immutable_payload(turn_id, RECEIPT_KIND)
            if row['status'] not in _TERMINAL or result is None or receipt is None:
                raise _failure('external_task_result_missing', True)
            payload_ref, body = result
            receipt_ref, proof = receipt
            expected = {'turn_id':turn_id, 'owner_id':owner_id, 'payload_ref':payload_ref,
                'run_revision':row['revision'], 'status':row['status'],
                'effect_certainty':proof.get('effect_certainty')}
            if (proof != expected or body.get('turn_id') != turn_id or body.get('owner_id') != owner_id
                    or proof.get('effect_certainty') not in {'confirmed_none','confirmed_applied','unknown'}
                    or row['status']=='completed' and proof['effect_certainty']!='confirmed_applied'
                    or row['status']!='completed' and proof['effect_certainty']=='confirmed_applied'
                    or row['started_at'] is not None and row['status']!='completed' and proof['effect_certainty']!='unknown'
                    or body.get('status') != row['status'] or body.get('exit_code') != row['exit_code']
                    or body.get('usage') != row['usage']):
                raise _failure('external_task_result_invalid', True)
            if row['status'] != 'completed':
                raise ToolProviderFailure('external_task_failed', effect_certainty=proof['effect_certainty'])
            return _dto(body, payload_ref, receipt_ref)
        reserved = True
        revision = row['revision']
        chunks = []
        message_bytes = 0

        def on_started():
            nonlocal started, revision
            # 真实 spawn 已观察，即使 CAS 随后失败也不能声称零副作用。
            started = True
            revision = runs.mark_started(turn_id, owner_id=owner_id, expected_revision=revision).run['revision']

        def receive(event):
            nonlocal message_bytes
            if event.get('kind') == 'message':
                encoded = event['text'].encode('utf-8')
                remaining = max(0, output_limit - message_bytes)
                fragment = encoded[:remaining].decode('utf-8', errors='ignore')
                chunks.append(fragment)
                message_bytes += len(fragment.encode('utf-8'))
            if event_sink is not None:
                event_sink(deepcopy(event))

        lease.validate()
        if before_launch is not None:
            before_launch()
        # 回调可能消耗时间或改变内部配置；真正派发前仍核同一签发 lease。
        lease.validate()
        lease._begin_execution()
        execution_active = True
        result = dispatch_external(plan, context, environment=lease.environment,
            secret_values=lease.secret_values, on_started=on_started, event_sink=receive,
            output_limit=output_limit, on_owner=lease._adopt_owner)
        may_have_started = result.may_have_started
        body = {'turn_id':turn_id, 'owner_id':owner_id, 'status':result.status,
            'exit_code':result.exit_code, 'usage':result.usage, 'message':''.join(chunks),
            'tail':list(result.tail), 'events':list(result.events), 'output_bytes':result.output_bytes}
        payload_ref = turns.get_or_create_immutable_payload(turn_id, RESULT_KIND, body)
        row = runs.finish(turn_id, owner_id=owner_id, expected_revision=revision,
            status=result.status, exit_code=result.exit_code, usage=result.usage).run
        proof = {'turn_id':turn_id, 'owner_id':owner_id, 'payload_ref':payload_ref,
            'run_revision':row['revision'], 'status':result.status,
            'effect_certainty':'confirmed_applied' if result.status=='completed' else 'unknown'
                if started or may_have_started else 'confirmed_none'}
        receipt_ref = turns.get_or_create_immutable_payload(turn_id, RECEIPT_KIND, proof)
        if result.status != 'completed':
            raise _failure('external_task_failed', started or may_have_started)
        return _dto(body, payload_ref, receipt_ref)
    except Exception as error:
        # 物理清理未完成与 started 回报独立，不能释放原槽或声称零副作用。
        if isinstance(error, ProcessCleanupError):
            cleanup_result = error._dispatch_result
            cleanup_body = {'turn_id':turn_id, 'owner_id':owner_id, 'status':'failed',
                'exit_code':cleanup_result.exit_code, 'usage':cleanup_result.usage,
                'message':''.join(chunks), 'tail':list(cleanup_result.tail),
                'events':list(cleanup_result.events), 'output_bytes':cleanup_result.output_bytes}

            def complete_cleanup():
                # 仅同一原 owner 已收口后由 Host 调用；原槽与原服务保幂等 CAS。
                payload_ref = turns.get_or_create_immutable_payload(turn_id, RESULT_KIND, cleanup_body)
                terminal = runs.finish(turn_id, owner_id=owner_id, expected_revision=revision,
                    status='failed', exit_code=cleanup_result.exit_code, usage=cleanup_result.usage).run
                turns.get_or_create_immutable_payload(turn_id, RECEIPT_KIND,
                    {'turn_id':turn_id, 'owner_id':owner_id, 'payload_ref':payload_ref,
                     'run_revision':terminal['revision'], 'status':'failed', 'effect_certainty':'unknown'})

            lease._retain_cleanup(error._cleanup_owner, complete_cleanup)
            raise _failure('external_task_cleanup_incomplete', True) from None
        if reserved and not started and not may_have_started:
            # 确知尚未进入 CLI 时释放本次槽位；回读不完整时仍禁止重启。
            try:
                if turns.get_immutable_payload(turn_id, RESULT_KIND) is None:
                    body = {'turn_id':turn_id, 'owner_id':owner_id, 'status':'failed',
                        'exit_code':None, 'usage':None, 'message':'', 'tail':[],
                        'events':[], 'output_bytes':0}
                    payload_ref = turns.get_or_create_immutable_payload(turn_id, RESULT_KIND, body)
                    row = runs.finish(turn_id, owner_id=owner_id, expected_revision=revision,
                        status='failed', exit_code=None).run
                    turns.get_or_create_immutable_payload(turn_id, RECEIPT_KIND,
                        {'turn_id':turn_id, 'owner_id':owner_id, 'payload_ref':payload_ref,
                         'run_revision':row['revision'], 'status':'failed', 'effect_certainty':'confirmed_none'})
            except Exception:
                raise _failure('external_task_save_failed', False) from None
        if isinstance(error, ToolProviderFailure):
            if reserved and not started and not may_have_started:
                raise _failure(error.error_code, False) from None
            raise
        # 两个原存储不能跨库原子提交；不完整材料保留占位，禁止再次执行。
        raise _failure('external_task_execution_failed', started or may_have_started) from None
    finally:
        if execution_active:
            lease._end_execution()
