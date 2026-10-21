"""为已选证据补充可追溯的日期运算，不访问时钟、存储或模型。"""
import calendar
from datetime import date, timedelta
import re


_MONTHS = {name.lower(): index for index, name in enumerate(calendar.month_name) if name}
_MONTHS.update({name.lower(): index for index, name in enumerate(calendar.month_abbr) if name})
_MONTH = '(?:' + '|'.join(sorted(_MONTHS, key=len, reverse=True)) + ')'
_DATE = re.compile(
    rf'(?<!\d)(?:\d{{4}}[-/]\d{{1,2}}[-/]\d{{1,2}}|\d{{4}}年\s*\d{{1,2}}月\s*\d{{1,2}}[日号]|'
    rf'\d{{1,2}}\s+{_MONTH},?\s+\d{{4}}|{_MONTH}\s+\d{{1,2}},?\s+\d{{4}})(?!\d)', re.I
)
_HEADER = re.compile(r'^\s*(?:session\s+date|record(?:ed)?\s+date|date|记录日期|会话日期|日期)\s*[:：]', re.I)
_RELATIVE = re.compile(
    r'\b(?:last night|yesterday|day before yesterday|tomorrow|day after tomorrow|last week|next week|'
    r'last month|next month|\d+ days? ago|\d+ days? later|in \d+ days?)\b|'
    r'(?<!大)(?:前天|昨天|昨日|昨晚|明天|后天)|(?<!上)上周|(?<!下)下周|'
    r'(?<!上)上个月|(?<!下)下个月|(?<![\d一二两三四五六七八九十百千万])[\d一二两三四五六七八九十]+天[前后]', re.I
)
_DAY_OFFSETS = {'last night': -1, 'yesterday': -1, 'day before yesterday': -2,
                'tomorrow': 1, 'day after tomorrow': 2,
                '前天': -2, '昨天': -1, '昨日': -1, '昨晚': -1, '明天': 1, '后天': 2}
_DIGITS = dict(zip('一二两三四五六七八九', (1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
_ENGLISH_DAYS = dict(zip(('one', 'two', 'three', 'four', 'five', 'six', 'seven',
    'eight', 'nine', 'ten', 'eleven', 'twelve'), range(1, 13)))
_ENGLISH_DAY_COUNT = '(?:' + '|'.join(_ENGLISH_DAYS) + ')'
_RELATIVE_V3 = re.compile(_RELATIVE.pattern + rf'|(?<![\w-])(?:{_ENGLISH_DAY_COUNT} days? ago|'
    rf'{_ENGLISH_DAY_COUNT} days? later|in {_ENGLISH_DAY_COUNT} days?)\b', re.I)
_ENGLISH_COUNT_PREFIX = re.compile(r'\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|'
    r'thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|'
    r'seventy|eighty|ninety|hundred|thousand)\s+(?:and\s+)?$', re.I)


def _number(value, *, calendar_version=1):
    if calendar_version == 3 and value.lower() in _ENGLISH_DAYS:
        return _ENGLISH_DAYS[value.lower()]
    if value.isascii() and value.isdigit():
        return int(value)
    if value in _DIGITS:
        return _DIGITS[value]
    if value.count('十') == 1:
        left, right = value.split('十')
        if (not left or left in _DIGITS) and (not right or right in _DIGITS):
            return _DIGITS.get(left, 1) * 10 + _DIGITS.get(right, 0)
    return None


def _anchor(text):
    """多日期或非法日期使当前锚失效，不能悄悄回退到标题。"""
    matches = list(_DATE.finditer(text))
    parsed = []
    for match in matches:
        value = match.group()
        parts = re.findall(r'\d+|[A-Za-z]+', value)
        try:
            if parts[0].isalpha():
                month, day, year = _MONTHS[parts[0].lower()], int(parts[1]), int(parts[2])
            elif len(parts[0]) == 4:
                year, month, day = map(int, parts)
            else:
                day, month, year = int(parts[0]), _MONTHS[parts[1].lower()], int(parts[2])
            parsed.append(date(year, month, day))
        except (ValueError, KeyError):
            return None, value
    if len(set(parsed)) != 1:
        return None, ' / '.join(match.group() for match in matches)
    return parsed[0], matches[0].group()


def _calendar_version(value):
    if type(value) is not int or value not in (1, 2, 3):
        raise ValueError('unsupported_date_calendar_version')


def _calculate(anchor, expression, *, calendar_version=1):
    _calendar_version(calendar_version)
    value = expression.lower()
    if value in {'last week', 'next week', '上周', '下周'}:
        direction = -1 if value in {'last week', '上周'} else 1
        start = anchor - timedelta(days=anchor.weekday()) + timedelta(weeks=direction)
        end = start + timedelta(days=6)
        if calendar_version in (2, 3):
            rolling_start = anchor + timedelta(days=-7 if direction < 0 else 1)
            rolling_end = anchor + timedelta(days=-1 if direction < 0 else 7)
            week = '上一' if direction < 0 else '下一'
            side = '前' if direction < 0 else '后'
            relation = '之前' if direction < 0 else '之后'
            return (f'{start.isoformat()} 至 {end.isoformat()}（{week}日历周，周一至周日；'
                    f'也可按记录日{side}连续七天约定解释为 {rolling_start.isoformat()} 至 {rolling_end.isoformat()}；'
                    f'两种约定均只指记录日{relation}，保留周精度，不能确定具体哪一天）')
        return (f'{start.isoformat()} 至 {end.isoformat()}（仅按周一至周日的日历周约定；'
                '原词也可能泛指前后约七天，不能据此断言确切日期）')
    if value in {'last month', 'next month', '上个月', '下个月'}:
        offset = -1 if value in {'last month', '上个月'} else 1
        index = anchor.year * 12 + anchor.month - 1 + offset
        year, month = divmod(index, 12)
        start = date(year, month + 1, 1)
        end = date(year, month + 1, calendar.monthrange(year, month + 1)[1])
        return f'{start.isoformat()} 至 {end.isoformat()}（日历月约定，不能确定其中某一天）'
    offset = _DAY_OFFSETS.get(value)
    if offset is None:
        number = re.search(r'\d+|[一二两三四五六七八九十]+' +
            (rf'|\b{_ENGLISH_DAY_COUNT}\b' if calendar_version == 3 else ''), value)
        count = _number(number.group(), calendar_version=calendar_version) if number else None
        if count is None:
            return None
        offset = -count if re.search(r'ago|before|前', value) else count
    return (anchor + timedelta(days=offset)).isoformat()


def _quote_spans(text):
    """按完整摘录定位引语，跨句、跨行及未闭合引语都保持保守边界。"""
    spans, stack = [], []
    pairs = {'"': '"', "'": "'", '“': '”', '‘': '’', '「': '」'}
    for index, character in enumerate(text):
        if character not in {*pairs, *pairs.values()}:
            continue
        if character in {"'", '’'} and index and index + 1 < len(text):
            if text[index - 1].isalnum() and text[index + 1].isalnum():
                continue
        if stack and character == stack[-1][1]:
            start, _ = stack.pop()
            spans.append((start, index + 1))
        elif character in pairs:
            stack.append((index, pairs[character]))
        else:
            # 仅看到闭引号，摘录可能从历史引语中途开始。
            spans.append((0, index + 1))
    # 对称引号可能是截断后仅剩的闭引号，无法区分时整份摘录保守处理。
    spans.extend((0 if closing in {'"', "'"} else start, len(text)) for start, closing in stack)
    return spans


def date_provenance(candidate, *, quote_version=3, calendar_version=2):
    """同一次算术保留原句位置及结果，既供显示也供新合同校验；最多八项。"""
    if type(quote_version) is not int or quote_version not in (1, 2, 3):
        raise ValueError('unsupported_date_quote_version')
    _calendar_version(calendar_version)
    title = candidate.get('title', '')
    anchor, anchor_text = _anchor(title)
    anchor_kind = 'frozen_title' if anchor else 'unavailable'
    notes, position = [], 0
    excerpt = candidate.get('excerpt', '')
    if quote_version == 1:
        quotes = _quote_spans(excerpt)
    else:
        from .date_quote_boundaries import quote_spans
        quotes = quote_spans(candidate, quote_version=quote_version)
    for raw_line in excerpt.splitlines(keepends=True):
        line_start, position = position, position + len(raw_line)
        line = raw_line.rstrip('\r\n')
        # 历史引语中的日期头只能属于引语，不能污染恢复到外部叙述后的日期锚。
        quoted_line = any(start < line_start + len(line) and end > line_start for start, end in quotes)
        if not quoted_line and (_HEADER.match(line) or _DATE.fullmatch(line.strip())):
            anchor, anchor_text = _anchor(line)
            anchor_kind = 'record_header' if anchor else 'unavailable'
        elif not quoted_line and line.strip() == '…':
            # 跨窗口没有连续性保证，下一窗口只能重新从标题或日期头取锚。
            anchor, anchor_text = _anchor(title)
            anchor_kind = 'frozen_title' if anchor else 'unavailable'
        for segment in re.finditer(r'.+?(?:(?<=[.!?。！？])\s+|$)', line):
            sentence = segment.group()
            relative = list((_RELATIVE_V3 if calendar_version == 3 else _RELATIVE).finditer(sentence))
            if not relative:
                continue
            local_anchor, local_text, local_kind = anchor, anchor_text, anchor_kind
            # 句首显式日期仅支配本句，避免把未来计划日期当成后续记录日期。
            explicit_local = bool(re.match(r'^\s*(?:on\s+|截至|于|在)?' + _DATE.pattern, sentence, re.I))
            if explicit_local:
                local_anchor, local_text = _anchor(sentence)
                local_kind = 'local_explicit' if local_anchor else 'unavailable'
            elif _DATE.search(sentence):
                local_anchor, local_text, local_kind = None, '句内另有日期，参照关系未确定', 'unavailable'
            for match in relative:
                # 有限一至十二语法不截取更长英文数词的尾部，未知数词保持不计算。
                if (calendar_version == 3 and re.match(r'[A-Za-z]', match.group())
                        and _ENGLISH_COUNT_PREFIX.search(sentence[:match.start()])):
                    continue
                # 嵌套引语的说话日期未知，不能把当前记录日期套给历史引语。
                absolute = line_start + segment.start() + match.start()
                quoted = any(start <= absolute < end for start, end in quotes) or bool(
                    re.search(r'\b(?:said|wrote)\b|说|写道', sentence[:match.start()], re.I))
                event_relative = bool(re.search(r'later\b|天后$', match.group(), re.I)) and not explicit_local
                try:
                    computed = _calculate(local_anchor, match.group(), calendar_version=calendar_version) if local_anchor and not quoted and not event_relative else None
                except (ValueError, OverflowError):
                    computed = None
                outcome = computed or ('无法确定：嵌套引语的说话日期未确定' if quoted else
                                       '无法确定：该表达可能相对于前述事件，缺少同句明确日期锚' if event_relative else
                                       '无法确定：缺少唯一合法的局部日期锚或超出可计算范围')
                left, right = max(0, match.start() - 70), min(len(sentence), match.end() + 70)
                original = ('…' if left else '') + sentence[left:right] + ('…' if right < len(sentence) else '')
                note = f'相对词 {match.group()}；日期锚 {local_text or "无"}；结果 {outcome}；原句片段：{original}'
                # 注释本身有界，避免重复长句挤掉实际证据；省略只用于展示，不改原文。
                if sum(len(item['note']) for item in notes) + len(note) > 1200:
                    return [*notes, {'note': '其余相对表达未计算（注释篇幅限制）。', 'computed': None}]
                notes.append({'note': note, 'sentence': sentence,
                    'sentence_span': (line_start + segment.start(), line_start + segment.end()),
                    'expression': match.group(), 'expression_span': (absolute, absolute + len(match.group())),
                    'anchor': local_anchor.isoformat() if local_anchor else None, 'anchor_text': local_text,
                    'computed': computed, 'published': True,
                    'anchor_kind': local_kind,
                    'options': calendar_values(computed) if computed else []})
                if len(notes) == 8:
                    return [*notes, {'note': '其余相对表达未计算（最多八项）。', 'computed': None}]
    return notes


def date_notes(candidate, *, quote_version=1, calendar_version=1):
    """旧显示格式、计算上限和默认版本保持不变。"""
    return [item['note'] for item in date_provenance(candidate, quote_version=quote_version, calendar_version=calendar_version)]


def append_date_notes(text, candidate, *, quote_version=1, calendar_version=1):
    notes = date_notes(candidate, quote_version=quote_version, calendar_version=calendar_version)
    if not notes:
        return text
    return text + '\n\n确定日期计算（程序派生注释，不是原文；只解释以上资料，不提供额外事实）：\n' + '\n'.join(notes)


def calendar_values(text):
    """只识别完整中英文/ISO年月日、年月及明确日范围，不承诺任意自然语言日期。"""
    result, occupied = [], []
    def add(start, end, left, right, precision):
        result.append({'span': (start, end), 'start': left, 'end': right, 'precision': precision})
        occupied.append((start, end))
    # 英文压缩日范围先处理，避免把一周范围误读成末端一个确切日。
    patterns = [
        rf'(?P<month>{_MONTH})\s+(?P<left>\d{{1,2}})\s*[-–—]\s*(?P<right>\d{{1,2}}),?\s+(?P<year>\d{{4}})',
        rf'(?P<left>\d{{1,2}})\s*[-–—]\s*(?P<right>\d{{1,2}})\s+(?P<month>{_MONTH}),?\s+(?P<year>\d{{4}})',
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.I):
            try:
                month, year = _MONTHS[match['month'].lower()], int(match['year'])
                left, right = date(year, month, int(match['left'])), date(year, month, int(match['right']))
            except (ValueError, KeyError):
                raise ValueError('invalid_calendar_value') from None
            if left > right:
                raise ValueError('invalid_calendar_value')
            add(match.start(), match.end(), left.isoformat(), right.isoformat(), 'range')
    dates = []
    for match in _DATE.finditer(text):
        if any(left < match.end() and right > match.start() for left, right in occupied):
            continue
        value, _ = _anchor(match.group())
        if value is None:
            raise ValueError('invalid_calendar_value')
        dates.append((match, value))
    index = 0
    while index < len(dates):
        match, value = dates[index]
        if index + 1 < len(dates):
            next_match, next_value = dates[index + 1]
            if re.fullmatch(r'\s*(?:to|through|至|到|[~～–—-])\s*', text[match.end():next_match.start()], re.I):
                if value > next_value:
                    raise ValueError('invalid_calendar_value')
                add(match.start(), next_match.end(), value.isoformat(), next_value.isoformat(), 'range')
                index += 2
                continue
        add(match.start(), match.end(), value.isoformat(), value.isoformat(), 'day')
        index += 1
    month_pattern = re.compile(
        rf'(?<!\d)(?:(?P<year>\d{{4}})[-/年]\s*(?P<number>\d{{1,2}})月?|'
        rf'(?P<name>{_MONTH}),?\s+(?P<english_year>\d{{4}}))(?![\d/-])', re.I)
    for match in month_pattern.finditer(text):
        if any(left < match.end() and right > match.start() for left, right in occupied):
            continue
        try:
            year = int(match['year'] or match['english_year'])
            month = int(match['number']) if match['number'] else _MONTHS[match['name'].lower()]
            left, right = date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
        except (ValueError, KeyError):
            raise ValueError('invalid_calendar_value') from None
        add(match.start(), match.end(), left.isoformat(), right.isoformat(), 'month')
    year_pattern = re.compile(r'(?:(?<!\w)(?:in|year|during|since|before|after)\s+(?P<en>\d{4})(?!\d)|(?<!\d)(?P<zh>\d{4})年)', re.I)
    for match in year_pattern.finditer(text):
        left, right = match.span('en' if match['en'] else 'zh')
        if any(start < right and end > left for start, end in occupied):
            continue
        try:
            year = int(match['en'] or match['zh'])
            start, end = date(year, 1, 1), date(year, 12, 31)
        except ValueError:
            raise ValueError('invalid_calendar_value') from None
        add(left, right, start.isoformat(), end.isoformat(), 'year')
    return sorted(result, key=lambda item: item['span'])
