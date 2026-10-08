"""Rebuildable, stable confirmed-profile prefixes with fresh source authority."""
from .policies import get
from .policies.types import RankInput
from copy import deepcopy
from uuid import uuid4

from backend.recognition import RecognitionConflict, RecognitionError, WorkScope
from backend.shared.memory_sidecars import decayed_score, utc_now
from ..context_adapter import ContextSelectionError, format_recognition_content
from ..recall_state import is_recall_excluded
from ..source_egress import SourceEgressService
from .budget import text_tokens
from .privacy import is_private_project, privacy_revision

COLLECTION = 'v2_profile_blocks'
VERSION = 'compose@1'
PREFIX = '已确认的画像（仅作本人背景，不作为编号资料；用户确认不等于事实核验）：\n'


def _eligible(records, service, *, rank_reference=None):
    scope = WorkScope('local-user', 'me')
    if is_private_project(records, 'me'):
        return []
    authority = SourceEgressService(records)
    result = []
    for entry in service.retrieval_entries(scope=scope):
        if is_recall_excluded(records, scope, entry['id']):
            continue
        try:
            snapshot = authority.snapshot(scope, [{'type':'recognition', 'id':entry['id'], 'revision':entry['revision']}])
            authority.require(snapshot, 'generation')
            content = format_recognition_content(entry, profile=True)
        except (RecognitionError, ContextSelectionError):
            continue
        item = {'id':entry['id'], 'revision':entry['revision'], 'content':content, 'snapshot':snapshot}
        decorate = getattr(get('rank'), 'decorate', None) if rank_reference is not None else None
        if callable(decorate):
            item = decorate({**item, 'title':'已确认画像'},
                RankInput(0, 1, conditions=entry['conditions'], reference=rank_reference))
        result.append(item)
    return sorted(result, key=lambda item:item['id'])


def _basis(items, records):
    return {'version':VERSION, 'privacy_revision':privacy_revision(records),
            'items':[{key:item[key] for key in ('id', 'revision', 'snapshot')} for item in items]}


def _projection_basis(items):
    # A privacy epoch is authorization, not profile content. Reuse only derived
    # bytes here; every request still obtains and validates fresh source roots.
    return {'version':VERSION,
            'items':[{key:item[key] for key in ('id', 'revision', 'content')} for item in items]}


def confirmed_profile(records, service, *, rank_reference=None, validate_input=None):
    if validate_input is not None and not callable(validate_input):
        raise TypeError('profile_input_validator_invalid')
    return get('compose')(_confirmed_profile, records, service, rank_reference=rank_reference, validate_input=validate_input)


def _confirmed_profile(records, service, *, rank_reference=None, validate_input=None):
    items = _eligible(records, service, rank_reference=rank_reference)
    basis = _basis(items, records)
    projection_basis = _projection_basis(items)
    empty = {'text':'', 'count':0, 'tokens':0, 'items':[], 'basis':basis}
    if not items:
        if validate_input is not None:
            validate_input(deepcopy(empty))
        return empty
    by_id = {item['id']:item for item in items}
    # Serialize first construction: competing readers use the winner's order,
    # even when usage changes without changing recognition revisions.
    with records.begin() as tx:
        existing = next((row.payload for row in tx.list(COLLECTION)
                         if row.payload.get('projection_basis') == projection_basis), None)
        constructing = existing is None
        if constructing:
            now = utc_now()
            def strength(item):
                row = tx.read('v2_usage_insight', item['id'])
                return -(decayed_score(row.payload, now) if row else 0), item['id']
            text, selected = '', []
            for item in sorted(items, key=strength):
                proposed = (text or PREFIX) + '- ' + item['content'] + '\n'
                # Keep each qualified recognition atomic, including conditions.
                if text_tokens(proposed) > 600:
                    break
                text = proposed
                selected.append(item['id'])
            existing = {'projection_basis':projection_basis, 'text':text, 'count':len(selected),
                        'tokens':text_tokens(text), 'selected':selected}
        if validate_input is not None:
            # 校验真实拟复制字段的副本，首次写入与缓存命中共用同一事务边界。
            validate_input(deepcopy({**existing, 'basis':basis,
                'items':[by_id[identity] for identity in existing['selected']]}))
        if constructing:
            tx.put(COLLECTION, 'profile-' + uuid4().hex, existing, expected_revision=0)
            tx.commit()
        else:
            tx.rollback()
    result = {**existing, 'basis':basis, 'items':[by_id[identity] for identity in existing['selected']]}
    render = getattr(get('rank'), 'profile_instruction', None)
    instruction = render(result['items']) if callable(render) else ''
    if instruction:
        result['instruction'] = instruction
    validate_profile(records, service, result)
    return result


def validate_profile(records, service, profile):
    if _basis(_eligible(records, service), records) != profile['basis']:
        raise RecognitionConflict('profile changed during generation')


def bounded_profile(profile, max_items):
    """收紧本轮发送条数，原完整来源依据仍参与生成前后的终检。"""
    if type(max_items) is not int or max_items < 0:
        raise ValueError('invalid_profile_item_limit')
    result = deepcopy(profile)
    items = result.get('items', [])[:max_items]
    text = PREFIX + ''.join('- ' + item['content'] + '\n' for item in items) if items else ''
    result.update(text=text, count=len(items), tokens=text_tokens(text), items=items)
    if 'selected' in result:
        result['selected'] = [item['id'] for item in items]
    # 原画像提示可能包含已移出的条目，必须和本轮实际文本一起重新构建。
    result.pop('instruction', None)
    render = getattr(get('rank'), 'profile_instruction', None)
    instruction = render(items) if items and callable(render) else ''
    if instruction:
        result['instruction'] = instruction
    return result


def profile_messages(profile, messages):
    return get('compose')(_profile_messages, profile, messages)


def _profile_messages(profile, messages):
    if not profile.get('text'):
        return messages
    if profile.get('instruction'):
        # 提示计入原指令；画像前缀的正文、顺序和独立预算保持完整。
        messages = [dict(message) for message in messages]
        system = next((message for message in messages if message['role'] == 'system'), None)
        if system is None:
            messages.insert(0, {'role':'system', 'content':profile['instruction']})
        else:
            system['content'] += '\n' + profile['instruction']
    return [{'role':'system', 'content':profile['text']}, *messages]


def freeze_task_profile(records, request, profile):
    """Bind the derived text to an immutable Turn's authoritative references."""
    with records.begin() as tx:
        tx.put('v2_task_profiles', request['turn_id'], {
            'project_id':request['scope']['project_id'], 'profile':profile,
            'input_refs':request['input']['refs']}, expected_revision=0)
        tx.commit()


def frozen_task_profile(records, service, request):
    row = records.read('v2_task_profiles', request['turn_id'])
    if row is None:
        return {'text':'', 'count':0, 'tokens':0}
    if (row.revision != 1 or row.payload['project_id'] != request['scope']['project_id']
            or row.payload['input_refs'] != request['input']['refs']):
        raise RecognitionConflict('task profile binding changed')
    profile = row.payload['profile']
    material_refs = request['privacy']['material_refs']
    snapshots = request['privacy']['source_snapshots']
    for item in profile['items']:
        if ({'type':'recognition', 'id':item['id'], 'revision':item['revision'], 'project_id':'me'} not in material_refs
                or item['snapshot'] not in snapshots):
            raise RecognitionConflict('task profile authority changed')
    validate_profile(records, service, profile)
    return profile
