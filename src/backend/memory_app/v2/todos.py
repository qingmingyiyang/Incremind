"""Current organized-note to-dos with revision-independent completion state."""
from collections import Counter
from datetime import timedelta
from hashlib import sha1

from fastapi import APIRouter, HTTPException, Request

from ..workspace_contracts import _json, _project
from .layers import todos_of
from .projects import DEFAULT_NAME, display_name
from .library import LibraryRead
from .usage import utc_now, timestamp


COLLECTION = 'v2_todo_state'


class TodoService:
    def __init__(self, records, read, *, now=utc_now):
        self.records, self.read, self.now = records, read, now

    def _current(self, project=None, *, reader=None):
        reader = reader or self.records
        projects = {project} if project is not None else {
            doc.get('project_id') for doc in self.read.documents.list()
            if isinstance(doc.get('project_id'), str)}
        result = []
        for identity in sorted(projects):
            registered = reader.read('v2_projects', identity)
            name = display_name(identity, registered.payload['name']) if registered else {'me':'我', 'inbox':'收件箱', 'default':DEFAULT_NAME}.get(identity, identity)
            for doc in self.read.docs(identity).values():
                recall = reader.read('v2_document_recall', doc['id'])
                if recall and recall.payload.get('state') == 'forgotten':
                    continue
                markdown = self.read.documents.markdown(doc['id'])
                if markdown is None:
                    continue
                occurrences = Counter()
                for raw in todos_of(markdown):
                    text = ' '.join(raw.split())
                    n = occurrences[text]
                    occurrences[text] += 1
                    # Product identity algorithm explicitly specified in T5.2.
                    key = 'td' + sha1((doc['id'] + '\n' + str(n) + '\n' + text).encode('utf-8')).hexdigest()[:24]
                    state = reader.read(COLLECTION, key)
                    result.append(({'id':key, 'document_id':doc['id'], 'project_id':identity,
                        'project_name':name, 'text':text, 'done':state.payload['done'] if state else False,
                        'done_at':state.payload.get('done_at') if state else None,
                        'revision':state.revision if state else None}, doc.get('updated_at') or doc.get('created_at')))
        return result

    def list(self, project=None, *, include_done=False):
        now = self.now()
        rows = [(row, updated) for row, updated in self._current(project)
                if not row['done'] or (include_done and
                    now - timedelta(days=7) <= timestamp(row['done_at'], now - timedelta(days=8)) <= now)]
        # Stable document/text iteration breaks timestamp ties without changing grouping.
        rows.sort(key=lambda pair: timestamp(pair[1], now - timedelta(days=365000)), reverse=True)
        rows.sort(key=lambda pair: pair[0]['done'])
        return {'items':[row for row, _ in rows[:100]]}

    def set_done(self, identity, done, expected_revision):
        with self.records.begin() as tx:
            # The write lock prevents document/publication changes between this
            # current visibility check and the independent sidecar CAS commit.
            current = next((row for row, _ in self._current(reader=tx) if row['id'] == identity), None)
            if current is None:
                raise HTTPException(404, 'todo_not_found')
            if current['done'] == done:
                return current
            if expected_revision != current['revision']:
                raise HTTPException(409, 'todo_revision_conflict')
            payload = {key:current[key] for key in ('document_id', 'project_id', 'text')}
            payload.update(done=done, done_at=self.now().isoformat() if done else None)
            saved = tx.put(COLLECTION, identity, payload,
                           expected_revision=expected_revision if expected_revision is not None else 0)
            tx.commit()
            return {**current, 'done':done, 'done_at':payload['done_at'], 'revision':saved.revision}


def install_todo_routes(application, *, records, service, documents, workspace):
    router = APIRouter(prefix='/api/v2/todos')
    todos = TodoService(records, LibraryRead(records, service, documents, workspace))

    @router.get('')
    def list_todos(project_id: str | None = None, include_done: bool = False):
        return todos.list(_project(project_id) if project_id is not None else None, include_done=include_done)

    async def update(identity, request, done):
        body = await _json(request)
        if set(body) != {'expected_revision'}:
            raise HTTPException(400, 'invalid_todo_fields')
        revision = body['expected_revision']
        if revision is not None and (type(revision) is not int or revision < 1):
            raise HTTPException(400, 'invalid_expected_revision')
        return todos.set_done(identity, done, revision)

    @router.post('/{id}/done')
    async def done(id: str, request: Request):
        return await update(id, request, True)

    @router.post('/{id}/undo')
    async def undo(id: str, request: Request):
        return await update(id, request, False)

    application.include_router(router)
