"""Synthetic local placement evaluation with real domain facts and no model calls."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from backend.memory_app.v2.overviews import ScopeOverviews
from backend.memory_app.v2.placement import PlacementSuggestions
from backend.memory_app.v2.policies import override, parse_overrides, version
from backend.memory_app.v2.projects import assign_scene
from backend.recognition import RecognitionService, WorkScope
from core.document_engine import DocumentDraft, SQLiteDocumentRepository
from core.storage_provider import SQLiteStructuredRecordStore


class NoModels:
    def __init__(self):
        self.attempts = 0

    def complete(self, *args, **kwargs):
        self.attempts += 1
        raise AssertionError('Local placement must not call a model')


def _seed(records, service, corpus):
    with records.begin() as tx:
        for project in corpus['projects']:
            tx.put('v2_projects', project['id'], {'name': project['name'],
                'scenes': list(project['scenes']), 'private': False, 'builtin': None}, expected_revision=0)
        tx.commit()
    for project in corpus['projects']:
        own = WorkScope('local-user', project['id'])
        for scene, content in project['scenes'].items():
            experience = service.stage_experience(scope=own, content=content,
                provenance={'kind': 'user_statement', 'actor': 'local-user'})
            candidate = service.propose(scope=own, content=content, source_experience_ids=[experience])
            recognition = service.publish(scope=own, candidate_id=candidate.id,
                expected_revision=candidate.revision, reviewer='local-user')
            assign_scene(records, 'recognition', recognition.id, project['id'], scene)


def evaluate(fixtures, selections=None):
    corpus = json.loads(Path(fixtures).read_text(encoding='utf-8'))
    rows = []
    with TemporaryDirectory(prefix='chriptmas-place-eval-') as directory, override(**(selections or {})):
        records = SQLiteStructuredRecordStore(Path(directory) / 'records.sqlite3')
        documents, models = SQLiteDocumentRepository(records), NoModels()
        service = RecognitionService(records)
        _seed(records, service, corpus)
        suggestions = PlacementSuggestions(records, documents, service, ScopeOverviews(records, documents, models))
        for material in corpus['materials']:
            if material.get('quick_note'):
                own = WorkScope('local-user', material['current_project'])
                content = material['title'] + '\n' + material['summary']
                experience = service.stage_experience(scope=own, content=content,
                    provenance={'kind': 'user_statement', 'actor': 'local-user'})
                service.propose(scope=own, content=content, source_experience_ids=[experience])
                kind = 'quick_note'
            else:
                documents.create(DocumentDraft(title=material['title'], document_type='placement-fixture',
                    markdown='# ' + material['title'] + '\n\n## 摘要\n\n' + material['summary'],
                    source_refs=({'source_id': 'fixture-' + material['id'], 'locator': 'text:0'},),
                    project_id=material['current_project']))
                kind = 'document'
            hint = suggestions.suggest(material['title'], material['summary'], material['current_project'])
            actual = {'project_id': hint.project_id, 'scene': hint.scene} if hint else None
            rows.append({'id': material['id'], 'kind': kind, 'current_project': material['current_project'],
                'title': material['title'], 'summary': material['summary'], 'expected': material['expected'],
                'actual': actual, 'suggestion': asdict(hint) if hint else None, 'hit': actual == material['expected']})
        policy = version('place')
        attempts = models.attempts
    return {'evaluation_kind': 'synthetic-local-placement', 'fixture': {
        'projects': len(corpus['projects']), 'materials': len(rows),
        'life_projects': sum(bool(project.get('life_project')) for project in corpus['projects']),
        'quick_notes': sum(bool(material.get('quick_note')) for material in corpus['materials'])},
        'policy_versions': {'place': policy}, 'correct': sum(row['hit'] for row in rows),
        'accuracy': sum(row['hit'] for row in rows) / len(rows),
        'model_attempts': attempts, 'remote_model_attempts': 0, 'materials': rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixtures', type=Path, default=ROOT / 'tests/fixtures/place_eval/corpus.json')
    parser.add_argument('--policy', action='append', default=[])
    parser.add_argument('--report', type=Path, required=True)
    arguments = parser.parse_args()
    report = evaluate(arguments.fixtures, parse_overrides(arguments.policy))
    report['repository_revision'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    arguments.report.parent.mkdir(parents=True, exist_ok=True)
    arguments.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f"{report['correct']}/{report['fixture']['materials']} correct; models={report['model_attempts']}; remote=0")


if __name__ == '__main__':
    main()
