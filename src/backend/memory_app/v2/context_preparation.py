"""文本上下文的统一准备入口；模型调用和来源权限仍由调用方负责。"""
from dataclasses import asdict, dataclass, replace
from copy import deepcopy
import json
import re

from backend.shared.llm.message_metadata import _estimate_input_tokens

from .policies import get
from .policies.context_preparation import ContextSpan


@dataclass(frozen=True)
class PreparedPart:
    focus: ContextSpan
    messages: list
    summaries: tuple
    raw_context: tuple
    progress: dict | None
    omitted: tuple


@dataclass(frozen=True)
class PreparedContext:
    parts: tuple
    structure: tuple
    coverage: tuple
    stage_refs: tuple
    schedule: dict
    compressed: bool
    source_structure: tuple = ()


def _preserve_categories(before, after):
    required = {'goal', 'concept', 'argument', 'condition', 'exception', 'unresolved'}
    previous = {item['kind'] for summary in before for item in summary['items']} & required
    current = {item['kind'] for item in after['items']}
    if not previous <= current:
        raise ValueError('context_preparation_semantic_category_missing')


def _verified(span, read_original):
    text = read_original(deepcopy(dict(span.source_ref)))
    if (not isinstance(text, str) or type(span.start) is not int or type(span.end) is not int
            or not 0 <= span.start < span.end <= len(text) or text[span.start:span.end] != span.text):
        raise ValueError('context_preparation_source_changed')


def _slice(span, start, end, suffix):
    return replace(span, key=span.key + suffix, start=start, end=end,
        text=span.text[start - span.start:end - span.start])


def adjacent_raw(focus, sources):
    """相邻完整句保留原文，跨记录作用域所需证据由调用方额外声明。"""
    result = []
    for source in sources:
        if (dict(source.source_ref) != dict(focus.source_ref)
                or source.coordinate_space != focus.coordinate_space
                or not source.start <= focus.start < focus.end <= source.end):
            continue
        left = source.text[:focus.start - source.start]
        right = source.text[focus.end - source.start:]
        before = list(re.finditer(r'.+?(?:[。！？]|[.!?](?=\s|$)|\n|$)', left, re.S))
        after = re.search(r'.+?(?:[。！？]|[.!?](?=\s|$)|\n|$)', right, re.S)
        if before:
            part = before[-1]
            if part.group().strip():
                result.append(_slice(source, source.start + part.start(), source.start + part.end(), '-before'))
        if after and after.group().strip():
            result.append(_slice(source, focus.end + after.start(), focus.end + after.end(), '-after'))
        break
    return tuple(result)


def _catalog(structure, selected):
    # 省略目录不假装选中摘要覆盖全文；完整结构仍交回调用方用于汇总和回查。
    included = [item for summary in selected for item in summary['items']]
    return tuple({'kind': item['kind'], 'evidence': deepcopy(item['evidence'])}
        for summary in structure for item in summary['items'] if item not in included)


def pack_part(request, focus, *, structure, progress=None, raw_context=(), policy=None):
    policy = policy or get('context_preparation', version='@1')
    packed = policy.pack_focus(request, focus, summaries=structure,
        raw_context=raw_context, progress=progress)
    return PreparedPart(focus, list(packed['messages']), tuple(packed['summaries']),
        tuple(packed['raw_context']), deepcopy(packed['progress']), _catalog(structure, packed['summaries']))


def prepare_context(request, *, complete_stage, validate_current, read_original,
                    stage_prefix='context', required_raw=None, freeze_schedule=None,
                    output_fits=None):
    """准备可回放文本输入；adapter必须走调用方既有Turn、回执和外发守卫。

    complete_stage(stage, messages, parser)返回已验证解析值。相同adapter也可只读
    重放真实检查点，因此本入口不初始化内核、不另设模型网关或授予来源资格。
    """
    policy = get('context_preparation', version='@1')
    validate_current()
    originals = [*request.fixed, *request.surrounding, request.focus]
    for span in originals:
        _verified(span, read_original)
    sources = tuple(request.surrounding) or (request.focus,)
    keys = [span.key for span in sources]
    if len(keys) != len(set(keys)):
        raise ValueError('context_preparation_source_invalid')
    for index, span in enumerate(sources):
        if any(dict(previous.source_ref) == dict(span.source_ref)
                and previous.coordinate_space == span.coordinate_space
                and previous.start < span.end and span.start < previous.end for previous in sources[:index]):
            raise ValueError('context_preparation_source_overlap')
    if policy.fits(request.original_messages, request.input_limit) and (
            output_fits is None or output_fits(request.focus)):
        schedule = {'compressed': False, 'map': [], 'max_calls': 0,
            'input_limit': request.input_limit, 'output_limit': request.output_limit}
        if freeze_schedule:
            freeze_schedule(schedule)
        return PreparedContext((PreparedPart(request.focus, deepcopy(list(request.original_messages)),
            (), (), None, ()),), (), (), (), schedule, False)

    # 周边来源可包含当前全文，但不得重复登记相同键或重叠坐标来伪造覆盖。
    leaves = []
    for span in sources:
        parts = policy.units(span, fits=lambda part: policy.fits(
            policy.map_messages(request, part), request.input_limit)
            and (output_fits is None or output_fits(part)))
        leaves.extend(parts)
    if not leaves:
        raise ValueError('context_preparation_source_invalid')
    schedule = {'compressed': True, 'map': [asdict(part) for part in leaves],
        'max_calls': 2 * len(leaves), 'input_limit': request.input_limit,
        'output_limit': request.output_limit, 'max_audit_calls': sum(len(leaf.text) for leaf in leaves)}
    if freeze_schedule:
        freeze_schedule(schedule)
    refs, summaries = [], []

    def dispatch(stage, messages, parser):
        if not policy.fits(messages, request.input_limit):
            raise ValueError('context_preparation_budget_insufficient')
        validate_current()
        sources_now = {}
        for leaf in leaves:
            identity = json.dumps(dict(leaf.source_ref), ensure_ascii=False, sort_keys=True)
            if identity not in sources_now:
                sources_now[identity] = read_original(deepcopy(dict(leaf.source_ref)))
            _verified(leaf, lambda _ref, identity=identity: sources_now[identity])
        value = complete_stage(stage, messages, parser)
        validate_current()
        refs.append(stage)
        return value

    def complete(stage, messages, coverage):
        return dispatch(stage, messages, lambda raw: policy.parse_summary(raw, spans=leaves, coverage=coverage))

    for index, leaf in enumerate(leaves, 1):
        summaries.append(complete(stage_prefix + '-map-' + str(index),
            policy.map_messages(request, leaf), [leaf.key]))
    source_structure = deepcopy(summaries)
    # 每一轮至少合并两个节点，调用上界为N次map和N-1次reduce；无法继续则明确失败。
    level = 0
    while len(summaries) > 1:
        level += 1
        groups, current = [], []
        for summary in summaries:
            combined = [*current, summary]
            if current and not policy.fits(policy.reduce_messages(request, combined), request.input_limit):
                groups.append(current)
                current = []
            current.append(summary)
        if current:
            groups.append(current)
        if all(len(group) == 1 for group in groups):
            raise ValueError('context_preparation_summary_uncompressible')
        reduced = []
        for index, group in enumerate(groups, 1):
            if len(group) == 1:
                reduced.append(group[0])
                continue
            coverage = [key for summary in group for key in summary['coverage']]
            value = complete(stage_prefix + '-reduce-' + str(level) + '-' + str(index),
                policy.reduce_messages(request, group), coverage)
            _preserve_categories(group, value)
            reduced.append(value)
        summaries = reduced
    structure = tuple(summaries)
    coverage = tuple(leaf.key for leaf in leaves)
    if tuple(structure[0]['coverage']) != coverage:
        raise ValueError('context_preparation_coverage_invalid')

    def raw_for(part):
        raw = [*adjacent_raw(part, sources), *(required_raw(part) if required_raw else ())]
        unique = []
        for span in raw:
            _verified(span, read_original)
            if span not in unique and span != part:
                unique.append(span)
        return tuple(unique)

    def fits(part):
        if output_fits and not output_fits(part):
            return False
        try:
            pack_part(request, part, structure=structure, raw_context=raw_for(part), policy=policy)
        except ValueError as error:
            if str(error) != 'context_preparation_budget_insufficient':
                raise
            return False
        return True

    try:
        focuses = policy.units(request.focus, fits=fits)
    except ValueError as error:
        if str(error) != 'context_preparation_budget_insufficient':
            raise
        # 完整重点句也装不下时仅追加一次有界摘要精简，不徒劳反复细分句子。
        messages = policy.refine_messages(request, structure)
        if policy.fits(messages, request.input_limit):
            refined = complete(stage_prefix + '-refine', messages, list(coverage))
        else:
            # 整份摘要也放不下时，按完整条目分组再做一次有界层级归并。
            items = [{'coverage': list(dict.fromkeys(proof['key'] for proof in item['evidence'])),
                'items': [item]} for summary in structure for item in summary['items']]
            try:
                compacted = reduce_summaries(request, items, spans=leaves, complete_stage=dispatch,
                    validate_current=validate_current, stage_prefix=stage_prefix + '-refine')
            except ValueError as error:
                raise ValueError('context_preparation_summary_uncompressible') from error
            refined = compacted[0]
        _preserve_categories(structure, refined)
        structure = (refined,)
        try:
            focuses = policy.units(request.focus, fits=fits)
        except ValueError as error:
            if str(error) != 'context_preparation_budget_insufficient':
                raise
            raise ValueError('context_preparation_summary_uncompressible') from error
    parts = tuple(pack_part(request, part, structure=structure, raw_context=raw_for(part), policy=policy)
        for part in focuses)
    # 摘要的语义另走独立原文核对；不能以精确quote/coverage代替条件和结论完整性。
    for index, leaf in enumerate(leaves, 1):
        before = [summary for summary in source_structure if leaf.key in summary['coverage']]
        after = []
        for summary in structure:
            items = [{**item, 'evidence': [proof for proof in item['evidence'] if proof['key'] == leaf.key]}
                for item in summary['items'] if any(proof['key'] == leaf.key for proof in item['evidence'])]
            after.append({'coverage': [leaf.key], 'items': items})
        builder = lambda part: policy.audit_messages(request, part, before=before, after=after)
        try:
            audit_parts = policy.units(leaf, fits=lambda part: policy.fits(builder(part), request.input_limit))
        except ValueError as error:
            raise ValueError('context_preparation_summary_audit_budget_insufficient') from error
        for number, part in enumerate(audit_parts, 1):
            audit = dispatch(stage_prefix + '-audit-' + str(index) + '-' + str(number), builder(part),
                lambda raw, part=part: policy.parse_audit(raw, span=part, before=before, after=after))
            if any(issue['verdict'] != 'preserved' for issue in audit['checks']):
                raise ValueError('context_preparation_summary_semantics_invalid')
    validate_current()
    return PreparedContext(parts, structure, coverage, tuple(refs), schedule, True, tuple(source_structure))


def reduce_summaries(request, summaries, *, spans, complete_stage, validate_current, stage_prefix):
    """已核验派生判断的有界层级压缩；原判断仍由调用方完整留存。"""
    policy = get('context_preparation', version='@1')
    original = [policy.parse_summary(json.dumps(value, ensure_ascii=False), spans=spans, coverage=value['coverage'])
        for value in summaries]
    current, level, singleton_pass = list(original), 0, False
    if not current:
        return ()
    while True:
        level += 1
        groups, group = [], []
        for summary in current:
            if group and not policy.fits(policy.refine_messages(request, [*group, summary]), request.input_limit):
                groups.append(group)
                group = []
            group.append(summary)
        groups.append(group)
        reduced = []
        for index, group in enumerate(groups, 1):
            messages = policy.refine_messages(request, group)
            if not policy.fits(messages, request.input_limit):
                raise ValueError('context_preparation_progress_uncompressible')
            coverage = list(dict.fromkeys(key for value in group for key in value['coverage']))
            validate_current()
            value = complete_stage(stage_prefix + '-' + str(level) + '-' + str(index), messages,
                lambda raw, coverage=coverage: policy.parse_summary(raw, spans=spans, coverage=coverage))
            validate_current()
            _preserve_categories(group, value)
            reduced.append(value)
        if len(reduced) == 1:
            audit_progress(request, original, reduced, spans=spans, complete_stage=complete_stage,
                validate_current=validate_current, stage_prefix=stage_prefix + '-audit')
            return tuple(reduced)
        if len(reduced) >= len(current):
            before_size = _estimate_input_tokens(policy.refine_messages(request, current))
            after_size = _estimate_input_tokens(policy.refine_messages(request, reduced))
            # 每个单项先显著缩短后才可能成对装入；最多允许一次此类预处理。
            if singleton_pass or after_size >= before_size:
                raise ValueError('context_preparation_progress_uncompressible')
            singleton_pass = True
        current = reduced


def audit_progress(request, before, after, *, spans, complete_stage, validate_current, stage_prefix):
    """逐项核对已作判断的状态和依据，coverage相同不能掩盖计划/未知升级。"""
    policy = get('context_preparation', version='@1')
    sources = {span.key: span for span in spans}
    for index, summary in enumerate(before, 1):
        for number, item in enumerate(summary['items'], 1):
            proof = item['evidence'][0]
            source = sources[proof['key']]
            start, end = proof['start'], proof['end']
            span = _slice(source, start, end, '-judgment-' + str(index) + '-' + str(number))
            comparison = []
            for value in after:
                selected = [{**row, 'evidence': [reference for reference in row['evidence']
                    if reference['key'] == source.key and reference['start'] < end and reference['end'] > start]}
                    for row in value['items'] if any(reference['key'] == source.key
                        and reference['start'] < end and reference['end'] > start for reference in row['evidence'])]
                if selected:
                    comparison.append({'coverage': value['coverage'], 'items': selected})
            protected = [{'coverage': summary['coverage'], 'items': [item]}]
            builder = lambda part: policy.audit_messages(request, part, before=protected, after=comparison)
            parts = policy.units(span, fits=lambda part: policy.fits(builder(part), request.input_limit))
            for part_number, part in enumerate(parts, 1):
                validate_current()
                result = complete_stage(stage_prefix + '-' + str(index) + '-' + str(number) + '-' + str(part_number),
                    builder(part), lambda raw, part=part: policy.parse_audit(raw, span=part,
                        before=protected, after=comparison))
                validate_current()
                if any(check['verdict'] != 'preserved' for check in result['checks']):
                    raise ValueError('context_preparation_progress_semantics_invalid')


def read_context(request, requests, *, read_original, validate_current):
    """回查只能访问调用方已声明的原件范围，摘要和模型自报不能扩大权限。"""
    if not isinstance(requests, list):
        raise ValueError('context_preparation_read_invalid')
    spans = {span.key: span for span in (*request.fixed, *request.surrounding, request.focus)}
    result, seen = [], set()
    for index, item in enumerate(requests, 1):
        if not isinstance(item, dict) or set(item) != {'key', 'start', 'end', 'reason'}:
            raise ValueError('context_preparation_read_invalid')
        span = spans.get(item['key'])
        if (span is None or any(type(item[key]) is not int for key in ('start', 'end'))
                or not span.start <= item['start'] < item['end'] <= span.end
                or not isinstance(item['reason'], str) or not item['reason'].strip()):
            raise ValueError('context_preparation_read_invalid')
        validate_current()
        _verified(span, read_original)
        identity = (json.dumps(dict(span.source_ref), ensure_ascii=False, sort_keys=True),
            span.coordinate_space, item['start'], item['end'])
        if identity not in seen:
            result.append(_slice(span, item['start'], item['end'], '-read-' + str(index)))
            seen.add(identity)
    validate_current()
    return tuple(result)
