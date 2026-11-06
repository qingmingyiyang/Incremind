from dataclasses import dataclass, replace
import json

from .material_analysis import _object, decode
from .material_angles import MaterialAnalysisAnglesPolicy
from .topic_grouping import label, labels


@dataclass(frozen=True)
class MaterialAnalysisTopicsPolicy(MaterialAnalysisAnglesPolicy):
    known_topics: tuple = ()

    def bind_topics(self, names):
        return replace(self, known_topics=tuple(label(name) for name in names))

    def instructions(self, operation):
        instructions = super().instructions(operation)
        if operation != 'plan':
            return instructions
        instructions += ('在同一个 JSON 对象增加 topic_groups 字段，包含一到三个主题组名。'
            '沿用适合本材料的已有组名，也可根据本材料提出新名称，每个名称最多二十四字。')
        if self.known_topics:
            instructions += '以下分类数据仅提供组名。' + json.dumps(self.known_topics, ensure_ascii=False)
        return instructions

    def parse_plan(self, raw):
        value = _object(decode(raw), ('title', 'angle_presence', 'supplemental_angles',
            'summary_by_angle', 'temporal', 'topic_groups'))
        names = labels(value['topic_groups'])
        existing = {name.casefold(): name for name in self.known_topics}
        base = {key: item for key, item in value.items() if key != 'topic_groups'}
        return {**super().parse_plan(json.dumps(base, ensure_ascii=False)),
            'topic_groups': [existing.get(name.casefold(), name) for name in names]}


v7 = MaterialAnalysisTopicsPolicy()
