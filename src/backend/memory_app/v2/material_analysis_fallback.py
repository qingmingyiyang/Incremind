"""材料第二版复用统一文本压缩入口，保留真实Turn及完整L1证据。"""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from copy import deepcopy
from dataclasses import asdict, replace
import json

from .context_preparation import prepare_context, pack_part, read_context, adjacent_raw, reduce_summaries, audit_progress
from .policies import get, override
from .policies.context_preparation import ContextPreparationInput, ContextSpan
from .policies.material_analysis import parse_plan, parse_claims, parse_temporal, parse_synthesis, _record_headers
from .policies.material_analysis_fallback import parse_focused, parse_review
from .source_sections import analysis_body_for_item


def _request(policy, operation, *, title, source, body, reference, input_limit, output, plan=None):
    span = ContextSpan('material-body', reference, 'workspace_source_text_v1', body['start'], body['end'], source)
    messages = (policy('plan', original_title=title, source=source) if operation == 'plan' else
        policy.focused(operation, original_title=title, full_text=source, body_start=body['start'],
            offset=0, source=source, plan=plan))
    task = json.dumps({'operation': operation, 'original_title': title,
        'source_catalog': [{'key': span.key, 'start': span.start, 'end': span.end}],
        **({'plan': plan} if operation == 'analysis' else {})}, ensure_ascii=False, sort_keys=True)
    return ContextPreparationInput(task, policy.instructions(operation), messages, span, (), (span,), input_limit, output)


def _execute(episode, *, source_text, complete, freeze, validate, read_original, workers):
    """同一确定编排用于实际运行和只读重建；不信任旁路自报的stage/coverage。"""
    selected = episode.get('policy_versions', {}).get('material_analysis', '@2')
    policy, context = get('material_analysis', version=selected), get('context_preparation', version='@1')
    body, title = episode['body_range'], episode['original_title']
    source = source_text[body['start']:body['end']]
    input_limit, output = episode['input_limit'], episode['output_limit']
    reference = {'type': 'original_item', 'id': episode['item_id'],
        'revision': episode['owner_revision'], 'project_id': episode['project_id']}
    stages, schedules = [], {}

    def stage(key, messages, parser):
        if not context.fits(messages, input_limit):
            raise ValueError('material_analysis_budget_insufficient')
        if key not in stages:
            stages.append(key)
        if len(stages) > policy.call_bound(source):
            raise ValueError('material_analysis_call_bound_exceeded')
        validate()
        result = complete(key, messages, parser)
        validate()
        return result

    def save_schedule(key):
        def save(schedule):
            schedules[key] = deepcopy(schedule)
            freeze(key, schedule)
        return save

    plan_request = _request(policy, 'plan', title=title, source=source, body=body,
        reference=reference, input_limit=input_limit, output=output)
    prepared = prepare_context(plan_request, complete_stage=stage, validate_current=validate,
        read_original=read_original, stage_prefix='material-context', freeze_schedule=save_schedule('context'))
    plans = []
    for index, part in enumerate(prepared.parts, 1):
        plans.append(stage('material-plan-' + str(index), part.messages, parse_plan))
    level = 0
    while len(plans) > 1:
        level += 1
        groups, current = [], []
        for plan in plans:
            candidate = [*current, plan]
            if current and not context.fits(policy('plan', original_title=title, material_plans=candidate), input_limit):
                groups.append(current)
                current = []
            current.append(plan)
        groups.append(current)
        if all(len(group) == 1 for group in groups):
            raise ValueError('material_analysis_plan_uncompressible')
        plans = [group[0] if len(group) == 1 else stage('material-plan-merge-' + str(level) + '-' + str(index),
            policy('plan', original_title=title, material_plans=group), parse_plan)
            for index, group in enumerate(groups, 1)]
    plan = plans[0]
    analysis_request = _request(policy, 'analysis', title=title, source=source, body=body,
        reference=reference, input_limit=input_limit, output=output, plan=plan)
    full_span = analysis_request.focus

    def required_raw(part):
        headers = _record_headers(source, source_offset=body['start'])
        selected = [header for header in headers if part.start <= header['start'] < part.end]
        nearest = next((header for header in reversed(headers) if header['end'] <= part.start), None)
        if nearest:
            selected.insert(0, nearest)
        return tuple(ContextSpan('record-header-' + str(header['start']), reference,
            full_span.coordinate_space, header['start'], header['end'], header['quote']) for header in selected
            if not part.start <= header['start'] < header['end'] <= part.end)

    # 容量足够仍发送全文及重点原文，输出预算只控制重点分组；不改第一版输入。
    try:
        normal_parts = policy.pieces(source, lambda text, offset: policy.focused('analysis',
            original_title=title, full_text=source, body_start=body['start'], offset=offset,
            source=text, plan=plan), input_limit=input_limit, output_limit=output, with_offsets=True)
    except ValueError as error:
        if str(error) != 'material_analysis_budget_insufficient':
            raise
        normal_parts = None
    if normal_parts is not None:
        focus_parts = [replace(full_span, key='material-focus-' + str(index), start=body['start'] + offset,
            end=body['start'] + offset + len(piece), text=piece) for index, (offset, piece) in enumerate(normal_parts, 1)]
        structure = ()
        compressed = False
    else:
        if prepared.compressed:
            structure = prepared.structure
            def fits(part):
                if not policy.output_fits(part, output):
                    return False
                try:
                    pack_part(analysis_request, part, structure=structure,
                        raw_context=(*adjacent_raw(part, (full_span,)), *required_raw(part)))
                except ValueError as error:
                    if str(error) != 'context_preparation_budget_insufficient':
                        raise
                    return False
                return True
            try:
                focus_parts = list(context.units(full_span, fits=fits))
            except ValueError as error:
                if str(error) != 'context_preparation_budget_insufficient':
                    raise
                # 规划可用的摘要未必能容纳分析指令与日期头，复用统一有界精简而非丢弃条件。
                leaves = [ContextSpan(**part) for part in prepared.schedule['map']]
                items = [{'coverage': list(dict.fromkeys(proof['key'] for proof in item['evidence'])),
                    'items': [item]} for summary in structure for item in summary['items']]
                structure = reduce_summaries(analysis_request, items, spans=leaves, complete_stage=stage,
                    validate_current=validate, stage_prefix='material-context-reuse-refine')
                try:
                    focus_parts = list(context.units(full_span, fits=fits))
                except ValueError as repeated:
                    if str(repeated) != 'context_preparation_budget_insufficient':
                        raise
                    raise ValueError('context_preparation_summary_uncompressible') from repeated
        else:
            prepared = prepare_context(analysis_request, complete_stage=stage, validate_current=validate,
                read_original=read_original, stage_prefix='material-context-analysis', required_raw=required_raw,
                freeze_schedule=save_schedule('analysis-context'),
                output_fits=lambda part: policy.output_fits(part, output))
            structure = prepared.structure
            focus_parts = [part.focus for part in prepared.parts]
        compressed = True
    schedule = {'compressed': compressed, 'focuses': [asdict(part) for part in focus_parts],
        'temporal': plan['temporal']['needed'], 'parallelism': workers,
        'normal_call_limit': (len(stages) + len(focus_parts) * (1 + int(plan['temporal']['needed']))
            + int(len(focus_parts) > 1)) if not compressed else None,
        'max_calls': policy.call_bound(source)}
    freeze('material', schedule)
    schedules['material'] = schedule
    claims, temporal, progress, progress_rows = [], [], None, []

    def parse(operation, focus, raw, compressed):
        temporal_parser = policy.parse_temporal if selected in {'@3', '@4'} else parse_temporal
        if compressed:
            focused_parser = policy.parse_focused if selected in {'@3', '@4'} else parse_focused
            return focused_parser(raw, operation=operation, piece=focus.text, offset=focus.start,
                plan=plan, original_title=title, full_source=source, source_offset=body['start'])
        if operation == 'analysis' and selected == '@4':
            return {'value': policy.parse_claims(raw, piece=focus.text, offset=focus.start, plan=plan,
                full_source=source, source_offset=body['start']), 'read_requests': []}
        return {'value': parse_claims(raw, piece=focus.text, offset=focus.start, plan=plan) if operation == 'analysis'
            else temporal_parser(raw, piece=focus.text, offset=focus.start, original_title=title,
                full_source=source, source_offset=body['start']), 'read_requests': []}

    def focus_messages(operation, focus, *, progress, extra=()):
        if not compressed:
            return policy.focused(operation, original_title=title, source=focus.text,
                full_text=source, body_start=body['start'], offset=focus.start - body['start'], plan=plan), ()
        request = analysis_request if operation == 'analysis' else _request(policy, 'temporal', title=title,
            source=source, body=body, reference=reference, input_limit=input_limit, output=output)
        raw = (*adjacent_raw(focus, (full_span,)), *required_raw(focus), *extra)
        packed = pack_part(request, focus, structure=structure, raw_context=raw, progress=progress)
        return packed.messages, packed.omitted

    jobs = []
    for operation in ('analysis', 'temporal'):
        if operation == 'temporal' and not plan['temporal']['needed']:
            continue
        for index, focus in enumerate(focus_parts, 1):
            jobs.append((operation, index, focus))
    omitted = {}
    # 每波至多实际aux容量允许的并发数，下一波能看到上一波已完成的判断与依据。
    for left in range(0, len(jobs), workers):
        wave = jobs[left:left + workers]
        previous_claim_count = len(claims)
        dispatched = []
        for operation, index, focus in wave:
            messages, missing = focus_messages(operation, focus, progress=progress)
            key = 'material-' + operation + '-' + str(index)
            stages.append(key)
            omitted[key] = list(missing)
            dispatched.append((key, operation, focus, messages))
        def run(job):
            key, operation, focus, messages = job
            parser = lambda raw: parse(operation, focus, raw, compressed)
            return stage(key, messages, parser)
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='material-context') as pool:
            futures = [pool.submit(copy_context().run, run, job) for job in dispatched]
            results = [future.result() for future in futures]
        for job, result in zip(dispatched, results, strict=True):
            key, operation, focus, _messages = job
            if result['read_requests']:
                raw = read_context(analysis_request, result['read_requests'], read_original=read_original, validate_current=validate)
                messages, missing = focus_messages(operation, focus, progress=progress, extra=raw)
                # 一次明确回查，仍有歧义则失败并保留原Turn诊断，不能无限消耗模型调用。
                result = stage(key + '-recheck', messages, lambda raw: parse(operation, focus, raw, True))
                omitted[key + '-recheck'] = list(missing)
                if result['read_requests']:
                    raise ValueError('material_analysis_original_evidence_unresolved')
            (claims if operation == 'analysis' else temporal).extend(result['value'])
        for index, claim in enumerate(claims, 1):
            claim['id'] = 'claim-' + str(index)
        if compressed and left + workers < len(jobs):
            completed = {operation: max(focus.end for op, _index, focus in jobs[:left + workers] if op == operation)
                for operation in ('analysis', 'temporal') if any(op == operation for op, _, _ in jobs[:left + workers])}
            # 已审核的交接摘要继续沿用，只添加本波新判断，避免反复压缩全历史造成二次方调用。
            progress_rows.extend(policy.progress_summary(claims[previous_claim_count:], full_span))
            progress = {'processed_through': completed, 'judgments': progress_rows,
                'claim_states': [{'id': claim['id'], 'epistemic_status': claim['epistemic_status'],
                    'event_status': claim['event_status']} for claim in claims]}
            next_op, _next_index, next_focus = jobs[left + workers]
            try:
                focus_messages(next_op, next_focus, progress=progress)
            except ValueError as error:
                if str(error) != 'context_preparation_budget_insufficient':
                    raise
                prefix = 'material-progress-' + str(left // workers + 1)
                progress['judgments'] = list(reduce_summaries(analysis_request,
                    progress['judgments'], spans=[full_span], complete_stage=stage,
                    validate_current=validate, stage_prefix=prefix))
                progress_rows = progress['judgments']
                # 精简后仍装不下，不能静默扔掉判断、必要原文或增大模型窗口。
                focus_messages(next_op, next_focus, progress=progress)
    for index, claim in enumerate(claims, 1):
        claim['id'] = 'claim-' + str(index)
        for evidence in claim['evidence']:
            evidence.update(source_type='original_item', source_id=episode['item_id'],
                revision=episode['owner_revision'], coordinate_space='workspace_source_text_v1')
    synthesis = None
    if len(focus_parts) > 1:
        messages = policy.synthesis_messages(title=title, plan=plan, claims=claims,
            full_text=source, body_start=body['start'], structure=structure)
        if context.fits(messages, input_limit):
            synthesis = stage('material-synthesis', messages, lambda raw: parse_synthesis(raw, claims=claims))
        else:
            synthesis = _hierarchical_review(claims, title=title, plan=plan, structure=structure,
                policy=policy, context=context, input_limit=input_limit, stage=stage,
                request=analysis_request, read_original=read_original, validate=validate)
    draft = {'title': plan['title'], 'summary': plan['summary'], 'topics': [], 'facts': [], 'todos': [],
        'uncertainties': [], 'people': [], 'dates': [], 'suggestions': []}
    return {'analysis': {'plan': plan, 'claims': claims, 'temporal': temporal, 'synthesis': synthesis},
        'draft': draft, 'generated_draft': deepcopy(draft), 'stage_names': stages, 'call_limit': len(stages),
        'schedules': schedules, 'omitted_context': omitted, 'context_structure': list(structure),
        'context_source_structure': list(prepared.source_structure)}


def _hierarchical_review(claims, *, title, plan, structure, policy, context, input_limit,
                         stage, request, read_original, validate):
    blocks = [{'key': 'block-' + str(index), 'claim_ids': [claim['id']],
        'summary': json.dumps(claim, ensure_ascii=False, sort_keys=True),
        'groups': [{'label': '原始认识', 'claim_ids': [claim['id']], 'inferences': [], 'contradictions': [], 'unknowns': []}]}
        for index, claim in enumerate(claims, 1)]
    # 零认识仍实际核对全文结构，不能把没有候选等同于不存在遗漏。
    if not blocks:
        blocks = [{'key': 'structure-only', 'claim_ids': [],
            'summary': json.dumps({'plan': plan, 'structure': structure}, ensure_ascii=False, sort_keys=True), 'groups': []}]
    by_id = {claim['id']: claim for claim in claims}
    level, singleton_pass = 0, False
    def messages(values, raw=()):
        return policy.review_messages([{'key': value['key'], 'summary': value['summary']} for value in values],
            title=title, plan=plan, structure=structure, raw_context=[asdict(span) for span in raw],
            source_catalog=[{'key': request.focus.key, 'start': request.focus.start, 'end': request.focus.end}])
    while len(blocks) > 1 or level == 0:
        level += 1
        groups, current = [], []
        for block in blocks:
            if current and not context.fits(messages([*current, block]), input_limit):
                groups.append(current)
                current = []
            current.append(block)
        groups.append(current)
        if len(blocks) > 1 and all(len(group) == 1 for group in groups):
            if singleton_pass or any(not context.fits(messages(group), input_limit) for group in groups):
                raise ValueError('material_analysis_review_uncompressible')
            singleton_pass = True
            compact_singletons = True
        else:
            compact_singletons = False
        merged = []
        for index, group in enumerate(groups, 1):
            if len(group) == 1 and len(blocks) > 1 and not compact_singletons:
                merged.append(group[0])
                continue
            key = 'material-review-' + str(level) + '-' + str(index)
            parsed = stage(key, messages(group), lambda raw: parse_review(raw, blocks=group))
            if parsed['read_requests']:
                originals = read_context(request, parsed['read_requests'], read_original=read_original, validate_current=validate)
                parsed = stage(key + '-recheck', messages(group, originals), lambda raw: parse_review(raw, blocks=group))
                if parsed['read_requests']:
                    raise ValueError('material_analysis_original_evidence_unresolved')
            result = policy.combine_review(parsed, group)
            identities = [identity for old in group for identity in old['claim_ids']]
            protected = policy.progress_summary([by_id[identity] for identity in identities], request.focus)
            # 父审阅只能使用已核对的摘要，外部留存完整claim不能替代输入中的状态保真。
            for old in group:
                proofs = [proof for identity in old['claim_ids'] for proof in by_id[identity]['evidence']]
                if old['key'].startswith('material-review-'):
                    protected.extend({'coverage': [request.focus.key], 'items': [{'kind': 'decision',
                        'text': old['summary'], 'evidence': [{'key': request.focus.key,
                            **{field: proof[field] for field in ('start', 'end', 'quote')}}]}]} for proof in proofs)
            # 全局保护项按实际原件坐标重新绑定，叶键只负责原先的分段覆盖。
            protected.extend({'coverage': [request.focus.key], 'items': [{**item,
                'evidence': [{**proof, 'key': request.focus.key} for proof in item['evidence']]}]}
                for summary in structure for item in summary['items'])
            summary_text = '\n'.join(item['summary'] for item in parsed['groups'])
            proofs = [proof for summary in protected for item in summary['items'] for proof in item['evidence']]
            if not proofs:
                proofs = [{'key': request.focus.key, 'start': request.focus.start,
                    'end': request.focus.end, 'quote': request.focus.text}]
            after = [{'coverage': [request.focus.key], 'items': [{'kind': 'decision',
                'text': summary_text, 'evidence': proofs}]}]
            audit_progress(request, protected or after, after, spans=[request.focus], complete_stage=stage,
                validate_current=validate, stage_prefix=key + '-audit')
            merged.append({'key': key, 'claim_ids': [identity for old in group for identity in old['claim_ids']],
                'summary': summary_text, 'groups': result['groups']})
        if compact_singletons and sum(len(block['summary']) for block in merged) >= sum(len(block['summary']) for block in blocks):
            raise ValueError('material_analysis_review_uncompressible')
        blocks = merged
    result = {'groups': [group for group in blocks[0]['groups'] if group['claim_ids']]}
    if not claims and any(group[kind] for group in blocks[0]['groups'] for kind in ('inferences', 'contradictions', 'unknowns')):
        raise ValueError('material_analysis_review_without_claim_evidence')
    return parse_synthesis(json.dumps(result, ensure_ascii=False), claims=claims)


def run_fallback(organize, episode, *, input_limit, output, workers):
    from .material_analysis import ANALYSES, _auxiliary_binding
    from .organize_turns import OrganizeTurns
    episode = {**episode, 'input_limit': input_limit, 'output_limit': output, 'parallelism': workers,
        'maximum_calls': get('material_analysis', version=episode['policy_versions']['material_analysis']).call_bound(
            organize.source[episode['body_range']['start']:episode['body_range']['end']])}
    metadata = {}
    def validate():
        organize.validate_current()
        if _auxiliary_binding(organize.models) != episode['auxiliary_binding']:
            raise ValueError('fast_model_binding_changed')
        row = organize.records.read('workspace_items', organize.item_id)
        if row is None or analysis_body_for_item(organize.records, row) != episode['body_range']:
            raise ValueError('material_analysis_binding_changed')
    def read_original(reference):
        validate()
        expected = {'type': 'original_item', 'id': organize.item_id,
            'revision': episode['owner_revision'], 'project_id': organize.project_id}
        if dict(reference) != expected:
            raise ValueError('material_analysis_binding_changed')
        return organize.source
    def freeze(key, schedule):
        validate()
        with organize.records.begin() as tx:
            organize._guard_write(tx)
            current = tx.read(ANALYSES, organize.item_id)
            old = current.payload.get('schedules', {}).get(key)
            if old is not None and old != schedule:
                raise ValueError('material_analysis_binding_changed')
            tx.put(ANALYSES, organize.item_id, {**current.payload, **episode,
                'schedules': {**current.payload.get('schedules', {}), key: schedule},
                **({'call_limit': schedule['normal_call_limit']} if schedule.get('normal_call_limit') is not None else {})},
                expected_revision=current.revision)
            tx.commit()
    def complete(key, messages, parser):
        # 在实际派发前更新同一个episode预算，外部runner可据此拒绝第8次而不等最终结果。
        validate()
        with organize.records.begin() as tx:
            organize._guard_write(tx)
            current = tx.read(ANALYSES, organize.item_id)
            dispatched = sorted(set([*current.payload.get('dispatch_stages', []), key]))
            if len(dispatched) > episode['maximum_calls']:
                raise ValueError('material_analysis_call_bound_exceeded')
            tx.put(ANALYSES, organize.item_id, {**current.payload, 'dispatch_stages': dispatched,
                'call_limit': max(current.payload.get('call_limit') or 0, len(dispatched))}, expected_revision=current.revision)
            tx.commit()
        owner = OrganizeTurns(root=organize.store._path.parent.parent, records=organize.records,
            models=organize.models, item_id=organize.item_id, project_id=organize.project_id,
            source=organize.source, validate_current=validate, lease=organize.lease, run_id=organize.run_id)
        with override(**episode['policy_versions']):
            raw, meta = owner.complete(messages, stage=key, max_tokens=output,
                validate_current=validate, validate_output=parser)
        metadata[key] = meta
        return parser(raw)
    result = _execute(episode, source_text=organize.source, complete=complete, freeze=freeze,
        validate=validate, read_original=read_original, workers=workers)
    validate()
    with organize.records.begin() as tx:
        organize._guard_write(tx)
        current = tx.read(ANALYSES, organize.item_id)
        tx.put(ANALYSES, organize.item_id, {**episode, **result, 'state': 'completed',
            'dispatch_stages': sorted(result['stage_names']),
            'stage_keys': ['organize-' + organize.item_id + '-' + key for key in result['stage_names']]},
            expected_revision=current.revision)
        tx.commit()
    usage = {}
    for meta in metadata.values():
        for key, value in (meta.get('usage') or {}).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                usage[key] = usage.get(key, 0) + value
    return result['draft'], {'model': next(reversed(metadata.values())).get('model', '') if metadata else '', 'usage': usage}


def checked_fallback(reader, row, payload):
    body = analysis_body_for_item(reader, row)
    frozen = reader.read('workspace_organize_inputs', row.object_id)
    if (body != payload['body_range'] or frozen is None
            or frozen.payload != {'project_id': row.payload['project_id'], 'source': row.payload['source_text']}):
        raise ValueError('material_analysis_binding_changed')
    def read_original(reference):
        if dict(reference) != {'type': 'original_item', 'id': row.object_id,
                'revision': payload['owner_revision'], 'project_id': row.payload['project_id']}:
            raise ValueError('material_analysis_binding_changed')
        return row.payload['source_text']
    def complete(key, messages, parser):
        step = reader.read('workspace_organize_steps', 'organize-' + row.object_id + '-' + key)
        if (step is None or step.payload.get('rejected') or step.payload['inputs']['messages'] != messages
                or step.payload['inputs']['max_tokens'] != payload['output_limit']
                or step.payload['inputs']['project_id'] != row.payload['project_id']):
            raise ValueError('material_analysis_binding_changed')
        return parser(step.payload['output'])
    def freeze(key, schedule):
        if payload['schedules'].get(key) != schedule:
            raise ValueError('material_analysis_binding_changed')
    result = _execute(payload, source_text=row.payload['source_text'], complete=complete,
        freeze=freeze, validate=lambda: None, read_original=read_original, workers=payload['parallelism'])
    # 并行度只决定调度，回放消息/合并顺序仍由冻结波次决定。
    if result['schedules']['material']['parallelism'] != payload['parallelism']:
        raise ValueError('material_analysis_binding_changed')
    for key in ('analysis', 'generated_draft', 'stage_names', 'call_limit', 'schedules', 'omitted_context',
            'context_structure', 'context_source_structure'):
        if result[key] != payload.get(key):
            raise ValueError('material_analysis_binding_changed')
    if payload.get('stage_keys') != ['organize-' + row.object_id + '-' + key for key in result['stage_names']]:
        raise ValueError('material_analysis_binding_changed')
    if (payload.get('dispatch_stages') != sorted(result['stage_names'])
            or payload.get('maximum_calls') != get('material_analysis', version=payload['policy_versions']['material_analysis']).call_bound(
                row.payload['source_text'][body['start']:body['end']])):
        raise ValueError('material_analysis_binding_changed')
    return deepcopy(result['analysis'])
