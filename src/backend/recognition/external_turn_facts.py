"""Read the original external delivery facts without creating a runtime/store."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3

from .external_evidence_json import encoded

from core.storage_provider import JsonObjectStore
from core.storage_provider.source_retrieval_index import COLLECTION as SOURCE_INDEX, PROJECTION_VERSION

CAPABILITY = 'external.context.execute'
ARCHIVE = 'external-context-handoff-v1'
IDENTITIES = 'external-context-identities-v1'


def source_store(reader):
    path = getattr(reader, 'database_path', None)
    if path is None:
        path = next(row[2] for row in reader.connection.execute('PRAGMA database_list') if row[1] == 'main')
    parent = Path(path).parent
    root = parent if parent.name == '.rebuild-data' else parent / '.rebuild-data'
    # The established legacy Document authority pairs this database with the
    # recognition namespace (storage_authority.resolve_recognition_document_store).
    # Resolve one namespace, never probe alternate stores for an identical ID.
    namespace = 'recognition' if Path(path).name == 'recognition.sqlite3' else 'default'
    return JsonObjectStore(root, namespace_id=namespace,
                           vector_cache_path=parent / 'recognition-vectors.sqlite3')


def completed(ref, archive, request, events, payload, effect):
    """One predicate for the live owner and the retained read-only consumer."""
    turn = archive['turn_id']
    fields = {'schema_version', 'owner_id', 'turn_id', 'request', 'binding', 'selections', 'handoff', 'mapping'}
    extra = {'1.0.0': set(), '2.0.0': {'origin'}, '3.0.0': {'catalog'}, '4.0.0': {'recall'}}
    version = archive.get('schema_version')
    if not isinstance(version, str) or version not in extra or set(archive) != fields | extra[version]:
        raise ValueError('external_context_binding_invalid')
    exact = request.get('capability_request', {})
    if (encoded(request) != encoded(archive['request']) or request.get('turn_id') != turn
            or request.get('desired_outcome') != 'external.context'
            or exact.get('mode') != 'execute_exact_v1' or exact.get('capability_id') != CAPABILITY
            or exact.get('arguments', {}).get('scope', {}).get('user_id') != archive['owner_id']):
        raise ValueError('external_context_binding_invalid')
    outcomes = [event for event in events if event['type'] == 'tool.outcome.recorded'
                and event.get('data', {}).get('capability_id') == CAPABILITY]
    if not events or events[-1]['type'] != 'turn.completed' or len(outcomes) != 1:
        raise ValueError('external_context_not_completed')
    event = outcomes[0]
    outcome_ref, invocation = event['data']['payload_ref'], event['correlation']['tool_call_id']
    outcome, settled = payload(outcome_ref), effect(invocation)
    if (outcome['status'] != 'completed' or outcome['capability_id'] != CAPABILITY
            or outcome['turn_id'] != turn or outcome['payload_ref'] != ref
            or settled is None or settled['state'] != 'SETTLED_OK' or settled['result_ref'] != outcome_ref
            or settled['turn_id'] != turn or type(outcome['attempt']) is not int
            or settled['attempt'] != outcome['attempt'] or outcome['invocation_id'] != invocation):
        raise ValueError('external_context_not_completed')
    matched = {}
    for kind in ('tool.requested', 'tool.intent.recorded', 'tool.dispatch.claimed', 'tool.completed'):
        rows = [value for value in events if value['type'] == kind
                and value.get('data', {}).get('capability_id') == CAPABILITY
                and value.get('correlation', {}).get('tool_call_id') == invocation]
        if len(rows) != 1:
            raise ValueError('external_context_not_completed')
        matched[kind] = rows[0]
    intent_ref = matched['tool.intent.recorded']['data']['payload_ref']
    intent = payload(intent_ref)
    if (intent['turn_id'] != turn or intent['invocation_id'] != invocation
            or intent['capability_id'] != CAPABILITY or encoded(intent['arguments']) != encoded(exact['arguments'])
            or settled.get('intent_ref') != intent_ref
            or matched['tool.dispatch.claimed']['data']['payload_ref'] != intent_ref
            or matched['tool.completed']['data']['payload_ref'] != ref
            or not matched['tool.requested']['sequence'] < matched['tool.intent.recorded']['sequence']
                < matched['tool.dispatch.claimed']['sequence'] < event['sequence']
                < matched['tool.completed']['sequence'] < events[-1]['sequence']
            or any(value['turn_id'] != turn or value['session_id'] != request['session_id']
                or value.get('correlation', {}).get('operation_id') != request['operation_id']
                for value in [*matched.values(), event, events[-1]])):
        raise ValueError('external_context_not_completed')
    return outcome_ref, events[-1]['sequence']


@contextmanager
def _readonly(database):
    if not database.is_file():
        raise ValueError('external_context_not_delivered')
    connection = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        yield connection
    finally:
        connection.close()


def source_is_committed(store, identity, project, revision, incarnation):
    """The original invalidation is an additional veto, never an egress grant."""
    prefix = '$.projections.' + json.dumps(store.namespace_id)
    fields = ('state', 'source_id', 'namespace_id', 'project_id',
              'source_revision', 'incarnation', 'projection_version')
    with _readonly(store.root / 'structured-records.sqlite3') as connection:
        row = connection.execute(
            'SELECT ' + ','.join('json_extract(payload_json,?)' for _ in fields)
            + ',json_type(payload_json,?) FROM crp_structured_records WHERE collection=? AND object_id=?',
            tuple(prefix + '.' + field for field in fields)
            + (prefix + '.source_revision', SOURCE_INDEX, identity),
        ).fetchone()
    return row is not None and tuple(row) == (
        'ready', identity, store.namespace_id, project, revision, incarnation, PROJECTION_VERSION, 'integer')


def delivery_facts(reader, turn, identity, *, identities_kind=IDENTITIES):
    """Fixed original namespace; no alternative database or missing-schema DDL."""
    from .sql_source_identities import KIND, validate_companion
    if identities_kind not in {IDENTITIES, KIND}:
        raise ValueError('external_context_binding_invalid')
    with _readonly(source_store(reader).root / 'ai-turns.sqlite3') as connection:
        def immutable(kind):
            row = connection.execute('SELECT payload_ref,payload_json FROM ai_turn_immutable_payloads WHERE turn_id=? AND kind=?',
                (turn, kind)).fetchone()
            if row is None:
                raise ValueError('external_context_not_delivered')
            return row[0], json.loads(row[1])
        ref, archive = immutable(ARCHIVE)
        identities_ref, identities = immutable(identities_kind)
        row = connection.execute('SELECT request_json FROM ai_turns WHERE turn_id=?', (turn,)).fetchone()
        if row is None or archive.get('turn_id') != turn:
            raise ValueError('external_context_not_delivered')
        request = json.loads(row[0])
        events = [json.loads(row[0]) for row in connection.execute(
            'SELECT event_json FROM ai_turn_events WHERE turn_id=? ORDER BY sequence', (turn,))]
        def payload(identity):
            row = connection.execute('SELECT payload_json FROM ai_turn_payloads WHERE turn_id=? AND payload_ref=?',
                (turn, identity)).fetchone()
            if row is None:
                raise ValueError('external_context_not_completed')
            return json.loads(row[0])
        def effect(identity):
            row = connection.execute('SELECT state,result_ref,turn_id,attempt,intent_ref FROM effect WHERE operation_id=?',
                (identity,)).fetchone()
            return dict(row) if row else None
        outcome, sequence = completed(ref, archive, request, events, payload, effect)
        if identities_kind == KIND:
            validate_companion(identities, owner_id=archive['owner_id'], turn_id=turn,
                immutable_ref=ref, mapping=archive['mapping'])
        arguments = request['capability_request']['arguments']
        # Return facts and typed identity only, never the query or handoff body.
        facts = {'owner_id': archive['owner_id'], 'turn_id': turn, 'client': arguments['client'],
            'scope': arguments['scope'], 'proof': archive['mapping'].get(identity)}
        return ref, facts, identities_ref, identities, outcome, sequence
