from dataclasses import dataclass
from datetime import timedelta
import json


@dataclass(frozen=True)
class SharedConsolidationPolicy:
    recent: timedelta = timedelta(days=7)
    lease: timedelta = timedelta(minutes=10)
    max_documents: int = 8
    max_fact_chars: int = 12000
    max_output_tokens: int = 1000

    def __call__(self, source, *, constraints):
        return [{'role': 'system', 'content': constraints
            + '从不同原件的近期事实提出待确认的共同规律。保留适用条件。每条列出至少两份支撑它的文稿编号。'
              '没有共同规律时返回空数组。输出格式为 {"patterns":[{"text":"不超过40字","conditions":[],"document_ids":[]}]}。'},
            {'role': 'user', 'content': json.dumps([{'id': identity, 'title': fact['title'], 'facts': fact['text']}
                for identity, fact in source.items()], ensure_ascii=False)}]


v1 = SharedConsolidationPolicy()
