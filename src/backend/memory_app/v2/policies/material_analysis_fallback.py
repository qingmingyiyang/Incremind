"""材料分析第二版的压缩与回查契约；第一版提示词保持冻结。"""
from dataclasses import dataclass
import json

from backend.shared.llm.message_metadata import _estimate_input_tokens
from .material_analysis import (
    MaterialAnalysisPolicy, _PLAN, _ANALYZE, _TEMPORAL, _SYNTHESIS,
    decode, parse_claims, parse_temporal, parse_synthesis,
)


_CONTEXT = ('上下文摘要仅帮助理解，不能代替原文证据。当前focus及raw_context是已核验原文，'
    '坐标是完整原件位置。只提取focus中的主证据。关注全文目标、核心概念、关键事实、论证关系、'
    '条件、例外及未决问题；接续progress中的已分析判断与依据。遇到指代歧义、矛盾或摘要证据不足，'
    '在read_requests请求回查原文，不能猜测或拿摘要证明事实。回查格式为'
    '{"key":"已提供原件键","start":原件起点,"end":原件终点,"reason":"为何影响判断"}。'
    '没有回查需求也返回read_requests空数组。回查后的最终输出仍保留未知与限制，不能重复要求相同回查。')
_REVIEW = ('结合全文结构检查跨片段概念、主体、结论与引用是否一致，以及遗漏、重复和论证断裂。'
    '原始claim完整保留；摘要不授予事实资格。每个输入block恰好归属一个group。'
    '只返回JSON：{"groups":[{"label":"综合角度","summary":"保留目标概念事实论证条件例外未决的完整结构摘要",'
    '"block_keys":["b1"],'
    '"inferences":[],"contradictions":[],"unknowns":[]}],"read_requests":[]}。'
    '关系字段每项为{"text":"推断或分歧/未知","block_keys":["b1","b2"]}；'
    '任何综合结论仅标为推断，不能升级原计划、未知或求证。存在证据歧义或矛盾时按原文坐标提出read_requests；'
    '不因摘要省略就断言原文不存在信息。')


def read_requests(value):
    if not isinstance(value, list):
        raise ValueError('material_analysis_read_invalid')
    for item in value:
        if (not isinstance(item, dict) or set(item) != {'key', 'start', 'end', 'reason'}
                or not isinstance(item['key'], str) or not item['key']
                or any(type(item[key]) is not int for key in ('start', 'end'))
                or not 0 <= item['start'] < item['end']
                or not isinstance(item['reason'], str) or not item['reason'].strip()):
            raise ValueError('material_analysis_read_invalid')
    return value


def parse_focused(raw, *, operation, piece, offset, plan, original_title, full_source, source_offset):
    value = decode(raw)
    name = 'claims' if operation == 'analysis' else 'events'
    if not isinstance(value, dict) or set(value) != {name, 'read_requests'}:
        raise ValueError('material_analysis_invalid')
    requests = read_requests(value['read_requests'])
    encoded = json.dumps({name: value[name]}, ensure_ascii=False)
    parsed = (parse_claims(encoded, piece=piece, offset=offset, plan=plan) if operation == 'analysis'
        else parse_temporal(encoded, piece=piece, offset=offset, original_title=original_title,
            full_source=full_source, source_offset=source_offset))
    return {'value': parsed, 'read_requests': requests}


def parse_review(raw, *, blocks):
    value = decode(raw)
    if not isinstance(value, dict) or set(value) != {'groups', 'read_requests'} or not isinstance(value['groups'], list):
        raise ValueError('material_analysis_invalid')
    keys, covered = {block['key'] for block in blocks}, []
    for group in value['groups']:
        if (not isinstance(group, dict) or set(group) != {'label', 'summary', 'block_keys', 'inferences', 'contradictions', 'unknowns'}
                or not isinstance(group['label'], str) or not group['label'].strip()
                or not isinstance(group['summary'], str) or not group['summary'].strip()
                or not isinstance(group['block_keys'], list) or not group['block_keys']
                or not all(isinstance(key, str) and key in keys for key in group['block_keys'])):
            raise ValueError('material_analysis_invalid')
        covered.extend(group['block_keys'])
        for kind in ('inferences', 'contradictions', 'unknowns'):
            if not isinstance(group[kind], list):
                raise ValueError('material_analysis_invalid')
            for relation in group[kind]:
                if (not isinstance(relation, dict) or set(relation) != {'text', 'block_keys'}
                        or not isinstance(relation['text'], str) or not relation['text'].strip()
                        or not isinstance(relation['block_keys'], list) or not relation['block_keys']
                        or any(key not in keys for key in relation['block_keys'])):
                    raise ValueError('material_analysis_invalid')
    if len(covered) != len(keys) or set(covered) != keys:
        raise ValueError('material_analysis_coverage_invalid')
    return {'groups': value['groups'], 'read_requests': read_requests(value['read_requests'])}


@dataclass(frozen=True)
class MaterialAnalysisFallbackPolicy(MaterialAnalysisPolicy):
    def instructions(self, operation):
        if operation == 'plan':
            return _PLAN
        if operation not in {'analysis', 'temporal'}:
            raise ValueError('material_analysis_operation_invalid')
        instruction = (_ANALYZE if operation == 'analysis' else _TEMPORAL)
        # 第二版压缩分支用真实focus/raw_context，不能声称模型读到了未发送的全文。
        instruction = instruction.replace('阅读全文full_text理解上下文，着重分析source所复制的重点段落。',
            '依据focus及相关上下文分析重点段落。').replace('阅读全文full_text理解时间上下文，着重提取source重点段的事件，',
            '依据focus及相关原文时间头提取重点段的事件，')
        instruction = instruction.replace('只返回JSON：{"claims":[', '只返回JSON并增加顶层read_requests数组：{"claims":[')
        instruction = instruction.replace('只返回JSON：{"events":[', '只返回JSON并增加顶层read_requests数组：{"events":[')
        return instruction + _CONTEXT

    def output_fits(self, span, output_limit):
        return _estimate_input_tokens([{'role': 'user', 'content': span.text}]) * 3 + 512 <= output_limit

    def review_messages(self, blocks, *, title, plan, structure, raw_context=(), source_catalog=()):
        return [{'role': 'system', 'content': _REVIEW}, {'role': 'user', 'content': json.dumps({
            'original_title': title, 'plan': plan, 'structure': structure, 'blocks': blocks,
            'raw_context': raw_context, 'source_catalog': source_catalog}, ensure_ascii=False, sort_keys=True)}]

    def synthesis_messages(self, *, title, plan, claims, full_text, body_start, structure=()):
        return [{'role': 'system', 'content': _SYNTHESIS + '检查全文目标及各段覆盖，明确遗漏、重复、指代和论证衔接问题。'},
            {'role': 'user', 'content': json.dumps({'original_title': title, 'plan': plan, 'claims': claims,
                'full_text': full_text, 'body_start': body_start, 'structure': structure},
                ensure_ascii=False, sort_keys=True)}]

    def progress_summary(self, claims, span):
        return [{'coverage': [span.key], 'items': [{'kind': 'decision',
            'text': json.dumps({key: claim[key] for key in ('id', 'text', 'epistemic_status', 'event_status', 'actors', 'entities')},
                ensure_ascii=False, sort_keys=True),
            'evidence': [{'key': span.key, **{key: evidence[key] for key in ('start', 'end', 'quote')}}
                for evidence in claim['evidence']]}]} for claim in claims]

    def call_bound(self, source):
        import re
        # 每句最多成为一个重点单元；规划/摘要各至多一棵二叉树，分析/时间各一次回查。
        units = len(re.findall(r'.+?(?:[。！？]|[.!?](?=\s|$)|\n|$)', source, re.S))
        return 20 * max(units, 1) + 5

    def combine_review(self, result, blocks):
        index = {block['key']: block for block in blocks}
        groups = []
        for group in result['groups']:
            ids = [identity for key in group['block_keys'] for identity in index[key]['claim_ids']]
            value = {'label': group['label'], 'claim_ids': ids, 'inferences': [], 'contradictions': [], 'unknowns': []}
            for key in group['block_keys']:
                for old in index[key]['groups']:
                    for kind in ('inferences', 'contradictions', 'unknowns'):
                        value[kind].extend(old[kind])
            for kind in ('inferences', 'contradictions', 'unknowns'):
                for relation in group[kind]:
                    refs = [identity for key in relation['block_keys'] for identity in index[key]['claim_ids']]
                    value[kind].append({'text': relation['text'], 'claim_ids': refs,
                        **({'epistemic_status': 'inference'} if kind == 'inferences' else {})})
            groups.append(value)
        return {'groups': groups}


v2 = MaterialAnalysisFallbackPolicy()
