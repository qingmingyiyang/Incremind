"""Offline annotated method recall through the actual domain and policy registry."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.memory_eval import seed, NoModels
from backend.memory_app.v2.budget import evidence_tokens
from backend.shared.llm.message_metadata import _estimate_input_tokens
from backend.memory_app.v2.policies import override, parse_overrides


class LocalRecallOnly(NoModels):
    def public(self):
        # This switch admits local synthetic method selection, never a wire call.
        return {'generation': {'base_url': 'http://127.0.0.1:0', 'allow_remote': True, 'model': 'offline'}}


def evaluate(fixture_path=None):
    fixture = json.loads(Path(fixture_path or ROOT / 'tests/fixtures/memory_eval/applicable_methods.json').read_text(encoding='utf-8'))
    with TemporaryDirectory(prefix='cm-method-') as directory, closing(sqlite3.connect(Path(directory) / 'records.sqlite3')) as keeper:
        keeper.execute('PRAGMA journal_mode=WAL').fetchone()
        query, names = seed(Path(directory), fixture)
        query.models = LocalRecallOnly()
        reverse = {identity: name for name, identity in names.items()}
        rows = []
        for item in fixture['questions']:
            plan = query.prepare_ask(item['project_id'], item['question'], scene=item.get('scene'))
            selected = {reverse[row['id']] for row in plan['chosen'] if row.get('supplemented')}
            expected = set(item['expected_ids'])
            evidence = '\n\n'.join(row['excerpt'] for row in plan['chosen'])
            fragments = []
            for row in plan['chosen']:
                body = row['entry']['content']
                conditions = '\n\n适用条件：\n' + '\n'.join('- ' + value for value in row['entry']['conditions']) if row['entry']['conditions'] else ''
                source = row['excerpt'][len(body + conditions):]
                def tokens(text):
                    return _estimate_input_tokens([{'role': 'user', 'content': text}]) if text else 0
                fragments.append({'id': reverse[row['id']], 'supplemented': bool(row.get('supplemented')),
                    'body_tokens': tokens(body), 'condition_tokens': tokens(conditions),
                    'source_metadata_tokens': tokens(source), 'excerpt_tokens': tokens(row['excerpt'])})
            rows.append({'id': item['id'], 'expected_ids': sorted(expected), 'selected_ids': sorted(selected),
                         'hits': len(selected & expected), 'expected_count': len(expected),
                         'extra': len(selected - expected), 'supplement_count': len(selected),
                         'estimated_tokens': _estimate_input_tokens([{'role': 'user', 'content': evidence}]) if evidence else 0,
                         'prompt_evidence_tokens': evidence_tokens(plan['chosen']), 'fragments': fragments,
                         'policy_versions': plan['policy_versions']})
        expected_count = sum(row['expected_count'] for row in rows)
        selected_count = sum(row['supplement_count'] for row in rows)
        revision = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT,
            capture_output=True, text=True, check=True).stdout.strip()
        return {'category': 'applicable_methods', 'questions': rows, 'repository_revision': revision,
                'hit_rate': sum(row['hits'] for row in rows) / expected_count,
                'over_supplement_rate': sum(row['extra'] for row in rows) / selected_count if selected_count else 0,
                'average_evidence_tokens': sum(row['estimated_tokens'] for row in rows) / len(rows),
                'token_measure': 'gateway UTF-8 estimate of selected evidence; identical to memory_eval',
                'model_attempts': query.models.attempts, 'remote_model_attempts': 0,
                'metric_definitions': {'hit_rate': 'Expected method IDs selected / annotated expected IDs',
                    'over_supplement_rate': 'Unannotated method IDs selected / all supplemented IDs'}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', action='append', default=[])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    with override(**parse_overrides(args.policy)):
        result = evaluate()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: result[key] for key in ('hit_rate', 'over_supplement_rate', 'average_evidence_tokens', 'model_attempts')}))


if __name__ == '__main__':
    main()
