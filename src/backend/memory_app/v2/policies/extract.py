"""Version one of the current short-insight prompt and output decisions."""
import json

from .types import ExtractInput, ExtractDecodeInput, ExtractOutput, ModelPolicy


def prepare(request: ExtractInput) -> list[dict[str, str]]:
    return [{"role": "system", "content":
        "根据材料提出待人工审核的短认识，不要自动发布。" + request.source_constraints +
        '只返回JSON {"insights":[{"text":"≤40字","conditions":[]}]}，最多3条，每条正文不超过40字。'},
        {"role": "user", "content": json.dumps({"experiences": list(request.experiences)}, ensure_ascii=False)}]


def decide(request: ExtractDecodeInput) -> ExtractOutput:
    output, rows, errors = request.output, [], []
    try:
        if not isinstance(output, dict) or set(output) != {"insights"} or not isinstance(output["insights"], list):
            raise ValueError
        for row in output['insights']:
            if (not isinstance(row, dict) or set(row) != {"text", "conditions"}
                    or not isinstance(row["text"], str) or not row["text"].strip()
                    or not isinstance(row["conditions"], list)):
                raise ValueError
            conditions = request.normalize_conditions(row['conditions'])
            text = row['text'].strip()
            if len(text) > 40:
                errors.append('insight_too_long')
                continue
            rows.append((text, conditions))
    except (ValueError, TypeError):
        return ExtractOutput((), (*errors, 'insight_invalid_output'), valid=False)
    return ExtractOutput(tuple(rows[:3]), tuple(errors))


v1 = ModelPolicy(prepare, decide)


def prepare_comparative(request: ExtractInput) -> list[dict[str, str]]:
    return [{"role": "system", "content":
        "对照近邻认识，只提炼材料带来的差异，提出待人工审核的认识，不自动发布。" + request.source_constraints +
        "三类：supplement补充新条件、例子或做法；differs主张不同；new_method近邻没有的方法。"
        "正文不超过40字，最多3条，方法必须写conditions说明什么时候该想起，条件不计字数。"
        "明显有时效的流行、价格、营业状态、规定须在conditions写明材料中的时间。"
        "仅重复已有认识不生成候选，只在supports提出支持建议，引用近邻id。"
        "relation只能为new（new_method）、supplement、differs或may_supersede（differs且可能取代）。"
        "target_id只能取近邻id，new_method为null。scope_hint仅在属于本人通用立场时为me，"
        "其他为清单项目id或null，不把资料观点当本人的立场。"
        '只返回JSON {"insights":[{"kind":"supplement|differs|new_method","relation":"new|supplement|differs|may_supersede",'
        '"text":"正文","conditions":["适用条件"],"target_id":null,"scope_hint":null}],'
        '"supports":[{"target_id":"近邻id","evidence":"材料怎样印证，≤300字"}]}。'},
        {"role": "user", "content": json.dumps({"experiences": list(request.experiences),
            "neighbors": list(request.neighbors), "projects": list(request.projects),
            "project_id": request.project_id}, ensure_ascii=False)}]


def decide_comparative(request: ExtractDecodeInput) -> ExtractOutput:
    rows, hints, supports, errors = [], [], [], []
    output = request.output
    targets = {row['id'] for row in request.neighbors}
    destinations = {row['id'] for row in request.projects} | {'me', request.project_id}
    allowed = {'supplement': {'supplement'}, 'differs': {'differs', 'may_supersede'}, 'new_method': {'new'}}
    try:
        if (not isinstance(output, dict) or set(output) != {'insights', 'supports'}
                or not isinstance(output['insights'], list) or not isinstance(output['supports'], list)
                or len(output['supports']) > 5):
            raise ValueError
        for row in output['insights']:
            if (not isinstance(row, dict) or set(row) != {'kind', 'relation', 'text', 'conditions', 'target_id', 'scope_hint'}
                    or not isinstance(row['kind'], str) or row['kind'] not in allowed
                    or row['relation'] not in allowed[row['kind']]
                    or not isinstance(row['text'], str) or not row['text'].strip()
                    or not isinstance(row['conditions'], list)
                    or row['scope_hint'] is not None and not isinstance(row['scope_hint'], str)):
                raise ValueError
            if row['kind'] == 'new_method':
                if row['target_id'] is not None:
                    raise ValueError
            elif not isinstance(row['target_id'], str) or row['target_id'] not in targets:
                raise ValueError
            conditions = request.normalize_conditions(row['conditions'])
            if row['kind'] == 'new_method' and not conditions:
                raise ValueError
            text = row['text'].strip()
            if len(text) > 40:
                errors.append('insight_too_long')
                continue
            rows.append((text, conditions))
            hints.append({'relation': row['relation'], 'target_id': row['target_id'],
                'scope_hint': row['scope_hint'] if row['scope_hint'] in destinations else None})
        for row in output['supports']:
            if (not isinstance(row, dict) or set(row) != {'target_id', 'evidence'}
                    or not isinstance(row['target_id'], str) or row['target_id'] not in targets
                    or not isinstance(row['evidence'], str) or not row['evidence'].strip() or len(row['evidence']) > 300):
                raise ValueError
            supports.append({'relation': 'duplicate_of', 'target_id': row['target_id'], 'evidence': row['evidence'].strip()})
    except (ValueError, TypeError):
        return ExtractOutput((), (*errors, 'insight_invalid_output'), valid=False)
    return ExtractOutput(tuple(rows[:3]), tuple(errors), hints=tuple(hints[:3]), supports=tuple(supports))


v2 = ModelPolicy(prepare_comparative, decide_comparative)
