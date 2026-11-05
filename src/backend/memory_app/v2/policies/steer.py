from dataclasses import dataclass
from copy import deepcopy
import json
import re
import unicodedata

from .insight_time import instant


_FUTURE = re.compile(r'以后|今后|每次|总是|下次|一直|往后')
_DIRECTIVE = re.compile(r'请|都|先|只|用|写|回答|标|给|保持|不要')
_POSTPONE = re.compile(r'(?:以后|今后|下次).{0,4}(?:再讨论|再说|再看|再处理)')
_SUPPLEMENT = re.compile(r'顺便|另外|再加|补充|截止|预算|先不做|这次|本次|提醒|开会')
_REMEMBER = re.compile(r'^(?:别忘|不要忘|记得)')
_CORRECTION = re.compile(r'^(?:不对|不是|别用|太长|换成|我要的是|这里(?:写)?错了|不用(?:铺垫|展开|背景))')


@dataclass(frozen=True)
class SteerPolicy:
    maximum_count: int = 5
    maximum_characters: int = 2000
    snapshot_characters: int = 300
    followup_seconds: int = 600

    def validate(self, text, *, count):
        if not isinstance(text,str) or not text.strip() or len(text)>self.maximum_characters:
            raise ValueError('invalid_steer_text')
        if type(count) is not int or count<0:
            raise ValueError('invalid_steer_count')
        if count>=self.maximum_count:
            raise ValueError('steer_limit')
        return text

    def __call__(self, text):
        if not isinstance(text,str):
            raise ValueError('invalid_steer_text')
        normalized=unicodedata.normalize('NFKC',text).strip()
        if _FUTURE.search(normalized) and _DIRECTIVE.search(normalized) and not _POSTPONE.search(normalized):
            category='future'
        elif _REMEMBER.search(normalized) or _SUPPLEMENT.search(normalized) and normalized.startswith(('这次','本次')):
            category='addition'
        elif _CORRECTION.search(normalized):
            category='correction'
        elif _SUPPLEMENT.search(normalized) or _POSTPONE.search(normalized):
            category='addition'
        else:
            category='other'
        direction=None
        if category in {'future','correction'}:
            # 不把相反要求仅凭同一主题归成同方向，未能证明相同的表达保留各自原意。
            direction=unicodedata.normalize('NFC',text).strip()
        return {'category':category,'direction':direction,'learnable':category in {'future','correction'}}

    def followup(self, previous_finished_at, next_created_at):
        previous,following=instant(previous_finished_at),instant(next_created_at)
        return previous is not None and following is not None and 0<=(following-previous).total_seconds()<=self.followup_seconds

    def tail(self, visible_text):
        if not isinstance(visible_text,str):
            raise ValueError('invalid_visible_steer_snapshot')
        return visible_text[-self.snapshot_characters:]

@dataclass(frozen=True)
class RuntimeSteerPolicy(SteerPolicy):
    new_task_characters: int = 600

    def validate_assignment_texts(self, proposed, originals, *, bound_ids=()):
        retained = [re.sub(r'\s+', '', originals[identity].get('division', {}).get('goal', originals[identity]['task'])) for identity in bound_ids]
        changed = []
        for spec in proposed:
            prior = originals.get(spec['assignment_id'])
            same = (prior is not None and spec['task'] == prior['task']
                and spec.get('division', {}).get('goal') == prior.get('division', {}).get('goal'))
            text = spec.get('division', {}).get('goal', spec['task'])
            normalized = re.sub(r'\s+', '', text)
            if same:
                retained.append(normalized)
            else:
                if not normalized or len(spec['task'].strip()) > self.new_task_characters or len(text.strip()) > self.new_task_characters:
                    raise ValueError('steer_new_assignment_too_long')
                changed.append(normalized)
        if len(changed) != len(set(changed)) or set(changed).intersection(retained):
            raise ValueError('steer_new_assignment_repeated')

    def answer_messages(self, messages, steers):
        if not isinstance(messages, list) or not steers:
            raise ValueError('invalid_steer_answer_input')
        pieces = []
        for steer in steers:
            text = self.validate(steer['text'], count=0)
            tail = steer.get('snapshot', {}).get('answer_tail', '')
            if not isinstance(tail, str) or len(tail) > self.snapshot_characters:
                raise ValueError('invalid_visible_steer_snapshot')
            pieces.append((f'已显示的回答末尾：\n{tail}\n' if tail else '') + f'用户补充：\n{text}')
        return [*deepcopy(messages), {'role': 'user', 'content':
            '请按以下补充重新完成当前回答。保留原资料依据。\n\n' + '\n\n'.join(pieces)}]

    def task_messages(self, messages, steers, *, topology, amend_allowed):
        if not isinstance(messages, list) or not steers:
            raise ValueError('invalid_steer_task_input')
        instruction = '用户对当前任务的补充：\n' + '\n\n'.join(self.validate(row['text'], count=0) for row in steers)
        if topology is not None and amend_allowed:
            instruction += ('\n\n实际有效分工：\n' + json.dumps(topology, ensure_ascii=False, separators=(',', ':'))
                + '\n已绑定的分工必须保留原内容与编号，已完成部分直接复用；运行中的子任务继续。'
                '需要调整尚未派发的部分时调用 agent.plan，mode=amend_unbound，沿原 plan_id 和 cluster_id；'
                'preserve_assignment_ids 精确列出全部已绑定编号，assignments 只列新的尚未派发分工。'
                '保留尚未开始的原目标时使用其原 assignment_id 和原 task、goal；新目标不超过600字且各不相同。'
                '新分工只能使用原分工中的角色、专家与技能，能力、总预算和并发维持原许可。'
                '只需调整最终汇总时沿原工具继续。')
        elif topology is not None:
            instruction += '\n此前补充继续参与当前任务；当前分工已按实际接纳的决定保存，沿现行分工执行并汇总。'
        block = {'role': 'user', 'content': instruction}
        return deepcopy(messages) if block in messages else [*deepcopy(messages), block]

    def task_assignment(self, task, steers):
        if not isinstance(task, str) or not task.strip() or not steers:
            raise ValueError('invalid_steer_assignment')
        result = task + '\n\n用户对本轮任务的补充：\n' + '\n\n'.join(self.validate(row['text'], count=0) for row in steers)
        if len(result) > 16000:
            raise ValueError('steer_assignment_too_long')
        return result


v1=SteerPolicy()
v2=RuntimeSteerPolicy()
