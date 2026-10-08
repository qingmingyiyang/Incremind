"""用固定人工标注计分实际成果观测，保留缺失和失败样本的分母。"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from core.document_engine.markdown_sections import _HEADING, _lines
from backend.memory_app.v2.outcome_corrections import _paragraphs


def _outline(markdown):
    # 复用原 Markdown 行扫描，围栏中的标题文本不参与路径匹配。
    headings, parents = [], []
    for offset, end, line, visible in _lines(markdown):
        match = _HEADING.match(line) if visible else None
        if match is None:
            continue
        level, title = len(match[1]), match[2]
        while parents and parents[-1][0] >= level:
            parents.pop()
        parents.append((level, title))
        headings.append({'path': [item[1] for item in parents], 'level': level,
                         'start': offset, 'body_start': end})
    for index, heading in enumerate(headings):
        # 只计最小标题自己的正文，放入下级小节不能冒充父级落点。
        end = headings[index + 1]['start'] if index + 1 < len(headings) else len(markdown)
        heading['body'] = markdown[heading['body_start']:end]
    return headings


def _path(value):
    return isinstance(value, list) and bool(value) and all(isinstance(item, str) and item for item in value)


def _cases(fixture):
    if (not isinstance(fixture, dict) or type(fixture.get('schema_version')) is not int
            or fixture['schema_version'] != 1):
        raise ValueError('invalid outcome annotation schema')
    cases = fixture.get('cases')
    if not isinstance(cases, list) or not cases:
        raise ValueError('empty outcome annotations')
    identities = set()
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get('id'), str) or not case['id']:
            raise ValueError('invalid annotation identity')
        if case['id'] in identities:
            raise ValueError('duplicate annotation identity')
        identities.add(case['id'])
        if case.get('partition') not in {'calibration', 'heldout'}:
            raise ValueError('invalid annotation partition')
        expected = case.get('expected')
        if not isinstance(expected, dict) or 'continue_from' not in expected:
            raise ValueError('missing annotated selection')
        outcomes = case.get('outcomes')
        if not isinstance(outcomes, list) or any(not isinstance(item, dict) or not isinstance(item.get('key'), str) or not item['key'] for item in outcomes):
            raise ValueError('invalid annotated outcomes')
        keys = [item['key'] for item in outcomes]
        if len(keys) != len(set(keys)) or (expected['continue_from'] is not None and expected['continue_from'] not in keys):
            raise ValueError('invalid annotated continuation target')
        placements = expected.get('placements')
        if not isinstance(placements, list) or not placements:
            raise ValueError('missing annotated placements')
        for item in placements:
            if (not isinstance(item, dict) or item.get('kind') not in {'added', 'updated'}
                    or not _path(item.get('path')) or not isinstance(item.get('required_text'), str)
                    or not item['required_text'] or ('after_path' in item and not _path(item['after_path']))):
                raise ValueError('invalid annotated placement')
        protected = expected.get('protected_paragraphs', [])
        if not isinstance(protected, list) or any(not isinstance(item, str) or not item for item in protected):
            raise ValueError('invalid protected paragraphs')
    return cases


def _placement_hit(placement, headings, changes, continuation, prior_headings):
    matches = [(index, heading) for index, heading in enumerate(headings) if heading['path'] == placement['path']]
    if len(matches) != 1:
        return False
    index, heading = matches[0]
    if placement['required_text'] not in heading['body']:
        return False
    if 'after_path' in placement:
        before = [position for position, item in enumerate(headings) if item['path'] == placement['after_path']]
        if len(before) != 1 or before[0] >= index:
            return False
        anchor = headings[before[0]]
        # 新标题位于锚点完整子树之后，锚点的旧子标题不会使合法补丁失分。
        if (heading['level'] not in (anchor['level'], anchor['level'] + 1)
                or any(item['level'] <= anchor['level'] for item in headings[before[0] + 1:index])):
            return False
        if prior_headings:
            origins = [position for position, item in enumerate(prior_headings)
                       if item['path'] == placement['after_path']]
            if len(origins) != 1:
                return False
            origin = origins[0]
            for old in prior_headings[origin + 1:]:
                if old['level'] <= prior_headings[origin]['level']:
                    break
                retained = [position for position, item in enumerate(headings) if item['path'] == old['path']]
                if len(retained) != 1 or not before[0] < retained[0] < index:
                    return False
    if continuation:
        return any(isinstance(change, dict) and change.get('kind') == placement['kind']
                   and change.get('path') == placement['path'] for change in changes)
    return True


def _ratio(hits, count):
    return {'hits': hits, 'count': count, 'accuracy': hits / count if count else None}


def _paragraph_matches(block, protected):
    return block == protected or block.startswith(protected + '\n') or block.startswith(protected + '\r\n')


def _paragraph_sections(markdown, headings):
    sections = {None: _paragraphs(markdown[:headings[0]['start']] if headings else markdown)}
    for heading in headings:
        path = tuple(heading['path'])
        if path in sections:
            # 重复路径没有唯一归属，不能将另一处同文段落当成保留证据。
            sections[path] = None
        else:
            sections[path] = _paragraphs(heading['body'])
    return sections


def _prior_markdown(case):
    previous = next((row for row in case['outcomes']
                     if row['key'] == case['expected']['continue_from']), {})
    return previous.get('user_markdown', previous.get('markdown'))


def _protected_hit(case, markdown, headings):
    protected = case['expected'].get('protected_paragraphs', [])
    if not protected:
        return True
    prior = _prior_markdown(case)
    actual = _paragraph_sections(markdown, headings)
    if not isinstance(prior, str):
        return all(any(_paragraph_matches(block, paragraph)
                       for blocks in actual.values() if blocks is not None for block in blocks)
                   for paragraph in protected)
    original = _paragraph_sections(prior, _outline(prior))
    if any(not any(_paragraph_matches(block, paragraph) for blocks in original.values()
                   if blocks is not None for block in blocks) for paragraph in protected):
        return False
    for path, blocks in original.items():
        if blocks is None:
            return False
        kept = [block for block in blocks if any(_paragraph_matches(block, paragraph) for paragraph in protected)]
        if not kept:
            continue
        if actual.get(path) is None:
            return False
        position = 0
        for block in kept:
            found = next((index for index in range(position, len(actual[path]))
                          if _paragraph_matches(actual[path][index], block)), None)
            if found is None:
                return False
            position = found + 1
    return True


def _metrics(rows):
    def placements(selected):
        return _ratio(sum(row['placement_hits'] for row in selected), sum(row['placement_count'] for row in selected))
    return {'selection': _ratio(sum(row['selection_hit'] for row in rows), len(rows)),
            'placement': placements(rows),
            'continuation_placement': placements([row for row in rows if row['continuation']]),
            'new_placement': placements([row for row in rows if not row['continuation']])}


def observed_selection(execution, aliases):
    """计分冻结时实际选中的成果；最终新稿回退不会抹去选择错误。"""
    frozen = json.loads(execution['request']['input']['text'])
    selected = execution.get('outcome_selection')
    if frozen.get('outcome_selection') != selected:
        raise ValueError('outcome selection differs from saved request')
    if selected is None:
        return None
    identity = selected['document_id']
    matches = [key for key, document in aliases.items() if document == identity]
    if len(matches) != 1:
        raise ValueError('selected outcome is not a unique seeded alias')
    return matches[0]


def observed_policies(execution):
    """从原不可变输入回读保存版本，命令行标签不充当生产证据。"""
    from backend.memory_app.v2.policies import get
    frozen = json.loads(execution['request']['input']['text'])
    if not isinstance(frozen, dict):
        raise ValueError('invalid saved outcome input')
    result = {}
    for interface, container, key in (('continuation','outcome_input','continuation_policy'),
                                      ('style','style_input','version')):
        if container not in frozen:
            continue
        block = frozen[container]
        if not isinstance(block, dict) or not isinstance(block.get(key), str):
            raise ValueError('invalid saved policy evidence')
        get(interface, version=block[key])
        result[interface] = block[key]
    return result


def _policy_evidence(selections, observations):
    from backend.memory_app.v2.policies import get
    requested = {}
    for selection in selections:
        interface, separator, version = selection.partition('=')
        if not separator or not interface or not version or '=' in version or interface in requested:
            raise ValueError('invalid evaluation policy selection')
        get(interface, version=version)
        requested[interface] = version
    for observation in observations:
        # 失败与缺失样本保留在计分分母，不虚构它们未能冻结的版本。
        if observation.get('error'):
            continue
        saved = observation.get('policies')
        if requested and (not isinstance(saved, dict) or any(saved.get(key) != value
                                                           for key, value in requested.items())):
            raise ValueError('policy evidence differs from requested evaluation policy')
    return requested


def evaluate(fixture, observations):
    """计分已交付正文与回执；协调器执行和出生证据由观测生产者负责。"""
    cases = _cases(fixture)
    identities = {case['id'] for case in cases}
    if not isinstance(observations, list):
        raise ValueError('invalid outcome observations')
    observed = {}
    for item in observations:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str) or item['id'] not in identities:
            raise ValueError('unknown observation identity')
        if item['id'] in observed:
            raise ValueError('duplicate observation identity')
        observed[item['id']] = item
    rows = []
    for case in cases:
        item, expected = observed.get(case['id'], {}), case['expected']
        markdown, changes = item.get('markdown'), item.get('changes')
        delivered = (not item.get('error') and isinstance(item.get('document_id'), str) and bool(item['document_id'])
                     and isinstance(markdown, str) and bool(markdown.strip()) and isinstance(changes, list)
                     and 'selected_key' in item)
        selection_hit = bool(delivered and item['selected_key'] == expected['continue_from'])
        headings = _outline(markdown) if delivered else []
        protected = delivered and _protected_hit(case, markdown, headings)
        prior = _prior_markdown(case)
        prior_headings = _outline(prior) if isinstance(prior, str) else []
        continuation = expected['continue_from'] is not None
        hits = [bool(selection_hit and protected and _placement_hit(placement, headings, changes, continuation, prior_headings))
                for placement in expected['placements']]
        rows.append({'id': case['id'], 'partition': case['partition'], 'continuation': continuation,
                     'selection_hit': selection_hit, 'placement_hits': sum(hits), 'placement_count': len(hits),
                     'placements': [{'path': placement['path'], 'hit': hit}
                                    for placement, hit in zip(expected['placements'], hits)],
                     'delivery_available': bool(delivered)})
    return {'evaluation_notes': fixture.get('evaluation_notes', ''),
            'overall': _metrics(rows),
            'partitions': {partition: _metrics([row for row in rows if row['partition'] == partition])
                           for partition in ('calibration', 'heldout')},
            'cases': rows,
            'metric_definitions': {
                'selection': '匹配人工指定成果或新写；未交付、失败和缺失样本保留在分母中。',
                'placement': '选择正确且内容位于唯一最小标题正文；续写同时匹配回执改动，新写不要求补丁回执。保护段保留段落边界，有上一版正文时同时核对标题归属、副本数量和原先后顺序。',
                'quality_boundary': '标注补丁管线的结构计分不表示真实模型自主生成补丁的质量。'}}


def main(argv=None):
    """读取外部观测报告；不从人工答案伪造交付或启动正式应用。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', type=Path, default=ROOT / 'tests/fixtures/outcome_eval/cases.json')
    parser.add_argument('--observations', type=Path, required=True,
                        help='观测列表文件，或真实管线逐组写入 JSON 的目录')
    parser.add_argument('--output', type=Path, default=ROOT / 'work/qa/T13.7/evaluation.json')
    parser.add_argument('--policy', action='append', default=[], metavar='NAME=@VERSION',
                        help='核对真实生产观测的保存版本；可重复指定多个接口')
    args = parser.parse_args(argv)
    try:
        fixture = json.loads(args.cases.read_text(encoding='utf-8-sig'))
        if args.observations.is_dir():
            observations = [json.loads(path.read_text(encoding='utf-8-sig'))
                            for path in sorted(args.observations.glob('*.json'))]
        else:
            observations = json.loads(args.observations.read_text(encoding='utf-8-sig'))
        report = {'mode': 'observations', **evaluate(fixture, observations)}
        report['policy_versions'] = _policy_evidence(args.policy, observations)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'mode': report['mode'], 'case_count': len(report['cases']),
                      'overall': report['overall']}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
