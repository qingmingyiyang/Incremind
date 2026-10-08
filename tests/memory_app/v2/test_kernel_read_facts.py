"""Kernel authority facts are observed through the fixed existing database."""
from contextlib import contextmanager
import json
import sqlite3
from types import SimpleNamespace

import pytest

from backend.memory_app.original_sources import source_store
from backend.memory_app.research_packets import authority_stores
from backend.recognition import RecognitionConflict
from core.ai_kernel import SQLiteAITurnStore, SQLiteAgentStore
from core.storage_provider import SQLiteStructuredRecordStore
from tests.memory_app.v2.research_fixture import research_request
from tests.memory_app.v2.test_workbench_do_agents import Organization
from tests.rebuild.test_ai_agent_store import _main, _spawn


OPERATION_QUERY = 'SELECT kind,request_json,result_json FROM ai_agent_operations WHERE operation_id=?'
SCHEMA_COLUMNS = (
    ('ai_turns', 'request_json'),
    ('ai_turn_events', 'event_json'),
    ('ai_turn_payloads', 'payload_json'),
    ('ai_turn_immutable_payloads', 'kind'),
    ('ai_agent_runs', 'parent_run_id'),
    ('ai_agent_operations', 'result_json'),
)


@pytest.fixture
def facts(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    path = source_store(records).root / 'ai-turns.sqlite3'
    turns = SQLiteAITurnStore(path)
    agents = SQLiteAgentStore(path)
    request = research_request('facts', 'project-a', 'Synthetic research')
    turns.claim_turn(request)
    accepted = Organization.event(request, 'turn.accepted', 1)
    turns.append(accepted, expected_sequence=0)
    event = Organization.event(request, 'turn.completed', 2)
    turns.append(event, expected_sequence=1)
    normal = turns.put(request['turn_id'], 'synthetic-result', {'result': ['synthetic']})
    immutable = turns.get_or_create_immutable_payload(request['turn_id'], 'synthetic-snapshot', {'snapshot': 1})
    main = _main(request['turn_id'])
    agents.register_run(main, operation_id='register-main')
    child, link, reservation = _spawn()
    agents.reserve_spawn(parent=main, child=child, link=link, reservation=reservation)
    return SimpleNamespace(records=records, path=path, request=request, event=event, accepted=accepted,
        normal=normal, immutable=immutable, main=main, child=child, turns=turns, agents=agents)


@contextmanager
def observe_connections(path, monkeypatch):
    """Wrap the real connector for observation; return its actual connections."""
    observed = SimpleNamespace(uris=[], statements=[], flags=[], write_errors=[])
    target = str(path.resolve()).replace('\\', '/').lower()
    connect = sqlite3.connect

    def observe(database, *args, **kwargs):
        connection = connect(database, *args, **kwargs)
        locator = str(database).replace('\\', '/').lower()
        if target in locator:
            observed.uris.append(locator)
            checked = False

            def trace(statement):
                nonlocal checked
                observed.statements.append(statement)
                if statement.lstrip().upper().startswith('SELECT') and not checked:
                    checked = True
                    observed.flags.append((connection.execute('PRAGMA query_only').fetchone()[0],
                        connection.in_transaction))
                    try:
                        connection.execute('UPDATE ai_turns SET request_json=request_json WHERE 0')
                    except sqlite3.OperationalError as error:
                        observed.write_errors.append(str(error))
            connection.set_trace_callback(trace)
        return connection

    with monkeypatch.context() as observer:
        observer.setattr(sqlite3, 'connect', observe)
        yield observed


def read_all(env, turns, agents):
    assert turns.get_request(env.request['turn_id']) == env.request
    assert turns.events_after(env.request['turn_id'], after_sequence=0) == (env.accepted, env.event)
    assert turns.events_after(env.request['turn_id'], after_sequence=1) == (env.event,)
    assert turns.events_after(env.request['turn_id'], 1) == (env.event,)
    assert turns.events_after(env.request['turn_id'], after_sequence=2) == ()
    assert turns.get(env.normal) == {'result': ['synthetic']}
    assert turns.get(env.immutable) == {'snapshot': 1}
    assert turns.immutable_payload_reference(env.request['turn_id'], 'synthetic-snapshot') == env.immutable
    assert agents.get_run(env.main.run_id) == env.main
    assert agents.get_run_by_turn_id(env.request['turn_id'], project_id='project-a') == (env.main, 1)
    assert agents.get_run_by_turn_id(env.request['turn_id'], project_id='another-project') is None
    assert agents.list_runs(project_id='project-a') == (env.child, env.main)
    assert agents.list_runs(project_id='project-a', parent_run_id=env.main.run_id) == (env.child,)
    assert agents._read_one(OPERATION_QUERY, ('register-main',))[0] == 'write_run'


def test_original_owner_facts_are_readonly_without_constructor_or_ddl(facts, monkeypatch):
    # The original stores above are only producers. Observe solely the new read path.
    operation = tuple(facts.agents._read_one(OPERATION_QUERY, ('register-main',)))
    with observe_connections(facts.path, monkeypatch) as observed:
        turns, agents = authority_stores(facts.records)
        read_all(facts, turns, agents)
        assert tuple(agents._read_one(OPERATION_QUERY, ('register-main',))) == operation
    assert observed.uris and all('?mode=ro' in uri for uri in observed.uris)
    assert observed.flags and all(flag == (1, True) for flag in observed.flags)
    assert len(observed.write_errors) == len(observed.flags)
    assert all('readonly' in error for error in observed.write_errors)
    assert not any(statement.lstrip().upper().startswith(('CREATE ', 'ALTER ', 'DROP ', 'INSERT ', 'DELETE '))
        for statement in observed.statements)


def test_existing_opaque_reference_and_absent_stable_locator_are_preserved(facts):
    opaque = 'crp://historical/opaque/snapshot'
    with sqlite3.connect(facts.path) as connection:
        connection.execute('UPDATE ai_turn_immutable_payloads SET payload_ref=? WHERE payload_ref=?',
            (opaque, facts.immutable))
    turns, agents = authority_stores(facts.records)
    assert turns.immutable_payload_reference(facts.request['turn_id'], 'synthetic-snapshot') == opaque
    assert turns.get(opaque) == {'snapshot': 1}
    expected = facts.turns.immutable_payload_reference('unaccepted-turn', 'missing-snapshot')
    assert turns.immutable_payload_reference('unaccepted-turn', 'missing-snapshot') == expected
    assert turns.get_request('unaccepted-turn') is None
    assert turns.events_after('unaccepted-turn') == ()
    assert agents.get_run('missing-run') is None
    assert agents._read_one(OPERATION_QUERY, ('missing-operation',)) is None
    with pytest.raises(KeyError) as missing:
        turns.get(expected)
    assert missing.value.args == (expected,)
    with sqlite3.connect(facts.path) as connection:
        assert connection.execute('SELECT COUNT(*) FROM ai_turn_immutable_payloads').fetchone()[0] == 1


@pytest.mark.parametrize('damage', ['missing_table', 'missing_column'])
@pytest.mark.parametrize('table,column', SCHEMA_COLUMNS)
def test_all_six_required_table_schemas_fail_closed_without_repair(facts, damage, table, column):
    with sqlite3.connect(facts.path) as connection:
        if damage == 'missing_table':
            connection.execute(f'DROP TABLE {table}')
        else:
            connection.execute(f'ALTER TABLE {table} RENAME COLUMN {column} TO removed_fact')
        before = tuple(connection.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name'))
    with pytest.raises(RecognitionConflict, match='^research terminal authority is unavailable$'):
        authority_stores(facts.records)
    with sqlite3.connect(facts.path) as connection:
        assert tuple(connection.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name')) == before


def test_missing_fixed_database_never_probes_valid_neighbor(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    fixed = source_store(records).root / 'ai-turns.sqlite3'
    adjacent = tmp_path / 'ai-turns.sqlite3'
    SQLiteAITurnStore(adjacent)
    SQLiteAgentStore(adjacent)
    with pytest.raises(RecognitionConflict, match='^research terminal authority is unavailable$'):
        authority_stores(records)
    assert not fixed.exists()


@pytest.mark.parametrize('kind,value', [
    ('request', '{'), ('request', '[]'), ('event', '{'), ('event', '[]'),
    ('normal', '{'), ('immutable', '{'), ('run', '{'), ('run', '{}'),
    ('operation_request', '{'), ('operation_request', '{}'),
    ('operation_result', '{'), ('operation_result', '{}'),
])
def test_malformed_json_is_rejected_with_safe_original_error(facts, kind, value):
    table, column, identity_column, identity = {
        'request': ('ai_turns', 'request_json', 'turn_id', facts.request['turn_id']),
        'event': ('ai_turn_events', 'event_json', 'turn_id', facts.request['turn_id']),
        'normal': ('ai_turn_payloads', 'payload_json', 'payload_ref', facts.normal),
        'immutable': ('ai_turn_immutable_payloads', 'payload_json', 'payload_ref', facts.immutable),
        'run': ('ai_agent_runs', 'payload_json', 'run_id', facts.main.run_id),
        'operation_request': ('ai_agent_operations', 'request_json', 'operation_id', 'register-main'),
        'operation_result': ('ai_agent_operations', 'result_json', 'operation_id', 'register-main'),
    }[kind]
    with sqlite3.connect(facts.path) as connection:
        connection.execute(f'UPDATE {table} SET {column}=? WHERE {identity_column}=?', (value, identity))
    turns, agents = authority_stores(facts.records)
    with pytest.raises(RecognitionConflict, match='^research terminal authority is unavailable$'):
        if kind == 'request': turns.get_request(identity)
        elif kind == 'event': turns.events_after(identity)
        elif kind in {'normal', 'immutable'}: turns.get(identity)
        elif kind == 'run': agents.get_run(identity)
        else: agents._read_one(OPERATION_QUERY, (identity,))


@pytest.mark.parametrize('query,params', [
    ('SELECT * FROM ai_agent_runs', ()),
    ('DELETE FROM ai_agent_operations WHERE operation_id=?', ('register-main',)),
    (OPERATION_QUERY, ('register-main', 'extra')),
    (OPERATION_QUERY, (None,)),
])
def test_operation_seam_accepts_only_the_one_existing_fixed_read(facts, query, params):
    _, agents = authority_stores(facts.records)
    with pytest.raises(RecognitionConflict, match='^research terminal authority is unavailable$'):
        agents._read_one(query, params)


def test_valid_turn_without_run_requires_existing_agent_schema(facts):
    with sqlite3.connect(facts.path) as connection:
        connection.execute('DELETE FROM ai_agent_runs')
    _, agents = authority_stores(facts.records)
    assert agents.get_run_by_turn_id(facts.request['turn_id'], project_id='project-a') is None
    assert agents.list_runs(project_id='project-a') == ()


def test_invalid_fixed_database_is_rejected_without_probing_neighbor(facts):
    invalid = source_store(facts.records).root / 'ai-turns.sqlite3'
    # Use a distinct locator after closing the actual original producers.
    records = SQLiteStructuredRecordStore(invalid.parent / 'invalid-records' / 'records.sqlite3')
    fixed = source_store(records).root / 'ai-turns.sqlite3'
    fixed.parent.mkdir(parents=True, exist_ok=True)
    fixed.write_bytes(b'synthetic invalid SQLite authority')
    with pytest.raises(RecognitionConflict, match='^research terminal authority is unavailable$'):
        authority_stores(records)
    assert fixed.read_bytes() == b'synthetic invalid SQLite authority'


@pytest.mark.parametrize('method', ['get_run_by_turn_id', 'list_runs'])
def test_corrupt_run_parser_is_also_safe_for_lookup_and_tree(facts, method):
    with sqlite3.connect(facts.path) as connection:
        connection.execute('UPDATE ai_agent_runs SET payload_json=? WHERE run_id=?', ('{}', facts.main.run_id))
    _, agents = authority_stores(facts.records)
    with pytest.raises(RecognitionConflict, match='^research terminal authority is unavailable$'):
        if method == 'get_run_by_turn_id':
            agents.get_run_by_turn_id(facts.request['turn_id'], project_id='project-a')
        else:
            agents.list_runs(project_id='project-a')


@pytest.mark.parametrize('value', [7, ['synthetic'], None])
def test_dynamic_payload_precedes_colliding_immutable_ref_and_keeps_json_values(facts, value):
    with sqlite3.connect(facts.path) as connection:
        connection.execute('UPDATE ai_turn_immutable_payloads SET payload_ref=? WHERE payload_ref=?',
            (facts.normal, facts.immutable))
        connection.execute('UPDATE ai_turn_payloads SET payload_json=? WHERE payload_ref=?',
            (json.dumps(value), facts.normal))
    assert facts.turns.get(facts.normal) == value
    turns, _ = authority_stores(facts.records)
    assert turns.immutable_payload_reference(facts.request['turn_id'], 'synthetic-snapshot') == facts.normal
    assert turns.get(facts.normal) == value
