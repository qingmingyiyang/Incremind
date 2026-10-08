"""第一版技能草稿提示词，只接收已经冻结的方法认识。"""
from dataclasses import dataclass
import json

from . import register


@dataclass(frozen=True)
class SkillAuthor:
    max_tokens: int = 4096

    def __call__(self, sources):
        return self.prepare(sources)

    def prepare(self, sources):
        evidence = [{key: source[key] for key in
            ('number', 'id', 'revision', 'text', 'conditions', 'scene', 'validity')
            if key in source} for source in sources]
        return [{'role': 'system', 'content':
            '仅根据给定有效方法认识生成待人工审阅的技能草稿，不安装、不执行、不确认认识。'
            '完整保留适用条件和限制，不编造材料未支持的步骤。每一步引用对应来源编号。'
            'name为不超过64字符的小写字母数字及连字符；description不超过1024字；'
            'trigger、每个步骤正文和验证规则不超过8192字，步骤及验证各1至100条。'
            '只返回JSON {"name":"skill-name","description":"描述","trigger":"触发边界",'
            '"steps":[{"text":"步骤","sources":[1]}],"validation":["验证规则"]}。'},
            {'role': 'user', 'content': json.dumps({'sources': evidence}, ensure_ascii=False)}]

    def render_source(self, source):
        return self.prepare([source])[1]['content']


v1 = SkillAuthor()
register('skill_author', '@1')(v1)
