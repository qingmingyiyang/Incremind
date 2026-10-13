"""Versions of the ladder's per-layer caps and the answer token budget.

Version one keeps the original design: per-layer caps from T3.3, neighbor
expansion from T10.9 and the token budget from T10.4 (total budget is the
smaller of budget_cap and half of the model window after the output
reserve, fallback_budget when the window is unknown; evidence may use
evidence_share of it). Later versions are candidates for offline comparison.
"""

_V1 = {
    'layers': (('L3', 3), ('L2', 2), ('L1', 2), ('L0', 2)),
    'persona': 2,
    'neighbors': 2,
    'budget_cap': 6000,
    'fallback_budget': 4000,
    'evidence_share': 0.8,
}


def v1():
    return dict(_V1)


def v2():
    """Three organized-note and three original excerpts."""
    return {**_V1, 'layers': (('L3', 3), ('L2', 2), ('L1', 3), ('L0', 3))}


def v3():
    """An 8,000-token budget cap."""
    return {**_V1, 'budget_cap': 8000}


def v4():
    """Both: three lower excerpts and an 8,000-token cap."""
    return {**v2(), 'budget_cap': 8000}


def multi_evidence(question):
    """只有明确的列举问法扩大来源数，总词元预算保持不变。"""
    import re
    return bool(re.search(r'\b(?:ways|examples|list|compare)\b|\b(?:what|which)\s+(?:are|do|does)\b|\b(?:what|which)\s+\w{3,}s\s+(?:are|have|did|does|is)\b|哪些|分别|列举|有哪些|对比', question.casefold()))


def v5():
    return v2()


def limits(question):
    return {**v2(), 'layers': (('L3', 3), ('L2', 2), ('L1', 5), ('L0', 5))} if multi_evidence(question) else v2()


v5.limits = limits
from . import register
register('ladder', '@5')(v5)


def v6():
    """保留来源数量与输出预留，在已知模型窗口内增加完整证据预算。"""
    return {**v5(), 'input_budget_share': (2, 3)}


def limits_v6(question):
    return {**limits(question), 'input_budget_share': (2, 3)}


v6.limits = limits_v6
register('ladder', '@6')(v6)


def v7():
    """在输出预留不变的硬窗口内增加逐项原话覆盖候选预算。"""
    return {**v6(), 'input_budget_share': (1, 1), 'budget_cap': 9000}


def limits_v7(question):
    return {**limits_v6(question), 'input_budget_share': (1, 1), 'budget_cap': 9000}


v7.limits = limits_v7
register('ladder', '@7')(v7)


def v8():
    """选材沿用旧候选，仅声明经可信实际端点确认的总容量。"""
    return v7()


def context_window_v8(provider, model, base_url):
    from urllib.parse import urlsplit
    # 官方能力与实际请求地址绑定；代理或未知路由仍使用旧保守窗口。
    if (provider not in {'openai', 'deepseek'} or model != 'deepseek-flash'
            or not isinstance(base_url, str) or base_url != base_url.strip()
            or any(ord(char) < 32 for char in base_url)):
        return 16000
    try:
        parsed = urlsplit(base_url)
        trusted = (parsed.scheme == 'https' and parsed.hostname == 'api.deepseek.com'
                   and parsed.netloc.lower() in {'api.deepseek.com', 'api.deepseek.com:443'}
                   and parsed.port in {None, 443} and parsed.username is None and parsed.password is None
                   and parsed.path in {'', '/', '/v1', '/v1/'} and not parsed.query and not parsed.fragment)
    except ValueError:
        return 16000
    # 官方 deepseek-flash 支持 1M；产品总容量上限固定为 200000。
    return 200000 if trusted else 16000


v8.limits = limits_v7
v8.context_window = context_window_v8
register('ladder', '@8')(v8)


# 新候选的需求和边际规则均为启发式，停止理由不能当作语义充分证明。
_DYNAMIC_FUNCTION_WORDS = frozenset('a an the and or to of in on at for with what which when where who why how did does do is are was were has have can could would should please answer briefly only using supplied evidence cite supporting source numbers if insufficient say so english chinese respond response question tell me about list give describe explain would happened happens been had have has done 做了 什么 时候 何时 请问 请说 告诉 哪些 怎么 是否'.split())


def v9():
    return {**v8(), 'layers': tuple((layer, None) for layer, _ in v8()['layers']),
            'budget_cap': None, 'evidence_share': None}


def input_budget_v9(window, reserve, *, fixed_tokens=0):
    if type(fixed_tokens) is not int or fixed_tokens < 0:
        raise ValueError('invalid_dynamic_fixed_tokens')
    known = type(window) is int and window > 0 and type(reserve) is int and reserve >= 0
    return max(0, (window - reserve if known else v9()['fallback_budget']) - fixed_tokens)


def demand_terms_v9(question):
    from core.search_and_recall.evidence_windows import query_terms
    # 检索整句不是需求词；逐行处理保留用户多行任务，明确输出语言等虚词不充当目标。
    return tuple(dict.fromkeys(term for line in question.splitlines()
        for term, weight in query_terms(line)
        if (weight != 5 or term.isascii() and term.isalpha() or len(term) <= 2)
        and term not in _DYNAMIC_FUNCTION_WORDS))


def event_states_v9(text):
    import re
    return tuple(name for name, pattern in (
        ('planned', r'\b(?:plans?|planned|planning|hopes?|hoped|going to|will)\b|计划|打算|希望'),
        ('completed', r'\b(?:went|attended|finished|completed|visited|moved|joined|started)\b|已经|完成|参加了|搬到了'),
        ('uncertain', r'\b(?:might|maybe|possible|thinking about)\b|可能|考虑')) if re.search(pattern, text, re.I))


def requested_states_v9(question):
    import re
    states = event_states_v9(question)
    # 过去时问法提供需求约束；来源中的 has/have 等持有表达不能充当完成证据。
    if not set(states) & {'planned', 'uncertain'} and re.search(r'\b(?:did|was|were|has|have|had)\b|曾经|过了', question, re.I):
        return tuple(dict.fromkeys((*states, 'completed')))
    return states


def demand_v9(question, *, reference_score=0, semantic_anchors=()):
    import re
    # 原问题决定需求；改写和缺口查询不能替换原始目标。
    text = question
    targets = demand_terms_v9(text)
    if not isinstance(semantic_anchors, (tuple, list)) or any(
            not isinstance(anchor, str) for anchor in semantic_anchors):
        raise ValueError('invalid_ladder_semantic_anchors')
    semantic_anchors = tuple(anchor.strip() for anchor in semantic_anchors if anchor.strip())
    resolved = tuple(dict.fromkeys(term for anchor in semantic_anchors for term in demand_terms_v9(anchor)))
    resolved_names = {word.casefold() for anchor in semantic_anchors for word in re.findall(r'\b[A-Z][a-z]+\b', anchor)}
    # 已解析检索问法只辅助正文相关性；原任务及其时间、状态和引用要求保持原样。
    resolved_dimensions = tuple(term for term in resolved if term not in resolved_names)
    anchors = {word.casefold() for word in re.findall(r'\b[A-Z][a-z]+\b', text)}
    dimensions = tuple(term for term in targets if term not in anchors)
    return {'question': question, 'mode': 'open' if multi_evidence(text) or re.search(r'比较|compare|versus|\bvs\b', text, re.I) else 'finite',
            'targets': targets, 'dimensions': dimensions, 'resolved_targets': resolved,
            'resolved_dimensions': resolved_dimensions, 'clauses': tuple(part.strip() for part in re.split(r'[;；?？\n]', text) if part.strip()),
            'date_roles': ('event_time',) if re.search(r'\bwhen\b|日期|什么时候|哪天', text, re.I) else (),
            'requires_original': bool(re.search(r'原文|原话|出处|引用|\b(?:quote|verbatim)\b', text, re.I)),
            'reference_score': max(0.0, float(reference_score))}


def statements_v9(chosen):
    import re
    # 来源问句及无终止标点的片段不能凭说话人冒号成为陈述证据。
    return tuple(line.strip().casefold() for row in chosen if not row.get('persona')
        for line in re.split(r'(?<=[.!。！])\s+|\n', row['excerpt']) if not re.search(r'[?？]', line)
        and re.search(r'[^\s:：][.!。！]\s*$', line))


def progress_v9(demand, chosen):
    import re
    statements = statements_v9(chosen)
    text = '\n'.join(statements)
    missing = tuple(term for term in demand['targets'] if term not in text)
    # 数量、页码和记录 ID 不是日期；这里只登记时间线索，不推导事件已发生。
    temporal = r'(?<![\w])(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}/\d{1,2})(?![\w])|\b(?:january|february|march|april|may(?=\s+\d)|june|july|august|september|october|november|december|monday|tuesday|wednesday|thursday|friday|saturday|sunday|yesterday|today|tomorrow)\b|\bin may\b|\b(?:last|next|this|previous|following)\s+(?:day|week|month|year)\b|\b\d+\s+(?:days?|weeks?|months?|years?)\s+(?:ago|later|before|after)\b|\d{2,4}年|\d{1,2}月(?:\d{1,2}[日号])?|昨天|今天|明天|(?:上|下|本|这)(?:周|星期|个月|月|年)|[一二三四五六七八九十两\d]+(?:天|周|个月|年)(?:前|后)'
    states = event_states_v9(text)
    # 每项任务分别匹配原文中的陈述和时态；计划、可能与已完成不能互相充当证明。
    requirements = [(demand_terms_v9(clause), requested_states_v9(clause)) for clause in demand['clauses']]
    def matches(statement, targets, wanted):
        return (all(term in statement for term in targets)
            and (not wanted or all(state in event_states_v9(statement) for state in wanted))
            and ('completed' not in wanted or not set(event_states_v9(statement)) & {'planned', 'uncertain'}))
    state_match = all(any(matches(statement, targets, wanted) for statement in statements)
        for targets, wanted in requirements if targets)
    # 时间线索必须与所求事件及状态同处局部陈述；无关事件的日期不能补足缺口。
    date_detail = not demand['date_roles'] or all(any(matches(statement, targets, wanted)
        and re.search(temporal, statement) for statement in statements)
        for targets, wanted in requirements if targets)
    original = not demand['requires_original'] or any(row['layer'] in {'L1', 'L0'} for row in chosen)
    complete_statement = bool(statements)
    sufficient = (demand['mode'] == 'finite' and bool(demand['targets']) and not missing
                  and date_detail and original and complete_statement and state_match)
    return {'missing': missing, 'date_detail': date_detail, 'event_states': states, 'state_match': state_match, 'original': original,
            'heuristic_sufficient': sufficient}


def marginal_v9(candidate, chosen, demand):
    import re
    text = candidate['excerpt'].casefold()
    if not text.strip() or any(text == row['excerpt'].casefold() for row in chosen):
        return 0.0
    score = max(0.0, float(candidate.get('score', 0)))
    hits = sum(term in text for term in demand['targets'])
    lexical = hits / max(1, len(demand['targets']))
    semantic = max(0.0, float(candidate.get('vector_score', candidate.get('similarity', 0)) or 0))
    reference = demand['reference_score']
    # 不能仅凭新来源 ID 或泛人名判增量；要求正文目标、相对分数及独立实质文本。
    dimension_hit = any(term in text for term in demand['dimensions'])
    relevant = semantic >= .3 or ((not demand['dimensions'] or dimension_hit)
        and (lexical >= .5 or hits > 0 and score > 0 and score >= reference * .5))
    resolved_hit = any(term in text for term in demand['resolved_targets'])
    resolved_dimension_hit = any(term in text for term in demand['resolved_dimensions'])
    relevant = relevant or (resolved_hit and score > 0 and score >= reference * .5
        and (not demand['resolved_dimensions'] or resolved_dimension_hit))
    words = set(re.findall(r'\w+', text)) - _DYNAMIC_FUNCTION_WORDS - set(demand['targets'])
    previous = set(re.findall(r'\w+', '\n'.join(row['excerpt'].casefold() for row in chosen)))
    novelty = len(words - previous) / max(1, len(words))
    if not relevant:
        return 0.0
    if relational_gain_v9(candidate, chosen, demand):
        return max(lexical, semantic, score / (1 + score))
    if not words or novelty < .15:
        return 0.0
    return max(lexical, semantic, score / (1 + score)) * novelty


def relational_gain_v9(candidate, chosen, demand):
    import re
    if any(candidate['excerpt'].casefold() == row['excerpt'].casefold() for row in chosen):
        return False
    before, after = progress_v9(demand, chosen), progress_v9(demand, [*chosen, candidate])
    improvement = (len(after['missing']) < len(before['missing'])
        or any(not before[key] and after[key] for key in ('date_detail', 'state_match', 'original')))
    # 词已出现也可能补足事件与日期或对象与属性关系；只比较连续陈述内的有序词对。
    def relations(rows):
        pairs = set()
        for statement in statements_v9(rows):
            tokens = [word for word in re.findall(r'\w+', statement) if word not in _DYNAMIC_FUNCTION_WORDS]
            if not any(term in statement for term in (*demand['targets'], *demand['resolved_targets'])):
                continue
            pairs.update(zip(tokens, tokens[1:]))
        return pairs
    new_relations = relations([candidate]) - relations(chosen)
    return improvement or bool(new_relations)


def context_ranges_v9(content):
    import re
    # 原段落或对话记录为最小完整范围；巨型单元放不下时拒绝，不裁掉问尾或否定。
    boundaries = {0, len(content)}
    boundaries.update(match.end() for match in re.finditer(r'\n[ \t]*\n', content))
    boundaries.update(match.start() for match in re.finditer(r'^\[[^\]\n]+\]\s+[^:\n]+:', content, re.M))
    points = sorted(boundaries)
    return tuple((left, right) for left, right in zip(points, points[1:]) if right > left)


def seed_order_v9(chunks, question, title, summary, vectors):
    terms = [(term, 1) for term in demand_terms_v9(question)]
    prefix = (title + '\n' + summary).casefold()
    ranked = []
    for index, chunk in enumerate(chunks):
        body = chunk.text.casefold()
        hit = sum(weight for term, weight in terms if term in body)
        contextual = sum(weight for term, weight in terms if term in prefix)
        similarity = max(0.0, min(1.0, float(vectors.get(index, 0))))
        if not hit and not contextual and similarity < .3:
            continue
        score = hit + .2 * contextual + similarity
        ranked.append((score, -index, index))
    return tuple(index for _, _, index in sorted(ranked, reverse=True))


v9.limits = lambda question: v9()
v9.context_window = context_window_v8
v9.input_budget = input_budget_v9
v9.demand = demand_v9
v9.progress = progress_v9
v9.marginal = marginal_v9
v9.relational_gain = relational_gain_v9
v9.context_ranges = context_ranges_v9
v9.seed_order = seed_order_v9
register('ladder', '@9')(v9)



def v10():
    """容量是硬上限，有限任务按真实相关性与需求增量收敛。"""
    return v9()


def demand_v10(question, *, reference_score=0, semantic_anchors=()):
    import re
    def normalize(text):
        return re.sub(r"\b(\w+)[’']s\b", r'\1', text)
    if not isinstance(semantic_anchors, (tuple, list)) or any(not isinstance(value, str) for value in semantic_anchors):
        raise ValueError('invalid_ladder_semantic_anchors')
    result = demand_v9(normalize(question), reference_score=reference_score,
        semantic_anchors=tuple(normalize(value) for value in semantic_anchors))
    result['question'] = question
    for key in ('targets', 'dimensions', 'resolved_targets', 'resolved_dimensions'):
        result[key] = tuple(term for term in result[key] if term not in {'s', 'its', 'their', 'her', 'his'})
    return result


def contains_v10(term, text):
    import re
    if not term.isascii():
        return term in text.casefold()
    # 与既有 rank 的整词边界一致，补上问题与陈述之间的简单时态变化。
    term = term.casefold()
    forms = {term}
    if term.isalpha() and len(term) > 2:
        stem = term[:-1] if term.endswith('s') and not term.endswith(('ss', 'us')) else term
        forms.update((stem, stem+'s', stem+'ed', stem+'ing'))
        if stem.endswith('e'):
            forms.update((stem+'d', stem[:-1]+'ing'))
    return bool(re.search(r'(?<!\w)(?:'+ '|'.join(re.escape(form) for form in forms) +r')(?!\w)', text, re.I))


def progress_v10(demand, chosen):
    result = progress_v9(demand, chosen)
    text = '\n'.join(statements_v9(chosen))
    result['missing'] = tuple(term for term in demand['targets'] if not contains_v10(term, text))
    result['heuristic_sufficient'] = result['heuristic_sufficient'] and not result['missing']
    return result


def relevance_v10(candidate, demand):
    from .rank import MIN_SIMILARITY
    text = candidate['excerpt']
    dimensions = tuple(dict.fromkeys((*demand['dimensions'], *demand['resolved_dimensions'])))
    hits = tuple(term for term in dimensions if contains_v10(term, text))
    semantic = max(0.0, float(candidate.get('vector_score', candidate.get('similarity', 0)) or 0))
    # 标题、人物定位词和高通用分数不能替代正文维度或已绑定向量片段。
    return hits, semantic if semantic >= MIN_SIMILARITY else 0.0


def relations_v10(rows, demand, *, semantic=False):
    import re
    dimensions = (*demand['dimensions'], *demand['resolved_dimensions'])
    pairs = set()
    for statement in statements_v9(rows):
        if not semantic and not any(contains_v10(term, statement) for term in dimensions):
            continue
        tokens = [word for word in re.findall(r'\w+', statement) if word not in _DYNAMIC_FUNCTION_WORDS and word != 's']
        pairs.update(zip(tokens, tokens[1:]))
    return pairs


def local_state_gain_v10(candidate, chosen, demand):
    import re
    # 仅在已绑定语义相关的同主体局部陈述中补状态变化；新日期或泛人物词本身不算增量。
    def states(row):
        result = []
        for statement in statements_v9([row]):
            body = re.sub(r'^\[[^\]\n]+\]\s+[^:\n]+:\s*', '', statement)
            match = re.match(r'(?P<subject>\w+)\s+(?P<verb>is|are|was|were|became|becomes|got|gets)\s+(?P<value>[^.!]+)', body)
            if match:
                result.append((match['subject'], match['verb'], match['value'].strip()))
        return result
    current = states(candidate)
    for old in chosen:
        hits, semantic = relevance_v10(old, demand)
        if not hits and not semantic:
            continue
        for subject, verb, value in current:
            # 明确变化谓词可补新状态；日期只延续同一属性，不能把进行时活动变成状态。
            def attribute(text):
                text = re.sub(r'^(?:(?:no longer|not|now)\s+)+', '', text)
                head = re.match(r'\w+', text)
                return head[0] if head and not head[0].endswith('ing') else None
            change = (verb in {'became', 'becomes'} or verb in {'got', 'gets'}
                and bool(re.match(r'\w+ed\b', value)))
            dated = bool(re.search(r'\b(?:last|next|this|previous|following)\s+(?:day|week|month|year)\b|\b(?:today|yesterday|tomorrow)\b|\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b|\d{1,2}月', value))
            for previous_subject, _, previous_value in states(old):
                if previous_subject != subject or previous_value == value:
                    continue
                continuation = (verb in {'is', 'are', 'was', 'were'} and attribute(value) is not None
                    and attribute(value) == attribute(previous_value)
                    and (dated or bool(re.search(r'\b(?:no longer|not|now)\b', value))))
                if change or continuation:
                    return True
    return False


def relational_gain_v10(candidate, chosen, demand):
    if any(candidate['excerpt'].casefold() == row['excerpt'].casefold() for row in chosen):
        return False
    hits, semantic = relevance_v10(candidate, demand)
    if not hits and not semantic:
        return False
    if not chosen:
        return True
    before, after = progress_v10(demand, chosen), progress_v10(demand, [*chosen, candidate])
    if len(after['missing']) < len(before['missing']) or any(not before[key] and after[key]
            for key in ('date_detail', 'state_match', 'original')):
        return True
    if hits and relations_v10([candidate], demand) - relations_v10(chosen, demand):
        return True
    if not semantic:
        return False
    if demand['mode'] == 'open':
        return bool(relations_v10([candidate], demand, semantic=True) - relations_v10(chosen, demand, semantic=True))
    if local_state_gain_v10(candidate, chosen, demand):
        return True
    # 有限任务不能仅凭新的泛人物陈述扩满容量；纯语义材料须改善已有相关性前沿。
    previous = max((relevance_v10(row, demand)[1] for row in chosen), default=0.0)
    return semantic > previous


def marginal_v10(candidate, chosen, demand):
    hits, semantic = relevance_v10(candidate, demand)
    if not relational_gain_v10(candidate, chosen, demand):
        return 0.0
    dimensions = set((*demand['dimensions'], *demand['resolved_dimensions']))
    return max(len(hits)/max(1, len(dimensions)), semantic)


def seed_scores_v10(chunks, question, title, summary, vectors):
    from . import get
    from .rank import MIN_SIMILARITY
    from core.search_and_recall.evidence_windows import query_terms
    demand = demand_v10(question)
    contextual = getattr(get('rank'), 'contextual', None)
    scored = []
    for index, chunk in enumerate(chunks):
        similarity = max(0.0, min(1.0, float(vectors.get(index, 0))))
        row = {'excerpt': chunk.text, 'vector_score': similarity}
        hits, semantic = relevance_v10(row, demand)
        if not hits and not semantic:
            continue
        if contextual is not None:
            _, score, _ = contextual((chunk,), query_terms(question), '', '',
                {0: similarity} if index in vectors else {}, len(chunk.text), lambda piece, room: piece)
        else:
            meaning = getattr(get('rank'), 'meaning', None)
            score = meaning(len(hits), similarity) if meaning else len(hits)
        if score is not None and score > 0 and (hits or similarity >= MIN_SIMILARITY):
            scored.append((float(score), index, similarity))
    scored.sort(key=lambda row: (-row[0], row[1]))
    if not scored:
        return ()
    # 分数只决定次序；正文相关的低分日期和状态补充仍须交给需求增量判断。
    return tuple(scored)


def seed_order_v10(chunks, question, title, summary, vectors):
    return tuple(index for _, index, _ in seed_scores_v10(chunks, question, title, summary, vectors))


def context_neighborhood_v10(content, ranges, first, last):
    import re
    def body(index):
        value = content[slice(*ranges[index])].strip()
        return re.sub(r'^\[[^\]\n]+\]\s+[^:\n]+:\s*', '', value)
    # 只沿连续原文补未完问答或开头指代，不把跨缺口图注猜成同一对象。
    while first > 0 and re.match(r'^(?:it|this|that|these|those|they|he|she|yes|no|yeah)\b', body(first), re.I):
        first -= 1
    while last+1 < len(ranges) and body(last).rstrip().endswith(('?', '？')):
        last += 1
    return ranges[first][0], ranges[last][1]


v10.limits = lambda question: v10()
v10.context_window = context_window_v8
v10.input_budget = input_budget_v9
v10.demand = demand_v10
v10.progress = progress_v10
v10.marginal = marginal_v10
v10.relational_gain = relational_gain_v10
v10.context_ranges = context_ranges_v9
v10.seed_order = seed_order_v10
v10.seed_scores = seed_scores_v10
v10.context_neighborhood = context_neighborhood_v10
register('ladder', '@10')(v10)



def v11():
    """局部主体与主题决定事实增量，容量不作为召回目标。"""
    return v10()


def demand_v11(question, *, reference_score=0, semantic_anchors=()):
    import re
    demand = demand_v10(question, reference_score=reference_score, semantic_anchors=semantic_anchors)
    # 全部事件、逐项日期或计数需要实例覆盖，不能用一种参与方式代表其他场次。
    exhaustive = bool(re.search(r'\b(?:all|each|every|how many|count|dates)\b|全部|所有|各次|每次|多少|几次', question, re.I))
    events = bool(re.search(r'\b(?:events?|activities|occasions?|meetings?|conferences?)\b|活动|场次|会议', question, re.I))
    demand['coverage'] = 'event_instances' if exhaustive and events else 'categories'
    phrasing = {'ways', 'way', 'be', 'considered', 'all', 'each', 'every', 'count'}
    for key in ('targets', 'dimensions', 'resolved_targets', 'resolved_dimensions'):
        demand[key] = tuple(term for term in demand[key] if term not in phrasing)
    # 标准群体名称只扩展词义，不把目标人物的全部私事变成群体活动。
    synonyms = {'lgbtq': ('lesbian', 'gay', 'bisexual', 'transgender', 'queer'),
                '性少数': ('同性恋', '双性恋', '跨性别', '酷儿')}
    for key in ('dimensions', 'resolved_dimensions'):
        demand[key] = tuple(dict.fromkeys((*demand[key], *(word for term in demand[key]
            for word in synonyms.get(term, ())))))
    targets = set((*demand['targets'], *demand['resolved_targets']))
    demand['actors'] = tuple(dict.fromkeys(word.casefold() for text in (question, *semantic_anchors)
        for word in re.findall(r'\b[A-Z][a-z]+\b', text) if word.casefold() in targets))
    demand['clauses'] = tuple(re.sub(r'\b(?:'+ '|'.join(phrasing) +r')\b', ' ', clause, flags=re.I)
        for clause in demand['clauses'])
    return demand


def local_units_v11(candidate, demand, *, topical=False):
    import re
    dimensions = (*demand['dimensions'], *demand['resolved_dimensions'])
    for left, right in context_ranges_v9(candidate['excerpt']):
        text = candidate['excerpt'][left:right]
        match = re.match(r'^\s*\[[^\]\n]+\]\s+(?P<speaker>[^:\n]+):\s*(?P<body>[\s\S]*)', text)
        speaker, body = (match['speaker'].casefold(), match['body']) if match else ('', text)
        actors = demand['actors']
        # 别人的发言中提及目标姓名，不等于目标主体的事实；无记录头时只认显式句首主体。
        owned = (not actors or speaker in actors or any(re.match(r'\s*'+re.escape(actor)+r'\s+\w', body, re.I) for actor in actors))
        if not owned:
            continue
        if topical and not any(contains_v10(term, body) for term in dimensions):
            continue
        yield text, body


def facts_v11(rows, demand, *, topical=False):
    facts = set()
    for row in rows:
        for _, body in local_units_v11(row, demand, topical=topical):
            statements = statements_v9([{'excerpt': body}])
            if topical:
                dimensions = (*demand['dimensions'], *demand['resolved_dimensions'])
                statements = tuple(text for text in statements if any(contains_v10(term, text) for term in dimensions))
            facts.update(statements)
    return facts


def relevance_v11(candidate, demand):
    dimensions = (*demand['dimensions'], *demand['resolved_dimensions'])
    units = tuple(local_units_v11(candidate, demand))
    hits = tuple(term for term in dimensions if any(contains_v10(term, body) for _, body in units))
    semantic = relevance_v10(candidate, demand)[1] if units else 0.0
    return hits, semantic


def relational_gain_v11(candidate, chosen, demand):
    hits, semantic = relevance_v11(candidate, demand)
    if not hits and not semantic:
        return False
    facts = facts_v11([candidate], demand)
    if not facts:
        # 首个相关问答允许完整邻域，但问句仍不能证明充分。
        return not chosen and bool(hits)
    if facts <= facts_v11(chosen, demand):
        return False
    before, after = progress_v10(demand, chosen), progress_v10(demand, [*chosen, candidate])
    if hits and (facts_v11([candidate], demand, topical=True) - facts_v11(chosen, demand, topical=True)):
        return True
    if any(not before[key] and after[key] for key in ('date_detail', 'state_match', 'original')):
        return True
    if not semantic:
        return False
    if demand['coverage'] == 'event_instances':
        import re
        # 语义绑定只支持把同主体的新参与记录交末轮判断，不证明活动属于所问群体。
        subjects = '|'.join(('i', 'we', *(re.escape(actor) for actor in demand['actors'])))
        action = r'\b(?:'+subjects+r')\s+(?:attended|participated|volunteered|performed|spoke|went to)\b'
        if any(re.search(action, statement, re.I) for statement in facts - facts_v11(chosen, demand)):
            return True
    if demand['mode'] == 'finite' and not demand['date_roles'] and local_state_gain_v10(candidate, chosen, demand):
        return True
    previous = max((relevance_v11(row, demand)[1] for row in chosen), default=0.0)
    if demand['mode'] == 'finite':
        # 有限推理中的部分字面线索不能锁死更相关的隐式背景，仍沿真实得分前沿探索。
        return semantic > previous
    topical = facts_v11(chosen, demand, topical=True)
    if topical:
        import re
        # 已定位主题之后，隐式补充须连接同主体已有的实质局部关系，不能另起无关闲聊。
        def pairs(statements):
            excluded = _DYNAMIC_FUNCTION_WORDS | set(demand['actors']) | {
                'i', 'you', 'he', 'she', 'we', 'they', 'it', 'my', 'your', 'our', 'their', 'its',
                'm', 's', 're', 've', 'll', 'am', 'being', 'that', 'this', 'these', 'those'}
            result = set()
            for statement in statements:
                words = [word for word in re.findall(r'\w+', statement) if word not in excluded]
                institutions = {'center', 'centre', 'group', 'organization', 'club', 'school',
                    'conference', 'workshop', 'event', 'program', 'project', 'council'}
                result.update(pair for pair in zip(words, words[1:]) if set(pair) & institutions)
            return result
        return bool(pairs(facts) & pairs(topical))
    # 无字面主题的探索按真实语义分数前沿渐进，不能以任意新陈述词对不断扩张。
    previous = max((relevance_v11(row, demand)[1] for row in chosen), default=0.0)
    return semantic > previous


def marginal_v11(candidate, chosen, demand):
    if not relational_gain_v11(candidate, chosen, demand):
        return 0.0
    hits, semantic = relevance_v11(candidate, demand)
    return max(len(hits)/max(1, len(set((*demand['dimensions'], *demand['resolved_dimensions'])))), semantic)


def seed_scores_v11(chunks, question, title, summary, vectors):
    from . import get
    from core.search_and_recall.evidence_windows import query_terms
    demand = demand_v11(question)
    contextual = getattr(get('rank'), 'contextual', None)
    terms = query_terms(question + '\n' + ' '.join(demand['dimensions']))
    result = []
    for index, chunk in enumerate(chunks):
        similarity = max(0.0, min(1.0, float(vectors.get(index, 0))))
        hits, semantic = relevance_v11({'excerpt': chunk.text, 'vector_score': similarity}, demand)
        if not hits and not semantic:
            continue
        if contextual is not None:
            _, score, _ = contextual((chunk,), terms, '', '', {0: similarity} if index in vectors else {},
                len(chunk.text), lambda piece, room: piece)
        else:
            score = len(hits) + semantic
        if score is not None and score > 0:
            result.append((float(score), index, similarity))
    return tuple(sorted(result, key=lambda row: (-row[0], row[1])))


def seed_order_v11(chunks, question, title, summary, vectors):
    return tuple(index for _, index, _ in seed_scores_v11(chunks, question, title, summary, vectors))


def context_spans_v11(content, ranges, first, last, seed, question, score, similarity):
    demand = demand_v11(question)
    spans = []
    for index in range(first, last+1):
        left, right = ranges[index]
        row = {'excerpt': content[left:right]}
        if tuple(local_units_v11(row, demand, topical=True)):
            start, end = context_neighborhood_v11(content, ranges, index, index)
            spans.append((start, end, similarity if start <= seed.start and seed.end <= end else 0.0))
    if spans:
        return tuple(dict.fromkeys(spans))
    # 纯语义必须保留整个已编码种子，不能把其分数广播给内部各条记录。
    start, end = context_neighborhood_v11(content, ranges, first, last)
    row = {'excerpt': content[start:end], 'vector_score': similarity}
    return ((start, end, similarity),) if relevance_v11(row, demand)[1] else ()


def context_neighborhood_v11(content, ranges, first, last):
    import re
    def record(index):
        text = content[slice(*ranges[index])].strip()
        match = re.match(r'^\[[^\]\n]+\]\s+(?P<speaker>[^:\n]+):\s*(?P<body>[\s\S]*)', text)
        return (match['speaker'], match['body']) if match else ('', text)
    def linked_question(text):
        if re.search(r'\b(?:by the way|anyway|what about|how about|speaking of)\b|换个话题|顺便说', text, re.I):
            return False
        questions = re.findall(r'[^.!?。！？]*[?？]', text)
        return any(re.search(r'\b(?:it|there|those|that|them|next time)\b|那里|这次|下次|它|他们', question, re.I)
                   for question in questions)
    # 显式事件是向前指代的锚点；只穿过连续原文中的未解决问答，不跨缺口。
    while first > 0:
        speaker, text = record(first)
        previous_speaker, previous_text = record(first-1)
        # 肯定或否定短答只回补紧邻的他人问句，不能离开该问答链继续扩张。
        if (re.match(r'^(?:yes|no|yeah)\b', text, re.I)
                and speaker != previous_speaker and previous_text.rstrip().endswith(('?', '？'))):
            first -= 1
            break
        if 'completed' in event_states_v9(text):
            break
        if not (linked_question(text) or re.match(r'^(?:it|this|that|these|those|they|he|she|i loved it)\b', text, re.I)):
            break
        first -= 1
    owner, _ = record(last)
    # 后续问题必须明确指回当前活动；答句由原说话人返回，不把另一个人的活动广播进来。
    while last+2 < len(ranges):
        speaker, question = record(last+1)
        reply_speaker, _ = record(last+2)
        if not owner or speaker == owner or reply_speaker != owner or not linked_question(question):
            break
        last += 2
    return ranges[first][0], ranges[last][1]


def refine_context_v11(accepted, candidate, demand):
    # 探索先发现的无关片段不能在明确主题出现后永久占位；只保留与主题实际相连的材料。
    if facts_v11([candidate], demand, topical=True) and not facts_v11(accepted, demand, topical=True):
        return [row for row in accepted if relational_gain_v11(row, [candidate], demand)]
    return accepted


v11.limits = lambda question: v11()
v11.context_window = context_window_v8
v11.input_budget = input_budget_v9
v11.demand = demand_v11
v11.progress = progress_v10
v11.marginal = marginal_v11
v11.relational_gain = relational_gain_v11
v11.context_ranges = context_ranges_v9
v11.seed_order = seed_order_v11
v11.seed_scores = seed_scores_v11
v11.context_neighborhood = context_neighborhood_v11
v11.context_spans = context_spans_v11
v11.post_fit_gain = relational_gain_v11
v11.refine_context = refine_context_v11
register('ladder', '@11')(v11)


def v12():
    """保真语义种子，以原问题关系和连续问答补充事实。"""
    return v11()


def demand_v12(question, *, reference_score=0, semantic_anchors=()):
    import re
    result = demand_v11(question, reference_score=reference_score, semantic_anchors=semantic_anchors)
    # 辅助问法只扩展主题词，不能给原问题换人物、任务或时间条件。
    result['actors'] = demand_v11(question)['actors']
    result['relation'] = ('duration' if re.search(r'\bhow long\b|多久|多长时间', question, re.I)
        else 'meaning' if re.search(r'\b(?:symbols?|symbolize|meaning|represent)\b|象征|寓意', question, re.I)
        else 'topic')
    # 年限问法和计量单位只说明要找时长，不能证明答句在谈同一对象或活动。
    duration_words = frozenset('long length duration time period many much number years year months month weeks week days day hours hour minutes minute seconds second since ago now been being doing done'.split()) | {
        '多久', '多长时间', '多长', '时间', '时长', '年限', '多少年', '几年', '年', '月', '周', '天', '小时', '分钟', '秒'}
    result['attribute_dimensions'] = tuple(term for term in result['dimensions'] if term not in duration_words)
    result['resolved_attribute_dimensions'] = tuple(term for term in result['resolved_dimensions'] if term not in duration_words)
    return result


def records_v12(content):
    import re
    result = []
    for left, right in context_ranges_v9(content):
        text = content[left:right]
        match = re.match(r'^\s*\[[^\]\n]+\]\s+(?P<speaker>[^:\n]+):\s*(?P<body>[\s\S]*)', text)
        speaker, body = (match['speaker'].casefold(), match['body']) if match else ('', text)
        # 图注不承担人物自述、提问或对象承接的证明。
        body = re.sub(r'\[Image caption:[\s\S]*?\]', '', body, flags=re.I).strip()
        result.append((left, right, speaker, body))
    return tuple(result)


def gap_v12(text):
    import re
    return bool(re.search(r'(?m)^\s*(?:…+|\.{3,})\s*$', text))


def switch_v12(text):
    import re
    return bool(re.search(r'\b(?:by the way|anyway|speaking of|changing the subject|what about|how about)\b|换个话题|顺便说', text, re.I))


def addressed_v12(question, owner):
    import re
    addressee = re.match(r'^\s*([A-Z][a-z]+),\s+', question)
    if addressee and addressee[1].casefold() != owner:
        return False
    return bool(owner and re.search(r'[?？]', question) and not switch_v12(question) and not gap_v12(question)
        and (re.search(r'\b(?:you|your)\b|你', question, re.I)
             or re.search(r'(?<!\w)'+re.escape(owner)+r'(?!\w)', question, re.I)))


def reference_v12(text):
    import re
    return bool(re.search(r'\b(?:it|its|this|that|these|those|they|them)\b|它|这个|那个|这些|那些', text, re.I))


def context_neighborhood_v12(content, ranges, first, last):
    # 每次只穿过真实连续的他人提问/反馈与原主体答复，不跨省略或话题切换。
    records = {left: (speaker, body) for left, _, speaker, body in records_v12(content)}
    def record(index):
        return records.get(ranges[index][0], ('', ''))
    owner, body = record(first)
    if first > 0 and owner:
        previous_owner, previous = record(first-1)
        if previous_owner and previous_owner != owner and addressed_v12(previous, owner):
            first -= 1
            # 提问依赖前一物件时补足锚；普通新问题不回溯上一件事。
            while first > 0 and reference_v12(previous):
                earlier_owner, earlier = record(first-1)
                if earlier_owner != owner or switch_v12(earlier) or gap_v12(earlier):
                    break
                first -= 1
                break
    owner, body = record(last)
    while owner and last+2 < len(ranges):
        speaker, bridge = record(last+1)
        reply_owner, reply = record(last+2)
        if (not speaker or speaker == owner or reply_owner != owner
                or switch_v12(bridge) or switch_v12(reply)
                or gap_v12(content[ranges[last][1]:ranges[last+2][1]])
                or not reference_v12(bridge)):
            break
        # 问题的指代或回复的开头指代才说明仍是该对象；泛夸奖不能带出新话题。
        import re
        if not addressed_v12(bridge, owner) and not re.search(
                r'(?:^|[.!]\s+)(?:it|this|that|these|those|they)\b', reply, re.I):
            break
        last += 2
        body = reply
    return ranges[first][0], ranges[last][1]


def relation_facts_v12(candidate, demand):
    import re
    records = records_v12(candidate['excerpt'])
    result = set()
    dimensions = (demand['attribute_dimensions']
        if demand['relation'] == 'duration' else (*demand['dimensions'], *demand['resolved_dimensions']))
    for index, (_, _, speaker, body) in enumerate(records):
        if gap_v12(body) or switch_v12(body):
            continue
        actors = demand['actors']
        owned = not actors or speaker in actors or (not speaker and any(
            re.match(r'\s*'+re.escape(actor)+r'\s+\w', body, re.I) for actor in actors))
        if not owned:
            continue
        previous = records[index-1] if index else None
        question = previous[3] if previous and previous[2] != speaker and addressed_v12(previous[3], speaker) else ''
        topical = any(contains_v10(term, body) for term in dimensions)
        asked = bool(question and any(contains_v10(term, question) for term in dimensions))
        if demand['relation'] == 'duration':
            # 短答前的 it 只能借紧邻同主体活动陈述，不能把任意时长问题当成同一活动。
            if not asked and question and index >= 2:
                anchor = records[index-2]
                # 辅助对象须由同一句真实原活动陈述桥接，改写文本自身不是同义证明。
                anchored = any(any(contains_v10(term, statement) for term in dimensions)
                    and (reference_v12(question) or any(contains_v10(term, statement) and contains_v10(term, question)
                        for term in demand['resolved_attribute_dimensions']))
                    for statement in statements_v9([{'excerpt': anchor[3]}]))
                asked = (anchor[2] == speaker and not gap_v12(anchor[3]) and not switch_v12(anchor[3])
                    and anchored)
            asked = asked and bool(re.search(r'\b(?:how long|since when|how many\s+(?:years?|months?|weeks?|days?|hours?|minutes?|seconds?))\b|多久|多长时间', question, re.I))
            value = bool(re.search(r'\b(?:\d+|[a-z]+)\s+(?:years?|months?|weeks?|days?)\b|\bsince\b|\d+年', body, re.I))
            related = value and (topical or asked)
        elif demand['relation'] == 'meaning':
            related = bool(re.search(r'\b(?:symbol\w*|represent\w*|means?|stands? for|remind\w*|reflect\w*)\b|象征|寓意|代表|提醒', body, re.I))
        else:
            related = topical or asked
        if related:
            # 问题提供关系，只有目标人物的陈述提供新增值。
            result.update(statements_v9([{'excerpt': body}]))
    return result


def relational_gain_v12(candidate, chosen, demand):
    hits, semantic = relevance_v11(candidate, demand)
    related = relation_facts_v12(candidate, demand)
    if not hits and not semantic and not related:
        return False
    facts = facts_v11([candidate], demand)
    if not facts or facts <= facts_v11(chosen, demand):
        return False
    previous = set().union(*(relation_facts_v12(row, demand) for row in chosen)) if chosen else set()
    if related - previous:
        return True
    # 陌生问法允许首个完整语义种子供原回答阶段判断；后续不能靠新闲聊扩满容量。
    return not chosen and bool(semantic)


def marginal_v12(candidate, chosen, demand):
    if not relational_gain_v12(candidate, chosen, demand):
        return 0.0
    hits, semantic = relevance_v11(candidate, demand)
    return max(semantic, len(hits)/max(1, len(demand['dimensions'])), 1.0 if relation_facts_v12(candidate, demand) else 0.0)


def context_spans_v12(content, ranges, first, last, seed, question, score, similarity):
    from .rank import MIN_SIMILARITY
    demand = demand_v12(question)
    spans = []
    # 完整编码种子独立保留；任何局部词命中都不能替代它或借用它的分数。
    if similarity >= MIN_SIMILARITY and not gap_v12(seed.text):
        start, end = context_neighborhood_v12(content, ranges, first, last)
        if not gap_v12(content[start:end]):
            spans.append((start, end, similarity))
    for index in range(first, last+1):
        start, end = context_neighborhood_v12(content, ranges, index, index)
        row = {'excerpt': content[start:end]}
        if not gap_v12(row['excerpt']) and relation_facts_v12(row, demand):
            spans.append((start, end, similarity if start <= seed.start and seed.end <= end else 0.0))
    return tuple(dict.fromkeys(spans))


def refine_context_v12(accepted, candidate, demand):
    # 已有真实需求关系不因后到的字面命中而失效，去重与硬预算由统一入口处理。
    return accepted


v12.limits = lambda question: v12()
v12.context_window = context_window_v8
v12.input_budget = input_budget_v9
v12.demand = demand_v12
v12.progress = progress_v10
v12.marginal = marginal_v12
v12.relational_gain = relational_gain_v12
v12.context_ranges = context_ranges_v9
v12.seed_order = seed_order_v11
v12.seed_scores = seed_scores_v11
v12.context_neighborhood = context_neighborhood_v12
v12.context_spans = context_spans_v12
v12.post_fit_gain = relational_gain_v12
v12.refine_context = refine_context_v12
v12.refresh_demand = True
v12.merge_vector_seeds = True
register('ladder', '@12')(v12)


def v13():
    """以固定原问重排参考探索非字面互补证据，评分不证明事实。"""
    return v12()


class _RerankBatch:
    # 无字段的请求内身份凭证；父正文拟合深复制候选时保持本批身份。
    __slots__ = ()

    def __deepcopy__(self, memo):
        return self


def rerank_evidence_v13(candidate, demand):
    from math import isfinite
    from .rank import MIN_SIMILARITY
    value = candidate.get('_semantic_rerank')
    if (not isinstance(value, dict) or not isinstance(value.get('batch'), _RerankBatch) or not value.get('query')
            or type(value.get('position')) is not int or value['position'] < 0
            or value.get('query') != demand.get('_retrieval_question')):
        return None
    score, binding = value.get('score'), value.get('binding', {})
    if type(score) not in {int, float} or not isfinite(score):
        return None
    entry = candidate.get('entry', {})
    if (value.get('candidate_id') != candidate.get('id') or binding.get('id') != entry.get('id')
            or binding.get('kind') != candidate.get('kind')
            or binding.get('revision') != entry.get('revision')
            or binding.get('item_revision') != entry.get('item_revision')
            or binding.get('coordinates') != candidate.get('coordinate_space')
            or binding.get('scene') != candidate.get('scene')
            or binding.get('snapshot') != candidate.get('snapshot')
            or binding.get('title') != candidate.get('title')
            or value.get('scope') != candidate.get('scope')
            or binding.get('excerpt') != candidate.get('excerpt')
            or binding.get('windows') != [[w.start, w.end] for w in candidate.get('windows', ())]):
        return None
    # 表示必须整体保持；拟合扩窗或裁片都不能借用旧表示的分数。
    windows = candidate.get('windows', ())
    if (not windows or '\n…\n'.join(w.text for w in windows) != candidate['excerpt']
            or not any(type(score) in {int, float} and isfinite(score) and MIN_SIMILARITY <= score <= 1
                and any(w.start <= start < end <= w.end and w.text[start-w.start:end-w.start] == text
                    for w in windows) for start, end, score, text in value.get('seeds', ()))):
        return None
    return value


def prepare_frontier_v13(rows, demand):
    # 每批原始前沿的首个有效探索表示固定一次；重排置换和成本探测均不改基准。
    if demand['relation'] != 'topic':
        return demand
    references = list(demand.get('_semantic_references', ()))
    batches = []
    for row in rows:
        evidence = rerank_evidence_v13(row, demand)
        if evidence is not None and not any(evidence['batch'] is old for old in batches):
            batches.append(evidence['batch'])
    for batch in batches:
        if any(batch is old for old, _ in references):
            continue
        frontier = [(rerank_evidence_v13(row, demand), row) for row in rows]
        frontier = sorted(((evidence, row) for evidence, row in frontier
            if evidence is not None and evidence['batch'] is batch), key=lambda value: value[0]['position'])
        baseline = next((evidence['score'] for evidence, row in frontier
            if facts_v11([row], demand) and not relation_facts_v12(row, demand)), None)
        # 没有可用探索参考也冻结为空，辅助刷新不能在同批中另造基准。
        references.append((batch, baseline))
    return {**demand, '_semantic_references': tuple(references)} if len(references) != len(
        demand.get('_semantic_references', ())) else demand


def semantic_gain_v13(candidate, chosen, demand):
    if demand['relation'] != 'topic' or not demand.get('_semantic_references'):
        return False
    evidence = rerank_evidence_v13(candidate, demand)
    if evidence is None:
        return False
    baseline = next((score for batch, score in demand['_semantic_references'] if evidence['batch'] is batch), None)
    if baseline is None or evidence['score'] <= baseline:
        return False
    # 分数只给整个表示候选资格；新增值仍须来自原目标主体，不能把他人事实移过来。
    return bool(facts_v11([candidate], demand) - facts_v11(chosen, demand))


def relational_gain_v13(candidate, chosen, demand):
    return relational_gain_v12(candidate, chosen, demand) or semantic_gain_v13(candidate, chosen, demand)


def marginal_v13(candidate, chosen, demand):
    original = marginal_v12(candidate, chosen, demand)
    return original or (1.0 if semantic_gain_v13(candidate, chosen, demand) else 0.0)


for _attribute in ('limits', 'context_window', 'input_budget', 'demand', 'progress',
        'context_ranges', 'seed_order', 'seed_scores', 'context_neighborhood', 'context_spans',
        'refine_context', 'refresh_demand', 'merge_vector_seeds'):
    setattr(v13, _attribute, getattr(v12, _attribute))
v13.marginal = marginal_v13
v13.relational_gain = relational_gain_v13
v13.post_fit_gain = relational_gain_v13
v13.dedup_gain = relational_gain_v12
v13.prepare_frontier = prepare_frontier_v13
v13.semantic_rerank = True
register('ladder', '@13')(v13)


class _DiscoveryChains:
    # 资格只由已校验原正文的collector创建，深复制不另造请求身份。
    __slots__ = ('query', 'binding', 'chains')

    def __init__(self, query, binding, chains):
        self.query, self.binding = query, binding
        # 原问题未绑定主体时，普通召回照旧，新增链不能借任意说话人的事实。
        self.chains = tuple(chains) if demand_v12(query)['actors'] else ()

    def __deepcopy__(self, memo):
        return self


def _frozen_v14(value):
    if isinstance(value, dict):
        return tuple((key, _frozen_v14(item)) for key, item in sorted(value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_frozen_v14(item) for item in value)
    return value


def source_identity_v14(row):
    entry = row.get('entry', {})
    return _frozen_v14((row.get('scope'), row.get('kind'), row.get('id'), row.get('layer'),
        row.get('scene'), row.get('coordinate_space'), row.get('title'), row.get('snapshot'),
        tuple(entry.get(key) for key in ('id', 'revision', 'item_revision', 'item_id'))))


def representation_identity_v14(row):
    return (source_identity_v14(row), tuple((w.start, w.end, w.text) for w in row.get('windows', ())),
        row.get('excerpt'))


def discover_chains_v14(content, question, chunks, vectors, *, title='', summary='', offset=0, span_slot=0):
    from .rank import MIN_SIMILARITY
    from bisect import bisect_right, bisect_left
    from math import isfinite
    from core.search_and_recall.evidence_windows import EvidenceWindow
    demand = demand_v12(question)
    if demand['relation'] != 'topic' or not demand['actors']:
        return ()
    ranges = context_ranges_v9(content)
    starts, ends = tuple(a for a, _ in ranges), tuple(b for _, b in ranges)
    results = []
    for score, index, similarity in seed_scores_v11(chunks, question, title, summary, vectors):
        seed = chunks[index]
        if (index not in vectors or not isfinite(similarity) or similarity < MIN_SIMILARITY
                or seed.text != content[seed.start:seed.end]):
            continue
        first, last = bisect_right(ends, seed.start), bisect_left(starts, seed.end)-1
        if first >= len(ranges) or last < first:
            continue
        for start, end, bound in context_spans_v12(content, ranges, first, last, seed, question, score, similarity):
            if not start <= seed.start < seed.end <= end or bound != similarity:
                continue
            row = {'excerpt': content[start:end], 'layer': 'L0'}
            if not facts_v11([row], demand):
                continue
            windows = (EvidenceWindow(start+offset, end+offset, row['excerpt']),)
            provenance = (span_slot, index, seed.start+offset, seed.end+offset, seed.text,
                similarity, title+'\n'+summary[:120]+'\n'+seed.text)
            old = next((value for value in results if value['windows'] == windows), None)
            if old is not None:
                old['seeds'] = (*old['seeds'], provenance)
            else:
                results.append({'windows': windows, 'excerpt': row['excerpt'], 'seeds': (provenance,)})
    return tuple(results)


def discovery_rows_v14(row, question):
    capability = row.get('_discovery_chains')
    if (not isinstance(capability, _DiscoveryChains) or capability.query != question
            or capability.binding != source_identity_v14(row) or demand_v12(question)['relation'] != 'topic'
            or not demand_v12(question)['actors']):
        return ()
    return tuple({**row, 'windows': chain['windows'], 'excerpt': chain['excerpt'],
        '_discovered_chain': (capability, index),
        '_original_vector_spans': tuple((seed[2], seed[3], seed[5]) for seed in chain['seeds']),
        '_vector_spans': tuple((seed[2], seed[3], seed[5]) for seed in chain['seeds']),
        'vector_score': max(seed[5] for seed in chain['seeds'])} for index, chain in enumerate(capability.chains))


def chain_binding_v14(row, question):
    marker = row.get('_discovered_chain')
    if not isinstance(marker, tuple) or len(marker) != 2:
        return False
    capability, index = marker
    if (not isinstance(capability, _DiscoveryChains) or type(index) is not int
            or not 0 <= index < len(capability.chains) or capability.query != question
            or capability.binding != source_identity_v14(row) or not demand_v12(capability.query)['actors']):
        return False
    chain = capability.chains[index]
    return row.get('windows') == chain['windows'] and row.get('excerpt') == chain['excerpt']


def pack_frontier_v14(representations, question, ceiling, tokens, priority):
    # 顺序先固定参考，再用实际完整JSON的边际成本消费有限库存，不裁链。
    demand = demand_v12(question)
    reference = next((i for i, row in enumerate(representations)
        if row.get('_original_vector_spans', row.get('_vector_spans')) and facts_v11([row], demand)
        and not relation_facts_v12(row, demand)), None)
    selected, pending, observations = [], list(range(len(representations))), []
    def payload(index):
        row = representations[index]
        return {'id': str(index), 'content': row['title']+'\n'+row['excerpt']}
    if reference is not None:
        pending.remove(reference)
        if tokens([payload(reference)]) <= ceiling:
            selected.append(reference)
        else:
            observations.append((reference, 'reference_capacity'))
    while pending:
        previous = [representations[i] for i in selected]
        old_facts = facts_v11(previous, demand)
        old_tokens = tokens([payload(i) for i in selected])
        choices = []
        for index in pending:
            row = representations[index]
            facts = facts_v11([row], demand)
            novelty = len(facts-old_facts)/max(1, len(facts))
            related = relation_facts_v12(row, demand)-set().union(*(relation_facts_v12(old, demand) for old in previous))
            if novelty == 0 and not related:
                continue
            proposed = tokens([*(payload(i) for i in selected), payload(index)])
            if proposed > ceiling:
                continue
            relevance = (row.get('vector_score', 0) if row.get('_discovered_chain')
                else marginal_v12(row, previous, demand))
            if relevance <= 0:
                continue
            choices.append((priority(row), -relevance*max(novelty, bool(related))/max(1, proposed-old_tokens), index))
        if not choices:
            break
        _, _, index = min(choices)
        pending.remove(index)
        selected.append(index)
    observations.extend((index, 'capacity_or_redundancy') for index in pending)
    return selected, reference, tuple(observations)


def project_sources_v14(ledger):
    from core.search_and_recall.evidence_windows import EvidenceWindow
    rows = []
    for row in ledger:
        old = next((value for value in rows if source_identity_v14(value) == source_identity_v14(row)), None)
        if old is None:
            rows.append(dict(row))
            continue
        merged = []
        for window in sorted([*old.get('windows', ()), *row.get('windows', ())], key=lambda w: (w.start, w.end)):
            if merged and window.start <= merged[-1].end:
                previous = merged[-1]
                overlap = max(0, previous.end-window.start)
                if previous.text[window.start-previous.start:window.start-previous.start+min(overlap,len(window.text))] != window.text[:min(overlap,len(window.text))]:
                    raise ValueError('discovery_union_content_changed')
                if window.end > previous.end:
                    merged[-1] = EvidenceWindow(previous.start, window.end, previous.text+window.text[overlap:])
            else:
                merged.append(window)
        old.update(windows=tuple(merged), excerpt='\n…\n'.join(w.text for w in merged))
        # union不是任何已评分表示，标量与逐链资格不得沿父行流回选择。
        for key in ('vector_score', '_vector_spans', '_original_vector_spans', '_semantic_rerank',
                    '_discovered_chain', '_discovery_chains', '_chain_candidates'):
            old.pop(key, None)
    return rows


def semantic_gain_v14(candidate, chosen, demand):
    if (not demand['actors'] or not chain_binding_v14(candidate, demand.get('_retrieval_question'))
            or demand['relation'] != 'topic'):
        return False
    value = rerank_evidence_v13(candidate, demand)
    if value is None:
        return False
    reference = value.get('discovery_reference')
    if (not isinstance(reference, tuple) or len(reference) != 2 or reference[0] is not value['batch']
            or type(reference[1]) not in {int, float} or value['score'] <= reference[1]):
        return False
    return bool(facts_v11([candidate], demand)-facts_v11(chosen, demand))


def marginal_v14(candidate, chosen, demand):
    if candidate.get('_discovered_chain'):
        return 1.0 if semantic_gain_v14(candidate, chosen, demand) else 0.0
    return marginal_v13(candidate, chosen, demand)


def gain_v14(candidate, chosen, demand):
    return semantic_gain_v14(candidate, chosen, demand) if candidate.get('_discovered_chain') else relational_gain_v13(candidate, chosen, demand)


def dedup_gain_v14(candidate, chosen, demand):
    # 独立链使用完整评分资格，普通来源保留13既有的近重复判定。
    return (semantic_gain_v14(candidate, chosen, demand) if candidate.get('_discovered_chain')
        else v13.dedup_gain(candidate, chosen, demand))


def v14():
    return v13()


for _attribute in ('limits', 'context_window', 'input_budget', 'demand', 'progress', 'context_ranges',
        'seed_order', 'seed_scores', 'context_neighborhood', 'context_spans', 'refine_context',
        'refresh_demand', 'merge_vector_seeds', 'prepare_frontier', 'semantic_rerank'):
    setattr(v14, _attribute, getattr(v13, _attribute))
v14.marginal = marginal_v14
v14.relational_gain = gain_v14
v14.post_fit_gain = gain_v14
v14.dedup_gain = dedup_gain_v14
v14.bounded_discovery = True
register('ladder', '@14')(v14)


def _dialogue_metadata_v15(value):
    if isinstance(value, dict):
        if any(value.get(key) for key in ('speaker', 'speakers', 'dialogue', 'conversation', 'messages', 'transcript')):
            return True
        if any(value.get(key) in {'dialogue', 'conversation', 'chat', 'transcript'}
                for key in ('type', 'kind', 'source_type', 'format') if isinstance(value.get(key), str)):
            return True
        return any(_dialogue_metadata_v15(item) for item in value.values())
    return isinstance(value, (tuple, list)) and any(_dialogue_metadata_v15(item) for item in value)


def _plain_units_v15(candidate, demand):
    import re
    if (candidate.get('persona') or candidate.get('_discovered_chain')
            or _dialogue_metadata_v15(candidate)):
        return set()
    text = candidate['excerpt']
    if candidate.get('kind') == 'recognition' and 'content' in candidate.get('entry', {}):
        # 原子认识只以实际正文判增量，条件和出处JSON仍随原件完整保留。
        body = candidate['entry']['content']
        if not isinstance(body, str) or not text.startswith(body):
            return set()
        text = body
    if (gap_v12(text) or switch_v12(text) or any(speaker for _, _, speaker, _ in records_v12(text))
            or re.search(r"(?m)^\s*[\w .'-]{1,60}[:：]\s*\S", text)):
        return set()
    units = set()
    for _, body in local_units_v11({'excerpt': text}, demand):
        for statement in re.split(r'(?<=[.!。！])\s+|\n', body):
            statement = statement.strip()
            # 行内书名或引语的问号不使整条陈述失效，纯问句仍不授予事实增量。
            tail = statement.rstrip(' \t\"”’」）)]')
            question = (tail.endswith(('?', '？')) or re.search(
                r'^(?:who|what|where|when|why|how|which|is|are|was|were|do|does|did|can|could|should|would|will|has|have)\b'
                r'|^(?:谁|什么|为何|为什么|如何|怎么|是否|能否|何时|哪里|多少)'
                r'|(?:是什么|什么|怎样|如何|哪里|多少|吗|么|呢)\s*$', statement, re.I))
            if statement and not question and re.search(r'\w', statement):
                # 只统一用于新信息比较的句末标点，不改摘录、窗口、正文或quote。
                units.add(statement.casefold().rstrip('.!。！').strip())
    return units


def plain_gain_v15(candidate, chosen, demand):
    if demand['relation'] != 'topic':
        return 0.0
    dimensions = tuple(dict.fromkeys((*demand['dimensions'], *demand['resolved_dimensions'])))
    facts = _plain_units_v15(candidate, demand)
    previous = set().union(*(_plain_units_v15(row, demand) for row in chosen)) if chosen else set()
    previous.update(text.rstrip('.!。！').strip() for text in facts_v11(chosen, demand))
    fresh = facts - previous
    hits = {term for term in dimensions if any(contains_v10(term, text) for text in fresh)}
    return len(hits) / max(1, len(dimensions))


def marginal_v15(candidate, chosen, demand):
    return marginal_v14(candidate, chosen, demand) or plain_gain_v15(candidate, chosen, demand)


def gain_v15(candidate, chosen, demand):
    return gain_v14(candidate, chosen, demand) or bool(plain_gain_v15(candidate, chosen, demand))


def dedup_gain_v15(candidate, chosen, demand):
    return dedup_gain_v14(candidate, chosen, demand) or bool(plain_gain_v15(candidate, chosen, demand))


def context_spans_v15(content, ranges, first, last, seed, question, score, similarity):
    spans = list(context_spans_v12(content, ranges, first, last, seed, question, score, similarity))
    demand = demand_v12(question)
    for index in range(first, last + 1):
        start, end = ranges[index]
        if plain_gain_v15({'excerpt': content[start:end], 'layer': 'L0'}, [], demand):
            spans.append((start, end, similarity if start <= seed.start and seed.end <= end else 0.0))
    return tuple(dict.fromkeys(spans))


def v15():
    """无末标点的普通正文也可提供增量，对话、来源与关系链边界沿用旧版。"""
    return v14()


for _attribute in ('limits', 'context_window', 'input_budget', 'demand', 'progress', 'context_ranges',
        'seed_order', 'seed_scores', 'context_neighborhood', 'refine_context', 'refresh_demand',
        'merge_vector_seeds', 'prepare_frontier', 'semantic_rerank', 'bounded_discovery'):
    setattr(v15, _attribute, getattr(v14, _attribute))
v15.marginal = marginal_v15
v15.relational_gain = gain_v15
v15.post_fit_gain = gain_v15
v15.dedup_gain = dedup_gain_v15
v15.context_spans = context_spans_v15
register('ladder', '@15')(v15)


def demand_v16(question, *, reference_score=0, semantic_anchors=()):
    import re
    demand = demand_v12(question, reference_score=reference_score, semantic_anchors=semantic_anchors)
    # 只扩大明确的感情/婚姻属性发现；工作、客户和朋友关系仍由原需求判断。
    status = re.search(r'\b(?:relationship|marital|romantic|dating)\s+status\b'
        r'|(?:感情|恋爱|婚姻|婚恋)(?:状态|状况)|是否(?:单身|已婚)', question, re.I)
    other_relation = re.search(r'\b(?:work|business|professional|employment|customers?|clients?|friends?|friendship)\b'
        r'|工作|职业|业务|客户|同事|朋友|人际', question, re.I)
    demand['relationship_status_clues'] = bool(status and not other_relation)
    return demand


def relationship_clues_v16(candidate, demand):
    import re
    if (not demand.get('relationship_status_clues') or candidate.get('persona')
            or candidate.get('_discovered_chain') or gap_v12(candidate['excerpt'])):
        return set()
    result = set()
    actors = '|'.join(re.escape(actor) for actor in demand['actors'])
    subject = r"(?:i(?:['’](?:m|ve|d|ll)|\s+(?:am|was|got|have|had|will|might|may|stay|remain))|we(?:['’](?:re|ve|d|ll)|\s+(?:are|were|got|have|had|will|might)))"
    if actors:
        subject += r'|(?:'+actors+r')\s+(?:is|was|got|has|had|will|might)'
    predicate = (r'(?:^|[,;]\s*)(?:'+subject+r')\s+(?:(?:not|never|still|currently|now|no longer|been|be|stay|remain|happily|recently)\s+)*'
        r'(?:single\b(?!-|\s+(?:ticket|file|room|piece))|married\b(?!\s+to\s+(?:my\s+)?(?:work|job))'
        r'|engaged\b(?!\s+(?:in|with))|divorced\b|separated\b|widowed\b|partnered\b|dating\b|in a relationship\b)')
    personal_event = (r'\b(?:my|our)\s+(?:(?:recent|tough|painful|difficult|past)\s+)*(?:break[ -]?up|divorce|separation)\b'
        r'|^(?:i|we)(?:\s+(?:went|go|am going|are going|have gone|had gone|got|am getting|are getting)'
        r'|[\x27’]ve\s+gone)\s+(?:through\s+)?(?:(?:a|the|my|our|tough|painful|recent|difficult)\s+)*'
        r'(?:break[ -]?up|divorce|separation)\b')
    parenting_subject = (r"(?:i(?:['’](?:m|ll|d)|\s+(?:am|will|would|might|may|plan|hope|want|expect|intend))"
        r"|we(?:['’](?:re|ll|d)|\s+(?:are|will|would|might|may|plan|hope|want|expect|intend)))")
    parent_plan = (r'^'+parenting_subject+r'\s+(?:(?:not|never|still|planning|hoping|trying|going|ready|to|be|become|becoming|a)\s+)*'
        r'(?:single|solo|lone)\s+parent\b')
    parent_anchor = (r'^'+parenting_subject+r'\s+(?:(?:thrilled|excited|ready|planning|hoping|trying|going|to|a)\s+)*'
        r'(?:make|start|build|raise|adopt)\b[^.!?]*\b(?:family|kids?|children)\b')
    chinese_state = (r'^(?:我|我们)(?:(?:目前|现在|仍然|还是|还|已经|曾经|从未|没有|尚未|不再|不|未|已|刚|准备|打算|计划|希望|可能|将|会|要|是|正在|考虑|决定|选择|一直))*'
        r'(?:单身|未婚|已婚|结婚|离婚|分手|订婚|丧偶|独自养育)')
    for _, body in local_units_v11(candidate, demand):
        # 图注及第三人的单身描述不承担目标人物的状态发现；同主体计划仍保留原句。
        body = re.sub(r'\[Image caption:[\s\S]*?\]', '', body, flags=re.I)
        previous_parenting = False
        for statement in statements_v9([{'excerpt': body}]):
            # 只为匹配遮住转述引号，最终保留的仍是完整原文，不能把引号内的我移给说话人。
            unquoted = re.sub(r'"[^"\n]*"|“[^”\n]*”|(?<!\w)\x27[^\x27\n]+\x27(?!\w)', ' ', statement)
            explicit = re.search(predicate, unquoted, re.I)
            continuation = re.search(r"^it(?:['’](?:ll|s)|\s+(?:is|will))\b[^.!?]*\bas a (?:single|solo|lone) parent\b", unquoted, re.I)
            # 养育角色之前明确写for时，只允许本人me/us承接；句尾up for等无关搭配不参与。
            own_continuation = bool(continuation and not re.search(r'\bfor\s+(?!me\b|us\b)', continuation[0], re.I))
            contextual = (re.search(personal_event, unquoted, re.I) or re.search(parent_plan, unquoted, re.I)
                or re.search(chinese_state, unquoted)
                or previous_parenting and own_continuation)
            if explicit or contextual:
                result.add(statement)
            # 只有同记录紧邻的本人养育意向可以承接下一句it，普通自述不构成状态资格。
            previous_parenting = bool(re.search(parent_anchor, unquoted, re.I))
    return result


def relationship_gain_v16(candidate, chosen, demand):
    previous = set().union(*(relationship_clues_v16(row, demand) for row in chosen)) if chosen else set()
    return bool(relationship_clues_v16(candidate, demand) - previous)


def marginal_v16(candidate, chosen, demand):
    return marginal_v15(candidate, chosen, demand) or (1.0 if relationship_gain_v16(candidate, chosen, demand) else 0.0)


def gain_v16(candidate, chosen, demand):
    return gain_v15(candidate, chosen, demand) or relationship_gain_v16(candidate, chosen, demand)


def dedup_gain_v16(candidate, chosen, demand):
    return dedup_gain_v15(candidate, chosen, demand) or relationship_gain_v16(candidate, chosen, demand)


def seed_scores_v16(chunks, question, title, summary, vectors):
    from . import get
    demand = demand_v16(question)
    scores = {index: (score, index, similarity) for score, index, similarity in
        seed_scores_v11(chunks, question, title, summary, vectors)}
    for index, chunk in enumerate(chunks):
        if relationship_clues_v16({'excerpt': chunk.text}, demand):
            similarity = max(0.0, min(1.0, float(vectors.get(index, 0))))
            # 明确的原文属性线索按既有词面命中排序，不降低向量或独立链的准入阈值。
            meaning = getattr(get('rank'), 'meaning', None)
            score = meaning(1, similarity if index in vectors else None) if meaning else 1.0
            if index not in scores or score > scores[index][0]:
                scores[index] = (float(score), index, similarity)
    return tuple(sorted(scores.values(), key=lambda row: (-row[0], row[1])))


def seed_order_v16(chunks, question, title, summary, vectors):
    return tuple(index for _, index, _ in seed_scores_v16(chunks, question, title, summary, vectors))


def context_spans_v16(content, ranges, first, last, seed, question, score, similarity):
    spans = list(context_spans_v15(content, ranges, first, last, seed, question, score, similarity))
    demand = demand_v16(question)
    for index in range(first, last + 1):
        left, right = ranges[index]
        if relationship_clues_v16({'excerpt': content[left:right]}, demand):
            start, end = context_neighborhood_v12(content, ranges, index, index)
            spans.append((start, end, similarity if start <= seed.start and seed.end <= end else 0.0))
    return tuple(dict.fromkeys(spans))


def v16():
    """关系状态问法保留同主体的原文状态线索，不据此断言当前状态。"""
    return v15()


for _attribute in ('limits', 'context_window', 'input_budget', 'progress', 'context_ranges',
        'context_neighborhood', 'refine_context', 'refresh_demand',
        'merge_vector_seeds', 'prepare_frontier', 'semantic_rerank', 'bounded_discovery'):
    setattr(v16, _attribute, getattr(v15, _attribute))
v16.demand = demand_v16
v16.marginal = marginal_v16
v16.relational_gain = gain_v16
v16.post_fit_gain = gain_v16
v16.dedup_gain = dedup_gain_v16
v16.seed_scores = seed_scores_v16
v16.seed_order = seed_order_v16
v16.context_spans = context_spans_v16
register('ladder', '@16')(v16)
