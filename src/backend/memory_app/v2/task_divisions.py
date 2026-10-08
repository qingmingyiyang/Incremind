"""Revisioned task examples, separate from recognition and document authority."""
from backend.recognition import RecognitionConflict
from types import SimpleNamespace
from .links import InsightLinks
from .privacy import is_private_project
from .outcome_corrections import record_division_adjust
from ..workspace_contracts import _now

COLLECTION = 'v2_task_divisions'


def validate_items(items):
    if not isinstance(items, list) or not 1 <= len(items) <= 8:
        raise ValueError('invalid_task_division')
    graph = {}
    for index, item in enumerate(items):
        if not isinstance(item, dict) or set(item) != {'goal','deliverable','capabilities','depends_on'}:
            raise ValueError('invalid_task_division')
        if any(not isinstance(item[key], str) or not item[key].strip() or len(item[key]) > 2000 for key in ('goal','deliverable')):
            raise ValueError('invalid_task_division')
        caps, deps = item['capabilities'], item['depends_on']
        if not isinstance(caps,list) or any(not isinstance(cap,str) or not cap for cap in caps) or len(caps) != len(set(caps)):
            raise ValueError('invalid_task_division')
        if not isinstance(deps,list) or any(type(dep) is not int or dep < 0 or dep >= len(items) or dep == index for dep in deps):
            raise ValueError('invalid_task_division')
        graph[index] = set(deps)
    completed = set()
    while len(completed) < len(graph):
        ready = {key for key, deps in graph.items() if key not in completed and deps <= completed}
        if not ready:
            raise ValueError('invalid_task_division')
        completed.update(ready)
    return items


class TaskDivisions:
    def __init__(self, records, models=None):
        self.records = records
        self.models = models

    def complete(self, identity, *, project, text, items, outcome):
        validate_items(items)
        if outcome not in {'done','partial','failed'}:
            raise ValueError('invalid_task_outcome')
        with self.records.begin() as tx:
            old = tx.read(COLLECTION,identity)
            if old is None:
                tx.put(COLLECTION,identity,{'project_id':project,'task_text':text,'items':items,
                    'outcome':outcome,'adjusted':False,'adjusted_at':None,'source_turn_id':identity}, expected_revision=0)
            tx.commit()

    def read(self, identity, project):
        row = self.records.read(COLLECTION,identity)
        if row is None or row.payload['project_id'] != project or row.payload.get('deleted'):
            raise ValueError('task_division_not_found')
        return {**row.payload, 'revision':row.revision}

    def adjust(self, identity, *, project, items, expected_revision):
        validate_items(items)
        self._change(identity, project, expected_revision,
                     {'items':items,'adjusted':True,'adjusted_at':_now()})

    def remove(self, identity, *, project, expected_revision):
        self._change(identity, project, expected_revision, {'deleted':True})

    def _change(self, identity, project, expected_revision, changes):
        with self.records.begin() as tx:
            row = tx.read(COLLECTION,identity)
            if row is None or row.payload['project_id'] != project or row.payload.get('deleted'):
                raise ValueError('task_division_not_found')
            if row.revision != expected_revision:
                raise RecognitionConflict('task_division_revision_conflicted')
            saved = tx.put(COLLECTION,identity,{**row.payload,**changes},expected_revision=row.revision)
            if changes.get('adjusted') is True:
                record_division_adjust(tx, row, saved)
            tx.commit()

    def similar(self, project, text, *, rows=None):
        ranked = []
        candidates = [row for row in (self.records.list(COLLECTION) if rows is None else rows)
            if not row.payload.get('deleted') and row.payload['outcome'] != 'failed'
            and not is_private_project(self.records, row.payload['project_id'])]
        if not candidates:
            return []
        def validate():
            for row in candidates:
                current = self.records.read(COLLECTION, row.object_id)
                if current is None or current.revision != row.revision or is_private_project(self.records, row.payload['project_id']):
                    raise RecognitionConflict('task_division_reference_changed')
        # Namespaced identities share the existing revision-bound cache without
        # colliding with recognition vectors. No new vector store or transport.
        entries = [SimpleNamespace(object_id='task-division-'+row.object_id, revision=row.revision,
                    payload={'project_id':row.payload['project_id'], 'content':row.payload['task_text']})
                   for row in candidates]
        anchor = SimpleNamespace(object_id='task-query', revision=1,
                                 payload={'project_id':project, 'content':text})
        scores, threshold = InsightLinks(self.records, None, self.models)._rank(
            project, anchor, entries, source_refs=lambda row: [], validate_current=validate,
            limit=len(entries), keyword_threshold=.2)
        validate()
        scores = {row.object_id:score for row, score in scores}
        for row in candidates:
            item = row.payload
            score = scores.get('task-division-'+row.object_id, 0)
            if score < threshold:
                continue
            ranked.append((item['project_id'] == project, item['adjusted'], score, row.object_id,
                           {**item,'revision':row.revision}))
        return [item[-1] for item in sorted(ranked, key=lambda item:item[:4],reverse=True)[:3]]
