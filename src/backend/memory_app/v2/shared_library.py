from contextlib import contextmanager
from threading import RLock

from fastapi import HTTPException
from fastapi.responses import FileResponse

from backend.recognition import WorkScope
from ..processing_lease import ProcessingLease
from ..source_egress import recognition_service
from ..transaction_records import TransactionRecords
from ..workspace import WorkspaceDomains
from ..workspace_intake import WorkspaceIntake
from ..workspace_items import WorkspaceItems
from ..workspace_review import WorkspaceReview
from .insights import insight_view
from .library import LibraryRead
from .shared_library_projection import library_projection
from .shared_projects import ATTRIBUTION, SharedProjectError, revision
from .shared_runtime import SharedWriteRecords, shared_read_capability


class SharedLibrary:
    def __init__(self, factory):
        self.factory, self.registry = factory, factory.registry

    def _reader(self, application, actor, project):
        query = self.factory.query(application, actor, project).local_reader()
        with self.registry.records.begin_read() as central:
            access = self.registry.authorize(central, actor, project)
        return query, self._view(query, access), access

    @contextmanager
    def _projection(self, application, actor, project):
        issued = self.factory.query(application, actor, project)
        with library_projection(issued) as (query, access):
            yield query, self._view(query, access), access

    @staticmethod
    def _view(query, access):
        from .shared_library_files import source_workspace
        source_workspace(query)
        root = query.material_owner.root
        items = WorkspaceItems(query.records,
            ProcessingLease(query.records, 'workspace_items', 'shared-library-read'), RLock())
        intake = WorkspaceIntake(root, items, query.models)
        review = WorkspaceReview(items, query.documents, query.service, None, None, root / 'workspace')
        workspace = WorkspaceDomains(items, intake, review, query, None, None)
        return LibraryRead(query.records, query.service, query.documents, workspace,
            can_write=access.role in {'owner', 'member', 'admin'}, source_namespace=query.source_namespace)

    @staticmethod
    def _actors(reader, insight):
        collection = 'recognitions' if insight['kind'] == 'recognition' else 'recognition_candidates'
        row = reader.read(collection, insight['id'])
        if row is None:
            raise SharedProjectError('shared_library_source_changed', status=409)
        writes = [entry for entry in reader.list(ATTRIBUTION)
            if entry.payload.get('collection') == collection and entry.payload.get('object_id') == row.object_id
            and entry.payload.get('object_revision') == 1 and entry.payload.get('action') == 'write']
        author = min(writes, key=lambda entry: (entry.payload['at'], entry.object_id)) if writes else None
        from .shared_insight_maintenance import recall_preference_revision
        return {**insight, 'recall_preference_revision': recall_preference_revision(reader, row, kind=insight['kind']),
            'by': author.payload['actor_user_id'] if author is not None else None,
            'confirmed_by': row.payload.get('published_by') if collection == 'recognitions' else None}

    def list(self, application, actor, project, *, layer, scene=None, q='', state=None):
        if layer not in {'insights', 'summaries', 'notes', 'sources'}:
            raise SharedProjectError('shared_library_layer_invalid')
        if state not in {None, 'pending', 'active', 'stale', 'forgotten', 'unverified', 'verified', 'archived'}:
            raise SharedProjectError('shared_library_state_invalid')
        if not isinstance(q, str) or len(q) > 2000 or scene is not None and not isinstance(scene, str):
            raise SharedProjectError('shared_library_filter_invalid')
        with self._projection(application, actor, project) as (query, read, access):
            result = read.list(layer, project, scene, q, state)
            if layer == 'insights':
                result['items'] = [self._actors(query.records, insight) for insight in result['items']]
            return {**result, 'namespace': query.source_namespace, 'can_write': read.can_write,
                'authority': access.authority}

    def drill(self, application, actor, project, *, layer, identity, document_id=None, source_id=None):
        if layer not in {'insight', 'summary', 'note', 'source'}:
            raise SharedProjectError('shared_library_layer_invalid')
        with self._projection(application, actor, project) as (query, read, access):
            result = read.drill(project, layer, identity, document_id=document_id, source_id=source_id)
            if result.get('insight') is not None:
                result['insight'] = self._actors(query.records, result['insight'])
                from .shared_insight_maintenance import _eligible_replacement
                insight = result['insight']
                result['can_merge_insight'] = (read.can_write and insight['kind'] == 'recognition'
                    and _eligible_replacement(query.records, query.service, WorkScope('local-user', project),
                        query.records.read('recognitions', insight['id'])))
            source = result.get('source')
            if source is not None and source.get('download_url') is not None:
                source['download_url'] = ('/api/v2/server/shared-projects/' + project
                    + '/library/originals/' + source['id'])
            return {**result, 'authority': access.authority}

    def original(self, application, actor, project, identity):
        query, read, _access = self._reader(application, actor, project)
        response = read.workspace.review.original(identity, project)
        query.validate_retrieval_origin()
        return query, response

    def full_source(self, application, actor, project, identity):
        with self._projection(application, actor, project) as (query, read, access):
            result = read.full_source(project, identity)
            if result.get('download_url') is not None:
                result['download_url'] = ('/api/v2/server/shared-projects/' + project
                    + '/library/originals/' + identity)
            return {**result, 'project_id': project, 'namespace': query.source_namespace,
                'can_write': read.can_write, 'authority': access.authority}

    def _images(self, application, actor, project, identity):
        from .image_read import uploaded_images, group_entries
        query, read, _access = self._reader(application, actor, project)
        row = read.workspace.items.item_for(identity, project)
        if row.payload.get('input_kind') != 'image':
            raise HTTPException(404, 'original_not_found')
        images = uploaded_images(query.records, query.material_owner.root, project,
            [{'type': 'original_item', 'id': identity, 'revision': row.revision, 'project_id': project}])
        names = group_entries(query.records, row.payload)
        from .shared_library_files import SourceFile
        for image in images:
            SourceFile(query, image['identity']['path']).validate()
        query.validate_retrieval_origin()
        return query, row, images, names

    def images(self, application, actor, project, identity):
        from .image_read import image_inference_for_document
        query, row, images, names = self._images(application, actor, project, identity)
        result = {'images': [{'ordinal': image['ordinal'], 'name': entry['name'],
            'url': '/api/v2/server/shared-projects/' + project + '/library/sources/' + identity
                + '/images/' + str(image['ordinal'])} for image, entry in zip(images, names)],
            'project_id': project, 'namespace': query.source_namespace}
        inference = image_inference_for_document(query.records, query.documents, row)
        if inference is not None:
            result['image_read'] = inference
        query.validate_retrieval_origin()
        return result

    def image(self, application, actor, project, identity, ordinal):
        query, _row, images, names = self._images(application, actor, project, identity)
        if not 1 <= ordinal <= len(images):
            raise HTTPException(404, 'original_not_found')
        return query, FileResponse(images[ordinal - 1]['identity']['path'], filename=names[ordinal - 1]['name'])

    def review(self, actor, project, candidate_id, *, action, expected_revision, authority):
        revision(expected_revision)
        if not isinstance(action, str) or action not in {'confirm', 'discard'}:
            raise SharedProjectError('shared_library_review_invalid')
        writer = SharedWriteRecords(self.registry, actor, project, authority=authority,
            action=action, object_kind='insight', object_id=candidate_id, object_title='认识审核')
        scope = WorkScope('local-user', project)
        with writer.begin() as tx:
            enlisted = TransactionRecords(tx)
            service = recognition_service(enlisted, source_owner=writer)
            candidate = enlisted.read('recognition_candidates', candidate_id)
            if candidate is not None:
                writer.object_title = candidate.payload['content'].splitlines()[0][:80]
            if action == 'confirm':
                result = service.publish(scope=scope, candidate_id=candidate_id,
                    expected_revision=expected_revision, reviewer=actor.user_id)
            else:
                result = service.reject_candidate(scope=scope, candidate_id=candidate_id,
                    expected_revision=expected_revision, reviewer=actor.user_id)
            value = insight_view(enlisted, scope, result.id, service=service, source_owner=writer)
            if value is not None:
                value = self._actors(enlisted, value)
            namespace = shared_read_capability(enlisted, registry=self.registry).namespace
            tx.commit()
        return {'insight': value, 'namespace': namespace}
