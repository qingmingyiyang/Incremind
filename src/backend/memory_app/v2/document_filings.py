"""Explicit document moves enlist existing writes and retained-source authority."""
import logging
from uuid import uuid4

from backend.recognition import RecognitionConflict, RecognitionError, RecognitionService, WorkScope
from .filing_sources import filing_experience
from core.document_engine import DocumentDraft, SQLiteDocumentRepository

from ..document_recognition import ensure_document_experience
from ..source_egress import SourceEgressService
from .insights import source_documents
from .layers import summary_of
from .placement import record_choice
from .privacy import is_private_project
from .projects import assign_scene
from .transaction_records import TransactionRecords


def filing_view(reader, document_id, project):
    recall = reader.read('v2_document_recall', document_id)
    if recall is None or recall.payload.get('by') != 'moved':
        return None
    target_id = recall.payload.get('moved_to')
    marker = reader.read('v2_document_filings', target_id) if isinstance(target_id, str) else None
    if (marker is not None and marker.payload.get('source_document_id') == document_id
            and marker.payload.get('source_project_id') == project and marker.payload.get('state') == 'filed'):
        destination = marker.payload.get('target_project_id')
    else:
        marker = reader.read('v2_document_filings', document_id)
        if (marker is None or marker.payload.get('target_project_id') != project
                or marker.payload.get('source_document_id') != target_id or marker.payload.get('state') != 'undone'):
            return None
        destination = marker.payload.get('source_project_id')
    try:
        filing_experience(reader, WorkScope('local-user', marker.payload['target_project_id']), marker.object_id)
    except RecognitionError:
        return None
    return {'target_project_id': destination, 'target_document_id': target_id,
        'filing_document_id': marker.object_id, 'filing_revision': marker.revision,
        'state': marker.payload['state']}


class DocumentFilings:
    def __init__(self, records, documents, service, models):
        self.records, self.documents, self.service, self.models = records, documents, service, models

    def _view(self, row):
        return {**dict(row.payload), 'filing_revision': row.revision}

    def move(self, document_id, source_project, target_project, *, scene=None, expected_revision):
        if type(expected_revision) is not int or expected_revision < 1:
            raise RecognitionConflict('document_revision_invalid')
        existing = None
        with self.records.begin() as tx:
            document = tx.read('documents', document_id)
            target = tx.read('v2_projects', target_project)
            if (document is None or document.payload.get('project_id') != source_project
                    or document.payload.get('revision') != expected_revision
                    or document.payload.get('status') == 'archived'):
                raise RecognitionConflict('document_unavailable')
            if (target is None or target_project in {'me', 'inbox', source_project}
                    or scene is not None and scene not in target.payload['scenes']):
                raise RecognitionError('document_destination_invalid')
            if is_private_project(tx, source_project):
                raise RecognitionError('document_private_source')
            for row in tx.list('v2_document_filings'):
                if (row.payload.get('source_document_id') == document_id
                        and row.payload.get('source_project_id') == source_project
                        and row.payload.get('state') == 'filed'):
                    if (row.payload.get('source_document_revision') != expected_revision
                            or row.payload.get('target_project_id') != target_project
                            or row.payload.get('scene') != scene):
                        raise RecognitionConflict('document_already_moved')
                    filing_experience(tx, WorkScope('local-user', target_project), row.object_id)
                    existing = row
                    break
            if existing is None:
                enlisted = TransactionRecords(tx)
                repository = SQLiteDocumentRepository(enlisted, namespace_id=self.documents.namespace_id)
                writer = RecognitionService(enlisted, cache_invalidation=self.service.cache_invalidation)
                scope = WorkScope('local-user', source_project)
                source_experience, revision = ensure_document_experience(repository, writer, source_project, document_id)
                experience = tx.read('recognition_experiences', source_experience)
                authority = SourceEgressService(enlisted)
                snapshot = authority.snapshot(scope, [{'type': 'experience', 'id': source_experience,
                    'revision': experience.revision}])
                authority.require(snapshot, 'generation')
                markdown = repository.markdown(document_id, revision=revision)
                destination = self.documents.create_or_replay_generated_in_uow(DocumentDraft(
                    title=document.payload['title'], document_type='filed-v2-' + uuid4().hex,
                    markdown=markdown, source_refs=tuple(document.payload['source_refs']), project_id=target_project), tx)
                copied = writer.stage_experience(scope=WorkScope('local-user', target_project), content=markdown,
                    copy_from={'project_id': source_project, 'experience_id': source_experience,
                               'revision': experience.revision})
                original_recall = tx.read('v2_document_recall', document_id)
                assignment = tx.read('v2_scene_assignments_document', document_id)
                marker = {'user_id': 'local-user', 'source_project_id': source_project,
                    'source_document_id': document_id, 'source_document_revision': revision,
                    'target_project_id': target_project, 'target_document_id': destination['id'],
                    'target_document_revision': destination['revision'], 'target_experience_id': copied,
                    'target_experience_revision': tx.read('recognition_experiences', copied).revision,
                    'prior_recall': dict(original_recall.payload) if original_recall else None,
                    'state': 'filed', 'scene': scene}
                existing = tx.put('v2_document_filings', destination['id'], marker, expected_revision=0)
                tx.put('v2_document_recall', document_id, {'state': 'forgotten', 'by': 'moved',
                    'moved_to': destination['id']}, expected_revision=original_recall.revision if original_recall else 0)
                for candidate in tx.list('recognition_candidates'):
                    if (candidate.payload.get('scope') == {'user_id': scope.user_id, 'project_id': scope.project_id}
                            and candidate.payload.get('state') == 'pending'
                            and not tx.read('v2_candidate_merges', candidate.object_id)
                            and document_id in source_documents(tx, scope,
                                candidate.payload.get('source_experience_ids', []),
                                candidate.payload.get('source_recognition_ids', []))):
                        writer.reject_candidate(scope=scope, candidate_id=candidate.object_id,
                            expected_revision=candidate.revision, reviewer='local-user')
                if scene is not None:
                    assign_scene(enlisted, 'document', destination['id'], target_project, scene)
                record_choice(tx, document_id, source_project, target_project, scene,
                    document_revision=revision, assignment_revision=assignment.revision if assignment else 0,
                    title=document.payload['title'], summary=summary_of(markdown)[0],
                    action_id=destination['id'], action='move')
                tx.put('v2_document_filing_aux', destination['id'], {
                    'project_id': target_project, 'document_revision': destination['revision'],
                    'state': 'pending', 'insights': [], 'error': None}, expected_revision=0)
                tx.commit()
        result = self._view(existing)
        result.update(self._extract(existing))
        return result

    def _extract(self, filing):
        identity, project = filing.object_id, filing.payload['target_project_id']
        row = self.records.read('v2_document_filing_aux', identity)
        if row is None or row.payload['state'] != 'pending':
            return {'insights': row.payload['insights'] if row else [], 'error': row.payload['error'] if row else None}
        errors, insights = [], []
        try:
            from .insight_generation import generate_insights
            insights = generate_insights(self.models, self.service, self.documents, project, identity,
                on_error=errors.append)
        except Exception as error:
            logging.getLogger(__name__).warning('document_filing_aux_failed exception_type=%s', type(error).__name__)
            errors.append('insight_generation_failed')
        if errors and not insights:
            # The Turn, rather than this auxiliary sidecar, owns dispatch.
            # A concurrent replay without its durable output cannot finalize
            # the first caller's still-running result as a permanent failure.
            from .memory_turn import MemoryTurn
            key = {'kind': 'memory.propose_insights', 'project': project,
                'key': f"candidate-v2-{identity}-r{row.payload['document_revision']}-"}
            request = next((item for item in self.records.list('v2_memory_turn_keys')
                if item.payload['identity'] == key), None)
            if request is not None:
                store = MemoryTurn.store_for(self.records)
                events = tuple(store.events_after(request.object_id))
                if (events and events[-1]['type'] not in {'turn.completed', 'turn.failed', 'turn.cancelled'}
                        and store.get_immutable_payload(request.object_id, 'memory-generation-output-v1') is None):
                    return {'insights': [], 'error': None}
        # The move is already durable. Derived extraction errors remain an
        # error on its receipt and cannot turn a successful move into a failure.
        with self.records.begin() as tx:
            current = tx.read('v2_document_filing_aux', identity)
            if current.payload['state'] == 'pending':
                current = tx.put(current.collection, identity, {**current.payload,
                    'state': 'done', 'insights': insights, 'error': errors[0] if errors else None},
                    expected_revision=current.revision)
                tx.commit()
        return {'insights': current.payload['insights'], 'error': current.payload['error']}

    def undo(self, target_document_id, target_project, *, expected_revision):
        if type(expected_revision) is not int or expected_revision < 1:
            raise RecognitionConflict('filing_revision_invalid')
        with self.records.begin() as tx:
            marker = tx.read('v2_document_filings', target_document_id)
            if (marker is None or marker.payload.get('target_project_id') != target_project
                    or marker.revision != expected_revision or marker.payload.get('state') != 'filed'):
                raise RecognitionConflict('document_filing_changed')
            filing_experience(tx, WorkScope('local-user', target_project), target_document_id)
            source_id = marker.payload['source_document_id']
            source = tx.read('documents', source_id)
            if source.payload['revision'] != marker.payload['source_document_revision']:
                raise RecognitionConflict('document_source_changed')
            moved = tx.read('v2_document_recall', source_id)
            if moved is None or dict(moved.payload) != {
                    'state': 'forgotten', 'by': 'moved', 'moved_to': target_document_id}:
                raise RecognitionConflict('document_recall_changed')
            copy_recall = tx.read('v2_document_recall', target_document_id)
            if copy_recall is not None:
                # A later move or a user recall decision is another action;
                # undoing this filing must not overwrite its current fact.
                raise RecognitionConflict('document_copy_recall_changed')
            prior = marker.payload['prior_recall']
            if prior is None:
                tx.delete(moved.collection, source_id, expected_revision=moved.revision)
            else:
                tx.put(moved.collection, source_id, prior, expected_revision=moved.revision)
            tx.put('v2_document_recall', target_document_id, {'state': 'forgotten', 'by': 'moved',
                'moved_to': source_id}, expected_revision=copy_recall.revision if copy_recall else 0)
            assignment = tx.read('v2_scene_assignments_document', source_id)
            repository = SQLiteDocumentRepository(TransactionRecords(tx), namespace_id=self.documents.namespace_id)
            record_choice(tx, source_id, marker.payload['source_project_id'], marker.payload['source_project_id'],
                assignment.payload['scene'] if assignment else None,
                document_revision=source.payload['revision'], assignment_revision=assignment.revision if assignment else 0,
                title=source.payload['title'], summary=summary_of(repository.markdown(source_id))[0],
                action_id='undo-' + target_document_id, action='undo')
            saved = tx.put(marker.collection, target_document_id, {**marker.payload, 'state': 'undone'},
                expected_revision=marker.revision)
            tx.commit()
        return self._view(saved)
