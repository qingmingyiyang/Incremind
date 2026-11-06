"""The workbench's explicit user confirmation and dismissal actions."""
from uuid import uuid4
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Query
from starlette.concurrency import run_in_threadpool
from starlette.background import BackgroundTask
from starlette.responses import FileResponse
from .audio_positions import AudioPositions
from .turn_timings import turn_timing
from core.storage_provider.connection_scope import connection_scope, with_connection_scope

from backend.recognition import RecognitionConflict, RecognitionError, WorkScope
from ..workspace_contracts import _json, _now, _project
from .insights import insight_view, resolve_insight
from .layers import summary_of, facts_of, todos_of, is_verified, mark_verified
from .projects import scene_of
from backend.shared.document_visibility import LegacyDocumentVisibility
from ..recall_state import preference
from .recall_preferences import set_preference, restore_document_preference, DocumentRecallUnavailable
from core.storage_provider import SQLiteUnitOfWorkConflict
from core.storage_provider.sqlite_uow import begin_read
from .usage import safe_record_usage, initialize_usage, read_usage_count
from ..transaction_records import TransactionRecords
from .outcome_corrections import record_edit
from core.document_engine import DocumentExpectedRevisionError, SQLiteDocumentRepository
from .request_reads import DocumentReadSet
from .outcomes import hidden_outcome_ids, versions as outcome_versions, candidates as outcome_candidates, conversation_origin
from .task_drafts import TaskDrafts


def _scoped(function, *args):
    # Work deferred past the reply reuses connections like the request did.
    with connection_scope():
        function(*args)


def read_document_revision(documents, *, project, document_id, revision, visibility=None):
    if type(revision) is not int or revision < 1:
        raise HTTPException(400, 'invalid_document_revision')
    document = documents.read(document_id)
    if visibility is None:
        visibility = LegacyDocumentVisibility.from_repository(documents, project_id=project)
    if (document is None or document.get('project_id') != project
            or not visibility.allows(document)):
        raise HTTPException(404, 'library_item_not_found')
    historical = documents.revision(document_id, revision)
    markdown = documents.records.read('document_markdown', f'{document_id}~r{revision}')
    current_revision = document.get('revision')
    if (type(current_revision) is not int or revision > current_revision
            or historical is None or historical.get('document_id') != document_id
            or type(historical.get('revision')) is not int or historical['revision'] != revision
            or markdown is None or markdown.payload.get('document_id') != document_id
            or type(markdown.payload.get('revision')) is not int or markdown.payload['revision'] != revision
            or not isinstance(markdown.payload.get('markdown'), str)):
        raise HTTPException(404, 'document_revision_not_found')
    return {'document_id': document_id, 'title': document['title'], 'revision': revision,
            'current_revision': current_revision, 'markdown': markdown.payload['markdown']}


class LibraryRead:
    """Scoped, read-only projections of existing documents and source bindings."""

    def __init__(self, records, service, documents, workspace, *, remember_observer=None, can_write=True,
            source_namespace=None, logical_scope_kind='personal'):
        if type(can_write) is not bool:
            raise ValueError('library_write_qualification_invalid')
        if source_namespace is not None and (not isinstance(source_namespace, str) or not source_namespace):
            raise ValueError('library_namespace_invalid')
        self.records, self.service = records, service
        self.documents, self.workspace = documents, workspace
        self.remember_observer = remember_observer
        if logical_scope_kind not in {'personal', 'shared'}:
            raise ValueError('library_logical_scope_invalid')
        self.logical_scope_kind = logical_scope_kind
        self.can_write = can_write
        self.namespace = source_namespace or '/'.join(('personal', getattr(records, 'user_id', 'local-user'), documents.namespace_id))
        self.audio_positions = AudioPositions(records, workspace.intake.runtime_root)

    def _scope_visible(self, kind, identity, project):
        from backend.recognition.material_logical_scope import material_scope_visible
        return material_scope_visible(self.records, material_type=kind, identity=identity,
            project_id=project, namespace_id=self.documents.namespace_id,
            logical_scope_kind=self.logical_scope_kind)

    def unfinished_sources(self, project):
        from .jobs import _turn_target
        from .workbench_receipts import ITEM_STATES
        latest = {}
        for row in self.records.list_matching(ITEM_STATES, project_id=project):
            state = row.payload
            if (not isinstance(state.get('turn_id'), str) or not state['turn_id']
                    or not isinstance(state.get('item_id'), str) or not state['item_id']):
                continue
            turn = self.records.read('v2_turns', state.get('turn_id'))
            if (turn is None or turn.object_id != row.object_id
                    or turn.payload.get('project_id') != project
                    or turn.payload.get('intent') != 'remember'
                    or turn.payload.get('thread_id') != state.get('thread_id')
                    or turn.payload.get('item_id') != state.get('item_id')
                    or turn.payload.get('receipt', {}).get('remember', {}).get('item_id') != state.get('item_id')
                    or _turn_target(self.records, project, turn) is None):
                continue
            item = self.records.read('workspace_items', state.get('item_id'))
            if (item is None or item.payload.get('project_id') != project
                    or item.payload.get('status') == 'confirmed'
                    or not self._scope_visible('workspace_item', item.object_id, project)):
                continue
            order = (turn.payload.get('created_at', ''), state.get('updated_at', ''), turn.object_id)
            if item.object_id not in latest or order > latest[item.object_id][0]:
                latest[item.object_id] = (order, item, turn, state)
        result = {}
        for _, item, turn, state in latest.values():
            observed = self.remember_observer(turn) if self.remember_observer is not None else state
            if observed.get('state') in {'failed', 'processing'}:
                result[item.object_id] = {'item': item, 'state': observed['state'],
                    'error': observed.get('error'), 'turn_id': turn.object_id}
        return result

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
                    items[view['id']] = {**view, 'project_id': project, 'namespace': self.namespace,
                        'usage_count': read_usage_count(self.records, 'insight', view['id'], project)}
        return sorted(items.values(), key=lambda r: (not (r.get('pattern') and r['state'] == 'pending'), r['id']))

    def docs(self, project, *, include_archived=False, prepared=None):
        visibility = LegacyDocumentVisibility.from_repository(self.documents, project_id=project,
            logical_scope_kind=self.logical_scope_kind)
        documents = prepared.documents.values() if prepared is not None else self.documents.list(include_archived=include_archived)
        return {doc['id']: doc for doc in documents
                if doc.get('project_id') == project
                and (include_archived or doc.get('status') != 'archived')
                and visibility.allows(doc)}

    def _external_items(self, project, *, identity=None):
        query = self.workspace.query
        if getattr(query, 'origin_admission', None) is not None:
            from core.storage_provider.record_lineage import verify_first_created
            from .material_origin_readers import qualified_record_read
            from .shared_intake import COMMANDS
            if (query.records is not self.records or query.source_namespace != self.namespace
                    or query.origin_admission.lease.project_id != project):
                raise ValueError('library_namespace_invalid')
            query.validate_retrieval_origin()
            fields = {'kind': 'text', 'external_tool': 'remember', 'project_id': project, 'namespace': self.namespace}
            if identity is not None:
                fields['item_id'] = identity
            accepted, items = {}, []
            with qualified_record_read(self.records, validate=query.validate_retrieval_origin) as reader:
                for command in self.records.list_matching(COMMANDS, **fields):
                    if reader.read(COMMANDS, command.object_id) != command:
                        raise ValueError('external_context_binding_invalid')
                    value = command.payload
                    item_id, revision = value['item_id'], value['object_revision']
                    if command.revision != 1 or type(revision) is not int or revision != 1:
                        raise ValueError('external_context_binding_invalid')
                    item = reader.read('workspace_items', item_id)
                    if item is None:
                        continue
                    verify_first_created(reader, value['birth'], collection='workspace_items', object_id=item_id)
                    accepted[item_id] = revision
                    items.append(item)
        else:
            fields = {'owner_id': 'local-user', 'tool': 'remember', 'project_id': project}
            if identity is not None:
                fields['object_id'] = identity
            receipts = self.records.list_matching('v2_external_agent_intakes', **fields)
            accepted = {row.payload['object_id']: row.payload['object_revision'] for row in receipts
                        if row.payload.get('schema_version') == 1}
            items = self.records.read_batch({'workspace_items': tuple(accepted)})['workspace_items']
        return {row.object_id: row for row in items
                if row.payload.get('id') == row.object_id and row.payload.get('project_id') == project
                and type(accepted[row.object_id]) is int and 0 < accepted[row.object_id] <= row.revision
                and self._scope_visible('workspace_item', row.object_id, project)}

    def _conversation_items(self, project, *, identity=None):
        query = self.workspace.query
        if getattr(query, 'origin_admission', None) is not None:
            from .shared_intake import shared_conversation_items
            if (query.records is not self.records or query.source_namespace != self.namespace
                    or query.origin_admission.lease.project_id != project):
                raise ValueError('library_namespace_invalid')
            query.validate_retrieval_origin()
            return shared_conversation_items(self.records, project, self.namespace, identity=identity)
        if self.logical_scope_kind == 'personal' and getattr(self.records, 'user_id', None) is not None:
            from .conversation_shares import conversation_originals
            return conversation_originals(self.records, project, self.documents.namespace_id, identity=identity)
        return {}

    def source_rows(self, project, docs, *, prepared=None, include_unfinished=False):
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
            if doc not in docs or not self._scope_visible('workspace_item', row.object_id, project):
                continue
            rows[row.object_id] = {'id': row.object_id, 'title': item.get('title', row.object_id),
                'kind': item['input_kind'], 'created_at': item.get('created_at'),
                'url': item.get('url'), 'document_id': doc}
        # 外部客户端已接受的原件可直接阅读，整理状态由原有流程维护。
        for row in self._external_items(project).values():
            item = row.payload
            doc = item.get('document_id')
            rows.setdefault(row.object_id, {'id': row.object_id, 'title': item.get('title', row.object_id),
                'kind': item['input_kind'], 'created_at': item.get('created_at'),
                'url': item.get('url'), 'document_id': doc if doc in docs else None})
        for row in self._conversation_items(project).values():
            if not self._scope_visible('workspace_item', row.object_id, project):
                continue
            item = row.payload
            doc = item.get('document_id')
            rows.setdefault(row.object_id, {'id': row.object_id, 'title': item['title'],
                'kind': item['input_kind'], 'created_at': item['created_at'], 'url': None,
                'document_id': doc if doc in docs else None})
        for source in self.workspace.query.source_store.list('sources'):
            identity = source.get('id')
            if (source.get('project_id', 'default') != project
                    or not self._scope_visible('original_source', identity, project)):
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
        if include_unfinished:
            for identity, unfinished in self.unfinished_sources(project).items():
                item = unfinished['item'].payload
                rows[identity] = {'id': identity, 'title': item.get('title', identity),
                    'kind': item['input_kind'], 'created_at': item.get('created_at'),
                    'url': item.get('url'), 'document_id': item.get('document_id') if item.get('document_id') in docs else None,
                    **{key: unfinished[key] for key in ('state', 'error', 'turn_id')}}
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
            return {'items': [r for r in rows if state is None or r['state'] == state], 'counts': counts,
                'namespace': self.namespace}
        prepared = DocumentReadSet.load(self.records, self.documents, project)
        docs = self.docs(project, include_archived=layer in {'notes', 'sources'}, prepared=prepared)
        if layer == 'sources':
            rows = self.source_rows(project, docs, prepared=prepared, include_unfinished=True)
            return {'items': [r for r in rows if (state is None or not r.get('state')) and (scene is None or
                self.scene('item', r['id'], project) == scene or
                any(self.scene('document', d, project) == scene for d in docs
                    if d == r['document_id'] or any(ref.get('source_id') == r['id'] for ref in docs[d].get('source_refs', []))))
                and (not q or q.casefold() in (r['title'] + ' ' + self.source_text(project, r['id'])).casefold())]}
        kinds = {}
        if layer == 'summaries':
            for source in self.source_rows(project, docs, prepared=prepared, include_unfinished=False):
                kinds.setdefault(source['document_id'], set()).add(source['kind'])
        hidden = hidden_outcome_ids(self.records, project)
        freshness = getattr(self.workspace.query, 'outcome_freshness', None)
        from .outcomes import qualified_lineages
        outcomes = qualified_lineages(self.records, project) if freshness is not None and layer == 'notes' else {}
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
                row['archived'] = doc.get('status') == 'archived'
                if identity in outcomes:
                    row.update(freshness.row_projection(project=project, document_id=identity))
            rows.append(row)
        if layer == 'notes':
            counts = {key: sum(('archived' if row['archived'] else 'verified' if row['verified'] else 'unverified') == key
                               for row in rows) for key in ('unverified', 'verified', 'archived')}
            return {'items': rows, 'counts': counts}
        return {'items': rows}

    def source_type(self, project, identity, docs):
        kinds = {r['kind'] for r in self.source_rows(project, docs, include_unfinished=False) if r['document_id'] == identity}
        return next(iter(kinds)) if len(kinds) == 1 else None

    def source_text(self, project, identity):
        source = self.workspace.query.source_store.read('sources', identity)
        if (source and source.get('project_id', 'default') == project
                and self._scope_visible('original_source', identity, project)):
            metadata = source.get('metadata', {})
            content = metadata.get('content_snapshot') or metadata.get('content') or ''
            return content if isinstance(content, str) else ''
        item = self.records.read('workspace_items', identity)
        if (item and item.payload.get('project_id') == project and item.payload.get('status') == 'confirmed'
                and self._scope_visible('workspace_item', identity, project)):
            return item.payload.get('source_text', '')
        archived = self._conversation_items(project, identity=identity)
        if identity in archived and self._scope_visible('workspace_item', identity, project):
            return archived[identity].payload['source_text']
        if identity in self._external_items(project, identity=identity):
            return self.workspace.review.source(identity, project)['text']
        if identity in self.unfinished_sources(project):
            return self.workspace.review.source(identity, project)['text']
        return ''

    def full_source(self, project, identity, *, docs=None, rows=None):
        docs = self.docs(project, include_archived=True) if docs is None else docs
        rows = self.source_rows(project, docs, include_unfinished=True) if rows is None else rows
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

    def verified_document_facts(self, project, document, markdown):
        items = self.records.list_matching('workspace_items', project_id=project,
            status='confirmed', document_id=document['id'])
        if len(items) != 1:
            return None, [], False
        item = items[0].payload
        original = item.get('source_text', '')
        current_facts = facts_of(markdown)
        frozen_facts = item.get('draft', {}).get('facts', [])
        facts = []
        for fact in frozen_facts:
            evidence = fact['evidence']
            start, end, quote = evidence['start'], evidence['end'], evidence['quote']
            if (type(start) is int and type(end) is int and 0 <= start < end <= len(original)
                    and current_facts.count(fact['text']) == 1
                    and sum(value['text'] == fact['text'] for value in frozen_facts) == 1
                    and original[start:end] == quote):
                facts.append({'text': fact['text'], 'evidence': dict(evidence)})
        sources = {ref.get('source_id') for ref in document.get('source_refs', []) if ref.get('source_id')}
        return items[0], facts, sources <= {items[0].object_id, item.get('source_id')}

    @staticmethod
    def original_window(original, evidence, *, seconds=None):
        start, end = evidence['start'], evidence['end']
        return {'pre': original[max(0, start-80):start], 'quote': original[start:end],
                'post': original[end:end+80], 'seconds': seconds}

    def insight_evidence(self, insight, locations, *, known_docs):
        if insight['kind'] != 'candidate' or insight['state'] != 'pending':
            return {}
        from .policies import get
        candidates, documents = [], dict(known_docs)
        for project, identity in sorted(locations):
            if project not in documents:
                documents[project] = self.docs(project, include_archived=True)
            document = documents[project].get(identity)
            if document is None:
                continue
            markdown = self.documents.markdown(identity)
            item, facts, unique_source = self.verified_document_facts(project, document, markdown)
            if item is None or not unique_source:
                continue
            for fact in facts:
                evidence = fact['evidence']
                candidates.append({'document_id': identity, 'title': document['title'],
                    'source_id': item.object_id,
                    'window': self.original_window(item.payload['source_text'], evidence,
                        seconds=self.audio_positions.seconds(item.payload, evidence['start'], evidence['end'])),
                    'text': fact['text'], 'created_at': document.get('created_at'),
                    'start': evidence['start'], 'end': evidence['end']})
        return {'evidence': [{key: row[key] for key in ('document_id', 'title', 'source_id', 'window')}
            for row in get('evidence_pick')(insight['text'], candidates)]}

    def drill(self, project, layer, identity, *, document_id=None, source_id=None):
        result = self._drill(project, layer, identity, document_id=document_id, source_id=source_id)
        insight = result.get('insight')
        actual = resolve_insight(self.records, WorkScope('local-user', project), insight['id']) if insight else None
        result['insight_project_id'] = actual.payload['scope']['project_id'] if actual is not None else None
        result['namespace'] = self.namespace
        result['can_write'] = self.can_write
        result['can_edit_insight'] = bool(self.can_write and actual is not None and actual.object_id == insight['id']
            and insight.get('state') in {'pending', 'active'} and actual.payload.get('state') in {'pending', 'active'})
        if insight is not None:
            result['insight'] = {**insight,
                'usage_count': read_usage_count(self.records, 'insight', insight['id'], result['insight_project_id'])}
        if result.get('note') is not None:
            from .external_task_files import generated_attachments
            document = self.documents.read(result['note']['document_id'])
            if document is None:
                raise HTTPException(404, 'library_item_not_found')
            try:
                result['attachments'] = generated_attachments(self.records, project, document, self.workspace.intake.runtime_root)
            except (ValueError, KeyError, TypeError, IndexError, AttributeError):
                raise HTTPException(404, 'library_item_not_found') from None
        from .citation_refs import used_by
        result['used_by'] = used_by(self.records, project, layer, identity,
            document_ids={row['document_id'] for row in result.get('documents', [])
                if row.get('project_id', project) == project})
        return result

    def _drill(self, project, layer, identity, *, document_id=None, source_id=None):
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
        evidence = {}
        document_ids = set()
        sources = self.source_rows(project, docs, include_unfinished=layer == 'source')
        if layer == 'insight':
            insight = insight_view(self.records, WorkScope('local-user', project), identity, service=self.service)
            if insight and not any(r['id'] == insight['id'] for r in views):
                insight = None
            if insight:
                foreign = insight.get('source_documents', [])
                if foreign:
                    allowed = {(row['project_id'], row['document_id']) for row in foreign}
                    source_documents = {own: self.docs(own, include_archived=True) for own in {own for own, _ in allowed}}
                    allowed = {(own, value) for own, value in allowed if value in source_documents[own]}
                    evidence = self.insight_evidence(insight, allowed, known_docs=source_documents)
                    document_choices = [{'document_id': value, 'project_id': own,
                        'title': source_documents[own][value]['title']} for own, value in sorted(allowed)]
                    matching = sorted((own, identity) for own, identity in allowed
                        if document_id is None or identity == document_id)
                    if document_id is not None and not matching:
                        raise HTTPException(404, 'library_item_not_found')
                    if len(matching) == 1:
                        own, original_document = matching[0]
                        result = self.drill(own, 'note', original_document, source_id=source_id)
                        result.update(insight=insight, grown=[], readonly=True,
                            documents=document_choices, **evidence)
                        result.setdefault('source_project_id', own)
                        return result
                    return {'insight': insight, 'grown': [], 'summary': None, 'note': None, 'source': None,
                        'documents': document_choices,
                        'sources': [], 'readonly': True, **evidence}
                document_ids = set(insight['document_ids']) & docs.keys()
                evidence = self.insight_evidence(insight, {(project, value) for value in document_ids},
                    known_docs={project: docs})
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
                  'summary': None, 'note': None, 'source': None, 'documents': choices, 'sources': [], **evidence}
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
        with begin_read(self.records) as tx:
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
                         'revision': doc['revision'], 'archived': doc.get('status') == 'archived'}
        origin = conversation_origin(self.records, project, doc['id'])
        if origin is not None:
            result['note']['origin'] = origin
            freshness = getattr(self.workspace.query, 'outcome_freshness', None)
            if freshness is not None:
                result['note'].update(freshness.row_projection(project=project, document_id=doc['id']))
        recall = self.records.read('v2_document_recall', doc['id'])
        if recall is not None:
            result['note'].update(recall_state=recall.payload.get('state'),
                                  recall_preference_revision=recall.revision)
        result['note'].update(self.placement_metadata(doc, project))
        from .document_filings import filing_view
        filed = filing_view(self.records, doc['id'], project)
        if filed is not None:
            result['note']['filing'] = filed
        item_row, facts, unique_source = self.verified_document_facts(project, doc, markdown)
        if item_row is None:
            return result
        from .image_read import comment_section_for_document
        section = comment_section_for_document(self.records, self.documents, item_row, revision=doc['revision'])
        if section is not None:
            result['note']['comment_section'] = section
        item = item_row.payload
        original = item.get('source_text', '')
        result['note']['facts'] = facts
        requested_source_matches = selected_source in {item_row.object_id, item.get('source_id')}
        if len(result['note']['facts']) == 1 and unique_source and requested_source_matches:
            evidence = result['note']['facts'][0]['evidence']
            original_view = self.workspace.review.source(item_row.object_id, project)
            result['source'] = {'id': item_row.object_id, 'title': item['title'], 'kind': item['input_kind'],
                'url': item.get('url'), 'window': self.original_window(original, evidence,
                    seconds=self.audio_positions.seconds(item, evidence['start'], evidence['end'])),
                'download_url': original_view.get('original_download_url')}
        return result


def install_library_routes(application, *, records, service, documents=None, workspace=None, models=None):
    router = APIRouter(prefix="/api/v2/library")

    if documents is not None and workspace is not None:
        read = LibraryRead(records, service, documents, workspace,
            remember_observer=getattr(application.state, 'workbench_remember_observer', None))

        @router.post('/inspirations/extract')
        async def extract_inspiration(request: Request):
            from .inspiration_extraction import InspirationExtractionError, extract_inspirations, parse_request
            try:
                project, scene, selected, retry_token = parse_request(await _json(request))
                return await run_in_threadpool(with_connection_scope(extract_inspirations),
                    models, service, documents, project=project, scene=scene, selected=selected,
                    retry_token=retry_token)
            except InspirationExtractionError as error:
                raise HTTPException(error.status, error.detail) from None
            except (RecognitionConflict, SQLiteUnitOfWorkConflict):
                raise HTTPException(409, {'code': 'inspiration_sources_changed'}) from None

        @router.get('/documents/{document_id}')
        def read_document(document_id: str, project_id: str = 'default'):
            project = _project(project_id)
            document = documents.read(document_id)
            visibility = LegacyDocumentVisibility.from_repository(documents, project_id=project)
            if document is None or document.get('project_id') != project or not visibility.allows(document):
                raise HTTPException(404, 'library_item_not_found')
            from .audit_objects import observe_document_read
            observe_document_read(records=records, documents=documents, project_id=project,
                                  document_id=document_id, revision=document['revision'])
            return {**document, 'markdown': documents.markdown(document_id),
                    **({'origin': origin} if (origin := conversation_origin(records, project, document_id)) is not None else {}),
                    'verified': is_verified(records, documents, document_id)}

        @router.get('/notes/{document_id}/revisions/{revision}')
        @with_connection_scope
        def read_revision(document_id: str, revision: int, project_id: str):
            return read_document_revision(documents, project=_project(project_id),
                document_id=_project(document_id), revision=revision)

        @router.patch('/documents/{document_id}')
        async def edit_document(document_id: str, request: Request):
            body = await _json(request)
            if (not isinstance(body, dict) or set(body) not in (
                    {'project_id', 'expected_revision', 'markdown'},
                    {'project_id', 'expected_revision', 'restore_revision'})
                    or type(body.get('expected_revision')) is not int or body['expected_revision'] < 0):
                raise HTTPException(400, 'invalid_document_fields')
            restoring = 'restore_revision' in body
            if (restoring and (type(body['restore_revision']) is not int or body['restore_revision'] < 1)
                    or not restoring and (not isinstance(body['markdown'], str) or not body['markdown'].strip()
                                          or len(body['markdown']) > 40000)):
                raise HTTPException(400, 'invalid_document_fields')
            project = _project(body['project_id'])
            document = documents.read(document_id)
            visibility = LegacyDocumentVisibility.from_repository(documents, project_id=project)
            if document is None or document.get('project_id') != project or not visibility.allows(document):
                raise HTTPException(404, 'library_item_not_found')

            def save():
                with records.begin() as tx:
                    enlisted = SQLiteDocumentRepository(TransactionRecords(tx), namespace_id=documents.namespace_id,
                                                        now=_now())
                    current = enlisted.read(document_id)
                    current_visibility = LegacyDocumentVisibility.from_repository(documents, project_id=project)
                    if (current is None or current.get('project_id') != project
                            or not current_visibility.allows(current)):
                        raise HTTPException(404, 'library_item_not_found')
                    if current.get('status') == 'archived':
                        raise RecognitionConflict('document is archived')
                    if current.get('revision') != body['expected_revision']:
                        raise DocumentExpectedRevisionError('document revision changed')
                    markdown = (read_document_revision(enlisted, project=project, document_id=document_id,
                        revision=body['restore_revision'], visibility=current_visibility)['markdown']
                        if restoring else body['markdown'])
                    saved = enlisted.save_user_edit(document_id, markdown=markdown,
                                                     expected_revision=body['expected_revision'])
                    from .material_angles import refresh_document_angles
                    refresh_document_angles(tx, saved, markdown)
                    record_edit(tx, enlisted, scope=WorkScope('local-user', project), document_id=document_id,
                                from_revision=body['expected_revision'], to_revision=saved['revision'], now=enlisted.now)
                    service.invalidate_document_dependents_in_uow(tx, scope=WorkScope('local-user', project),
                        document_id=document_id, to_revision=saved['revision'])
                    from .audit_objects import record_document_write
                    record_document_write(tx, records=records, documents=documents, project_id=project,
                                          document_id=document_id, revision=saved['revision'], restoring=restoring)
                    tx.commit()
                    return saved
            try:
                saved = await run_in_threadpool(save)
            except (RecognitionConflict, DocumentExpectedRevisionError, SQLiteUnitOfWorkConflict):
                raise HTTPException(409, 'document_revision_conflict') from None
            safe_record_usage(records, 'document', document_id, project, 1.0, reset=True)
            return {**saved, 'markdown': documents.markdown(document_id, revision=saved['revision']),
                    'verified': is_verified(records, documents, document_id)}
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
                    _project(body['target_project_id']), scene=body.get('scene'), expected_revision=body['expected_revision'],
                    retry_token=uuid4().hex)
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

        @router.post('/outcomes/{source_document_id}/restore')
        async def restore_outcome(source_document_id: str, request: Request):
            body = await _json(request)
            if (not isinstance(body, dict) or set(body) != {'project_id', 'source_revision',
                    'expected_latest_id', 'expected_latest_revision'}
                    or any(type(body.get(key)) is not int or body[key] < 1
                           for key in ('source_revision', 'expected_latest_revision'))):
                raise HTTPException(400, 'invalid_outcome_restore_fields')
            project, source = _project(body['project_id']), _project(source_document_id)
            latest = _project(body['expected_latest_id'])

            def restore():
                from .outcomes import restore_outcome_in_uow
                with records.begin() as tx:
                    visibility = LegacyDocumentVisibility.from_repository(documents, project_id=project)
                    for identity in (source, latest):
                        row = tx.read('documents', identity)
                        if (row is None or row.payload.get('project_id') != project
                                or not visibility.allows(row.payload)):
                            raise HTTPException(404, 'library_item_not_found')
                    result = restore_outcome_in_uow(tx, documents, TaskDrafts(records, documents),
                        project=project, source_document_id=source, source_revision=body['source_revision'],
                        expected_latest_id=latest, expected_latest_revision=body['expected_latest_revision'], now=_now())
                    tx.commit()
                    return result
            try:
                return await run_in_threadpool(restore)
            except (RecognitionConflict, DocumentExpectedRevisionError, SQLiteUnitOfWorkConflict):
                raise HTTPException(409, 'document_revision_conflict') from None

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

        @router.get('/sources/{id}/clip')
        async def source_clip(id: str, project_id: str, start: str, end: str):
            project, identity = _project(project_id), _project(id)
            def prepare():
                source = read.full_source(project, identity)
                if source.get('coordinate_space') != 'workspace_source_text_v1':
                    raise HTTPException(404, 'audio_clip_unavailable')
                item = workspace.items.item_for(identity, project)
                return AudioPositions(records, workspace.intake.runtime_root).clip(item.payload, start, end)
            path, cleanup = await run_in_threadpool(with_connection_scope(prepare))
            try:
                return FileResponse(path, media_type='audio/wav',
                                    background=BackgroundTask(cleanup))
            except BaseException:
                cleanup()
                raise

        @router.get('/drill')
        def drill(project_id: str, id: str, from_: str = Query(alias='from'),
                  document_id: str | None = None, source_id: str | None = None):
            if from_ not in {'insight', 'summary', 'note', 'source', 'inspiration'}:
                raise HTTPException(400, 'invalid_library_layer')
            return read.drill(_project(project_id), from_, _project(id),
                document_id=_project(document_id) if document_id is not None else None,
                source_id=_project(source_id) if source_id is not None else None)

    @with_connection_scope
    async def act(insight_id, request, *, confirm, background=None):
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
                    # The model's supports/refutes suggestion takes seconds; reply first, suggest after.
                    later = (lambda *args: background.add_task(_scoped, *args)) if background is not None else None
                    await run_in_threadpool(
                        InsightLinks(records, service, models).discover, scope.project_id, saved.id, later=later)
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
    async def confirm(insight_id: str, request: Request, background: BackgroundTasks):
        return await act(insight_id, request, confirm=True, background=background)

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
        insight_id = _project(insight_id)
        row = resolve_insight(records, scope, insight_id)
        if row is None:
            raise HTTPException(404, 'insight_not_found')
        if row.object_id != insight_id:
            raise HTTPException(409, 'insight_revision_conflict')
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
            from .audit_objects import record_insight_preference
            set_preference(records, scope, row.object_id, recognition_revision=row.revision,
                preference_revision=current['recall_preference_revision'],
                state='forgotten' if body['forgotten'] else 'normal',
                _audit_writer=lambda tx, recognition, updated: record_insight_preference(tx, records=records,
                    scope=scope, recognition=recognition, preference=updated))
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
