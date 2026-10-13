"""在既有整理 Turn 和确认事务中保存材料分析，不建立新的证据权限。"""
from copy import deepcopy
from contextvars import copy_context
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
from pathlib import Path

from ..model_config import ModelConfigurationError
from .policies import get, override, version
from .policies.material_analysis import parse_plan, parse_claims, parse_temporal, parse_synthesis
from .source_sections import analysis_body_for_item


ANALYSES = 'v2_material_analyses'
PUBLICATIONS = 'v2_material_analysis_publications'


def selected_flow(organize):
    """旧 episode 优先于当前 ACTIVE，防止恢复时半途更换流水线。"""
    saved = organize.records.read(ANALYSES, organize.item_id)
    if saved is not None:
        return True
    prefix = 'organize-' + organize.item_id + '-'
    for step in organize.records.list('workspace_organize_steps'):
        if step.object_id.startswith(prefix):
            request = organize.store.get_request(step.payload['turn_id'])
            if request is None or request.get('policy_versions', {}).get('organize', '@1') != '@2':
                return False
    return version('organize') == '@2'


_UNFROZEN = object()


def _auxiliary_binding(models):
    freeze = getattr(models, 'freeze_auxiliary_binding', None)
    return deepcopy(freeze()) if callable(freeze) else None


def _limits(organize, *, binding=_UNFROZEN):
    models = organize.models
    binding = _auxiliary_binding(models) if binding is _UNFROZEN else binding
    if binding is not None:
        models = models.for_auxiliary(binding)
    public = models.public()['generation']
    read = getattr(models, 'generation_budget_limits', None)
    if not callable(read):
        raise ModelConfigurationError('material_analysis_capacity_unavailable')
    limits = read(expected_revision=public['revision'])
    if limits is None:
        raise ModelConfigurationError('material_analysis_capacity_unavailable')
    policy = get('material_analysis', version='@1')
    input_limit, output = policy.budget(limits)
    from urllib.parse import urlsplit
    hostname = urlsplit(public.get('base_url') or '').hostname
    local = hostname in {'localhost', '127.0.0.1', '::1'} or not public.get('allow_remote', False)
    return input_limit, output, policy.parallelism(local=local)


def _episode(organize, original_title):
    auxiliary_binding = _auxiliary_binding(organize.models)
    with organize.records.begin() as tx:
        organize._guard_write(tx)
        row = tx.read('workspace_items', organize.item_id)
        if row is None or row.payload.get('source_text') != organize.source:
            raise ModelConfigurationError('remote_processing_target_changed')
        saved = tx.read(ANALYSES, organize.item_id)
        body_range = analysis_body_for_item(tx, row)
        if saved is None:
            selected = version('material_analysis')
            policies = {'organize': '@2', 'material_analysis': selected}
            if selected in {'@2', '@3', '@4'}:
                policies['context_preparation'] = '@1'
            payload = {'item_id': organize.item_id, 'project_id': organize.project_id,
                'source_text': organize.source, 'original_title': original_title,
                'body_range': body_range,
                'auxiliary_binding': auxiliary_binding,
                'owner_revision': row.revision, 'policy_versions': policies,
                'state': 'processing', 'plan_stage': None, 'analysis_stages': [], 'temporal_stages': []}
            saved = tx.put(ANALYSES, organize.item_id, payload, expected_revision=0)
            tx.commit()
        if (saved.payload['source_text'] != organize.source or saved.payload['project_id'] != organize.project_id
                or saved.payload['policy_versions'] not in (
                    {'organize': '@2', 'material_analysis': '@1'},
                    {'organize': '@2', 'material_analysis': '@2', 'context_preparation': '@1'},
                    {'organize': '@2', 'material_analysis': '@3', 'context_preparation': '@1'},
                    {'organize': '@2', 'material_analysis': '@4', 'context_preparation': '@1'})
                or saved.payload['body_range'] != body_range):
            raise ModelConfigurationError('remote_processing_target_changed')
        if saved.payload.get('auxiliary_binding') != auxiliary_binding:
            raise ModelConfigurationError('fast_model_binding_changed')
        return deepcopy(saved.payload)


def run_analysis(organize, *, original_title, local_model=False):
    """先规划后分析，连续覆盖实际原文；小材料每种阶段只需一次调用。"""
    episode = _episode(organize, original_title)
    if episode['policy_versions'].get('material_analysis') in {'@2', '@3', '@4'}:
        from .material_analysis_fallback import run_fallback
        input_limit, output, workers = _limits(organize, binding=episode['auxiliary_binding'])
        return run_fallback(organize, episode, input_limit=input_limit, output=output, workers=workers)
    policy = get('material_analysis', version='@1')
    # localhost 只描述传输位置，不能据此推断模型容量或强加旧768输出上限。
    input_limit, output, workers = _limits(organize, binding=episode['auxiliary_binding'])
    metadata, stage_keys = [], []
    body = episode['body_range']
    source = organize.source[body['start']:body['end']]

    def validate():
        organize.validate_current()
        if _auxiliary_binding(organize.models) != episode['auxiliary_binding']:
            raise ModelConfigurationError('fast_model_binding_changed')
        current = organize.records.read('workspace_items', organize.item_id)
        if current is None or analysis_body_for_item(organize.records, current) != body:
            raise ValueError('material_analysis_binding_changed')

    def complete(stage, messages, parse, *, owner=None, record=True):
        with override(**episode['policy_versions']):
            raw, meta = (owner or organize).complete(messages, stage=stage, max_tokens=output,
                validate_current=validate, validate_output=parse)
        parsed = parse(raw)
        if not record:
            return parsed, meta, 'organize-' + organize.item_id + '-' + stage
        metadata.append(meta)
        stage_keys.append('organize-' + organize.item_id + '-' + stage)
        return parsed

    title = episode['original_title']
    plan_pieces = policy.pieces(source,
        lambda text: policy('plan', original_title=title, source=text), input_limit=input_limit)
    plans = [complete('material-plan-' + str(index), policy('plan', original_title=title, source=piece), parse_plan)
        for index, (_offset, piece) in enumerate(plan_pieces, 1)]
    if len(plans) == 1:
        plan = plans[0]
    else:
        # 汇总只决定分析角度；所有原文仍会送入后续分析，不用摘要替换原件。
        messages = policy('plan', original_title=title, material_plans=plans)
        from backend.shared.llm.message_metadata import _estimate_input_tokens
        if _estimate_input_tokens(messages) > input_limit:
            raise ValueError('material_analysis_budget_insufficient')
        plan = complete('material-plan-merged', messages, parse_plan)
    plan_stage = stage_keys[-1]
    analysis_pieces = policy.pieces(source,
        lambda text, offset: policy.focused('analysis', original_title=title, plan=plan,
            full_text=source, body_start=body['start'], offset=offset, source=text),
        input_limit=input_limit, output_limit=output, with_offsets=True)
    temporal_pieces = (policy.pieces(source,
        lambda text, offset: policy.focused('temporal', original_title=title,
            full_text=source, body_start=body['start'], offset=offset, source=text),
        input_limit=input_limit, output_limit=output, with_offsets=True)
        if plan['temporal']['needed'] else [])
    # 在分析派发前明确本 episode 的剩余阶段数；不能根据输出偷偷无限追加。
    planned = {'plan_stage': plan_stage,
        'analysis_stages': [{'key': 'organize-' + organize.item_id + '-material-analysis-' + str(index),
            'offset': offset + body['start'], 'length': len(piece)} for index, (offset, piece) in enumerate(analysis_pieces, 1)],
        'temporal_stages': [{'key': 'organize-' + organize.item_id + '-material-temporal-' + str(index),
            'offset': offset + body['start'], 'length': len(piece)} for index, (offset, piece) in enumerate(temporal_pieces, 1)],
        'synthesis_stage': 'organize-' + organize.item_id + '-material-synthesis' if len(analysis_pieces) > 1 else None,
        'call_limit': len(plan_pieces) + int(len(plans) > 1) + len(analysis_pieces) + len(temporal_pieces) + int(len(analysis_pieces) > 1),
        'input_limit': input_limit, 'output_limit': output, 'parallelism': workers}
    validate()
    with organize.records.begin() as tx:
        organize._guard_write(tx)
        current = tx.read(ANALYSES, organize.item_id)
        if current.payload.get('call_limit') is not None:
            if any(current.payload.get(key) != value for key, value in {**planned, 'plan': plan}.items()):
                raise ValueError('material_analysis_binding_changed')
        else:
            tx.put(ANALYSES, organize.item_id, {**episode, **planned, 'plan': plan}, expected_revision=current.revision)
            tx.commit()
    claims, temporal, tasks = [], [], []
    for operation, parts in (('analysis', analysis_pieces), ('temporal', temporal_pieces)):
        for index, (offset, piece) in enumerate(parts, 1):
            absolute = offset + body['start']
            parse = (lambda raw, piece=piece, absolute=absolute: parse_claims(raw, piece=piece, offset=absolute, plan=plan)) if operation == 'analysis' else (
                lambda raw, piece=piece, absolute=absolute: parse_temporal(raw, piece=piece, offset=absolute,
                    original_title=title, full_source=source, source_offset=body['start']))
            tasks.append((operation, 'material-' + operation + '-' + str(index),
                policy.focused(operation, original_title=title, source=piece, full_text=source,
                    body_start=body['start'], offset=offset, plan=plan), parse))
    def run_task(task):
        # 每个任务独立 Turn 实例；存储复用既有每次连接/UOW，不共享 index。
        from .organize_turns import OrganizeTurns
        owner = OrganizeTurns(root=organize.store._path.parent.parent, records=organize.records,
            models=organize.models, item_id=organize.item_id, project_id=organize.project_id,
            source=organize.source, validate_current=validate, lease=organize.lease, run_id=organize.run_id)
        return complete(task[1], task[2], task[3], owner=owner, record=False)

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='material-analysis') as pool:
        futures = [pool.submit(copy_context().run, run_task, task) for task in tasks]
        for task, future in zip(tasks, futures, strict=True):
            parsed, meta, key = future.result()
            (claims if task[0] == 'analysis' else temporal).extend(parsed)
            metadata.append(meta)
            stage_keys.append(key)
    for index, claim in enumerate(claims, 1):
        claim['id'] = 'claim-' + str(index)
        for evidence in claim['evidence']:
            evidence.update(source_type='original_item', source_id=organize.item_id,
                revision=episode['owner_revision'], coordinate_space='workspace_source_text_v1')
    synthesis = None
    if planned['synthesis_stage'] is not None:
        messages = policy('synthesis', original_title=title, plan=plan, claims=claims)
        from backend.shared.llm.message_metadata import _estimate_input_tokens
        if _estimate_input_tokens(messages) > input_limit:
            raise ValueError('material_analysis_budget_insufficient')
        synthesis = complete('material-synthesis', messages, lambda raw: parse_synthesis(raw, claims=claims))
    draft = {'title': plan['title'], 'summary': plan['summary'], 'topics': [], 'facts': [], 'todos': [],
        'uncertainties': [], 'people': [], 'dates': [], 'suggestions': []}
    analysis = {'plan': plan, 'claims': claims, 'temporal': temporal, 'synthesis': synthesis}
    validate()
    with organize.records.begin() as tx:
        organize._guard_write(tx)
        current = tx.read(ANALYSES, organize.item_id)
        if current is None or current.payload['source_text'] != organize.source:
            raise ModelConfigurationError('remote_processing_target_changed')
        tx.put(ANALYSES, organize.item_id, {**episode, **planned, 'state': 'completed',
            'analysis': analysis, 'draft': draft, 'generated_draft': deepcopy(draft),
            'stage_keys': stage_keys}, expected_revision=current.revision)
        tx.commit()
    usage = {}
    for meta in metadata:
        for name, value in (meta.get('usage') or {}).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                usage[name] = usage.get(name, 0) + value
    return draft, {'model': metadata[-1].get('model', '') if metadata else '', 'usage': usage}


def _check(row, payload, draft):
    if (payload.get('item_id') != row.object_id or payload.get('project_id') != row.payload.get('project_id')
            or payload.get('source_text') != row.payload.get('source_text') or payload.get('draft') != draft):
        raise ValueError('material_analysis_binding_changed')


def _checked_analysis(reader, row, payload):
    _check(row, payload, row.payload.get('draft'))
    if payload.get('state') != 'completed':
        raise ValueError('material_analysis_incomplete')
    if payload.get('policy_versions', {}).get('material_analysis') in {'@2', '@3', '@4'}:
        from .material_analysis_fallback import checked_fallback
        if (payload['policy_versions'] != {'organize': '@2',
                'material_analysis': payload['policy_versions']['material_analysis'], 'context_preparation': '@1'}
                or type(payload.get('owner_revision')) is not int or not 0 < payload['owner_revision'] <= row.revision):
            raise ValueError('material_analysis_binding_changed')
        return checked_fallback(reader, row, payload)
    original, analysis = payload['source_text'], payload['analysis']
    body = analysis_body_for_item(reader, row)
    if body != payload['body_range']:
        raise ValueError('material_analysis_binding_changed')
    source = original[body['start']:body['end']]
    frozen_source = reader.read('workspace_organize_inputs', row.object_id)
    if frozen_source is None or frozen_source.payload != {'project_id': row.payload['project_id'], 'source': original}:
        raise ValueError('material_analysis_binding_changed')
    policy = get('material_analysis', version='@1')
    title, output = payload['original_title'], payload['output_limit']
    seen = []
    def read_stage(key, messages):
        step = reader.read('workspace_organize_steps', key)
        if (step is None or step.payload.get('rejected')
                or step.payload['inputs'].get('messages') != messages
                or step.payload['inputs'].get('max_tokens') != output
                or step.payload['inputs'].get('project_id') != row.payload['project_id']):
            raise ValueError('material_analysis_binding_changed')
        seen.append(key)
        return step.payload['output']
    prefix = 'organize-' + row.object_id + '-'
    plan_pieces = policy.pieces(source, lambda text: policy('plan', original_title=title, source=text),
        input_limit=payload['input_limit'])
    plans = [parse_plan(read_stage(prefix + 'material-plan-' + str(index),
        policy('plan', original_title=title, source=piece))) for index, (_offset, piece) in enumerate(plan_pieces, 1)]
    plan_key = prefix + ('material-plan-merged' if len(plans) > 1 else 'material-plan-1')
    plan = (parse_plan(read_stage(plan_key, policy('plan', original_title=title, material_plans=plans)))
        if len(plans) > 1 else plans[0])
    if plan != analysis['plan'] or payload['plan_stage'] != plan_key:
        raise ValueError('material_analysis_binding_changed')
    generated_draft = {'title': plan['title'], 'summary': plan['summary'], 'topics': [], 'facts': [], 'todos': [],
        'uncertainties': [], 'people': [], 'dates': [], 'suggestions': []}
    if payload.get('generated_draft') != generated_draft:
        raise ValueError('material_analysis_binding_changed')
    # 根据冻结预算与完整原文重建连续分段，不能同时篡改旁路与结果来漏掉尾段。
    for name, operation in (('analysis_stages', 'analysis'), ('temporal_stages', 'temporal')):
        builder = lambda text, offset: policy.focused(operation, original_title=title, source=text,
            full_text=source, body_start=body['start'], offset=offset, plan=plan)
        parts = (policy.pieces(source, builder, input_limit=payload['input_limit'], output_limit=output, with_offsets=True)
            if operation == 'analysis' or plan['temporal']['needed'] else [])
        expected = [{'key': prefix + 'material-' + operation + '-' + str(index), 'offset': offset + body['start'], 'length': len(piece)}
            for index, (offset, piece) in enumerate(parts, 1)]
        if payload[name] != expected:
            raise ValueError('material_analysis_binding_changed')
    claims = []
    for part in payload['analysis_stages']:
        piece = original[part['offset']:part['offset'] + part['length']]
        raw = read_stage(part['key'], policy.focused('analysis', original_title=title, plan=plan,
            source=piece, full_text=source, body_start=body['start'], offset=part['offset'] - body['start']))
        claims.extend(parse_claims(raw, piece=piece,
            offset=part['offset'], plan=analysis['plan']))
    for index, claim in enumerate(claims, 1):
        claim['id'] = 'claim-' + str(index)
        for evidence in claim['evidence']:
            evidence.update(source_type='original_item', source_id=row.object_id,
                revision=payload['owner_revision'], coordinate_space='workspace_source_text_v1')
    temporal = []
    for part in payload['temporal_stages']:
        piece = original[part['offset']:part['offset'] + part['length']]
        raw = read_stage(part['key'], policy.focused('temporal', original_title=title,
            source=piece, full_text=source, body_start=body['start'], offset=part['offset'] - body['start']))
        temporal.extend(parse_temporal(raw, piece=piece,
            offset=part['offset'], original_title=payload['original_title'], full_source=source, source_offset=body['start']))
    synthesis = None
    expected_synthesis = prefix + 'material-synthesis' if len(payload['analysis_stages']) > 1 else None
    if payload['synthesis_stage'] != expected_synthesis:
        raise ValueError('material_analysis_binding_changed')
    if expected_synthesis is not None:
        synthesis = parse_synthesis(read_stage(expected_synthesis,
            policy('synthesis', original_title=title, plan=plan, claims=claims)), claims=claims)
    if analysis != {'plan': analysis['plan'], 'claims': claims, 'temporal': temporal, 'synthesis': synthesis}:
        raise ValueError('material_analysis_binding_changed')
    if (payload['stage_keys'] != seen or payload['call_limit'] != len(seen)
            or type(payload['owner_revision']) is not int or payload['owner_revision'] < 1):
        raise ValueError('material_analysis_binding_changed')
    return deepcopy(analysis)


def save_reviewed_draft(tx, row, draft):
    """人工稿沿用既有验证与CAS，原模型稿和分析证据保持冻结。"""
    current = tx.read(ANALYSES, row.object_id)
    if current is None:
        return
    _checked_analysis(tx, row, current.payload)
    tx.put(ANALYSES, row.object_id, {**current.payload, 'draft': deepcopy(draft)},
        expected_revision=current.revision)


def item_analysis(reader, row, draft):
    frozen = reader.read(PUBLICATIONS, 'confirm-' + row.object_id)
    if frozen is not None:
        _check(row, frozen.payload, draft)
        return deepcopy(frozen.payload['analysis'])
    current = reader.read(ANALYSES, row.object_id)
    if current is None:
        return None
    _check(row, current.payload, draft)
    return _checked_analysis(reader, row, current.payload)


def render_analysis(analysis):
    if analysis is None:
        return ''
    labels = {'reported_fact': '材料陈述', 'inference': '推断', 'plan': '计划', 'unknown': '未知'}
    states = {'asserted': '陈述', 'planned': '计划中', 'missed': '未发生', 'conditional': '有条件',
        'question': '求证', 'unknown': '未知'}
    lines = ['\n\n## 材料分析']
    rendered_claims = set()
    for angle in analysis['plan']['angles']:
        lines.extend(['', '### ' + angle['label']])
        for claim in analysis['claims']:
            if angle['id'] not in claim['angle_ids']:
                continue
            if claim['id'] in rendered_claims:
                lines.append('- 参见本整理稿 ' + claim['id'] + '。')
                continue
            rendered_claims.add(claim['id'])
            actors = '、'.join(claim['actors']) or '主体未确定'
            lines.append(f"- {claim['id']} · {labels[claim['epistemic_status']]} · {states[claim['event_status']]} · {actors}：{claim['text']}")
            for evidence in claim['evidence']:
                lines.append('  原文依据：' + json.dumps(evidence['quote'], ensure_ascii=False))
    if analysis.get('synthesis') is not None:
        lines.extend(['', '### 跨段综合复核'])
        for group in analysis['synthesis']['groups']:
            lines.append('- ' + group['label'] + '；关联：' + '、'.join(group['claim_ids']))
            for field, label in (('inferences', '推断'), ('contradictions', '分歧'), ('unknowns', '未知')):
                for relation in group[field]:
                    lines.append('  ' + label + '：' + relation['text'] + '；依据：' + '、'.join(relation['claim_ids']))
    if analysis['temporal']:
        lines.extend(['', '### 时间依据'])
        for event in analysis['temporal']:
            options = event['event_time']['options']
            precision = {'week': '周', 'month': '月', 'day': '日', 'range': '范围'}
            rendered = ('程序推算：' + event['computed'] if event['computed'] else
                '；'.join(f"{value['start']} 至 {value['end']}（{precision.get(value['precision'], value['precision'])}精度）"
                    for value in options) or '事件时间未知')
            actors = '、'.join(event['actors']) or '主体未确定'
            expression = event['expression'] if event['expression'] is not None else '未注明'
            lines.append(f"- {actors} · {states[event['event_status']]}：{event['event_text']}；时间原词：{expression}；{rendered}；记录日期锚：{event['record_anchor'] or '未知'}")
            lines.append('  原文依据：' + json.dumps(event['evidence']['quote'], ensure_ascii=False))
            if event['anchor_evidence'] is not None:
                lines.append('  锚原文：' + json.dumps(event['anchor_evidence']['quote'], ensure_ascii=False))
    return '\n'.join(lines) + '\n'


def analysis_source_refs(analysis, source_id):
    """把完整分析的真实L0位置交给既有Document引用，不改来源闭包。"""
    if analysis is None:
        return []
    evidence = [item for claim in analysis['claims'] for item in claim['evidence']]
    evidence += [event['evidence'] for event in analysis['temporal']]
    evidence += [event['anchor_evidence'] for event in analysis['temporal']
        if event['anchor_evidence'] is not None and 'start' in event['anchor_evidence']]
    result, seen = [], set()
    for item in evidence:
        key = (item['start'], item['end'], item['quote'])
        if key not in seen:
            seen.add(key)
            result.append({'source_id': source_id, 'locator': f"text:{item['start']}:{item['end']}",
                'quote': item['quote']})
    return result


def freeze_publication(tx, row, draft, markdown):
    current = tx.read(ANALYSES, row.object_id)
    if current is None:
        return None
    analysis = _checked_analysis(tx, row, current.payload)
    if render_analysis(analysis) not in markdown:
        raise ValueError('material_analysis_publication_missing')
    key = 'confirm-' + row.object_id
    payload = {'item_id': row.object_id, 'project_id': row.payload['project_id'],
        'source_text': row.payload['source_text'], 'original_title': current.payload['original_title'],
        'draft': deepcopy(draft), 'analysis': analysis,
        'generation': {key: deepcopy(value) for key, value in current.payload.items()
            if key not in {'analysis', 'draft', 'source_text'}},
        'analysis_ref': {'collection': ANALYSES, 'id': row.object_id, 'revision': current.revision},
        'markdown': markdown, 'document_id': None, 'document_revision': None}
    saved = tx.read(PUBLICATIONS, key)
    if saved is not None:
        if saved.payload != payload:
            raise ValueError('material_analysis_binding_changed')
        return saved
    return tx.put(PUBLICATIONS, key, payload, expected_revision=0)


def commit_publication(tx, row, operation, document):
    frozen = tx.read(PUBLICATIONS, operation['id'])
    if frozen is None:
        return
    _check(row, frozen.payload, operation['draft'])
    if frozen.payload['markdown'] != operation['markdown']:
        raise ValueError('material_analysis_binding_changed')
    tx.put(PUBLICATIONS, frozen.object_id, {**frozen.payload,
        'document_id': document['id'], 'document_revision': document['revision']}, expected_revision=frozen.revision)


def read_document_analysis(records, documents, project_id, document_id, *, revision=None, reader=None):
    reader = reader or records
    row = reader.read('documents', document_id)
    document = row.payload if row else documents.read(document_id)
    if document is None or document.get('project_id') != project_id:
        raise ValueError('material_analysis_document_unavailable')
    selected = document['revision'] if revision is None else revision
    if selected != document['revision']:
        return None
    filing = reader.read('v2_document_filings', document_id)
    if filing is not None:
        origin = read_document_analysis(records, documents, filing.payload['source_project_id'],
            filing.payload['source_document_id'], revision=filing.payload['source_document_revision'], reader=reader)
        if origin is None:
            return None
        # 目标文档仍依现有 copied experience/filing 闭包核权限，不外发外项目原件 refs。
        if documents.markdown(document_id, revision=selected) != documents.markdown(
                filing.payload['source_document_id'], revision=filing.payload['source_document_revision']):
            return None
        origin['document'] = {'id': document_id, 'revision': selected}
        origin['original_material_refs'] = []
        origin['bindings'].append({'filing_id': filing.object_id, 'filing_revision': filing.revision})
        return origin
    item_id = next((ref.get('source_id') for ref in document.get('source_refs', [])
        if ref.get('locator') == 'workspace://' + str(ref.get('source_id'))), None)
    if not item_id:
        return None
    publication = reader.read(PUBLICATIONS, 'confirm-' + item_id)
    if publication is None:
        return None
    item = reader.read('workspace_items', item_id)
    payload = publication.payload
    if (item is None or item.payload.get('status') != 'confirmed'
            or payload['document_id'] != document_id or payload['document_revision'] != selected
            or payload['markdown'] != documents.markdown(document_id, revision=selected)):
        return None
    _check(item, payload, item.payload.get('draft'))
    return {'document': {'id': document_id, 'revision': selected}, 'analysis': deepcopy(payload['analysis']),
        'analysis_ref': {'collection': PUBLICATIONS, 'id': publication.object_id, 'revision': publication.revision},
        'bindings': [{'document_id': document_id, 'document_revision': selected,
            'publication_id': publication.object_id, 'publication_revision': publication.revision,
            'origin_item_id': item_id, 'origin_item_revision': item.revision}],
        **read_document_analysis_sources(records, documents, project_id, document_id,
            revision=selected, reader=reader)}


def read_document_analysis_sources(records, documents, project_id, document_id, *, revision=None, reader=None):
    """从实际存储独立重建正文与生成修订，不信任提炼输入自报来源。"""
    reader = reader or records
    document = reader.read('documents', document_id)
    if document is None or document.payload.get('project_id') != project_id:
        raise ValueError('material_analysis_document_unavailable')
    selected = document.payload['revision'] if revision is None else revision
    if selected != document.payload['revision']:
        raise ValueError('material_analysis_document_unavailable')
    filing = reader.read('v2_document_filings', document_id)
    if filing is not None:
        origin = read_document_analysis_sources(records, documents, filing.payload['source_project_id'],
            filing.payload['source_document_id'], revision=filing.payload['source_document_revision'], reader=reader)
        if documents.markdown(document_id, revision=selected) != documents.markdown(
                filing.payload['source_document_id'], revision=filing.payload['source_document_revision']):
            raise ValueError('material_analysis_binding_changed')
        return {**origin, 'original_material_refs': []}
    item_id = next((ref.get('source_id') for ref in document.payload.get('source_refs', [])
        if ref.get('locator') == 'workspace://' + str(ref.get('source_id'))), None)
    item = reader.read('workspace_items', item_id) if item_id else None
    publication = reader.read(PUBLICATIONS, 'confirm-' + item_id) if item_id else None
    if item is None or publication is None:
        raise ValueError('material_analysis_document_unavailable')
    payload = publication.payload
    if (item.payload.get('status') != 'confirmed' or item.payload.get('project_id') != project_id
            or payload['document_id'] != document_id or payload['document_revision'] != selected
            or payload['markdown'] != documents.markdown(document_id, revision=selected)):
        raise ValueError('material_analysis_binding_changed')
    _check(item, payload, item.payload.get('draft'))
    generation = {**payload['generation'], 'source_text': payload['source_text'],
        'draft': payload['draft'], 'analysis': payload['analysis']}
    _checked_analysis(reader, item, generation)
    # 资格读取不能构造会初始化数据库的 Turn store；缺少原库时直接拒绝。
    record_path = Path(records.database_path)
    root = record_path.parent / '.rebuild-data' if record_path.name == 'recognition.sqlite3' else record_path.parent
    database = root / 'ai-turns.sqlite3'
    if not database.is_file():
        raise ValueError('material_analysis_binding_changed')
    connection = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    refs = []
    try:
        connection.execute('BEGIN')
        for key in generation['stage_keys']:
            step = reader.read('workspace_organize_steps', key)
            stored = connection.execute('SELECT request_json FROM ai_turns WHERE turn_id=?',
                (step.payload['turn_id'],)).fetchone()
            request = json.loads(stored[0]) if stored else None
            materials = request['privacy']['material_refs'] if request else []
            if (request is None or request.get('desired_outcome') != 'memory.organize'
                    or any(request.get('policy_versions', {}).get(name) != selected
                        for name, selected in generation['policy_versions'].items())
                    or len(materials) != 1 or materials[0].get('id') != item_id
                    or materials[0].get('type') != 'original_item' or materials[0].get('project_id') != project_id
                    or type(materials[0].get('revision')) is not int
                    or not 0 < generation['owner_revision'] <= materials[0]['revision'] <= item.revision
                    or key == generation['stage_keys'][0] and materials[0]['revision'] != generation['owner_revision']):
                raise ValueError('material_analysis_binding_changed')
            refs.append({'collection': 'workspace_organize_steps', 'id': key,
                'revision': step.revision, 'turn_id': step.payload['turn_id']})
    finally:
        connection.close()
    body = analysis_body_for_item(reader, item)
    return {'body_sources': [{'source_type': 'original_item', 'source_id': item_id,
        'project_id': project_id, 'revision': item.revision, 'generation_revision': generation['owner_revision'],
        'coordinate_space': 'workspace_source_text_v1', 'text': item.payload['source_text'],
        'body': {key: body[key] for key in ('start', 'end')}, 'section_binding': body['section_binding']}],
        'analysis_generation_refs': refs,
        'original_material_refs': [{'type': 'original_item', 'id': item_id,
            'revision': item.revision, 'project_id': project_id}]}
