"""Detached, deterministic condition matching and covered-method selection."""
import re

_STOP = frozenset(("什么时候", "时候", "进行", "可以", "如何", "什么", "怎么", "阶段", "项目", "任务", "工作", "关于"))
_ALIASES = (("礼物", "送礼"), ("旅行", "出行"), ("面试", "应聘"))
_COVERED = .8
MAX_METHODS = 3


def keywords(text):
    folded = text.casefold()
    for group in _ALIASES:
        for value in group:
            folded = folded.replace(value, group[0])
    for word in _STOP:
        folded = folded.replace(word, " ")
    words = set(re.findall(r"[a-z0-9_]+", folded))
    for chunk in re.findall(r"[\u4e00-\u9fff]+", folded):
        words.update(chunk[index:index + 2] for index in range(len(chunk) - 1))
    return words


def condition_score(conditions, situation):
    query = keywords(situation)
    return max((len(keywords(condition) & query) / max(1, len(keywords(condition)))
                for condition in conditions if isinstance(condition, str)), default=0)


def covered(content, instruction):
    words = keywords(content)
    return bool(words) and len(words & keywords(instruction)) / len(words) >= _COVERED


def applicable(rows, instruction, situation):
    return [row for row in rows if condition_score(row["entry"].get("conditions", ()), situation)
            and not covered(row["entry"]["content"], instruction)]
