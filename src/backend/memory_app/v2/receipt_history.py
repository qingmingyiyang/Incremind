import base64
import binascii
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import json
import math
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backend.recognition import RecognitionError, WorkScope
from backend.shared.secret_detection import redact_secrets
from ..kernel.receipt_projection import PURPOSES, aggregate_cost, aggregate_usage, kernel_call_groups
from ..original_sources import resolve_turn_material
from ..source_egress import SourceEgressService
from .external_context import delivery_receipts
from .task_egress import task_call_groups


LABELS = {'ask': '问', 'organize': '整理', 'insight': '认识', 'task': '干活', 'asr': '转写',
          'embedding': '向量', 'rerank': '重排', 'vision': '识图', 'search': '搜索', 'external': '外部 agent'}
_PURPOSE_IDS = {value: key for key, value in LABELS.items()}
LEGACY_FIELDS = ('at', 'purpose', 'model', 'items', 'usage', 'duration', 'model_cost')


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result.astimezone(timezone.utc) if result.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def month_window(month=None, zone='UTC', *, now=None):
    try:
        if not isinstance(zone, str) or len(zone) > 100:
            raise ValueError('receipt_timezone_invalid')
        tz = ZoneInfo(zone)
        month = month or (now or datetime.now(timezone.utc)).astimezone(tz).strftime('%Y-%m')
        if not isinstance(month, str) or re.fullmatch(r'[0-9]{4}-(?:0[1-9]|1[0-2])', month) is None:
            raise ValueError('receipt_month_invalid')
        year, position = map(int, month.split('-'))
        start = datetime(year, position, 1, tzinfo=tz)
        end = datetime(year + (position == 12), 1 if position == 12 else position + 1, 1, tzinfo=tz)
        return {'month': month, 'timezone': zone, 'start': start.isoformat(), 'end': end.isoformat(),
                'start_inclusive': True, 'end_exclusive': True}
    except (ZoneInfoNotFoundError, ValueError, OverflowError):
        raise ValueError('receipt_window_invalid') from None


def cost_totals(values):
    amounts, known, unknown = {}, 0, 0
    for cost in values:
        try:
            if (not isinstance(cost, dict) or cost.get('observed_only') or not isinstance(cost.get('currency'), str)
                    or re.fullmatch(r'[A-Z]{3}', cost['currency']) is None or type(cost.get('amount')) not in (int, float, str)):
                raise ValueError('cost_unknown')
            text = str(cost['amount'])
            if re.fullmatch(r'(?:0|[1-9][0-9]*)(?:\.[0-9]+)?', text) is None:
                raise ValueError('cost_unknown')
            amount = Decimal(text)
            if not amount.is_finite() or amount < 0:
                raise ValueError('cost_unknown')
        except (ValueError, InvalidOperation):
            unknown += 1
            continue
        known += 1
        amounts.setdefault(cost['currency'], []).append(amount)
    totals = []
    for currency, values in sorted(amounts.items()):
        with localcontext() as context:
            context.prec = max(28, max(len(value.as_tuple().digits) + abs(value.as_tuple().exponent) for value in values) + len(str(len(values))) + 2)
            totals.append({'currency': currency, 'amount': format(sum(values, Decimal(0)), 'f')})
    return {'state': 'partial' if known and unknown else 'known' if known else 'unknown',
            'known_count': known, 'unknown_count': unknown,
            'amounts': totals}


def usage_view(usage, *, partial=False):
    if not isinstance(usage, dict) or not usage:
        return {'input': None, 'output': None, 'state': 'unknown'}
    incoming = number(usage.get('input_tokens', usage.get('prompt_tokens')))
    outgoing = number(usage.get('output_tokens', usage.get('completion_tokens')))
    state = 'unknown' if incoming is None and outgoing is None else 'partial' if (
        partial or usage.get('observed_only') or incoming is None or outgoing is None) else 'known'
    return {'input': incoming, 'output': outgoing, 'state': state}


def usage_totals(calls):
    result = aggregate_usage(calls)
    if not result:
        return None
    partial = any(call.get('usage_status') in {'partial', 'not_recorded', 'unknown'}
        or not isinstance(call.get('usage'), dict) or call['usage'].get('observed_only')
        or any(type(call['usage'].get(key)) is not int or call['usage'][key] < 0
               for key in ('input_tokens', 'output_tokens')) for call in calls)
    if not partial:
        return result
    # 原 owner 分别累计实际出现的计数；不从互补的部分观察量推导完整总量。
    observed = {}
    for key in ('input_tokens', 'output_tokens', 'total_tokens'):
        values = [{'usage': {key: call['usage'][key]}} for call in calls
            if isinstance(call.get('usage'), dict) and type(call['usage'].get(key)) is int
            and call['usage'][key] >= 0]
        if values:
            observed[key] = aggregate_usage(values)[key]
    return {**observed, 'observed_only': True} if observed else None


def _id(*parts):
    return 'receipt-' + base64.urlsafe_b64encode(json.dumps(parts, ensure_ascii=True, separators=(',', ':')).encode()).decode().rstrip('=')


def wire_costs(calls):
    return [cost for call in calls for cost in (
        [wire.get('cost') for wire in call['wire_attempts']] if isinstance(call.get('wire_attempts'), list) else [call.get('cost')])]


def _record(kind, identity, at, purpose, *, model=None, items=None, usage=None, duration=None, costs=None,
            project_id=None, turn_id=None, calls=None, materials=None, snapshots=None, excluded=None, sub=None, status=None):
    calls = calls or []
    usage = usage if isinstance(usage, dict) else None
    costs = costs if costs is not None else [None]
    total = cost_totals(costs)
    complete = total['amounts'][0] if total['state'] == 'known' and len(total['amounts']) == 1 else None
    return {'id': _id(kind, identity, sub), 'at': at if isinstance(at, str) else None,
            'purpose': purpose, 'purpose_id': _PURPOSE_IDS.get(purpose, 'other'),
            'model': redact_secrets(model) if isinstance(model, str) else None, 'items': number(items),
            'duration': number(duration), 'usage': usage_view(usage,
                partial=any(call.get('usage_status') == 'partial' for call in calls)),
            'model_cost': complete, 'cost': total, 'status': status,
            'origin': {'kind': kind, 'record_id': identity, 'turn_id': turn_id, 'project_id': project_id},
            'call_count': len(calls) if kind in {'kernel', 'task', 'external'} else None,
            'wire_attempt_count': sum(len(call['wire_attempts']) for call in calls) if all(
                isinstance(call.get('wire_attempts'), list) for call in calls) and kind in {'kernel', 'task', 'external'} else None,
            '_calls': calls, '_usage': usage, '_costs': costs,
            '_materials': materials, '_snapshots': snapshots or [], '_excluded': excluded}


def public_row(row):
    return {key: value for key, value in row.items() if not key.startswith('_')}


def legacy_row(row):
    value = {key: row.get(key) for key in LEGACY_FIELDS}
    usage = row['_usage']
    value['usage'] = None if not usage or usage.get('observed_only') else {
        'input': number(usage.get('input_tokens', usage.get('prompt_tokens'))),
        'output': number(usage.get('output_tokens', usage.get('completion_tokens')))}
    return value


class ReceiptHistory:
    def __init__(self, records, runtime_root):
        self.records, self.root = records, runtime_root
        self.coverage = {'external_unverified': 0, 'external_incomplete': 0}

    def collect(self, *, strict_external=True, include_attempts=True, scope='egress', legacy=False):
        if scope not in {'egress', 'models'}:
            raise ValueError('receipt_scope_invalid')
        result, covered, ask_ids, organized = [], set(), set(), {}
        if self.root is not None:
            external = delivery_receipts(self.records, self.root, include_identity=True, strict=strict_external)
            if external == [] and not strict_external:
                external = {'rows': [], 'unverified_count': 0, 'incomplete_count': 0}
            self.coverage = {'external_unverified': external['unverified_count'], 'external_incomplete': external['incomplete_count']}
            for row in external['rows']:
                value = _record('external', row['id'], row['at'], '外部 agent', items=row['items'],
                    project_id=row['project_id'], turn_id=row['turn_id'], materials=row['materials'],
                    snapshots=row['snapshots'], excluded=row['excluded'], status='delivered')
                value['client'] = row['client']
                result.append(value)
        for row in self.records.list('v2_egress_receipts'):
            body = row.payload
            result.append(_record('historical', row.object_id, body.get('at'), '认识', model=body.get('model'),
                items=body.get('items'), usage=body.get('usage'), status='historical'))
        kernel = kernel_call_groups(self.root, records=self.records, include_attempts=include_attempts, remote_only=scope != 'models')
        call_owners = {(call['turn_id'], call['model_request_id']): call for group in kernel for call in group['calls']}
        for group in kernel:
            if legacy and group['kind'] not in PURPOSES:
                continue
            calls, request = group['calls'], group['request']
            if not calls:
                continue
            covered.update((call['turn_id'], call['model_request_id']) for call in calls)
            ask = (group['answer'] or {}).get('receipt', {}).get('ask', {})
            if ask.get('egress_receipt_id'):
                ask_ids.add(ask['egress_receipt_id'])
            privacy = request.get('privacy', {})
            if group['kind'] == 'memory.organize':
                for item in privacy.get('material_refs', []):
                    if item.get('type') == 'original_item':
                        organized.setdefault(item['id'], []).append(request.get('created_at', ''))
            purposes = {purpose for call in calls for purpose in (call.get('egress') or {}).get('settings_revision', {})}
            base_label = PURPOSES.get(group['kind'], '其他')
            label = {'rerank': '重排', 'embedding': '向量', 'asr': '转写'}.get(next(iter(purposes)), base_label) if len(purposes) == 1 else base_label
            value = _record('kernel', group['turn_id'], max(call['completed_at'] for call in calls), label,
                model=' · '.join(sorted({call['model_id'] for call in calls if call.get('model_id')})) or None,
                usage=usage_totals(calls), duration=sum(call['duration_ms'] for call in calls) / 1000,
                costs=wire_costs(calls), project_id=group['project_id'], turn_id=group['turn_id'],
                calls=calls, materials=privacy.get('material_refs'), snapshots=privacy.get('source_snapshots'),
                excluded=privacy.get('excluded_refs'), sub=group['project_id'], status='recorded')
            value['_request'] = request
            value['materials_basis'] = 'frozen_turn_inputs'
            if group['kind'] == 'project.answer':
                value['_materials'] = value['_excluded'] = None
                value['materials_basis'] = 'answer_input_requires_verification'
            # 旧数组费用继续使用原完整费用 owner；新读模型另保留部分已知金额。
            value['model_cost'] = aggregate_cost(calls)
            result.append(value)
        for task in self.records.list('recognition_tasks'):
            for calls in task_call_groups(self.records, self.root, task_id=task.object_id):
                calls = [call_owners.get((call['turn_id'], call['model_request_id']), call)
                         for call in calls if (call['turn_id'], call['model_request_id']) not in covered]
                if not calls:
                    continue
                value = _record('task', task.object_id, max(call['completed_at'] for call in calls), '干活', calls=calls,
                    model=' · '.join(sorted({call['model_id'] for call in calls if call.get('model_id')})) or None,
                    usage=usage_totals(calls), duration=sum(call['duration_ms'] for call in calls) / 1000,
                    costs=wire_costs(calls), project_id=task.payload.get('project_id'),
                    turn_id=task.payload.get('turn_id'), status='recorded')
                value['model_cost'] = aggregate_cost(calls)
                result.append(value)
                covered.update((call['turn_id'], call['model_request_id']) for call in calls)
        for row in self.records.list('workspace_ask_receipts'):
            body, target = row.payload, row.payload.get('target', {})
            if row.object_id in ask_ids or not isinstance(target, dict) or target.get('execution_location') != 'remote':
                continue
            sources = body.get('sources')
            result.append(_record('ask', row.object_id, body.get('created_at'), '问', model=target.get('model'),
                items=len(sources) if isinstance(sources, list) else None, usage=body.get('usage'),
                project_id=body.get('project_id'), status=body.get('status') if body.get('status') in {
                    'completed', 'failed', 'consented', 'started', 'cancelled'} else 'historical'))
        for row in self.records.list('workspace_items'):
            receipts = row.payload.get('remote_processing_receipts', [])
            if not isinstance(receipts, list):
                continue
            for position, receipt in enumerate(receipts):
                if not isinstance(receipt, dict):
                    continue
                for label, key in (('整理', 'generation'), ('转写', 'asr')):
                    following = receipts[position + 1].get('consented_at', '') if position + 1 < len(receipts) and isinstance(receipts[position + 1], dict) else ''
                    if key == 'generation' and any(receipt.get('consented_at', '') <= at and (not following or at < following)
                                                  for at in organized.get(row.object_id, [])):
                        continue
                    if isinstance(receipt.get(key), dict):
                        result.append(_record('intake', row.object_id, receipt.get('consented_at'), label,
                            model=receipt[key].get('model'), items=1, project_id=row.payload.get('project_id'),
                            sub=[position, key], status='historical'))
        return sorted(result, key=self._key, reverse=True)

    @staticmethod
    def _key(row):
        at = timestamp(row['at'])
        return (at.isoformat() if at is not None else '', row['id'])

    @staticmethod
    def _scope(rows, month, zone):
        if month is None:
            month_window(zone=zone)
            return rows, None
        window = month_window(month, zone)
        start, end = timestamp(window['start']), timestamp(window['end'])
        result = []
        for row in rows:
            if row['_calls']:
                calls = []
                for call in row['_calls']:
                    wires = call.get('wire_attempts')
                    if not isinstance(wires, list):
                        continue
                    selected = [wire for wire in wires if (at := timestamp(wire.get('completed_at'))) is not None and start <= at < end]
                    if not selected:
                        continue
                    usage = usage_totals(selected)
                    calls.append({**call, 'wire_attempts': selected, 'usage': usage,
                        'usage_status': 'partial' if usage and usage.get('observed_only') else 'recorded' if usage else 'not_recorded',
                        'cost': aggregate_cost(selected), 'completed_at': max(selected, key=lambda wire: timestamp(wire['completed_at']))['completed_at'],
                        'duration_ms': sum(wire['duration_ms'] for wire in selected)})
                if not calls:
                    continue
                usage, costs = usage_totals(calls), wire_costs(calls)
                result.append({**row, '_calls': calls, '_usage': usage, '_costs': costs,
                    'at': max(calls, key=lambda call: timestamp(call['completed_at']))['completed_at'],
                    'call_count': len(calls), 'wire_attempt_count': sum(len(call['wire_attempts']) for call in calls),
                    'model': redact_secrets(' · '.join(sorted({call['model_id'] for call in calls if call.get('model_id')}))) or None,
                    'usage': usage_view(usage), 'cost': cost_totals(costs), 'model_cost': aggregate_cost(calls),
                    'duration': sum(call['duration_ms'] for call in calls) / 1000})
            elif (at := timestamp(row['at'])) is not None and start <= at < end:
                result.append(row)
        return sorted(result, key=ReceiptHistory._key, reverse=True), window

    def summary(self, *, month=None, zone='UTC', scope='egress'):
        window = month_window(month, zone)
        all_rows = self.collect(scope=scope)
        rows, _window = self._scope(all_rows, window['month'], zone)
        model_rows = [row for row in rows if row['_calls']]
        cost_rows = model_rows if scope == 'models' else rows
        purposes = []
        for identity in sorted({row['purpose_id'] for row in cost_rows}):
            selected = [row for row in cost_rows if row['purpose_id'] == identity]
            purposes.append({'id': identity, 'label': LABELS.get(identity, '其他'), **self._totals(selected)})
        coverage = {**self.coverage, 'scope': 'all_history', 'undated': sum(timestamp(row['at']) is None for row in all_rows),
                    'model_attempts': 'terminal_receipts_only', 'undated_attempts': sum(
                        timestamp(wire.get('completed_at')) is None for row in all_rows for call in row['_calls']
                        for wire in call.get('wire_attempts', [])),
                    'unattributed_receipts': sum(row['call_count'] is None for row in all_rows)}
        return {'window': window, 'scope': scope, **self._totals(model_rows if scope == 'models' else rows),
                'cost': self._totals(cost_rows)['cost'], 'usage': self._totals(cost_rows)['usage'],
                'external_delivery_count': sum(row['origin']['kind'] == 'external' for row in rows),
                'model_receipt_count': len(model_rows), 'purposes': purposes, 'coverage': coverage,
                'count_basis': 'verified_receipts', 'call_basis': 'registered_model_requests', 'cost_basis': 'wire_completed_at'}

    @staticmethod
    def _totals(rows):
        return {'receipt_count': len(rows), 'model_call_count': sum(row['call_count'] or 0 for row in rows),
                'wire_attempt_count': sum(row['wire_attempt_count'] or 0 for row in rows),
                'call_count_unknown_receipts': sum(row['call_count'] is None for row in rows),
                'attempt_count_unknown_receipts': sum(row['wire_attempt_count'] is None for row in rows),
                'cost': cost_totals([cost for row in rows for cost in row['_costs']]),
                'usage': usage_view(usage_totals([{'usage': row['_usage'],
                    'usage_status': row['usage']['state']} for row in rows]))}

    def page(self, *, month=None, zone='UTC', purpose=None, limit=25, cursor=None, scope='egress'):
        if purpose is not None and purpose not in {*LABELS, 'other'}:
            raise ValueError('receipt_purpose_invalid')
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('receipt_limit_invalid')
        rows, window = self._scope(self.collect(scope=scope), month, zone)
        if scope == 'models':
            rows = [row for row in rows if row['_calls']]
        purposes = [{'id': identity, 'label': LABELS.get(identity, '其他')} for identity in sorted({row['purpose_id'] for row in rows})]
        rows = [row for row in rows if purpose is None or row['purpose_id'] == purpose]
        total = len(rows)
        if cursor:
            try:
                if not isinstance(cursor, str) or len(cursor) > 2048:
                    raise ValueError('cursor_invalid')
                value = json.loads(base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True))
                if value['filter'] != [month, zone, purpose, scope] or not isinstance(value['before'], list) or len(value['before']) != 2:
                    raise ValueError('cursor_invalid')
                before = tuple(value['before'])
                rows = [row for row in rows if self._key(row) < before]
            except (ValueError, TypeError, KeyError, binascii.Error):
                raise ValueError('receipt_cursor_invalid') from None
        selected = rows[:limit]
        after = None
        if len(rows) > limit:
            after = base64.urlsafe_b64encode(json.dumps({'filter': [month, zone, purpose, scope],
                'before': self._key(selected[-1])}, separators=(',', ':')).encode()).decode().rstrip('=')
        return {'rows': [public_row(row) for row in selected], 'total': total, 'next_cursor': after,
                'window': window, 'timezone': zone, 'scope': scope, 'purposes': purposes, 'coverage': self.coverage}

    def detail(self, identity, *, month=None, zone='UTC', scope='egress'):
        row = next((row for row in self.collect(scope=scope) if row['id'] == identity and (scope != 'models' or row['_calls'])), None)
        if row is None:
            return None
        scoped, window = self._scope([row], month, zone)
        if not scoped:
            return None
        row = scoped[0]
        if row.get('_request', {}).get('desired_outcome') == 'project.answer':
            groups = kernel_call_groups(self.root, records=self.records, turn_id=row['origin']['turn_id'],
                include_answer_input=True, remote_only=scope != 'models')
            group = next((group for group in groups if group['turn_id'] == row['origin']['turn_id']
                          and group['project_id'] == row['origin']['project_id']), None)
            proof = ((group or {}).get('answer_input') or {}).get('material_proof', {})
            if isinstance(proof.get('privacy'), dict):
                privacy = proof['privacy']
                row['_materials'], row['_snapshots'], row['_excluded'] = privacy.get('material_refs'), privacy.get('source_snapshots', []), privacy.get('excluded_refs')
                row['materials_basis'] = 'verified_answer_input'
        result = public_row(row)
        result['scope'] = scope
        result['window'] = window
        result['calls'] = [{
            'turn_id': call['turn_id'], 'model_request_id': call['model_request_id'],
            'receipt_id': call.get('receipt_id'),
            'model': redact_secrets(call['model_id']) if isinstance(call.get('model_id'), str) else None,
            'at': call.get('completed_at'), 'status': call.get('status'), 'duration_ms': call.get('duration_ms'),
            'usage': usage_view(call.get('usage'), partial=call.get('usage_status') == 'partial'),
            'cost': cost_totals(wire_costs([call])),
            'attempts': [{
                'id': wire['attempt_id'], 'number': wire['attempt_number'], 'at': wire['completed_at'],
                'status': wire['status'], 'usage': usage_view(wire.get('usage'), partial=wire.get('usage_status') == 'partial'),
                'cost': cost_totals([wire.get('cost')]),
            } for wire in call.get('wire_attempts', [])],
        } for call in row['_calls']]
        result['materials'] = None if row['_materials'] is None else [self._material(item, row) for item in row['_materials']]
        result['excluded'] = None if row['_excluded'] is None else [{
            'id': item.get('object_id'), 'type': item.get('kind'), 'project_id': item.get('project_id'),
            'availability': 'unavailable', 'reason': 'historical_revision_not_recorded',
        } for item in row['_excluded']]
        return result

    def _material(self, descriptor, receipt):
        fields = ('type', 'id', 'revision', 'project_id')
        result = {key: descriptor.get(key) for key in fields}
        result.update(availability='unavailable', reason='material_identity_unavailable')
        if set(descriptor) == {*fields, 'namespace'} and isinstance(descriptor['namespace'], str) and descriptor['namespace']:
            return {**result, 'namespace': descriptor['namespace'], 'reason': 'material_authority_unavailable'}
        try:
            if set(descriptor) != set(fields) or not isinstance(descriptor['project_id'], str):
                return result
            # 跨根材料必须由原资格 owner 提供，个人库中同名编号不构成读取资格。
            if descriptor['project_id'].startswith('shared-') or any(
                    isinstance(ref.get('uri'), str) and ref['uri'].startswith('crp://material-origin/')
                    for ref in receipt.get('_request', {}).get('input', {}).get('refs', [])):
                result['reason'] = 'material_authority_unavailable'
                return result
            scope = WorkScope('local-user', descriptor['project_id'])
            material, roots = resolve_turn_material(self.records, scope, descriptor)
            current = SourceEgressService(self.records).snapshot(scope, roots)
            frozen = next((item for item in receipt['_snapshots'] if item.get('scope') == current['scope']
                           and item.get('roots') == current['roots']), None)
            if frozen is None:
                return result
            for node in current['nodes']:
                if node['type'] == 'original_source' and not any(
                        prior.get('type') == node['type'] and prior.get('id') == node['id']
                        and prior.get('incarnation') == node.get('incarnation') for prior in frozen.get('nodes', [])):
                    return result
            payload = material['payload']
            title = payload.get('title') or payload.get('original_name') or payload.get('filename')
            result.update(availability='available', reason=None,
                title=redact_secrets(title) if isinstance(title, str) else None,
                current_private=not bool(next((node['effective_purposes'] for node in current['nodes']
                    if node['type'] == roots[0]['type'] and node['id'] == roots[0]['id']), [])))
            return result
        except (RecognitionError, ValueError, KeyError, TypeError):
            return result
