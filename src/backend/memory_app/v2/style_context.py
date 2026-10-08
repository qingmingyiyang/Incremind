"""沿原确认、召回与来源权限投影可重建的稳定写法块。"""
from uuid import uuid4
import json

from backend.recognition import RecognitionConflict, RecognitionError, WorkScope
from backend.shared.memory_sidecars import decayed_score, utc_now
from ..context_adapter import ContextSelectionError, format_recognition_content
from ..recall_state import is_recall_excluded
from ..source_egress import SourceEgressService
from .budget import text_tokens
from .insights import insight_view
from .policies import get, version as policy_version
from .policies.types import ScopeInput
from .privacy import is_private_project, privacy_revision

COLLECTION = 'v2_style_blocks'
TASK_COLLECTION = 'v2_task_styles'
_INPUT_FIELDS = ('version','text','count','tokens','selected')


def _eligible(records, service, project, scene, scope_version):
    authority, result = SourceEgressService(records), []
    visibility = get('scope', version=scope_version)
    for own in sorted({project, 'me'}):
        if is_private_project(records, own):
            continue
        scope = WorkScope('local-user', own)
        for entry in service.retrieval_entries(scope=scope):
            if is_recall_excluded(records, scope, entry['id']):
                continue
            view = insight_view(records, scope, entry['id'], service=service)
            if view is None or (own != 'me' and not visibility(ScopeInput(scene, view['scene']))):
                continue
            try:
                snapshot = authority.snapshot(scope, [{'type':'recognition',
                    'id':entry['id'], 'revision':entry['revision']}])
                authority.require(snapshot, 'generation')
                text = format_recognition_content(entry, profile=True)
            except (RecognitionError, ContextSelectionError):
                continue
            result.append({'id':entry['id'], 'revision':entry['revision'],
                'project_id':own, 'scene':view['scene'], 'content':entry['content'],
                'conditions':list(entry.get('conditions', ())), 'text':text, 'snapshot':snapshot})
    return sorted(result, key=lambda item:item['id'])


def _projection(items, project, scene, version, scope_version):
    return {'project_id':project, 'scene':scene, 'version':version, 'scope_version':scope_version,
        'items':[{key:item[key] for key in ('id','revision','project_id','scene','content','conditions','text')}
                 for item in items]}


def _basis(items, records, projection):
    return {'projection':projection, 'privacy_revision':privacy_revision(records),
        'sources':[{key:item[key] for key in ('id','revision','project_id','snapshot')} for item in items]}


def _cached_result(payload):
    # 只复用经保存版本重新校验的派生字节；缓存从不授予来源权限。
    rows = payload['projection_basis']['items']
    if len(rows) != len(payload['strengths']):
        raise RecognitionConflict('style_cache_changed')
    calculated = get('style', version=payload['projection_basis']['version'])(
        [{**item, 'strength':strength} for item, strength in zip(rows, payload['strengths'])],
        estimate_tokens=text_tokens)
    if any(calculated[key] != payload[key] for key in ('text','count','tokens','selected')):
        raise RecognitionConflict('style_cache_changed')
    return calculated


def confirmed_style(records, service, project, *, scene=None, version='@1', scope_version=None):
    """同一认识修订集合使用首个原子投影的排序，每次刷新外发来源资格。"""
    policy = get('style', version=version)
    scope_version = scope_version or policy_version('scope')
    items = _eligible(records, service, project, scene, scope_version)
    projection = _projection(items, project, scene, version, scope_version)
    basis = _basis(items, records, projection)
    empty = {'project_id':project, 'scene':scene, 'version':version, 'scope_version':scope_version,
        'text':'', 'count':0, 'tokens':0, 'items':[], 'selected':[], 'basis':basis, 'cache_id':None}
    if not items:
        return empty
    with records.begin() as tx:
        row = next((row for row in tx.list(COLLECTION)
                    if row.payload.get('projection_basis') == projection), None)
        if row is None:
            now = utc_now()
            strengths = []
            for item in items:
                usage = tx.read('v2_usage_insight', item['id'])
                strengths.append(max(0, decayed_score(usage.payload, now)) if usage else 0)
            result = policy([{**item,'strength':strength} for item,strength in zip(items,strengths)],
                estimate_tokens=text_tokens)
            payload = {'projection_basis':projection, 'strengths':strengths, **result}
            row = tx.put(COLLECTION, 'style-'+uuid4().hex, payload, expected_revision=0)
            tx.commit()
        else:
            tx.rollback()
    if row.revision != 1:
        raise RecognitionConflict('style_cache_changed')
    result = _cached_result(row.payload)
    by_id = {item['id']:item for item in items}
    block = {**empty, **result, 'cache_id':row.object_id,
        'items':[by_id[item['id']] for item in result['selected']]}
    validate_style(records, service, block)
    return block


def validate_style(records, service, block):
    """校验当前确认、遗忘、场景、私密及完整来源，拒绝旧授权或改写的缓存。"""
    get('style', version=block['version'])
    items = _eligible(records, service, block['project_id'], block['scene'], block['scope_version'])
    projection = _projection(items, block['project_id'], block['scene'], block['version'], block['scope_version'])
    if _basis(items, records, projection) != block['basis']:
        raise RecognitionConflict('style_authority_changed')
    if block['cache_id'] is None:
        if items or block['text'] or block['count'] or block['tokens'] or block['items']:
            raise RecognitionConflict('style_binding_changed')
        return
    row = records.read(COLLECTION, block['cache_id'])
    if row is None or row.revision != 1 or row.payload.get('projection_basis') != projection:
        raise RecognitionConflict('style_cache_changed')
    result = _cached_result(row.payload)
    by_id = {item['id']:item for item in items}
    selected = [by_id[item['id']] for item in result['selected']]
    if block['items'] != selected or any(block[key] != result[key] for key in ('text','count','tokens','selected')):
        raise RecognitionConflict('style_binding_changed')


def style_messages(block, messages):
    """写法为空时不增加提示词或空行。"""
    return [{'role':'system','content':block['text']}, *messages] if block.get('text') else messages


def style_input(block):
    """只有选中的写法字节进入模型输入；未选资料与权限依据只在旁路保存。"""
    return {key:block[key] for key in _INPUT_FIELDS}


def freeze_task_style(records, request, block):
    """沿原画像冻结方式，绑定产品不可变输入及其原始来源引用。"""
    frozen = json.loads(request['input']['text']).get('style_input')
    if request['scope']['project_id'] != block['project_id'] or frozen != style_input(block):
        raise RecognitionConflict('task_style_input_changed')
    with records.begin() as tx:
        tx.put(TASK_COLLECTION, request['turn_id'], {'project_id':block['project_id'],
            'input_refs':request['input']['refs'], 'style':block}, expected_revision=0)
        tx.commit()


def frozen_task_style(records, service, request):
    """回读绑定的保存版本与字节，发送前仍需复验当前资格和原引用。"""
    frozen = json.loads(request['input']['text']).get('style_input')
    row = records.read(TASK_COLLECTION, request['turn_id'])
    if frozen is None and row is None:
        return {'text':'','count':0,'tokens':0,'items':[]}
    if (row is None or row.revision != 1 or row.payload['project_id'] != request['scope']['project_id']
            or row.payload['input_refs'] != request['input']['refs']):
        raise RecognitionConflict('task_style_binding_changed')
    block = row.payload['style']
    if frozen != style_input(block):
        raise RecognitionConflict('task_style_input_changed')
    for item in block['items']:
        material = {'type':'recognition','id':item['id'],'revision':item['revision'],
            'project_id':item['project_id']}
        if material not in request['privacy']['material_refs'] or item['snapshot'] not in request['privacy']['source_snapshots']:
            raise RecognitionConflict('task_style_authority_changed')
    validate_style(records, service, block)
    return block
