from dataclasses import dataclass
import re

from .policies import get


@dataclass(frozen=True)
class MarkedGroundedAnswer:
    value: object
    reasons: tuple[tuple[str, ...], ...]

    def model_dump(self, **kwargs):
        return self.value.model_dump(**kwargs)


def mark_semantic_failure(reasons, index, code):
    """只接受策略点名的语义原因，未知错误继续终止。"""
    if code not in get('compose', version='@37')(None, operation='unverified_publication')['codes']:
        raise ValueError(code)
    values = reasons.setdefault(index, [])
    if code not in values:
        values.append(code)


def publication_matches(public, expected):
    """布尔值不能借Python数值相等冒充声明编号或文字坐标。"""
    spans = public.get('unverified_claim_spans')
    if type(public.get('unverified_claims')) is not int or type(spans) is not list:
        return False
    for span in spans:
        if (type(span) is not dict or set(span) != {'start', 'end', 'claim', 'reason_codes'}
                or any(type(span.get(key)) is not int for key in ('start', 'end', 'claim'))
                or type(span.get('reason_codes')) is not list
                or any(type(code) is not str for code in span['reason_codes'])):
            return False
    return all(public.get(key) == value for key, value in expected.items())


def reviewed_marked_answer(output, catalog, first_output, *, chosen, question):
    from .answer_evidence import build_evidence_catalog, reviewed_referenced_answer
    if build_evidence_catalog(chosen, compose_version='@34') != catalog:
        raise ValueError('first_frozen_catalog_invalid')
    reasons = {}
    value = reviewed_referenced_answer(output, catalog, first_output, chosen=chosen,
        question=question, compose_version='@34', _unverified=reasons)
    return MarkedGroundedAnswer(value, tuple(tuple(reasons.get(index, ()))
        for index in range(1, len(value.statements) + 1)))


def _marked_sentences(text, reasons, claim, offset):
    """核验范围是整项声明，标记覆盖其中所有自然句，不细分语义结论。"""
    marker = '【未核实】'
    ends = [match.end() for match in re.finditer(r'[。！？]+[”’」』"]*|[.!?]+[”’"]*(?=\s|$)|\n+', text)]
    ends.append(len(text))
    parts, spans, cursor, size = [], [], 0, offset
    for end in sorted(set(ends)):
        segment = text[cursor:end]
        body = segment.rstrip()
        if body:
            parts.extend((body, marker, segment[len(body):]))
            spans.append({'start': size + len(body), 'end': size + len(body) + len(marker),
                'claim': claim, 'reason_codes': sorted(reasons)})
            size += len(segment) + len(marker)
        else:
            parts.append(segment)
            size += len(segment)
        cursor = end
    return ''.join(parts), spans


def render_marked_answer(output, catalog, first_partition, *, max_output_tokens,
                         inference, limitation, chosen, _on_publication=None):
    from ..structured_generation import AskOutput
    from .answer_evidence import grounded_statement_text, validate_grounded_answer
    from .budget import text_tokens

    # 渲染仍全量复验硬边界；语义原因只在本次派生对象中，不改模型原件。
    validate_grounded_answer(output.value, catalog, first_partition, chosen=chosen, _unverified={})
    units = {unit['id']: unit for unit in catalog['units']}
    positive = any(item.kind != 'limitation' and not reasons
        for item, reasons in zip(output.value.statements, output.reasons))
    lines, citations, count, spans = [], [], 0, []
    if not positive:
        lines.append(get('compose', version='@37')(None, operation='unverified_publication')['unknown'])
        # 只公开当轮完整目录中的真实已读来源，不把无依据声明附作答案。
        citations = list(dict.fromkeys(unit['source_number'] for unit in catalog['units']))
        if citations:
            lines.append(' '.join(f'[{number}]' for number in citations))
    else:
        for claim, (item, reasons) in enumerate(zip(output.value.statements, output.reasons), 1):
            numbers = list(dict.fromkeys(units[evidence.unit_id]['source_number'] for evidence in item.evidence))
            label = inference if item.kind == 'inference' else limitation if item.kind == 'limitation' else ''
            text = grounded_statement_text(item, units)
            if reasons:
                text, marks = _marked_sentences(text, reasons, claim,
                    sum(len(line) + 1 for line in lines) + len(label))
                spans.extend(marks)
            lines.append(label + text + ' ' + ' '.join(f'[{number}]' for number in numbers))
            count += bool(reasons)
            citations.extend(number for number in numbers if number not in citations)
    answer = '\n'.join(lines)
    raw = '\n'.join(item.text.strip() for item in output.value.statements)
    overhead = max(0, text_tokens(answer) - text_tokens(raw))
    if (type(max_output_tokens) is not int or max_output_tokens <= 0
            or text_tokens(raw) > max_output_tokens or text_tokens(answer) > max_output_tokens + overhead):
        raise ValueError('grounded_answer_render_limit_exceeded')
    if _on_publication is not None:
        _on_publication({'unverified_claims': count, 'unverified_claim_spans': spans})
    return AskOutput(answer=answer, citations=citations)
