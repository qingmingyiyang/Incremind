import argparse
import json
import math
from pathlib import Path
import re
import statistics
import sys
import xml.etree.ElementTree as ET


class SourceReader:
    def __init__(self):
        self.originals = {}

    def read(self, path):
        path = Path(path).resolve(strict=True)
        before = path.stat()
        content = path.read_bytes()
        self.originals.setdefault(path, (content, before.st_size, before.st_mtime_ns))
        return json.loads(content)

    def verify_unchanged(self):
        for path, (content, size, modified) in self.originals.items():
            current = path.stat()
            if (current.st_size, current.st_mtime_ns) != (size, modified) or path.read_bytes() != content:
                raise ValueError(f'原始评测文件发生变化：{path}')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def number(value):
    require(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0,
            '指标必须是非负有限数字')
    return value


def token_totals(usages):
    fields = ('input_tokens', 'output_tokens', 'total_tokens')
    known = {field: sum(number(row[field]) for row in usages if row.get(field) is not None) for field in fields}
    unknown = sum(any(row.get(field) is None for field in fields) for row in usages)
    return {'recorded': known, 'unknown_calls': unknown,
            'complete': known if unknown == 0 else None,
            'money': {'amount': None, 'currency': None, 'status': 'not_recorded'}}


def quality_review(reader, path, identities):
    review = reader.read(path)
    counts = {key: 0 for key in ('correct', 'partial', 'wrong', 'unpublished')}
    if review['kind'] == 'independent_source_review':
        require({row['qa_index'] for row in review['verdicts']} == identities
                and len(review['verdicts']) == len(identities), '事实裁决题目与运行分母不一致')
        for row in review['verdicts']:
            require(row['verdict'] in counts, '未知事实裁决')
            counts[row['verdict']] += 1
        raw_counts = counts.copy()
    else:
        require(review['kind'] == 'documented_independent_review', '未知评分来源类型')
        counts = review['reported_counts']
        require(set(counts) == {'correct', 'partial', 'wrong', 'unpublished'}, '历史评分字段不完整')
        for value in counts.values():
            require(type(value) is int and value >= 0, '历史评分数量无效')
        raw_counts = None
    require(sum(counts.values()) == len(identities), '评分分母不一致')
    return {'source_kind': review['kind'], 'counts': counts, 'raw_verdict_counts': raw_counts,
            'denominator': len(identities), 'method': review['method'],
            'reviewed_at': review.get('reviewed_at'), 'held_out': False}


def rag30_report(reader, root, review_path):
    manifest = reader.read(root / 'manifest.json')
    configuration = reader.read(root / 'answers-attempt' / 'server30-ladder15' / 'run-config.json')
    identities = {row['qa_index'] for row in manifest['questions']}
    require(len(identities) == len(manifest['questions']), '问题身份重复')
    frozen = reader.read(root / 'run-outcomes-frozen.json')
    require(set(frozen['covered_qids']) == identities, '运行清单不一致')
    outcomes = {row['qa_index']: row for row in frozen['outcomes']}
    require(set(outcomes) == identities and len(frozen['outcomes']) == len(identities), '运行结果身份不完整')
    audits = [reader.read(root / 'fact-review-no-gold' / name)
              for name in ('audit-main.json', 'audit-dates-symbols.json')]
    audited = [identity for audit in audits for identity in audit['covered_qids']]
    require(set(audited) == identities and len(audited) == len(identities), '独立送审清单不完整')
    times, retrieval_times, usages, policies, models, budgets = [], [], [], [], set(), set()
    published, primary_calls = 0, 0
    for identity in sorted(identities):
        row = reader.read(root / f'project-{identity}.json')
        audit = reader.read(root / 'fact-review-no-gold' / f'question-{identity}.json')
        require(row['qa_index'] == identity and audit['qid'] == identity, '逐题身份不一致')
        require(audit['outcome_status'] == outcomes[identity]['status'], '送审状态与运行终态不一致')
        published += audit['outcome_status'] == 'succeeded'
        require(audit['public_answer'] == row['result']['answer'], '送审回答与运行回答不一致')
        canonical = reader.read(root / 'answers-attempt' / 'server30-ladder15' / f'canonical-{identity}.json')
        call_usages = []
        for call in canonical['calls']:
            metadata = call['result']['metadata']
            models.add(metadata['model'])
            budgets.add((metadata['context_budget']['window'], metadata['context_budget']['reserve']))
            call_usages.append(metadata.get('usage', {}))
        primary_calls += len(call_usages)
        usages.extend(call_usages)
        require(token_totals(call_usages)['complete'] == row['result']['model_usage'], '主要调用用量与逐题汇总不一致')
        times.append(number(row['total_seconds']))
        retrieval_times.append(number(row['retrieval_seconds']))
        require(row['retrieval_seconds'] <= row['total_seconds'], '检索耗时超过逐题总耗时')
        policies.append({name: audit['policies'][name] for name in ('compose', 'ladder', 'retrieve', 'rerank')})
    require(all(policy == policies[0] for policy in policies), '同轮策略组合发生变化')
    require(models == {configuration['generation']['model']} and len(budgets) == 1, '模型或调用预算不一致')
    require(all(configuration['policies'][name] == value for name, value in policies[0].items()), '实际策略与冻结配置不一致')
    reranks = reader.read(root / 'rerank-wires.json')
    usages.extend(row.get('usage', {}) for row in reranks)
    wire_ids = [identity for outcome in outcomes.values() for identity in outcome['wire_numbers']]
    generation = reader.read(root / 'answers-frozen.json')
    require(len(set(wire_ids)) == len(wire_ids) == generation['generation_new_calls'], '物理请求身份或分母不一致')
    require(primary_calls + sum(len(row['wire_numbers']) for row in reranks) == len(wire_ids), '主要与重排调用总数不一致')
    quality = quality_review(reader, review_path, identities)
    require(quality['counts']['unpublished'] == len(identities) - published, '质量裁决与未发布数量不一致')
    sessions = len(list((root / 'fact-review-no-gold' / 'public-sessions').glob('session-*.txt')))
    return {'run': 'RAG30-20261011', 'date': '2026-10-11', 'model': sorted(models), 'policies': policies[0],
            'questions': len(identities), 'conversation_sessions': sessions, 'published': published,
            'failed_or_unpublished': len(identities) - published, 'quality': quality,
            'latency_seconds': {'denominator': len(times), 'population': 'all_attempted_questions',
                                'total_median': statistics.median(times), 'retrieval_median': statistics.median(retrieval_times),
                                'total_min': min(times), 'total_max': max(times)},
            'usage': token_totals(usages), 'physical_calls': len(wire_ids),
            'physical_call_breakdown': {'primary': primary_calls,
                                        'rerank': sum(len(row['wire_numbers']) for row in reranks),
                                        'failed_rerank': sum(row['status'] != 'succeeded' for row in reranks)},
            'source_types': {'latency_and_usage': 'original_per_question_and_call_records',
                             'audit_manifests': 'coverage_only_not_semantic_grades'},
            'protocol': {'dataset': 'LoCoMo', 'seed': manifest['seed'], 'conversation_index': manifest['conversation_index'],
                         'question_categories': sorted({row['category'] for row in manifest['questions']}),
                         'temperature': configuration['temperature'], 'history': configuration['history'],
                         'context_window': next(iter(budgets))[0], 'output_reserve': next(iter(budgets))[1]}}


def pilot_report(reader, root):
    manifest, frozen = reader.read(root / 'manifest.json'), reader.read(root / 'summary.json')
    policies = reader.read(root / 'policy-versions.json')['project.answer']
    modes = {}
    for mode in ('keyword', 'vector', 'rerank'):
        rows, grades = [], []
        for item in manifest['questions']:
            identity = item['qa_index']
            path = root / f'{mode}-{identity}.json'
            if not path.exists():
                path = root / f'attempt-{mode}-{identity}.json'
            row = reader.read(path)
            require(row['qa_index'] == identity, '试点题目身份不一致')
            rows.append(row)
            grades.append(reader.read(root / f'judge-{identity}.json')['grades'][mode])
        labeled = [row for row in rows if row['evidence_recall'] is not None]
        score = sum(grade['correct'] for grade in grades)
        recall = statistics.mean(row['evidence_recall'] for row in labeled)
        fallback = sum(row.get('evaluation_status') == 'rerank_failed_fallback' for row in rows)
        expected = frozen['modes'][mode]
        require(score == expected['semantic_correct'] and fallback == expected['fallback']
                and len(rows) == expected['attempted'] and len(labeled) == expected['evidence_questions']
                and math.isclose(recall, expected['mean_evidence_recall']), '早期试点汇总与逐题结果不一致')
        modes[mode] = {'questions': len(rows), 'raw_model_judge_correct': score, 'judged': len(grades),
                       'evidence_questions': len(labeled), 'mean_evidence_recall': recall, 'rerank_fallback': fallback}
    return {'run': 'PILOT-20261010', 'date': '2026-10-10', 'scoring': 'same_model_blind_judging',
            'held_out': False, 'modes': modes,
            'policies': {name: policies[name] for name in ('compose', 'ladder', 'retrieve', 'rank', 'rerank')},
            'comparison_to_RAG30': 'different_scoring_contracts'}


def svg_drawing(root):
    ns = '{http://www.w3.org/2000/svg}'
    def rect(x, y, width, height, color, **attributes):
        ET.SubElement(root, ns + 'rect', {'x': str(x), 'y': str(y), 'width': str(width), 'height': str(height), 'fill': color, **attributes})
    def text(x, y, value, size=18, color='#1C1A17', weight='400'):
        ET.SubElement(root, ns + 'text', {'x': str(x), 'y': str(y), 'fill': color, 'font-size': str(size),
            'font-family': 'Segoe UI, Noto Sans SC, Arial, sans-serif', 'font-weight': weight}).text = value
    return rect, text


def svg_report(report):
    ET.register_namespace('', 'http://www.w3.org/2000/svg')
    ns = '{http://www.w3.org/2000/svg}'
    root = ET.Element(ns + 'svg', {'viewBox': '0 0 960 720', 'role': 'img', 'aria-labelledby': 'title description'})
    ET.SubElement(root, ns + 'title', {'id': 'title'}).text = 'RAG30 development evaluation: quality, latency and token usage'
    data = report['rag30']
    counts, timing, usage = data['quality']['counts'], data['latency_seconds'], data['usage']
    ET.SubElement(root, ns + 'desc', {'id': 'description'}).text = (
        f"{data['questions']} development questions from {data['conversation_sessions']} conversation sessions. "
        f"Quality: {counts['correct']} correct, {counts['partial']} partial, {counts['wrong']} wrong, "
        f"{counts['unpublished']} unpublished. All-attempt median: {timing['total_median']:.3f} seconds. "
        f"Token usage: {usage['recorded']['input_tokens']} input and {usage['recorded']['output_tokens']} output. "
        f"Review source: {data['quality']['source_kind']}. Currency cost is unknown.")
    ET.SubElement(root, ns + 'metadata').text = json.dumps(report, ensure_ascii=False, sort_keys=True)
    rect, text = svg_drawing(root)
    rect(0, 0, 960, 720, '#F7F4EE')
    text(40, 50, 'RAG30 · Development evaluation', 30, weight='600')
    versions = ' / '.join(f'{name} {data["policies"][name].removeprefix("@")}'
                          for name in ('compose', 'ladder', 'retrieve', 'rerank'))
    text(40, 80, f"{data['questions']} questions / {data['conversation_sessions']} sessions · {' / '.join(data['model'])}", 17, '#4E4A44')
    for y in (105, 290, 470, 657):
        rect(40, y, 880, 1, '#D2CABC')
    text(40, 140, 'Answer quality', 21, weight='600')
    text(40, 179, f"{counts['correct']}/{data['questions']} correct", 30, weight='600')
    text(350, 179, f"{counts['partial']} partial · {counts['wrong']} wrong · {counts['unpublished']} unpublished", 18)
    x = 40
    for label, color in [('correct', '#1C1A17'), ('partial', '#C0282F'), ('wrong', '#6F6A62'), ('unpublished', '#D2CABC')]:
        width = 880 * counts[label] / data['questions']
        rect(x, 201, width, 24, color, **{'data-metric': 'quality-' + label, 'data-value': str(counts[label])})
        x += width
    reviewed = data['quality'].get('reviewed_at')
    label = f" · {reviewed[:10]}" if reviewed else ''
    text(40, 253, f"Review: {data['quality']['source_kind']}{label}", 16, '#4E4A44')
    text(40, 326, 'Latency · all attempted questions', 21, weight='600')
    text(40, 365, f"{timing['total_median']:.3f} s", 30, weight='600')
    text(255, 365, 'median total', 18, '#4E4A44')
    scale = max(timing['total_median'], timing['retrieval_median'])
    for index, (metric, label, color) in enumerate([('total_median', 'Total', '#1C1A17'), ('retrieval_median', 'Retrieval', '#C0282F')]):
        y = 390 + index * 34
        text(40, y + 16, label, 16)
        rect(150, y, 600 * timing[metric] / scale, 20, color, **{'data-metric': metric, 'data-value': str(timing[metric])})
        text(770, y + 16, f'{timing[metric]:.3f} s', 16)
    text(40, 505, f"Token usage · {data['physical_calls']} physical calls", 21, weight='600')
    tokens = usage['recorded']
    text(40, 546, f"{tokens['total_tokens']:,}", 30, weight='600')
    text(255, 546, 'total recorded tokens', 18, '#4E4A44')
    x = 40
    for field, color in [('input_tokens', '#1C1A17'), ('output_tokens', '#C0282F')]:
        width = 880 * tokens[field] / tokens['total_tokens']
        rect(x, 570, width, 24, color, **{'data-metric': field, 'data-value': str(tokens[field])})
        x += width
    text(40, 626, f"Input {tokens['input_tokens']:,} · Output {tokens['output_tokens']:,} · Unknown-usage calls {usage['unknown_calls']}", 18)
    text(40, 684, f"{data['date']} · {versions}", 16, '#4E4A44')
    text(40, 707, 'Development questions · Monetary cost not recorded', 15, '#4E4A44')
    return ET.tostring(root, encoding='utf-8', xml_declaration=True) + b'\n'


def validate_svg(content, report):
    root = ET.fromstring(content)
    ns = {'svg': 'http://www.w3.org/2000/svg'}
    require(root.find('svg:title', ns) is not None and root.find('svg:desc', ns) is not None, 'SVG缺少可访问文本')
    require(json.loads(root.find('svg:metadata', ns).text) == report, 'SVG聚合元数据不一致')
    actual = {node.attrib['data-metric']: float(node.attrib['data-value']) for node in root.iter() if 'data-metric' in node.attrib}
    data = report['rag30']
    expected = {**{'quality-' + key: value for key, value in data['quality']['counts'].items()},
                **{key: data['latency_seconds'][key] for key in ('total_median', 'retrieval_median')},
                **{key: data['usage']['recorded'][key] for key in ('input_tokens', 'output_tokens')}}
    require(actual == expected, 'SVG数值与聚合指标不一致')
    for node in root.iter():
        metric = node.attrib.get('data-metric')
        if not metric:
            continue
        if metric.startswith('quality-'):
            width = 880 * expected[metric] / data['questions']
        elif metric in {'total_median', 'retrieval_median'}:
            width = 600 * expected[metric] / max(data['latency_seconds'][key] for key in ('total_median', 'retrieval_median'))
        else:
            width = 880 * expected[metric] / data['usage']['recorded']['total_tokens']
        require(math.isclose(float(node.attrib['width']), width), 'SVG条形宽度与数据比例不一致')


HELDOUT_VERDICTS = ('correct', 'partial', 'wrong', 'clarify', 'failed', 'unpublished')
TOKEN_FIELDS = ('input_tokens', 'output_tokens', 'total_tokens')
EVIDENCE_INPUT = 'answer-model-input-evidence-answer-v1'
EVIDENCE_RESULT = 'answer-model-result-evidence-answer-v1'
REVIEW_INPUT = 'answer-model-input-answer-review-v1'


def message_catalog(messages, *, json_payload=False):
    catalogs = []
    for message in messages:
        content = message.get('content')
        if message.get('role') != 'user' or not isinstance(content, str):
            continue
        if content.startswith('资料：\n{'):
            data, _ = json.JSONDecoder().raw_decode(content[len('资料：\n'):])
        elif json_payload and content.startswith('{'):
            data = json.loads(content)
        else:
            continue
        if 'evidence_catalog' in data:
            catalogs.append(data['evidence_catalog'])
    require(len(catalogs) <= 1, '同一次模型输入出现多个证据目录')
    return catalogs[0] if catalogs else None


def input_messages_match(actual, frozen, compose_version, *, response_model=None, evidence_answer=False):
    messages = frozen['messages']
    if actual == messages:
        return True
    from backend.shared.llm.message_metadata import (
        _build_json_mode_messages, _build_prompt_fallback_messages, _build_validation_retry_message,
    )
    errors = [None]
    marker = '__REPORT_VALIDATION_ERROR__'
    prefix, suffix = _build_validation_retry_message(marker)['content'].split(marker)
    last = actual[-1] if actual else {}
    content = last.get('content')
    if isinstance(content, str) and content.startswith(prefix) and content.endswith(suffix):
        error = content[len(prefix):-len(suffix)]
        if last == _build_validation_retry_message(error):
            errors.append(error)
    if any(actual == _build_json_mode_messages(messages=messages, validation_error=error) for error in errors):
        return True
    if response_model is None:
        if evidence_answer:
            from backend.memory_app.v2.evidence_answer import EvidenceAnswerOutput
            response_model = EvidenceAnswerOutput
        else:
            from backend.memory_app.v2.answer_evidence import answer_response_model
            response_model = answer_response_model(compose_version, frozen_schema=frozen['response_schema'])
    require(response_model.model_json_schema() == frozen['response_schema'], '实际模型合同与冻结Schema不一致')
    return any(actual == _build_prompt_fallback_messages(messages=messages, response_model=response_model, validation_error=error)
               for error in errors)


def final_answer_candidates(native, frozen, compose_version, *, status):
    """Keep the historical last-input boundary; a proposal is never final evidence."""
    requests = [json.loads(turn['request_json']) for group in native for turn in group['turn']
                if isinstance(turn.get('request_json'), str)]
    requests = [request for request in requests if request.get('desired_outcome') == 'project.answer'
                and request.get('policy_versions', {}).get('retrieve') == '@12']
    if not requests and EVIDENCE_INPUT not in frozen and EVIDENCE_RESULT not in frozen:
        return [(key, frozen[key]) for key in ('answer-model-input-answer', REVIEW_INPUT) if key in frozen], False
    require(len(requests) == 1, '新单轮问答缺少唯一冻结Turn请求')
    request = requests[0]
    require(request['policy_versions'].get('compose') == compose_version, '新单轮冻结策略与报告策略不一致')
    from backend.memory_app.answer_proof import (
        has_evidence_answer_proof, select_answer_input, select_evidence_answer_input,
    )
    capsule = frozen.get('answer-continuation-plan-v1')
    product = frozen.get('product-answer-result-v2')
    if has_evidence_answer_proof(request, capsule):
        selected = select_evidence_answer_input(request, product, frozen.get(EVIDENCE_INPUT),
                                               frozen.get(EVIDENCE_RESULT), capsule)
        if selected is None or selected is not frozen.get(EVIDENCE_INPUT):
            return [], False
        refs = frozen[EVIDENCE_RESULT].get('metadata', {}).get('kernel_receipt_refs', [])
        completed = [json.loads(event['event_json']) for group in native for event in group['events']]
        completed = [event for event in completed if event.get('type') == 'model.completed'
                     and refs and event.get('data', {}).get('evidence_refs') == refs
                     and event['data'].get('model_call_purpose') == 'primary']
        require(len(completed) == 1, '单轮冻结结果缺少唯一原生完成回执')
        model_request = completed[0]['correlation']['model_request_id']
        require(any(attempt['model_request_id'] == model_request and attempt['terminal_status'] == 'succeeded'
                    for group in native for attempt in group['attempts']), '单轮完成回执缺少成功物理尝试')
        return [(EVIDENCE_INPUT, selected)], False
    review = frozen.get(REVIEW_INPUT)
    if review is None:
        return [], status != 'answered'
    if status == 'answered':
        selected = select_answer_input(request, product,
            frozen.get('answer-model-input-answer'), frozen.get('answer-model-result-answer'),
            review, frozen.get('answer-model-result-answer-review-v1'), capsule=capsule)
        if selected is not review:
            return [], False
    return [(REVIEW_INPUT, review)], False


def heldout_question(reader, path, http_rows, wires, compose_version):
    row = reader.read(path)
    identity = (row['conversation_id'], row['qa_index'])
    label = identity[0] + ':' + str(identity[1])
    native = [row['native']] if 'native' in row else list(row.get('native_failures', {}).values())
    attempts = [attempt for group in native for attempt in group['attempts']
                if attempt['dispatch_payload']['provider_id'] != 'local']
    require(len({attempt['attempt_id'] for attempt in attempts}) == len(attempts), '题内原生调用身份重复')
    for attempt in attempts:
        receipt = attempt['terminal_receipt']
        require(attempt['status'] == 'terminal' and receipt is not None, '题目仍有未终态模型尝试')
        require('deepseek' in receipt['model_id'].lower(), '回答批次存在非DeepSeek远端调用')
        require(receipt['attempt_id'] == attempt['attempt_id'] and receipt['model_request_id'] == attempt['model_request_id'],
                '原生终态回执与调用身份不一致')
    actual_http = [item for item in http_rows if item['completion']['label'] == label]
    require(len(actual_http) == row['physical_http'] == len(attempts), '真实HTTP与原生远端调用数量不一致')
    turn_ids = {turn['turn_id'] for group in native for turn in group['turn']}
    require(all(item['completion']['turn_id'] in turn_ids for item in actual_http), '真实HTTP与原生Turn身份不一致')
    events = [json.loads(event['event_json']) for group in native for event in group['events']]
    dispatched = {event['correlation']['model_request_id'] for event in events if event['type'] == 'model.attempt.dispatched'}
    require({attempt['model_request_id'] for attempt in attempts} <= dispatched, '原生调用缺少dispatch事件')
    frozen = {item['kind']: item['payload'] for group in native for item in group['immutable']}
    ask = (row.get('body') or {}).get('turn', {}).get('receipt', {}).get('ask', {})
    status = ('failed' if row['http_status'] >= 400 or ask.get('error') else
              'clarify' if any(trace.get('agentic', {}).get('status') == 'clarify' for trace in ask.get('trace', [])) else
              'answered' if isinstance(ask.get('answer'), str) and ask['answer'].strip() else 'unpublished')
    inputs, no_final = final_answer_candidates(native, frozen, compose_version, status=status)
    candidates = [(key, payload, message_catalog(payload['messages'], json_payload=key == EVIDENCE_INPUT))
                  for key, payload in inputs]
    candidates = [(key, payload, catalog) for key, payload, catalog in candidates if catalog is not None]
    selected, input_state, selected_rank = set(), 'missing', -1
    actual_numbers = {item['completion']['number'] for item in actual_http}
    for wire in sorted(wires, key=lambda item: item['number']):
        if wire['number'] not in actual_numbers:
            continue
        require(wire['label'] == label and wire['turn_id'] in turn_ids
                and all(item['completion']['turn_id'] == wire['turn_id'] for item in actual_http
                        if item['completion']['number'] == wire['number']), '真实HTTP与请求消息身份不一致')
        messages = reader.read(path.parent.parent / f"messages-{wire['number']}.json")
        catalog = message_catalog(messages, json_payload=any(key == EVIDENCE_INPUT for key, _, _ in candidates))
        matches = [rank for rank, (key, frozen_payload, frozen_catalog) in enumerate(candidates)
                   if catalog == frozen_catalog and input_messages_match(messages, frozen_payload, compose_version,
                       evidence_answer=key == EVIDENCE_INPUT)]
        if catalog is not None and matches and max(matches) >= selected_rank:
            require(isinstance(catalog['units'], list), '模型证据目录缺少原话单元')
            selected = {value for unit in catalog['units'] for value in re.findall(r'\bD\d+:\d+\b', unit['text'])}
            input_state, selected_rank = 'recorded', max(matches)
    if input_state == 'missing' and no_final:
        input_state = 'not_reached'
    if input_state == 'missing':
        rag_requests = {value['model_request_id'] for key, value in frozen.items() if key.startswith('answer-gap-binding-rag-decision-')}
        terminal = bool(native) and all(any(json.loads(event['event_json'])['type'] in
            {'turn.completed', 'turn.failed', 'turn.cancelled'} for event in group['events']) for group in native)
        if terminal and {attempt['model_request_id'] for attempt in attempts} <= rag_requests:
            input_state = 'not_reached'
    return {'identity': identity, 'seconds': number(row['seconds']), 'attempts': attempts,
            'input_state': input_state, 'selected': selected, 'status': status, 'source': row['source'],
            'answer': ask.get('answer'), 'answer_ref': path.parent.name + '/' + path.name}


def partition_quality(reader, paths, identities, answers, source_revision):
    verdicts, dates = {}, []
    for path in paths:
        review = reader.read(path)
        if review['source_revision'] != source_revision:
            continue
        require(review['kind'] == 'independent_source_review' and len(review['verdicts']) == 24, '分区事实审阅须覆盖24题')
        dates.append(review['reviewed_at'])
        for row in review['verdicts']:
            identity = (row['conversation_id'], row['qa_index'])
            require(identity not in verdicts and row['verdict'] in HELDOUT_VERDICTS, '事实审阅重叠或类别无效')
            require(identity in answers, '审阅题目未在对应批次作答')
            actual = answers[identity]
            require(row['answer_ref'] == actual['answer_ref'] and row['answer'] == actual['answer'], '审阅文本与该批次真实回答不一致')
            require(row.get('reason') and row.get('evidence'), '事实裁决缺少理由或原文依据')
            if actual['status'] in {'failed', 'clarify', 'unpublished'}:
                require(row['verdict'] == actual['status'], '未作答状态与事实审阅不一致')
            verdicts[identity] = row['verdict']
    require(len(dates) <= 2, '同批次存在超过两份审阅')
    if len(dates) < 2:
        return None
    require(set(verdicts) == identities, '两份分区审阅未覆盖固定48题')
    return {'source_kind': 'independent_partition_source_review', 'reviewed_at': dates, 'denominator': 48,
            'counts': {key: sum(value == key for value in verdicts.values()) for key in HELDOUT_VERDICTS}}


def verify_local_unknown(reader, paths, attempt):
    proofs = [reader.read(path) for path in paths]
    matches = [proof for proof in proofs if proof['attempt_id'] == attempt['attempt_id']]
    require(len(matches) == 1, '未终态本机调用缺少唯一实际中断证明')
    proof = matches[0]
    require(proof['state'] == 'interrupted_local_unknown' and proof['persisted_quarantine'] is False
            and proof['original_process_exists'] is False and proof['attempt_rows_unchanged'] is True
            and proof['leases'] == [] and proof['valid_inflight_effects'] == [] and proof['new_remote_http'] == 0,
            '本机中断证明仍有活动执行者或修改原始尝试')
    require('MainPID=0' in proof['systemd'] and 'ActiveState=inactive' in proof['systemd']
            and 'SubState=dead' in proof['systemd'], '本机中断原进程尚未退出')
    require(proof['turn_id'] == attempt['turn_id'] and len(proof['attempts']) == 1
            and all(attempt.get(key) == value for key, value in proof['attempts'][0].items()), '本机中断证明与原始调用不一致')
    require(attempt['dispatch_payload']['provider_id'] == 'local' and attempt['status'] == 'committed'
            and attempt['terminal_status'] is None and attempt['terminal_receipt'] is None
            and attempt['lease_owner_id'] is None and attempt['lease_generation'] is None,
            '本机未知调用状态或租约不符合实际证明')
    require(len(proof['turn']) == 1 and proof['turn'][0]['turn_id'] == attempt['turn_id'], '本机中断Turn身份不一致')
    require(proof['effects'] and all(effect['turn_id'] == attempt['turn_id'] and effect['state'] != 'INFLIGHT'
                                   for effect in proof['effects']), '本机中断Effect仍在执行')
    events = proof['events']
    require(events and all(event['turn_id'] == attempt['turn_id'] for event in events)
            and events[-1]['type'] == 'turn.failed'
            and any(event['type'] == 'model.failed' for event in events)
            and not any(event['type'] == 'model.attempt.terminal' for event in events), '本机中断原生事件不一致')


def validate_batch_labels(labels, batch_count):
    if labels:
        require(len(labels) == batch_count, '--batch-label数量须等于--heldout数量')
        require(all(isinstance(label, str) and label.strip() for label in labels), '批次标签不能为空')
        require(len({label.strip() for label in labels}) == len(labels), '批次标签不能重复')


def heldout_report(reader, roots, reviews, run_label, date, environment_path=None, local_recovery_paths=(), batch_labels=()):
    validate_batch_labels(batch_labels, len(roots))
    batches, all_ids, all_models, all_attempts = [], None, set(), set()
    baseline_index = reader.read(roots[0] / 'dataset/index.json')
    baseline_materials, baseline_valid = {}, {}
    for entry in baseline_index['conversations']:
        directory = roots[0] / entry['sample_id']
        manifest = reader.read(directory / 'manifest.json')
        annotations = reader.read(directory / 'annotation-quality.json')
        baseline_materials[entry['sample_id']] = (manifest, annotations)
        for item in manifest['questions']:
            if annotations[str(item['qa_index'])]['eligible']:
                baseline_valid[(entry['sample_id'], item['qa_index'])] = set(item['evidence'])
    require(len(baseline_valid) == 47, '初始完整数据的有效原子证据分母不等于47')
    for batch_index, root in enumerate(roots):
        index = reader.read(root / 'dataset/index.json')
        require(index['contract'] == 'locomo-heldout@1', '留出数据合同不一致')
        require(index == baseline_index, '批次冻结数据清单与初始完整批次不一致')
        identities = {(entry['sample_id'], qa) for entry in index['conversations'] for qa in entry['qa_indices']}
        require(len(identities) == 48 and len(index['conversations']) == 4, '留出数据身份未覆盖四会话48题')
        require(all_ids is None or all_ids == identities, '批次使用不同题目')
        all_ids = identities
        actual_run_label = None
        if batch_labels:
            # 展示名不冒充实际运行身份；显式模式核对四份冻结运行配置。
            labels = [reader.read(root / entry['sample_id'] / 'run-config.json').get('run_label')
                      for entry in index['conversations']]
            require(all(isinstance(label, str) and label.strip() for label in labels)
                    and all(label == labels[0] for label in labels), '同批次四份实际运行标签缺失或不一致')
            actual_run_label = labels[0]
        http_rows, wires = reader.read(root / 'http-requests.json'), reader.read(root / 'wires.json')
        audit, closed = reader.read(root / 'attempt-audit.json'), reader.read(root / 'closed.json')
        require(not closed['prepare_only'] and audit['pending'] == 0
                and not audit['uncovered_attempt_ids'], '真实批次调用尚未完整关闭')
        questions, valid, policies, source = [], baseline_valid, None, None
        local_encoding = {'scope': 'native_since_batch_baseline', 'calls': 0, 'succeeded_calls': 0,
                          'failed_calls': 0, 'unknown_calls': 0, 'pending_calls': 0,
                          'known_seconds_subtotal': 0, 'complete_seconds': None}
        prepared_index = {'total': 0, 'missing': 0, 'oversized': 0}
        vector_load_seconds = number(reader.read(root / 'vector-load.json')['seconds'])
        for entry in index['conversations']:
            directory = root / entry['sample_id']
            paths = [(qa, directory / f'question-{qa}.json') for qa in entry['qa_indices']]
            if batch_index != 0:
                paths = [(qa, path) for qa, path in paths if path.exists()]
                if not paths:
                    continue
            config = reader.read(directory / 'run-config.json')
            require(config['contract'] == 'heldout-workbench@1' and config['gold_in_generation'] is False
                    and config['automatic_judge'] is False, '运行入口或评分协议不一致')
            require(policies is None or policies == config['policies'], '同批次使用不同策略')
            require(source is None or source == config['source'], '同批次使用不同源码来源')
            policies = config['policies']
            source = config['source']
            annotations = reader.read(directory / 'annotation-quality.json')
            verification = reader.read(directory / 'index.json')['verification']
            for key in prepared_index:
                prepared_index[key] += count(verification[key])
            require(verification['missing'] == verification['oversized'] == 0, '运行前检索索引不完整')
            manifest = reader.read(directory / 'manifest.json')
            require((manifest, annotations) == baseline_materials[entry['sample_id']], '修复批次题目或原始证据标注改变')
            local = reader.read(directory / 'local-native.json')
            local_encoding['calls'] += len(local)
            for item in local:
                require(item['dispatch_payload']['provider_id'] == 'local', '本机账本混入远端调用')
                if item['status'] != 'terminal':
                    verify_local_unknown(reader, local_recovery_paths, item)
                    local_encoding['unknown_calls'] += 1
                    local_encoding['pending_calls'] += 1
                else:
                    require(item['terminal_receipt'] is not None, '本机终态缺少原生回执')
                    local_encoding['succeeded_calls' if item['terminal_status'] == 'succeeded' else 'failed_calls'] += 1
                    local_encoding['known_seconds_subtotal'] += number(item['terminal_receipt']['duration_ms']) / 1000
            for qa, path in paths:
                identity = (entry['sample_id'], qa)
                row = heldout_question(reader, path, http_rows, wires, policies['compose'])
                require(row['identity'] == identity and row['source'] == config['source'], '实际题目身份或源码来源不一致')
                questions.append(row)
        require(len(valid) == 47, '有效原子证据分母不等于47')
        require(questions, '修复批次没有实际已尝试题目')
        require(local_encoding['calls'] == audit['local_unique_count'] == closed['local_native_unique']
                and local_encoding['pending_calls'] == audit['local_pending'] == closed['local_native_pending'],
                '本机账本与原始pending计数不一致')
        if local_encoding['unknown_calls'] == 0:
            local_encoding['complete_seconds'] = local_encoding['known_seconds_subtotal']
        require(batch_index != 0 or {row['identity'] for row in questions} == identities, '初始批次未完成固定48题')
        attempts = [attempt for row in questions for attempt in row['attempts']]
        ids = {item['attempt_id'] for item in attempts}
        require(len(ids) == len(attempts) and not ids & all_attempts, '批次内或批次间重复计算模型尝试')
        require(ids == {item['attempt_id'] for item in audit['new_attempts']} and len(ids) == len(http_rows)
                == audit['new_unique_count'] == closed['physical_http'], '批次原生、HTTP及题目账本不一致')
        all_attempts.update(ids)
        models = sorted({item['terminal_receipt']['model_id'] for item in attempts})
        all_models.update(models)
        usages = token_totals([item['terminal_receipt']['usage'] or {} for item in attempts])
        times = [row['seconds'] for row in questions]
        missing = len(valid.keys() - {row['identity'] for row in questions}) + sum(
            row['input_state'] == 'missing' for row in questions if row['identity'] in valid)
        no_input = sum(row['input_state'] == 'not_reached' for row in questions if row['identity'] in valid)
        recalls = [len(row['selected'] & valid[row['identity']]) / len(valid[row['identity']])
                   for row in questions if row['identity'] in valid]
        complete = usages['complete']
        batches.append({'label': batch_labels[batch_index] if batch_labels else
                       'Initial batch' if batch_index == 0 else f'Repair batch {batch_index}',
            **({'actual_run_label': actual_run_label} if batch_labels else {}),
            'kind': 'initial' if batch_index == 0 else 'repair', 'model': models, 'policies': policies,
            'attempted_questions': len(questions), 'unattempted_questions': 48 - len(questions), 'physical_calls': len(attempts),
            'outcomes': {key: sum(row['status'] == key for row in questions)
                         for key in ('answered', 'clarify', 'failed', 'unpublished')},
            'failed_calls': sum(item['terminal_status'] != 'succeeded' for item in attempts), 'local_encoding': local_encoding,
            'preparation': {'vector_load_seconds': vector_load_seconds, 'index': prepared_index,
                            'timing_boundary': 'excluded_from_question_latency'},
            'quality': partition_quality(reader, reviews, identities,
                                         {row['identity']: row for row in questions}, source['revision']) if len(questions) == 48 else None,
            'evidence': {'boundary': 'last_dispatched_answer_input', 'denominator': 47,
                'missing_input_questions': missing, 'no_final_input_questions': no_input,
                'mean_recall': statistics.mean(recalls) if not missing else None,
                'all_found_questions': sum(valid[row['identity']] <= row['selected'] for row in questions
                    if row['identity'] in valid) if not missing else None},
            'latency_seconds': {'population': 'all_attempts', 'count': len(times), 'unknown_attempts': 0,
                'boundary': 'workbench_request', 'p95_method': 'nearest_rank',
                'median': statistics.median(times), 'p95': sorted(times)[math.ceil(.95 * len(times)) - 1],
                'total': sum(times), 'mean_seconds': statistics.mean(times)},
            'usage': {'known_subtotal': usages['recorded'], 'unknown_calls': usages['unknown_calls'],
                'complete_total': complete, 'mean_per_question': {key: complete[key] / len(questions)
                    for key in TOKEN_FIELDS} if complete is not None else None}})
    report = {'schema': 'rag-heldout-report@1', 'run_label': run_label, 'date': date,
            'model': sorted(all_models), 'policies': batches[0]['policies'],
            'dataset': {'questions': 48, 'evidence_eligible': 47, 'conversations': 4}, 'batches': batches}
    if environment_path is not None:
        environment = reader.read(environment_path)['environment']
        report['environment'] = {'logical_cpu_available': count(environment['logical_cpu_available']),
            'cpu_model': environment['cpu_model'], 'memory_bytes': count(environment['MemTotal_bytes'])}
    return report


def count(value):
    require(type(value) is int and value >= 0, '数量必须是非负整数')
    return value


def validate_heldout(report):
    require(report['schema'] == 'rag-heldout-report@1', '留出聚合格式不支持')
    require(report['dataset'] == {'questions': 48, 'evidence_eligible': 47, 'conversations': 4}, '留出数据分母不一致')
    require(report['batches'] and report['batches'][0]['kind'] == 'initial', '缺少初始完整批次')
    require(len({batch['label'] for batch in report['batches']}) == len(report['batches']), '批次身份重复')
    for index, batch in enumerate(report['batches']):
        require(batch['kind'] == ('initial' if index == 0 else 'repair'), '批次类型或顺序无效')
        if 'actual_run_label' in batch:
            require(isinstance(batch['actual_run_label'], str) and batch['actual_run_label'].strip(), '实际运行标签不能为空')
        require(batch['model'] and all(isinstance(value, str) and value for value in batch['model']), '批次模型未记录')
        require(all(key in batch['policies'] for key in ('compose', 'ladder', 'retrieve', 'rerank')), '批次策略未记录')
        attempted = count(batch['attempted_questions'])
        require(attempted <= 48 and (index != 0 or attempted == 48), '尝试题数与固定分母不一致')
        require(set(batch['outcomes']) == {'answered', 'clarify', 'failed', 'unpublished'}
                and sum(count(value) for value in batch['outcomes'].values()) == attempted, '运行结果未覆盖实际尝试题目')
        quality = batch['quality']
        if quality is not None:
            require(quality['source_kind'] == 'independent_partition_source_review', '质量必须来自分区独立事实审阅')
            require(quality['denominator'] == 48 and set(quality['counts']) == set(HELDOUT_VERDICTS), '质量分母或类别无效')
            require(sum(count(value) for value in quality['counts'].values()) == 48, '质量计数未覆盖48题')
            require(len(quality['reviewed_at']) == 2 and all(quality['reviewed_at']), '缺少两份审阅日期')
        evidence = batch['evidence']
        require(evidence['boundary'] == 'last_dispatched_answer_input' and evidence['denominator'] == 47, '证据边界或分母无效')
        require(count(evidence['missing_input_questions']) <= 47, '未记录证据输入题数无效')
        require(count(evidence['no_final_input_questions']) + evidence['missing_input_questions'] <= 47,
                '未进入最终回答与缺失输入题数无效')
        if evidence['mean_recall'] is not None:
            require(number(evidence['mean_recall']) <= 1 and evidence['missing_input_questions'] == 0,
                    '证据Recall必须覆盖全部有效题')
            ceiling = (47 - evidence['no_final_input_questions']) / 47
            require(evidence['mean_recall'] <= ceiling or math.isclose(evidence['mean_recall'], ceiling),
                    '证据Recall超过实际最终输入题数上限')
        if evidence['all_found_questions'] is not None:
            require(count(evidence['all_found_questions']) <= 47 - evidence['no_final_input_questions']
                    - evidence['missing_input_questions'] and evidence['mean_recall'] is not None, '完整证据题数无效')
        timing = batch['latency_seconds']
        require(timing['population'] == 'all_attempts' and count(timing['count']) >= attempted, '耗时必须覆盖全部尝试')
        require(count(timing['unknown_attempts']) <= timing['count'], '未知耗时数量无效')
        for field in ('median', 'p95', 'total', 'mean_seconds'):
            if timing[field] is not None:
                number(timing[field])
                require(timing['unknown_attempts'] == 0 and timing['count'] > 0, '不完整耗时不能呈现总体指标')
        calls = count(batch['physical_calls'])
        require(count(batch['failed_calls']) <= calls, '失败调用数量无效')
        usage = batch['usage']
        require(set(usage['known_subtotal']) == set(TOKEN_FIELDS), '词元字段不完整')
        for value in usage['known_subtotal'].values():
            count(value)
        require(count(usage['unknown_calls']) <= calls, '未知用量数量无效')
        require(usage['complete_total'] == (usage['known_subtotal'] if usage['unknown_calls'] == 0 else None),
                '不完整用量必须保留空总量')
        local = batch['local_encoding']
        require(local['scope'] == 'native_since_batch_baseline'
                and sum(count(local[key]) for key in ('succeeded_calls', 'failed_calls', 'unknown_calls')) == count(local['calls'])
                and count(local['pending_calls']) == local['unknown_calls'], '本机调用数量或基线范围不一致')
        number(local['known_seconds_subtotal'])
        require(local['complete_seconds'] == (None if local['unknown_calls'] else local['known_seconds_subtotal']),
                '本机未知调用不能补齐总耗时')
        average = usage['mean_per_question']
        if usage['complete_total'] is None or attempted == 0:
            require(average is None, '用量不完整时每题平均词元必须为空')
        else:
            require(average is not None and set(average) == set(TOKEN_FIELDS), '缺少完整每题平均词元')
            for field in TOKEN_FIELDS:
                require(math.isclose(number(average[field]), usage['complete_total'][field] / attempted),
                        '每题平均词元与实际尝试题数不一致')


def heldout_metrics(report):
    metrics = {}
    for index, batch in enumerate(report['batches']):
        prefix = f'batch-{index}-'
        if batch['quality'] is not None:
            metrics.update({prefix + 'quality-' + key: (value, 48) for key, value in batch['quality']['counts'].items()})
        evidence = batch['evidence']
        if evidence['mean_recall'] is not None:
            metrics[prefix + 'recall'] = (evidence['mean_recall'], 1)
        timing = batch['latency_seconds']
        scale = max((timing[key] or 0 for key in ('median', 'p95')), default=0) or 1
        for key in ('median', 'p95'):
            if timing[key] is not None:
                metrics[prefix + key] = (timing[key], scale)
        tokens = batch['usage']['known_subtotal']
        scale = max(tokens['input_tokens'], tokens['output_tokens'], 1)
        metrics.update({prefix + key: (tokens[key], scale) for key in ('input_tokens', 'output_tokens')})
    return metrics


def heldout_svg(report):
    validate_heldout(report)
    ET.register_namespace('', 'http://www.w3.org/2000/svg')
    ns = '{http://www.w3.org/2000/svg}'
    environment_offset = 30 if report.get('environment') else 0
    height = 165 + environment_offset + 550 * len(report['batches'])
    root = ET.Element(ns + 'svg', {'viewBox': f'0 0 960 {height}', 'role': 'img', 'aria-labelledby': 'title description'})
    ET.SubElement(root, ns + 'title', {'id': 'title'}).text = 'Held-out evaluation: answer quality, evidence recall, latency and tokens'
    ET.SubElement(root, ns + 'desc', {'id': 'description'}).text = (
        '48 fixed questions from four conversations; evidence denominator 47. '
        'Evidence uses the last actually dispatched answer input. Initial and repair batches remain separate. Quality uses independent source review. '
        'Latency includes all attempts. Missing usage remains unknown. Monetary cost is not recorded.')
    ET.SubElement(root, ns + 'metadata').text = json.dumps(report, ensure_ascii=False, sort_keys=True)
    rect, text = svg_drawing(root)
    rect(0, 0, 960, height, '#F7F4EE')
    text(40, 48, 'Held-out evaluation · 48 questions', 30, weight='600')
    text(40, 79, f"{report['date']} · {' / '.join(report['model'])} · 4 conversations", 17, '#4E4A44')
    versions = ' / '.join(f'{key} {report["policies"][key]}' for key in ('compose', 'ladder', 'retrieve', 'rerank'))
    initial_label = report['batches'][0]['label'] if 'actual_run_label' in report['batches'][0] else 'Initial'
    text(40, 108, initial_label + ': ' + versions, 16, '#4E4A44')
    if environment_offset:
        environment = report['environment']
        text(40, 134, f"Server · {environment['logical_cpu_available']} logical CPUs · {environment['cpu_model']} · {environment['memory_bytes'] / 1024 ** 3:.2f} GiB", 14, '#4E4A44')
    metrics = heldout_metrics(report)
    def bar(metric, x, y, label, color='#1C1A17'):
        value, scale = metrics[metric]
        text(x, y, label, 15)
        rect(x, y + 9, 400 * value / scale, 14, color,
             **{'data-metric': metric, 'data-value': str(value)})
    def display(value, suffix=''):
        return 'Not recorded' if value is None else f'{value:,.3f}{suffix}'
    for index, batch in enumerate(report['batches']):
        y = 146 + environment_offset + index * 550
        prefix = f'batch-{index}-'
        rect(40, y - 12, 880, 1, '#D2CABC')
        text(40, y + 17, f"{batch['label']} · {batch['attempted_questions']}/48 questions attempted", 22, weight='600')
        versions = ' / '.join(f'{key} {batch["policies"][key]}' for key in ('compose', 'ladder', 'retrieve', 'rerank'))
        text(40, y + 42, ' / '.join(batch['model']) + ' · ' + versions, 14, '#4E4A44')
        y += 26
        text(40, y + 54, 'Answer quality · fixed denominator 48', 18, weight='600')
        quality = batch['quality']
        if quality is None:
            text(40, y + 87, 'Awaiting independent source review', 16, '#6F6A62')
            outcomes = batch['outcomes']
            text(40, y + 119, f"Returned text {outcomes['answered']} · Clarify {outcomes['clarify']}", 16)
            text(40, y + 148, f"Failed {outcomes['failed']} · Unpublished {outcomes['unpublished']}", 16)
        else:
            for row, key in enumerate(HELDOUT_VERDICTS):
                bar(prefix + 'quality-' + key, 40, y + 84 + row * 37,
                    f'{key.capitalize()} {quality["counts"][key]}/48', '#C0282F' if key == 'partial' else '#1C1A17')
            text(40, y + 323, 'Reviewed ' + ' / '.join(quality['reviewed_at']), 14, '#4E4A44')
        evidence = batch['evidence']
        text(520, y + 54, 'Evidence Recall · 47 valid questions', 18, weight='600')
        if evidence['mean_recall'] is None:
            text(520, y + 87, f"Not recorded · {evidence['missing_input_questions']} missing inputs", 16, '#6F6A62')
        else:
            bar(prefix + 'recall', 520, y + 84, f"Mean {100 * evidence['mean_recall']:.2f}%", '#C0282F')
            text(520, y + 130, f"All evidence found: {evidence['all_found_questions']}/47", 16)
        text(520, y + 157, f"Last dispatched answer input · {evidence['no_final_input_questions']} not reached", 14, '#4E4A44')
        timing = batch['latency_seconds']
        text(520, y + 192, f"Latency · all {timing['count']} attempts", 18, weight='600')
        for row, key in enumerate(('median', 'p95')):
            label = f'{key.capitalize()} {display(timing[key], " s")}'
            if timing[key] is None:
                text(520, y + 222 + row * 41, label, 15, '#6F6A62')
            else:
                bar(prefix + key, 520, y + 222 + row * 41, label)
        text(520, y + 314, f"Total {display(timing['total'], ' s')} · Unknown {timing['unknown_attempts']}", 15)
        text(520, y + 339, f"Mean {display(timing['mean_seconds'], ' s')} / attempt", 15)
        usage = batch['usage']
        text(40, y + 362, f"Tokens · {batch['physical_calls']} calls / {batch['failed_calls']} failed", 18, weight='600')
        for field, x in (('input_tokens', 40), ('output_tokens', 520)):
            bar(prefix + field, x, y + 392, f"{field.replace('_', ' ').capitalize()} {usage['known_subtotal'][field]:,}",
                '#C0282F' if field == 'output_tokens' else '#1C1A17')
        complete = usage['complete_total']
        total = 'Unknown' if complete is None else f"{complete['total_tokens']:,}"
        text(40, y + 454, f"Known subtotal {usage['known_subtotal']['total_tokens']:,} · Complete total {total} · Unknown calls {usage['unknown_calls']}", 16)
        average = usage['mean_per_question']
        text(40, y + 481, 'Mean tokens / attempted question: ' + ('Unknown' if average is None else
             f"{average['input_tokens']:,.1f} input / {average['output_tokens']:,.1f} output / {average['total_tokens']:,.1f} total"), 15)
        local = batch['local_encoding']
        text(40, y + 508, f"Local since baseline · {local['calls']} calls / {local['failed_calls']} failed / {local['unknown_calls']} unknown · Known {local['known_seconds_subtotal']:.3f} s", 14, '#4E4A44')
    text(40, height - 13, 'Independent source grading · Separate fixed batches · Monetary cost not recorded', 14, '#4E4A44')
    return ET.tostring(root, encoding='utf-8', xml_declaration=True) + b'\n'


def validate_heldout_svg(content, report):
    root = ET.fromstring(content)
    ns = {'svg': 'http://www.w3.org/2000/svg'}
    require(root.find('svg:title', ns) is not None and root.find('svg:desc', ns) is not None, 'SVG缺少可访问文本')
    require(json.loads(root.find('svg:metadata', ns).text) == report, 'SVG聚合元数据不一致')
    expected = heldout_metrics(report)
    nodes = [node for node in root.iter() if 'data-metric' in node.attrib]
    require(len(nodes) == len(expected) and {node.attrib['data-metric'] for node in nodes} == set(expected), 'SVG指标覆盖不一致')
    for node in nodes:
        value, scale = expected[node.attrib['data-metric']]
        require(float(node.attrib['data-value']) == value and math.isclose(float(node.attrib['width']), 400 * value / scale),
                'SVG数值或条形比例不一致')


def main():
    parser = argparse.ArgumentParser(description='只读汇总已有RAG结果并生成公开聚合JSON和SVG，不调用模型。')
    parser.add_argument('--rag30', type=Path, help='原始RAG30结果目录')
    parser.add_argument('--pilot', type=Path, help='原始2026-10-10试点目录')
    parser.add_argument('--review', type=Path, help='独立事实裁决或明确标记的历史裁决记录')
    parser.add_argument('--summary', type=Path, help='从已公开聚合JSON重建图示，无需私有原文')
    parser.add_argument('--heldout', type=Path, action='append', help='真实工作台留出结果目录；初始批次在前，修复批次可追加')
    parser.add_argument('--batch-label', action='append', default=[], help='逐批展示标签，重复顺序对应--heldout；显式提供时核对实际运行标签')
    parser.add_argument('--partition-review', type=Path, action='append', default=[], help='独立来源审阅，每份24题，按批次源码来源绑定')
    parser.add_argument('--run-label', help='留出运行公开标签')
    parser.add_argument('--date', help='留出运行日期')
    parser.add_argument('--environment', type=Path, help='服务器真实环境记录，只公开CPU与内存字段')
    parser.add_argument('--local-recovery', type=Path, action='append', default=[], help='真实本机中断未知证明，保留原始pending计数')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--check', action='store_true', help='只核验已有输出，不写文件')
    args = parser.parse_args()
    reader = SourceReader()
    if args.summary:
        require(not any((args.rag30, args.pilot, args.review, args.heldout, args.batch_label, args.partition_review, args.run_label, args.date, args.environment, args.local_recovery)),
                '--summary与原始输入模式互斥')
        report = reader.read(args.summary)
    elif args.heldout:
        require(not any((args.rag30, args.pilot, args.review)) and args.run_label and args.date, '留出汇总需独立入口、运行标签和日期')
        validate_batch_labels(args.batch_label, len(args.heldout))
        require(not any(args.output.resolve().is_relative_to(path.resolve()) for path in args.heldout), '输出目录必须位于原始运行目录之外')
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
        report = heldout_report(reader, args.heldout, args.partition_review, args.run_label, args.date, args.environment,
                                args.local_recovery, batch_labels=args.batch_label)
    else:
        require(not any((args.batch_label, args.partition_review, args.run_label, args.date, args.environment, args.local_recovery)), '留出参数需配合--heldout')
        require(all((args.rag30, args.pilot, args.review)), '原始汇总需同时提供--rag30、--pilot、--review')
        require(not any(args.output.resolve().is_relative_to(path.resolve()) for path in (args.rag30, args.pilot)), '输出目录必须位于原始运行目录之外')
        report = {'schema': 'rag-public-report@1', 'rag30': rag30_report(reader, args.rag30, args.review),
                  'early_pilot': pilot_report(reader, args.pilot)}
    content = (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode('utf-8')
    if report['schema'] == 'rag-heldout-report@1':
        svg = heldout_svg(report)
        validate_heldout_svg(svg, report)
        artifacts = {'evaluation-heldout-summary.json': content, 'evaluation-heldout.svg': svg}
        metrics_checked, questions = len(heldout_metrics(report)), 48
    else:
        require(report['schema'] == 'rag-public-report@1', '聚合格式不支持')
        svg = svg_report(report)
        validate_svg(svg, report)
        artifacts = {'evaluation-summary.json': content, 'evaluation-rag30.svg': svg}
        metrics_checked, questions = 8, report['rag30']['questions']
    if args.check:
        for name, data in artifacts.items():
            require((args.output / name).read_bytes() == data, f'已有输出与汇总结果不一致：{name}')
    else:
        pending = []
        for name in artifacts:
            target = args.output / name
            require(target.resolve() not in reader.originals, '输出不能覆盖输入来源')
            if target.exists():
                require(target.read_bytes() == artifacts[name], f'已有输出内容冲突：{name}')
            else:
                pending.append(name)
        args.output.mkdir(parents=True, exist_ok=True)
        for name in pending:
            with (args.output / name).open('xb') as stream:
                stream.write(artifacts[name])
    reader.verify_unchanged()
    print(json.dumps({'source_files_unchanged': len(reader.originals), 'svg_metrics_checked': metrics_checked,
                      'questions': questions, 'new_model_calls': 0, 'check_only': args.check}))


if __name__ == '__main__':
    main()
