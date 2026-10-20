import argparse
import json
import math
from pathlib import Path
import statistics
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
    def rect(x, y, width, height, color, **attributes):
        ET.SubElement(root, ns + 'rect', {'x': str(x), 'y': str(y), 'width': str(width), 'height': str(height), 'fill': color, **attributes})
    def text(x, y, value, size=18, color='#1C1A17', weight='400'):
        ET.SubElement(root, ns + 'text', {'x': str(x), 'y': str(y), 'fill': color, 'font-size': str(size),
            'font-family': 'Segoe UI, Noto Sans SC, Arial, sans-serif', 'font-weight': weight}).text = value
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


def main():
    parser = argparse.ArgumentParser(description='只读汇总已有RAG结果并生成公开聚合JSON和SVG，不调用模型。')
    parser.add_argument('--rag30', type=Path, help='原始RAG30结果目录')
    parser.add_argument('--pilot', type=Path, help='原始2026-10-10试点目录')
    parser.add_argument('--review', type=Path, help='独立事实裁决或明确标记的历史裁决记录')
    parser.add_argument('--summary', type=Path, help='从已公开聚合JSON重建图示，无需私有原文')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--check', action='store_true', help='只核验已有输出，不写文件')
    args = parser.parse_args()
    reader = SourceReader()
    if args.summary:
        require(not any((args.rag30, args.pilot, args.review)), '--summary与原始输入模式互斥')
        report = reader.read(args.summary)
    else:
        require(all((args.rag30, args.pilot, args.review)), '原始汇总需同时提供--rag30、--pilot、--review')
        require(not any(args.output.resolve().is_relative_to(path.resolve()) for path in (args.rag30, args.pilot)), '输出目录必须位于原始运行目录之外')
        report = {'schema': 'rag-public-report@1', 'rag30': rag30_report(reader, args.rag30, args.review),
                  'early_pilot': pilot_report(reader, args.pilot)}
    require(report['schema'] == 'rag-public-report@1', '聚合格式不支持')
    content = (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode('utf-8')
    svg = svg_report(report)
    validate_svg(svg, report)
    artifacts = {'evaluation-summary.json': content, 'evaluation-rag30.svg': svg}
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
    print(json.dumps({'source_files_unchanged': len(reader.originals), 'svg_metrics_checked': 8,
                      'questions': report['rag30']['questions'], 'new_model_calls': 0, 'check_only': args.check}))


if __name__ == '__main__':
    main()
