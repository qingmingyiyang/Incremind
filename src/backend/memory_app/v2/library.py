"""The workbench's explicit user confirmation and dismissal actions."""
from fastapi import APIRouter, HTTPException, Request, Query
from starlette.concurrency import run_in_threadpool
from .turn_timings import turn_timing
from core.storage_provider.connection_scope import with_connection_scope

from backend.recognition import RecognitionConflict, RecognitionError, WorkScope
from ..workspace_contracts import _json, _project
from .insights import insight_view, resolve_insight
from .layers import summary_of, facts_of, todos_of, is_verified, mark_verified
from .projects import scene_of
from backend.shared.document_visibility import LegacyDocumentVisibility
from ..recall_state import preference
from .recall_preferences import set_preference, restore_document_preference, DocumentRecallUnavailable
from core.storage_provider import SQLiteUnitOfWorkConflict
from .usage import safe_record_usage, initialize_usage
from .request_reads import DocumentReadSet
from .outcomes import hidden_outcome_ids, versions as outcome_versions, candidates as outcome_candidates


class LibraryRead:
    """Scoped, read-only projections of existing documents and source bindings."""

    def __init__(self, records, service, documents, workspace):
        self.records, self.service = records, service
        self.documents, self.workspace = documents, workspace

    def insights(self, project):
        scope = WorkScope('local-user', project)
        items = {}
        discovered = self.records.read_batch({'recognition_candidates': None, 'recognitions': None})
        for rows in discovered.values():
            for row in rows:
                view = insight_view(self.records, scope, row.object_id, service=self.service)
                if view:
                    if view['kind'] == 'candidate' and self.records.read('v2_candidate_merges', view['id']):
                        continue
                    if view['state'] == 'active':
                        qualified = self.service.get_recognition(scope=scope, recognition_id=view['id'])
                        if qualified is None or not qualified.authorized:
                            continue
                    items[view['id']] = view
        return sorted(items.values(), key=lambda r: (not (r.get('pattern') and r['state'] == 'pending'), r['id']))

    def docs(self, project, *, include_archived=False, prepared=None):
        visibility = LegacyDocumentVisibility.from_repository(self.documents, project_id=project)
        documents = prepared.documents.values() if prepared is not None else self.documents.list(include_archived=include_archived)
        return {doc['id']: doc for doc in documents
                if doc.get('project_id') == project
                and (include_archived or doc.get('status') != 'archived')
                and visibility.allows(doc)}

    def source_rows(self, project, docs, *, prepared=None):
        rows = {}
        linked = {}
        for doc in docs.values():
            for ref in doc.get('source_refs', []):
                linked.setdefault(ref.get('source_id'), set()).add(doc['id'])
        # Recall excludes archived material; the library retains its originals.
        visible = {e['id'] for e in self.workspace.query.query_entries(project, prepared=prepared) if e['kind'] == 'source'}
        archived_sources = {ref.get('source_id') for doc in docs.values()
                            if doc.get('status') == 'archived' for ref in doc.get('source_refs', [])}
        items = prepared.items.values() if prepared is not None else self.records.list_matching('workspace_items', project_id=project, status='confirmed')
        for row in items:
            item = row.payload
            doc = item.get('document_id')
            if doc not in docs:
                continue
            rows[row.object_id] = {'id': row.object_id, 'title': item.get('title', row.object_id),
                'kind': item['input_kind'], 'created_at': item.get('created_at'),
                'url': item.get('url'), 'document_id': doc}
        for source in self.workspace.query.source_store.list('sources'):
            identity = source.get('id')
            if source.get('project_id', 'default') != project:
                continue
            ids = linked.get(identity, set())
            if identity not in visible:
                metadata = source.get('metadata')
                content = (metadata.get('content_snapshot') or metadata.get('content') or '') if isinstance(metadata, dict) else ''
                if (identity not in archived_sources or not isinstance(content, str) or not content.strip()
                        or source.get('identity_method') == 'workspace_confirmation'):
                    continue
            rows[identity] = {'id': identity, 'title': source.get('title') or identity,
                'kind': source.get('type', 'other'), 'created_at': source.get('created_at'),
                'url': source.get('original_url'), 'document_id': next(iter(ids)) if len(ids) == 1 else None}
        return [rows[key] for key in sorted(rows)]

    def scene(self, kind, identity, project):
        assignment = scene_of(self.records, kind, identity)
        return assignment['scene'] if assignment and assignment.get('project_id') == project else None

    def placement_metadata(self, document, project):
        from .policies import version
        hint = self.records.read('v2_place_hints', document['id'])
        if version('place') == '@1' and hint is None:
            return {}
        assignment = self.records.read('v2_scene_assignments_document', document['id'])
        result = {'scene': assignment.payload['scene'] if assignment else None,
                  'assignment_revision': assignment.revision if assignment else 0}
        if (hint is not None and hint.payload.get('source_project_id') == project
                and hint.payload['document_revision'] == document['revision']):
            result['placement'] = {**hint.payload, **result, 'current_scene': result['scene']}
        return result

    def list(self, layer, project, scene, q, state=None):
        if layer == 'insights':
            rows = self.insights(project)
            rows = [r for r in rows if (scene is None or r['scene'] == scene)
                    and q.casefold() in r['text'].casefold()]
            counts = {s: sum(r['state'] == s for r in rows) for s in ('pending', 'active', 'stale', 'forgotten')}
            return {'items': [r for r in rows if state is None or r['state'] == state], 'counts': counts}
        prepared = DocumentReadSet.load(self.records, self.documents, project)
        docs = self.docs(project, include_archived=layer in {'notes', 'sources'}, prepared=prepared)
        if layer == 'sources':
            rows = self.source_rows(project, docs, prepared=prepared)
            return {'items': [r for r in rows if (scene is None or
                self.scene('item', r['id'], project) == scene or
                any(self.scene('document', d, project) == scene for d in docs
                    if d == r['document_id'] or any(ref.get('source_id') == r['id'] for ref in docs[d].get('source_refs', []))))
                and q.casefold() in (r['title'] + ' ' + self.source_text(project, r['id'])).casefold()]}
        kinds = {}
        if layer == 'summaries':
            for source in self.source_rows(project, docs, prepared=prepared):
                kinds.setdefault(source['document_id'], set()).add(source['kind'])
        hidden = hidden_outcome_ids(self.records, project)
        rows = []
        for identity, doc in sorted(docs.items()):
            if identity in hidden:
                continue
            markdown = prepared.markdown[identity]
            if markdown is None or (scene is not None and self.scene('document', identity, project) != scene):
                continue
            summary = summary_of(markdown)[0]
            text = summary if layer == 'summaries' else markdown
            if q.casefold() not in (doc.get('title', '') + ' ' + text).casefold():
                continue
            row = {'document_id': identity, 'title': doc['title'], 'created_at': doc.get('created_at'),
                   'verified': is_verified(self.records, self.documents, identity)}
            recall = self.records.read('v2_document_recall', identity)
            row['recall_state'] = recall.payload['state'] if recall else 'normal'
            row.update(self.placement_metadata(doc, project))
            from .document_filings import filing_view
            filed = filing_view(self.records, identity, project)
            if filed is not None:
                row['filing'] = filed
            if layer == 'summaries':
                types = kinds.get(identity, set())
                row.update(summary=summary, source_type=next(iter(types)) if len(types) == 1 else None)
            else:
                row['revision'] = doc['revision']
            rows.append(row)
        return {'items': rows}

    def source_type(self, project, identity, docs):
        kinds = {r['kind'] for r in self.source_rows(project, docs) if r['document_id'] == identity}
        return next(iter(kinds)) if len(kinds) == 1 else None

    def source_text(self, project, identity):
        source = self.workspace.query.source_store.read('sources', identity)
        if source and source.get('project_id', 'default') == project:
            metadata = source.get('metadata', {})
            content = metadata.get('content_snapshot') or metadata.get('content') or ''
            return content if isinstance(content, str) else ''
        item = self.records.read('workspace_items', identity)
        if item and item.payload.get('project_id') == project and item.payload.get('status') == 'confirmed':
            return item.payload.get('source_text', '')
        return ''

    def full_source(self, project, identity, *, docs=None, rows=None):
        docs = self.docs(project, include_archived=True) if docs is None else docs
        rows = self.source_rows(project, docs) if rows is None else rows
        row = next((r for r in rows if r['id'] == identity), None)
        if row is None:
            raise HTTPException(404, 'library_item_not_found')
        source = self.workspace.query.source_store.read('sources', identity)
        download_url = None
        if source and source.get('project_id', 'default') == project:
            coordinates = 'source_content_v1'
        else:
            coordinates = 'workspace_source_text_v1'
            original = self.workspace.review.source(identity, project)
            download_url = original.get('original_download_url')
        document_ids = sorted(d for d, doc in docs.items() if d == row['document_id'] or
            any(ref.get('source_id') == identity for ref in doc.get('source_refs', [])))
        return {'id': identity, 'title': row['title'], 'kind': row['kind'],
                'coordinate_space': coordinates, 'text': self.source_text(project, identity),
                'url': row.get('url'), 'download_url': download_url, 'document_ids': document_ids}

    def drill(self, project, layer, identity, *, document_id=None, source_id=None):
        if layer == 'inspiration':
            from .inspirations import resolve_inspiration, COORDINATES
            if document_id is not None or source_id is not None:
                raise HTTPException(404, 'library_item_not_found')
            try:
                bound = resolve_inspiration(self.records, project, identity, recall=False)
            except RecognitionError:
                raise HTTPException(404, 'library_item_not_found') from None
            row = bound['row']
            return {'insight': None, 'grown': [], 'summary': None, 'note': None, 'source': None,
                'documents': [], 'sources': [], 'readonly': True,
                'source_project_id': bound['scope'].project_id,
                'inspiration': {'id': row.object_id, 'title': '你的灵感', 'text': row.payload['content'],
                                'revision': row.revision, 'coordinate_space': COORDINATES}}
        docs = self.docs(project, include_archived=layer in {'note', 'source'})
        views = self.insights(project)
        insight = None
        document_ids = set()
        sources = self.source_rows(project, docs)
        if layer == 'insight':
            insight = insight_view(self.records, WorkScope('local-user', project), identity, service=self.service)
            if insight and not any(r['id'] == insight['id'] for r in views):
                insight = None
            if insight:
                foreign = insight.get('source_documents', [])
                if foreign:
                    allowed = {(row['project_id'], row['document_id']) for row in foreign}
                    matching = sorted((own, identity) for own, identity in allowed
                        if document_id is None or identity == document_id)
                    if document_id is not None and not matching:
                        raise HTTPException(404, 'library_item_not_found')
                    if len(matching) == 1:
                        own, original_document = matching[0]
                        result = self.drill(own, 'note', original_document, source_id=source_id)
                        result.update(insight=insight, grown=[], readonly=True)
                        result.setdefault('source_project_id', own)
                        return result
                    return {'insight': insight, 'grown': [], 'summary': None, 'note': None, 'source': None,
                        'documents': [{'document_id': identity, 'project_id': own,
                            'title': self.documents.read(identity)['title']} for own, identity in matching], 'sources': [], 'readonly': True}
                document_ids = set(insight['document_ids']) & docs.keys()
        elif layer in {'summary', 'note'} and identity in docs:
            document_ids = {identity}
        elif layer == 'source':
            source = next((r for r in sources if r['id'] == identity), None)
            if source:
                document_ids = {d for d in docs if d == source['document_id'] or
                    any(r.get('source_id') == identity for r in docs[d].get('source_refs', []))}
        else:
            raise HTTPException(404, 'library_item_not_found')
        if layer == 'insight' and insight is None or layer == 'source' and not any(r['id'] == identity for r in sources):
            raise HTTPException(404, 'library_item_not_found')
        choices = sorted(({'document_id': d, 'title': docs[d]['title']} for d in document_ids),
                         key=lambda row: (row['title'], row['document_id']))
        result = {'insight': insight, 'grown': [r for r in views if set(r['document_ids']) & document_ids],
                  'summary': None, 'note': None, 'source': None, 'documents': choices, 'sources': []}
        if document_id is not None and document_id not in document_ids:
            raise HTTPException(404, 'library_item_not_found')
        if layer == 'source' and source_id is not None and source_id != identity:
            raise HTTPException(404, 'library_item_not_found')
        selected_document = document_id or (next(iter(document_ids)) if len(document_ids) == 1 else None)
        if selected_document is None:
            if not document_ids and layer == 'source':
                original = self.full_source(project, identity, docs=docs, rows=sources)
                result['sources'] = [{k: original[k] for k in ('id', 'title', 'kind')}]
                result['source'] = {k: original[k] for k in ('id', 'title', 'kind', 'url', 'download_url')}
                result['source']['window'] = None
            return result
        doc = docs[selected_document]
        source_project = project
        from .filing_sources import filing_experience
        source_document = doc['id']
        with self.records.begin() as tx:
            copied = filing_experience(tx, WorkScope('local-user', project), doc['id'])
            if copied is not None:
                visited = set()
                while (marker := tx.read('v2_document_filings', source_document)) is not None:
                    visit = (source_project, source_document)
                    if visit in visited or len(visited) >= 256:
                        raise RecognitionConflict('document filing source is unavailable or changed')
                    visited.add(visit)
                    source_project = marker.payload['source_project_id']
                    source_document = marker.payload['source_document_id']
        source_docs, source_rows = docs, sources
        if copied is not None:
            source_docs = self.docs(source_project, include_archived=True)
            if source_document not in source_docs:
                raise HTTPException(404, 'library_item_not_found')
            source_docs = {source_document: source_docs[source_document]}
            source_rows = self.source_rows(source_project, source_docs)
            result.update(source_project_id=source_project, source_readonly=True)
        source_doc = source_docs[source_document]
        source_choices = [r for r in source_rows if r['document_id'] == source_document or
                          any(ref.get('source_id') == r['id'] for ref in source_doc.get('source_refs', []))]
        result['sources'] = [{k: row[k] for k in ('id', 'title', 'kind')} for row in source_choices]
        selected_source = identity if layer == 'source' else source_id
        if selected_source is not None and not any(r['id'] == selected_source for r in source_choices):
            raise HTTPException(404, 'library_item_not_found')
        if selected_source is None and len(source_choices) == 1:
            selected_source = source_choices[0]['id']
        if selected_source is not None:
            original = self.full_source(source_project, selected_source, docs=source_docs, rows=source_rows)
            result['source'] = {k: original[k] for k in ('id', 'title', 'kind', 'url', 'download_url')}
            result['source']['window'] = None
        markdown = self.documents.markdown(doc['id'])
        if doc.get('status') != 'archived':
            result['summary'] = {'document_id': doc['id'], 'title': doc['title'], 'text': summary_of(markdown)[0],
                                 'source_type': self.source_type(project, doc['id'], docs), 'created_at': doc.get('created_at')}
        # The Markdown preserves all facts; only genuine frozen evidence may be projected.
        result['note'] = {'document_id': doc['id'], 'title': doc['title'], 'markdown': markdown,
                         'facts': [], 'todos': todos_of(markdown), 'verified': is_verified(self.records, self.documents, doc['id']),
                         'revision': doc['revision']}
        recall = self.records.read('v2_document_recall', doc['id'])
        if recall is not None:
            result['note'].update(recall_state=recall.payload.get('state'),
                                  recall_preference_revision=recall.revision)
        result['note'].update(self.placement_metadata(doc, project))
        from .document_filings import filing_view
        filed = filing_view(self.records, doc['id'], project)
        if filed is not None:
            result['note']['filing'] = filed
        items = [r for r in self.records.list_matching('workspace_items', project_id=project, status='confirmed', document_id=doc['id'])]
        if len(items) != 1:
            return result
        from .image_read import comment_section_for_document
        section = comment_section_for_document(self.records, self.documents, items[0], revision=doc['revision'])
        if section is not None:
            result['note']['comment_section'] = section
        item = items[0].payload
        original = item.get('source_text', '')
        current_facts = facts_of(markdown)
        frozen_facts = item.get('draft', {}).get('facts', [])
        for fact in frozen_facts:
            evidence = fact['evidence']
            start, end, quote = evidence['start'], evidence['end'], evidence['quote']
            if (type(start) is int and type(end) is int and 0 <= start < end <= len(original)
                    and current_facts.count(fact['text']) == 1
                    and sum(f['text'] == fact['text'] for f in frozen_facts) == 1
                    and original[start:end] == quote):
                result['note']['facts'].append({'text': fact['text'], 'evidence': dict(evidence)})
        source_ids = {ref.get('source_id') for ref in doc.get('source_refs', []) if ref.get('source_id')}
        unique_source = source_ids <= {items[0].object_id, item.get('source_id')}
        requested_source_matches = selected_source in {items[0].object_id, item.get('source_id')}
        if len(result['note']['facts']) == 1 and unique_source and requested_source_matches:
            evidence = result['note']['facts'][0]['evidence']
            start, end = evidence['start'], evidence['end']
            original_view = self.workspace.review.source(items[0].object_id, project)
            result['source'] = {'id': items[0].object_id, 'title': item['title'], 'kind': item['input_kind'],
                'url': item.get('url'), 'window': {'pre': original[max(0, start-80):start],
                'quote': original[start:end], 'post': original[end:end+80]},
                'download_url': original_view.get('original_download_url')}
        return result


def install_library_routes(application, *, records, service, documents=None, workspace=None, models=None):
    router = APIRouter(prefix="/api/v2/library")

    if documents is not None and workspace is not None:
        read = LibraryRead(records, service, documents, workspace)
        from .inbox import install_inbox_routes
        install_inbox_routes(router, records=records, service=service, read=read)
        from .document_filings import DocumentFilings
        from .placement import PlacementSuggestions
        from .overviews import ScopeOverviews
        filings = DocumentFilings(records, documents, service, models)
        placement = PlacementSuggestions(records, documents, service, ScopeOverviews(records, documents, models))

        @router.get('/inbox/project-suggestions')
        def project_suggestions():
            return {'items': placement.inbox_projects(read.insights('inbox'))}

        @router.post('/notes/{document_id}/file')
        async def file_document(document_id: str, request: Request):
            body = await _json(request)
            if (set(body).difference({'project_id', 'target_project_id', 'scene', 'expected_revision'})
                    or not {'project_id', 'target_project_id', 'expected_revision'} <= set(body)):
                raise HTTPException(400, 'invalid_document_filing_fields')
            try:
                return await run_in_threadpool(filings.move, _project(document_id), _project(body['project_id']),
                    _project(body['target_project_id']), scene=body.get('scene'), expected_revision=body['expected_revision'])
            except RecognitionConflict:
                raise HTTPException(409, 'document_filing_conflict') from None
            except RecognitionError:
                raise HTTPException(400, 'document_filing_invalid') from None

        @router.post('/notes/{document_id}/unfile')
        async def unfile_document(document_id: str, request: Request):
            body = await _json(request)
            if set(body) != {'project_id', 'expected_revision'}:
                raise HTTPException(400, 'invalid_document_filing_fields')
            try:
                return await run_in_threadpool(filings.undo, _project(document_id), _project(body['project_id']),
                    expected_revision=body['expected_revision'])
            except RecognitionError:
                raise HTTPException(409, 'document_filing_conflict') from None

        @router.patch('/notes/{document_id}/scene')
        async def correct_document_scene(document_id: str, request: Request):
            body = await _json(request)
            if set(body) != {'project_id', 'scene', 'expected_revision', 'assignment_revision'}:
                raise HTTPException(400, 'invalid_document_scene_fields')
            try:
                return await run_in_threadpool(placement.correct_scene, _project(document_id),
                    _project(body['project_id']), body['scene'], expected_revision=body['expected_revision'],
                    assignment_revision=body['assignment_revision'])
            except RecognitionError:
                raise HTTPException(409, 'document_scene_conflict') from None

        @router.get('/insights')
        @with_connection_scope
        def insights(project_id: str, scene: str | None = None, state: str | None = None, q: str = ''):
            if state not in {None, 'pending', 'active', 'stale', 'forgotten'}:
                raise HTTPException(400, 'invalid_insight_state')
            with turn_timing(records, 'library'):
                return read.list('insights', _project(project_id), scene, q, state)

        @router.get('/summaries')
        @with_connection_scope
        def summaries(project_id: str, scene: str | None = None, q: str = ''):
            with turn_timing(records, 'library'):
                return read.list('summaries', _project(project_id), scene, q)

        @router.get('/notes')
        @with_connection_scope
        def notes(project_id: str, scene: str | None = None, q: str = ''):
            with turn_timing(records, 'library'):
                return read.list('notes', _project(project_id), scene, q)

        @router.get('/outcomes')
        @with_connection_scope
        def outcomes(project_id: str):
            return {'items': [{key: item[key] for key in ('document_id', 'title', 'version')}
                for item in outcome_candidates(records, project=_project(project_id))]}

        @router.get('/outcomes/{document_id}/versions')
        @with_connection_scope
        def versions(document_id: str, project_id: str):
            try:
                return outcome_versions(records, project=_project(project_id), document_id=_project(document_id),
                                        documents=documents)
            except RecognitionConflict:
                raise HTTPException(404, 'library_item_not_found') from None

        @router.get('/sources')
        @with_connection_scope
        def sources(project_id: str, scene: str | None = None, q: str = ''):
            with turn_timing(records, 'library'):
                return read.list('sources', _project(project_id), scene, q)

        from ..source_egress import SourceEgressService
        from ..original_sources import resolve
        authority = SourceEgressService(records)

        def privacy_binding(project, identity):
            read.full_source(project,identity)
            with records.begin() as reader:
                return resolve(reader,WorkScope('local-user',project),identity)

        @router.get('/sources/{id}/privacy')
        def source_privacy(id: str, project_id: str):
            project,identity = _project(project_id),_project(id)
            try:
                kind,identity = privacy_binding(project,identity)
                return authority.policy(WorkScope('local-user',project),kind,identity)
            except RecognitionError:
                raise HTTPException(409,'source_privacy_conflicted') from None

        @router.put('/sources/{id}/privacy')
        async def update_source_privacy(id: str, request: Request):
            body = await _json(request)
            if set(body) != {'project_id','source_revision','policy_revision','allowed_purposes'}:
                raise HTTPException(400,'invalid_source_privacy_fields')
            project,identity = _project(body['project_id']),_project(id)
            try:
                kind,identity = privacy_binding(project,identity)
                return authority.set_policy(WorkScope('local-user',project),kind,identity,
                    body['source_revision'],body['policy_revision'],body['allowed_purposes'])
            except RecognitionError:
                raise HTTPException(409,'source_privacy_conflicted') from None

        @router.get('/sources/{id}/text')
        def source_text(id: str, project_id: str):
            return read.full_source(_project(project_id), _project(id))

        @router.get('/drill')
        def drill(project_id: str, id: str, from_: str = Query(alias='from'),
                  document_id: str | None = None, source_id: str | None = None):
            if from_ not in {'insight', 'summary', 'note', 'source', 'inspiration'}:
                raise HTTPException(400, 'invalid_library_layer')
            return read.drill(_project(project_id), from_, _project(id),
                document_id=_project(document_id) if document_id is not None else None,
                source_id=_project(source_id) if source_id is not None else None)

    async def act(insight_id, request, *, confirm):
        insight_id = _project(insight_id)
        body = await _json(request)
        if set(body) != {"project_id", "expected_revision"}:
            raise HTTPException(400, "invalid_insight_fields")
        scope = WorkScope("local-user", _project(body["project_id"]))
        revision = body["expected_revision"]
        if type(revision) is not int or revision < 1:
            raise HTTPException(400, "invalid_expected_revision")
        row = resolve_insight(records, scope, insight_id)
        if row is None:
            raise HTTPException(404, "insight_not_found")
        candidate = records.read("recognition_candidates", row.object_id)
        if candidate is None or candidate.payload.get("state") != "pending":
            raise HTTPException(409, "insight_not_pending")
        try:
            if confirm:
                saved = service.publish(scope=scope, candidate_id=row.object_id,
                    expected_revision=revision, reviewer="local-user")
                initialize_usage(records, 'insight', saved.id, scope.project_id)
                from .stats import record_activity
                record_activity(records, 'confirm', scope.project_id, saved.id,
                                event_id='confirm-' + saved.id)
                from .links import InsightLinks
                try:
                    await run_in_threadpool(
                        InsightLinks(records, service).propose_candidate_hint,
                        scope.project_id, row.object_id, saved.id)
                    await run_in_threadpool(
                        InsightLinks(records, service, models).discover, scope.project_id, saved.id)
                except Exception:
                    # Publication stays committed; derived connections can be recomputed.
                    import logging
                    logging.getLogger(__name__).warning('link_discovery_failed')
                return insight_view(records, scope, saved.id, service=service)
            saved = service.reject_candidate(scope=scope, candidate_id=row.object_id,
                expected_revision=revision, reviewer="local-user")
            return {"id": saved.id, "revision": saved.revision}
        except RecognitionConflict:
            raise HTTPException(409, "insight_revision_conflict") from None
        except RecognitionError:
            raise HTTPException(400, "insight_invalid") from None

    @router.post("/insights/{insight_id}/confirm")
    async def confirm(insight_id: str, request: Request):
        return await act(insight_id, request, confirm=True)

    @router.post("/insights/{insight_id}/drop")
    async def drop(insight_id: str, request: Request):
        return await act(insight_id, request, confirm=False)

    @router.patch('/insights/{insight_id}')
    async def edit(insight_id: str, request: Request):
        body = await _json(request)
        if set(body) != {'project_id', 'text', 'conditions', 'expected_revision'}:
            raise HTTPException(400, 'invalid_insight_fields')
        scope = WorkScope('local-user', _project(body['project_id']))
        revision = body['expected_revision']
        if type(revision) is not int or revision < 1:
            raise HTTPException(400, 'invalid_expected_revision')
        row = resolve_insight(records, scope, _project(insight_id))
        if row is None:
            raise HTTPException(404, 'insight_not_found')
        try:
            if records.read('recognition_candidates', row.object_id) is not None:
                saved = service.edit_candidate(scope=scope, candidate_id=row.object_id,
                    expected_revision=revision, content=body['text'], conditions=body['conditions'])
            else:
                saved = service.revise(scope=scope, recognition_id=row.object_id,
                    expected_revision=revision, content=body['text'], conditions=body['conditions'])
                safe_record_usage(records, 'insight', saved.id, scope.project_id, 1.0, reset=True)
            return insight_view(records, scope, saved.id, service=service)
        except (RecognitionConflict, SQLiteUnitOfWorkConflict):
            raise HTTPException(409, 'insight_revision_conflict') from None
        except RecognitionError:
            raise HTTPException(400, 'insight_invalid') from None

    @router.post('/insights/{insight_id}/forget')
    async def forget(insight_id: str, request: Request):
        body = await _json(request)
        if set(body) != {'project_id', 'forgotten'} or type(body['forgotten']) is not bool:
            raise HTTPException(400, 'invalid_forget_fields')
        scope = WorkScope('local-user', _project(body['project_id']))
        row = resolve_insight(records, scope, _project(insight_id))
        if row is None:
            raise HTTPException(404, 'insight_not_found')
        if records.read('recognitions', row.object_id) is None:
            if row.payload.get('state') != 'pending' or body['forgotten']:
                raise HTTPException(409, 'insight_not_active')
            try:
                with records.begin() as tx:
                    if resolve_insight(tx, scope, row.object_id) != row:
                        raise HTTPException(409, 'insight_revision_conflict')
                    faded = tx.read('v2_candidate_fade', row.object_id)
                    if faded:
                        tx.delete('v2_candidate_fade', row.object_id, expected_revision=faded.revision)
                    tx.commit()
                return insight_view(records, scope, row.object_id, service=service)
            except SQLiteUnitOfWorkConflict:
                raise HTTPException(409, 'insight_revision_conflict') from None
        try:
            current = preference(records, scope, row.object_id)
            set_preference(records, scope, row.object_id, recognition_revision=row.revision,
                preference_revision=current['recall_preference_revision'],
                state='forgotten' if body['forgotten'] else 'normal')
            return insight_view(records, scope, row.object_id, service=service)
        except (RecognitionConflict, SQLiteUnitOfWorkConflict):
            raise HTTPException(409, 'insight_revision_conflict') from None
        except RecognitionError:
            raise HTTPException(400, 'insight_invalid') from None

    if documents is not None:
        @router.post('/notes/{document_id}/restore-recall')
        async def restore_recall(document_id: str, request: Request):
            body = await _json(request)
            if set(body) != {'project_id', 'document_revision', 'preference_revision'}:
                raise HTTPException(400, 'invalid_restore_recall_fields')
            try:
                return restore_document_preference(records, documents, _project(body['project_id']),
                    _project(document_id), document_revision=body['document_revision'],
                    preference_revision=body['preference_revision'])
            except DocumentRecallUnavailable:
                raise HTTPException(404, 'document_not_found') from None
            except (RecognitionConflict, SQLiteUnitOfWorkConflict):
                raise HTTPException(409, 'document_recall_conflict') from None
            except RecognitionError:
                raise HTTPException(400, 'invalid_document_recall_revision') from None

        @router.post('/notes/{document_id}/verify')
        async def verify(document_id: str, request: Request):
            body = await _json(request)
            if set(body) != {'project_id', 'document_revision'}:
                raise HTTPException(400, 'invalid_verify_fields')
            revision = body['document_revision']
            if type(revision) is not int or revision < 1:
                raise HTTPException(400, 'invalid_document_revision')
            doc = documents.read(_project(document_id))
            if doc is None or doc.get('project_id') != _project(body['project_id']):
                raise HTTPException(404, 'document_not_found')
            if doc['revision'] != revision or doc.get('status') == 'archived':
                raise HTTPException(409, 'document_revision_conflict')
            try:
                mark_verified(records, document_id, revision, expected_current_revision=revision)
            except (ValueError, SQLiteUnitOfWorkConflict):
                raise HTTPException(409, 'document_revision_conflict') from None
            return {'document_id': document_id, 'verified': True}

    application.include_router(router)
