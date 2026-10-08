"""Local placement projections and sidecars over existing project facts."""
from dataclasses import asdict
from uuid import uuid4

from backend.recognition import RecognitionConflict, WorkScope
from core.document_engine import SQLiteDocumentRepository
from core.search_and_recall.evidence_windows import query_terms

from ..recall_state import is_recall_excluded
from .layers import summary_of
from .policies import get, version
from .policies.types import PlaceExample, PlaceProject, PlaceSuggestionInput, PlaceInboxInput, PlaceInboxItem
from .projects import assign_scene, scene_of
from .transaction_records import TransactionRecords
from .usage import utc_now


def _terms(text):
    return frozenset(term for term, _ in query_terms(text))


def record_choice(tx, document_id, source_project, target_project, scene, *,
                  document_revision, assignment_revision, title, summary, action_id=None, action='scene', kind='document'):
    """Enlist one explicit correction and its local example in the move UOW."""
    choice = {'document_id': document_id, 'project_id': source_project,
              'target_project_id': target_project, 'scene': scene,
              'document_revision': document_revision,
              'assignment_revision': assignment_revision,
              **({'kind': kind} if kind != 'document' else {}),
              **({'action_id': action_id, 'action': action} if action_id is not None else {})}
    for row in tx.list('v2_place_corrections'):
        if all(row.payload.get(key) == value for key, value in choice.items()):
            return dict(row.payload)
    identity = 'place-' + uuid4().hex
    event = {**choice, 'at': utc_now().isoformat()}
    tx.put('v2_place_corrections', identity, event, expected_revision=0)
    tx.put('v2_place_examples', identity, {
        'current_project': source_project, 'project_id': target_project, 'scene': scene,
        'terms': sorted(_terms(title + ' ' + summary)), 'document_id': document_id,
        'document_revision': document_revision, 'correction_id': identity,
        **({'kind': kind} if kind != 'document' else {}),
    }, expected_revision=0)
    return event


class PlacementSuggestions:
    def __init__(self, records, documents, service, overviews):
        self.records, self.documents = records, documents
        self.service, self.overviews = service, overviews

    def _projects(self):
        result = []
        for row in self.records.list('v2_projects'):
            if row.object_id in {'me', 'inbox'}:
                continue
            scenes = {name: set(_terms(name)) for name in row.payload['scenes']}
            vocabulary = set(_terms(row.payload['name']))
            overview = self.overviews.current(row.object_id)
            if overview:
                vocabulary.update(_terms(overview['text']))
            for name in scenes:
                overview = self.overviews.current(row.object_id, name)
                if overview:
                    scenes[name].update(_terms(overview['text']))
            own = WorkScope('local-user', row.object_id)
            for recognition in self.service.list_recognitions(scope=own):
                if not recognition.authorized or is_recall_excluded(self.records, own, recognition.id):
                    continue
                terms = _terms(recognition.content)
                vocabulary.update(terms)
                assignment = scene_of(self.records, 'recognition', recognition.id)
                if assignment and assignment.get('project_id') == row.object_id:
                    scene = assignment.get('scene')
                    if scene in scenes:
                        scenes[scene].update(terms)
            vocabulary.update(term for terms in scenes.values() for term in terms)
            result.append(PlaceProject(row.object_id, frozenset(vocabulary),
                {name: frozenset(terms) for name, terms in scenes.items()}))
        return result

    def suggest(self, title, summary, current_project, *, policy_version=None):
        selected = policy_version or version('place')
        if selected == '@1':
            return None
        latest = {}
        events = {row.object_id: row.payload for row in self.records.list('v2_place_corrections')}
        for row in self.records.list('v2_place_examples'):
            value = row.payload
            identity = (value['current_project'], value.get('kind', 'document'), value['document_id'])
            order = (events[value['correction_id']]['at'], row.object_id)
            if identity not in latest or latest[identity][0] < order:
                latest[identity] = (order, value)
        examples = [PlaceExample(value['current_project'], frozenset(value['terms']),
            value['project_id'], value['scene']) for _, value in latest.values()]
        return get('place', version=selected)(PlaceSuggestionInput(current_project,
            query_terms(title + ' ' + summary), self._projects(), examples))

    def inbox_projects(self, rows, *, policy_version=None):
        selected = policy_version or version('place')
        if selected == '@1':
            return []
        groups = get('place', version=selected)(PlaceInboxInput([
            PlaceInboxItem(row['id'], query_terms(row['text'])) for row in rows
            if row['kind'] == 'candidate' and row['state'] == 'pending']))
        return [{**asdict(group), 'count': len(group.ids)} for group in groups]

    def admit(self, document_id, project, *, tagged=False, policy_version=None):
        selected = policy_version or version('place')
        if tagged or selected == '@1':
            return None
        document = self.documents.read(document_id)
        if (document is None or document.get('project_id') != project
                or document.get('status') == 'archived'):
            raise RecognitionConflict('placement_document_unavailable')
        old = self.records.read('v2_place_hints', document_id)
        if old is not None:
            if (old.payload.get('source_project_id') != project
                    or old.payload.get('document_revision') != document['revision']):
                raise RecognitionConflict('placement_document_changed')
            return dict(old.payload)
        summary = summary_of(self.documents.markdown(document_id) or '')[0]
        hint = self.suggest(document['title'], summary, project, policy_version=selected)
        if hint is None:
            return None
        payload = {**asdict(hint), 'source_project_id': project,
            'document_revision': document['revision'], 'policy_version': selected}
        with self.records.begin() as tx:
            current = tx.read('documents', document_id)
            if current is None or current.payload.get('revision') != document['revision']:
                raise RecognitionConflict('placement_document_changed')
            old = tx.read('v2_place_hints', document_id)
            if old is not None:
                if dict(old.payload) != payload:
                    raise RecognitionConflict('placement_hint_changed')
                return dict(old.payload)
            tx.put('v2_place_hints', document_id, payload, expected_revision=0)
            if hint.project_id == project and hint.scene is not None:
                assign_scene(TransactionRecords(tx), 'document', document_id, project,
                    hint.scene, if_absent=True)
            tx.commit()
        return payload

    def correct_scene(self, document_id, project, scene, *, expected_revision, assignment_revision):
        if (type(expected_revision) is not int or expected_revision < 1
                or type(assignment_revision) is not int or assignment_revision < 0):
            raise RecognitionConflict('placement_revision_invalid')
        with self.records.begin() as tx:
            document = tx.read('documents', document_id)
            target = tx.read('v2_projects', project)
            if (document is None or document.payload.get('project_id') != project
                    or document.payload.get('status') == 'archived'
                    or document.payload.get('revision') != expected_revision
                    or target is None or scene not in target.payload['scenes']):
                raise RecognitionConflict('placement_document_unavailable')
            current = tx.read('v2_scene_assignments_document', document_id)
            if (current.revision if current else 0) != assignment_revision:
                raise RecognitionConflict('placement_scene_changed')
            repository = SQLiteDocumentRepository(TransactionRecords(tx), namespace_id=self.documents.namespace_id)
            summary = summary_of(repository.markdown(document_id) or '')[0]
            event = record_choice(tx, document_id, project, project, scene,
                document_revision=expected_revision, assignment_revision=assignment_revision,
                title=document.payload['title'], summary=summary)
            if current is None or current.payload != {'project_id': project, 'scene': scene}:
                assign_scene(TransactionRecords(tx), 'document', document_id, project, scene)
            tx.commit()
        return event
