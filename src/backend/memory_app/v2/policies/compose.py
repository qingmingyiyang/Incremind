"""Register existing evidence/profile framing entries through caller injection."""
from .types import invoke_entry as v1
from . import register
from .method_situation import applicable


def v2(entrypoint, /, *args, **kwargs):
    operation = kwargs.pop('operation', None)
    if operation == 'methods':
        rows, instruction, situation = args
        return applicable(rows, instruction, situation)
    result = entrypoint(*args, **kwargs)
    if operation == 'sources':
        return [('情境补全方法：\n' + text) if row.get('supplemented') else text
                for text, row in zip(result, args[0])]
    if operation == 'instruction' and any(row.get('supplemented') for row in args[0]):
        return result + '情境补全方法只在适用条件满足时参考，与普通资料分开，不视为任务事实。'
    return result


register('compose', '@2')(v2)


def v3(entrypoint, /, *args, **kwargs):
    from .insight_time import describe, dedup_comparison, INSTRUCTION
    operation = kwargs.get('operation')
    if operation == 'dedup_comparison':
        return dedup_comparison(*args)
    result = v2(entrypoint, *args, **kwargs)
    if operation == 'sources':
        return [describe(row['validity']) + text if row.get('temporal') else text
                for text, row in zip(result, args[0])]
    if operation == 'instruction' and any(row.get('temporal') for row in args[0]):
        return result + INSTRUCTION
    return result


register('compose', '@3')(v3)


def v4(entrypoint, /, *args, **kwargs):
    """沿用原证据格式，新增灵感单独标注。"""
    operation = kwargs.get('operation')
    result = v3(entrypoint, *args, **kwargs)
    if operation == 'sources':
        return ['你的灵感：\n' + text if row.get('inspiration') else text
                for text, row in zip(result, args[0])]
    if operation == 'instruction' and any(row.get('inspiration') for row in args[0]):
        result += '你的灵感是你以前写下的原话，作为构思参考，不视为已确认的事实。'
        if any(row.get('brainstorming') for row in args[0]):
            result += '先结合这些灵感，再给出新的想法。'
    return result


register('compose', '@4')(v4)
