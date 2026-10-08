"""Offline trigger and real correction consolidation comparisons; fake wires only."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
from unittest.mock import patch

from memory_eval import ROOT, seed
from backend.memory_app.v2.consolidation import Consolidation
from backend.memory_app.v2.learning_events import events
from backend.memory_app.v2.memory_turn import MemoryTurn
from backend.memory_app.v2.policies import get, override, parse_overrides, version
from backend.memory_app.v2.policies.types import TriggerInput
from backend.recognition import WorkScope
from backend.shared.llm.message_metadata import _estimate_input_tokens
from core.storage_provider.connection_scope import connection_scope


class FakeWire:
    def __init__(self):
        self.calls = []

    def public(self):
        return {'generation': {'configured': True, 'enabled': True, 'allow_remote': True,
                'base_url': 'https://example.invalid/v1', 'revision': 1, 'model': 'fake'},
                'generation_mode': {'revision': 1}}

    def complete(self, messages, *, max_tokens, validate_current, wire_attempt_sink):
        validate_current()
        attempt = wire_attempt_sink.begin_model_wire_attempt()
        def wire():
            self.calls.append(messages)
            event_ids = re.findall(r'"event_id":\s*"([^"]+)"', messages[-1]['content'])
            value = {'text': '送礼前逐项核对售后政策', 'conditions': ['挑礼物时']}
            if event_ids:
                value.update(event_ids=event_ids, kind='correction')
            if any('最多300字' in message['content'] for message in messages):
                value = {'text': '合成概览'}
            incoming = _estimate_input_tokens(messages)
            attempt.succeeded(usage={'input_tokens': incoming, 'output_tokens': 8, 'total_tokens': incoming + 8}, cache_observation=None)
            return json.dumps(value, ensure_ascii=False)
        output = attempt.invoke_wire(wire)
        validate_current()
        return output, {'model': 'fake', 'configuration_revision': 1}


def evaluate(fixture):
    policy = get('trigger')
    cases = []
    for case in fixture['trigger_cases']:
        due = (case['run_seconds'] >= policy(TriggerInput(False)) if version('trigger') == '@1' else
               policy(None, operation='due', score=case['score'], run_seconds=case['run_seconds']))
        cases.append({**case, 'actual': due, 'hit': due == case['expected']})
    previous = os.environ.get('CHRIPTMAS_APP_ROOT')
    with TemporaryDirectory(prefix='ct127-eval-', dir=Path(ROOT.anchor)) as directory, connection_scope():
        os.environ['CHRIPTMAS_APP_ROOT'] = directory
        try:
            query, _ = seed(Path(directory), fixture)
            with patch('backend.recognition.service._now', return_value='2026-10-04T00:00:00+00:00'):
                query.service.revise(scope=WorkScope('local-user', 'alpha'), recognition_id='gift-method',
                                     expected_revision=1, content='下次先核实售后期限')
            model = FakeWire()
            outcome = Consolidation(query.records, query.service, query.documents, model,
                now=lambda: datetime.fromisoformat('2026-10-05T00:00:00+00:00')).run('alpha')
            store = MemoryTurn.store_for(query.records)
            requests = [store.get_request(row.object_id) for row in query.records.list('v2_memory_turn_keys')]
            inputs = [request['input']['text'] for request in requests if request['desired_outcome'] == 'memory.consolidate']
            domain = {'new_suggestions': outcome['new_suggestions'], 'expected': 1,
                      'hit': outcome['new_suggestions'] == 1, 'event_count': len(query.records.list('v2_correction_events')),
                      'learning_fact_count': len(events(query.records)['alpha']), 'frozen_event_present': any('event_id' in text for text in inputs),
                      'wire_count': len(model.calls), 'wire_input_tokens': sum(_estimate_input_tokens(messages) for messages in model.calls),
                      'active_recognition_count': len(query.records.list('recognitions'))}
        finally:
            if previous is None:
                os.environ.pop('CHRIPTMAS_APP_ROOT', None)
            else:
                os.environ['CHRIPTMAS_APP_ROOT'] = previous
    producer = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    return {'producer': producer, 'tracked_dirty': bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip()),
            'policies': {'trigger': version('trigger'), 'consolidate': version('consolidate')},
            'trigger_cases': cases, 'trigger_hits': sum(case['hit'] for case in cases), 'trigger_total': len(cases),
            'domain': domain, 'paid_calls': 0, 'formal_runtime_writes': 0, 'old_memory72_rerun': False,
            'scope': 'early trigger decisions and one real source-bound correction; no retrieval corpus rerun'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--policy', action='append', default=[])
    parser.add_argument('--fixture', type=Path, default=ROOT / 'tests/fixtures/memory_eval/learning.json')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    with override(**parse_overrides(args.policy)):
        result = evaluate(json.loads(args.fixture.read_text(encoding='utf-8')))
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'producer': result['producer'], 'trigger': f"{result['trigger_hits']}/{result['trigger_total']}",
        'domain': result['domain'], 'paid_calls': 0, 'formal_runtime_writes': 0}, ensure_ascii=False))
