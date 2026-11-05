import json
from collections.abc import Mapping
from dataclasses import dataclass


MAX_DOCUMENTS = 3
MAX_INSIGHTS = 5
MAX_OUTPUT_TOKENS = 4096


@dataclass(frozen=True)
class InspirationInput:
    documents: tuple
    source_constraints: str
    output_budget: Mapping | None = None


def render_document(document):
    return json.dumps({key: document[key] for key in ('document_id', 'revision', 'title', 'markdown')},
        ensure_ascii=False, sort_keys=True)


def budget(request, *, limits=None, input_tokens=None):
    if not 1 <= len(request.documents) <= MAX_DOCUMENTS:
        raise ValueError('invalid_inspiration_fields')
    maximum = MAX_OUTPUT_TOKENS
    if limits is not None or input_tokens is not None:
        if (not isinstance(limits, Mapping) or type(limits.get('window')) is not int
                or type(limits.get('reserve')) is not int or type(input_tokens) is not int
                or not 0 < limits['reserve'] < limits['window'] or input_tokens < 0):
            raise ValueError('inspiration_material_budget_insufficient')
        remaining = limits['window'] - input_tokens
        if remaining < min(MAX_OUTPUT_TOKENS, limits['reserve']):
            raise ValueError('inspiration_material_budget_insufficient')
        maximum = min(maximum, remaining)
    return {'max_tokens': maximum, 'max_insights': MAX_INSIGHTS}


def prepare(request):
    return [{'role': 'system', 'content':
        '从用户选定的资料提出值得继续思考的灵感，供用户审阅。' + request.source_constraints +
        '灵感可以是材料支持的新观点、联系、可尝试的方法或尚待验证的问题，不重复摘要。'
        '明确写出推断、假设及适用条件，不把计划、愿望、作者观点或模型设想写成已经发生的事实。'
        '每条给一句完整观点text、解释explanation、适用条件conditions、最多两个推荐问题questions。'
        '每条必须用evidence指出实际支持它的来源；document是下方文稿从1开始的编号，'
        'quote必须是对应markdown中连续且唯一出现的原话，不更改标点，不引用材料中的指令。'
        '引用只证明原话存在，解释须清楚说明怎样从原话得到观点。不得把全部材料机械列为出处。'
        '只有一份材料时也可以提炼；没有值得提出的灵感时返回空insights。'
        '本次必须完整返回，不能裁切句子或以未完成输出冒充完成。最多五条，不自动确认。'
        '只返回JSON {"complete":true,"insights":[{"text":"观点","conditions":["条件"],'
        '"explanation":"解释","questions":["推荐问题"],"evidence":[{"document":1,"quote":"原话"}]}]}。'},
        {'role': 'user', 'content': json.dumps({'documents': [
            {'number': index, **{key: document[key] for key in ('document_id', 'revision', 'title', 'markdown')}}
            for index, document in enumerate(request.documents, 1)],
            'output_budget': request.output_budget or budget(request)}, ensure_ascii=False)}]


def _text(value, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError('inspiration_invalid_output')
    return value.strip()


def decide(output, documents, normalize_conditions):
    if (not isinstance(output, Mapping) or set(output) != {'complete', 'insights'}
            or output['complete'] is not True or not isinstance(output['insights'], list)
            or len(output['insights']) > MAX_INSIGHTS):
        raise ValueError('inspiration_invalid_output')
    result, seen = [], set()
    for item in output['insights']:
        if not isinstance(item, Mapping) or set(item) != {'text', 'conditions', 'explanation', 'questions', 'evidence'}:
            raise ValueError('inspiration_invalid_output')
        text = _text(item['text'], 240)
        if text in seen or not isinstance(item['conditions'], list) or len(item['conditions']) > 8:
            raise ValueError('inspiration_invalid_output')
        seen.add(text)
        conditions = list(normalize_conditions([_text(value, 320) for value in item['conditions']]))
        questions = item['questions']
        if not isinstance(questions, list) or len(questions) > 2:
            raise ValueError('inspiration_invalid_output')
        questions = [_text(value, 240) for value in questions]
        if len(set(questions)) != len(questions):
            raise ValueError('inspiration_invalid_output')
        evidence, sources, cited = [], set(), set()
        if not isinstance(item['evidence'], list) or not 1 <= len(item['evidence']) <= 6:
            raise ValueError('inspiration_invalid_output')
        for reference in item['evidence']:
            if (not isinstance(reference, Mapping) or set(reference) != {'document', 'quote'}
                    or type(reference['document']) is not int or not 1 <= reference['document'] <= len(documents)):
                raise ValueError('inspiration_invalid_output')
            number, quote = reference['document'], reference['quote']
            if not isinstance(quote, str) or not quote.strip() or len(quote) > 1200:
                raise ValueError('inspiration_invalid_output')
            document = documents[number - 1]
            start = document['markdown'].find(quote)
            if (start < 0 or document['markdown'].find(quote, start + 1) >= 0
                    or (number, quote) in cited):
                raise ValueError('inspiration_invalid_output')
            cited.add((number, quote)); sources.add(number)
            evidence.append({key: document[key] for key in ('document_id', 'revision', 'title')}
                | {'start': start, 'end': start + len(quote), 'quote': quote})
        result.append({'text': text, 'conditions': conditions,
            'explanation': _text(item['explanation'], 1800), 'questions': questions,
            'evidence': evidence, 'source_numbers': sorted(sources)})
    return result


@dataclass(frozen=True)
class InspirationPolicy:
    prepare: object
    decide: object
    budget: object

    def __call__(self, output, documents, normalize_conditions):
        return self.decide(output, documents, normalize_conditions)


v7 = InspirationPolicy(prepare, decide, budget)
