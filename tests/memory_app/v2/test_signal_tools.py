import sqlite3
from contextlib import closing
from pathlib import Path
import pytest
from core.storage_provider import SQLiteStructuredRecordStore


@pytest.fixture(autouse=True)
def isolate_deployment(tmp_path, monkeypatch):
    for name in list(__import__('os').environ):
        if name.startswith('CHRIPTMAS_RUNTIME_'):
            monkeypatch.delenv(name)
    monkeypatch.setenv('CHRIPTMAS_DEPLOY','desktop')
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT',str(tmp_path/'formal-absent'))
    from backend.memory_app.v2.policies import override
    with override(reask='@1'):
        yield


@pytest.fixture
def copy_root(tmp_path,monkeypatch):
    for name in list(__import__('os').environ):
        if name.startswith('CHRIPTMAS_RUNTIME_'):
            monkeypatch.delenv(name)
    monkeypatch.setenv('CHRIPTMAS_DEPLOY','desktop')
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT',str(tmp_path/'formal-absent'))
    root=tmp_path/'copy';root.mkdir()
    records=SQLiteStructuredRecordStore(root/'recognition.sqlite3')
    with records.begin() as tx:
        tx.put('v2_turns','ask-a',{'project_id':'alpha','intent':'ask','created_at':'2026-10-06T12:00:00+00:00','user_text':'Synthetic private question','receipt':{'ask':{'answer':'Synthetic private answer','citations':[]}}},expected_revision=0);tx.commit()
    from backend.memory_app.v2.policies import override
    with override(reask='@1'):
        yield root


def inventory(root):
    return {str(p.relative_to(root)):(p.stat().st_size,p.stat().st_mtime_ns) for p in root.rglob('*') if p.is_file()}


def test_copy_reader_wal_visible_and_input_unchanged(copy_root):
    from tools.signal_report import open_signal_copy
    connection=sqlite3.connect(copy_root/'recognition.sqlite3')
    try:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute("INSERT INTO crp_structured_records(collection,object_id,payload_json,revision) VALUES (?, ?, ?, ?)", ("wal_facts", "only-wal", '{"count":7}', 1))
        connection.commit()
        assert (copy_root/'recognition.sqlite3-wal').stat().st_size>0
        before=inventory(copy_root)
        with open_signal_copy(copy_root) as opened:
            assert opened.records.read('wal_facts','only-wal').payload=={'count':7}
            scratch=opened.scratch_root
            assert scratch!=copy_root and scratch.is_dir()
            with opened.records.begin() as reader:
                assert reader.read('wal_facts','only-wal').payload=={'count':7}
                with pytest.raises(sqlite3.OperationalError):
                    reader.put('wal_facts','forbidden',{'count':1},expected_revision=0)
            with closing(opened.records._connect()) as sql:
                for statement in ('BEGIN IMMEDIATE','CREATE TABLE prohibited(id INTEGER)',"DELETE FROM crp_structured_records"):
                    with pytest.raises(sqlite3.OperationalError):sql.execute(statement)
        assert not scratch.exists()
        assert inventory(copy_root)==before
        assert connection.execute("SELECT payload_json FROM crp_structured_records WHERE object_id='only-wal'").fetchone()==('{"count":7}',)
    finally:connection.close()


def test_formal_root_and_ancestors_refused_before_database_read(tmp_path,monkeypatch):
    from tools.signal_report import open_signal_copy
    formal=tmp_path/'formal';formal.mkdir();(formal/'recognition.sqlite3').write_bytes(b'not a database')
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT',str(formal))
    for target in (formal,tmp_path,formal/'child'):
        with pytest.raises(ValueError,match='formal_root_forbidden'):
            with open_signal_copy(target):pass
    assert (formal/'recognition.sqlite3').read_bytes()==b'not a database'


def test_server_entire_user_tree_is_formal(tmp_path,monkeypatch):
    from tools.signal_report import open_signal_copy
    monkeypatch.setenv('CHRIPTMAS_DEPLOY','server')
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT',str(tmp_path/'server'))
    with pytest.raises(ValueError,match='formal_root_forbidden'):
        with open_signal_copy(tmp_path/'server/users/other-user'):pass
    assert not (tmp_path/'server').exists()


def test_missing_copy_database_does_not_initialize_schema(tmp_path,monkeypatch):
    from tools.signal_report import open_signal_copy
    root=tmp_path/'empty';root.mkdir();monkeypatch.setenv('CHRIPTMAS_APP_ROOT',str(tmp_path/'formal'))
    with pytest.raises(ValueError,match='copy_records_missing'):
        with open_signal_copy(root):pass
    assert list(root.iterdir())==[]


def test_report_off_clear_and_body_free(copy_root):
    from tools.signal_report import signal_report
    from backend.memory_app.v2.signals import SignalService
    records=SQLiteStructuredRecordStore(copy_root/'recognition.sqlite3')
    before=inventory(copy_root)
    result=signal_report(copy_root)
    assert 'Synthetic' not in __import__('json').dumps(result)
    assert inventory(copy_root)==before
    owner=SignalService(records)
    settings=owner.settings()
    owner.set_enabled(False,expected_revision=settings['revision'])
    assert signal_report(copy_root)=={}


from tests.memory_app.v2.test_workbench_ask import env as ask_env


def test_real_answer_input_and_readonly_query_preserve_input(ask_env):
    from tests.memory_app.v2.test_workbench_ask import add_document,ask
    from tools.signal_report import open_signal_copy
    identity,_=add_document(ask_env)
    response=ask(ask_env)
    assert response.status_code==200
    turn_id=response.json()['turn']['id']
    root=analysis_copy(ask_env)
    before=inventory(root)
    with open_signal_copy(root) as opened:
        frozen=opened.answer_input(turn_id)
        assert frozen and any('alpha' in message['content'] for message in frozen['messages'])
        query=opened.query()
        collected=query.collect_candidates('alpha','alpha beta gamma')
        plan=query.prepare_ask('alpha','alpha beta gamma',collected=collected)
        assert plan['chosen']
        assert query.models.attempts==0 and query.retrieval_index.worker is None
    assert inventory(root)==before


def test_readonly_cold_index_reports_coverage_without_repair(ask_env):
    from tests.memory_app.v2.test_workbench_ask import add_document
    from core.document_engine.retrieval_index import COLLECTION
    from tools.signal_report import open_signal_copy
    identity,_=add_document(ask_env)
    row=ask_env.records.read(COLLECTION,identity)
    with ask_env.records.begin() as tx:
        tx.delete(COLLECTION,identity,expected_revision=row.revision);tx.commit()
    root=analysis_copy(ask_env)
    before=inventory(root)
    with open_signal_copy(root) as opened:
        query=opened.query()
        assert ('document',identity) in query.retrieval_index.unavailable
        query.collect_candidates('alpha','alpha')
        assert opened.records.read(COLLECTION,identity) is None
        assert query.retrieval_index.worker is None
    assert inventory(root)==before


def test_replay_two_actual_scope_versions_and_body_free(ask_env):
    from tests.memory_app.v2.test_workbench_ask import add_document
    from tools.memory_eval import replay
    from datetime import datetime,timezone
    add_document(ask_env)
    with ask_env.records.begin() as tx:
        tx.put('v2_turns','replay-a',{'project_id':'alpha','scene':'work','intent':'ask',
            'created_at':datetime.now(timezone.utc).isoformat(),'user_text':'alpha beta gamma',
            'receipt':{'ask':{'answer':'Synthetic private answer','citations':[]}}},expected_revision=0);tx.commit()
    root=analysis_copy(ask_env)
    before=inventory(root)
    result=replay(root,policies=(['scope=@1'],['scope=@2']))
    row=next(row for row in result['turns'] if row['turn_id']=='replay-a')
    assert row['before']['selected']<row['after']['selected']
    assert result['model_attempts']==0
    assert 'alpha beta gamma' not in __import__('json').dumps(result)
    assert 'Synthetic' not in __import__('json').dumps(result)
    assert inventory(root)==before


def test_real_confirmation_source_uses_readonly_publication_owner(ask_env):
    from tests.memory_app.v2.test_workbench_ask import add_document
    from tools.signal_report import open_signal_copy,ReadOnlyRecords
    from core.storage_provider.source_asset_runtime import SourceAssetRuntimeStore
    import shutil
    _,item_id=add_document(ask_env)
    root=analysis_copy(ask_env)
    before=inventory(root)
    with open_signal_copy(root) as opened:
        publication=ReadOnlyRecords(opened.scratch_root/'.rebuild-data/structured-records.sqlite3')
        sources=SourceAssetRuntimeStore(json_store=opened.sources._json,sqlite_records=opened.sources._records,
            library_root=opened.scratch_root/'library',authority_identity=opened.sources.authority_identity,
            publication_records=publication)
        values=sources.list('sources')
        assert values and any(value.get('identity_method')=='workspace_confirmation' for value in values)
        for value in values:
            if value.get('identity_method')=='workspace_confirmation':
                assert sources.read('sources',value['id'])==value
        assert publication.list('workspace_confirmation_operations')
    assert inventory(root)==before


def analysis_copy(env, *, suffix="-copy"):
    """Synthetic explicit cutover copy of genuine domain-created facts."""
    import shutil
    from tests.rebuild.test_aggregate_repository_factory import _exact_document_authority
    root=env.root.with_name(env.root.name+suffix)
    env.domains.query.retrieval_index.wait_for_repairs()
    from contextlib import ExitStack
    with ExitStack() as leases:
        for database in env.root.rglob('*.sqlite3'):
            keeper=leases.enter_context(closing(sqlite3.connect(database)))
            keeper.execute('SELECT name FROM sqlite_schema').fetchall()
        shutil.copytree(env.root,root)
    target=root/'.rebuild-data/structured-records.sqlite3'
    records=SQLiteStructuredRecordStore(target)
    records.list('documents')
    # Original injected test store is not a deployed authority. Preserve each
    # genuine row's payload and CAS revision in an explicit synthetic cutover.
    with closing(sqlite3.connect(root/'records.sqlite3')) as origin, closing(sqlite3.connect(target)) as destination:
        rows=origin.execute('SELECT collection,object_id,payload_json,revision FROM crp_structured_records').fetchall()
        destination.executemany('INSERT OR REPLACE INTO crp_structured_records(collection,object_id,payload_json,revision) VALUES (?,?,?,?)',rows)
        destination.commit()
    _exact_document_authority(root,records)
    return root


def test_markdown_reports_actual_seven_metrics():
    from tools.signal_report import markdown_report
    result={'unused':{'layers':{'note':{'sent':5,'cited':2}},'unknown_turns':3},
        'corrections':{'groups':[],'unknown_events':4},'reask':{'pairs':[{}]},
        'after_answer':{'turns':[{'copy':2,'do':1}],'opens':3},'dwell':{'groups':[],'objects':[]},
        'document_edits':{'objects':[{'paragraphs':7}],'unknown_versions':1},
        'interruptions':{'turns':[{'stop':2,'steer':None}]}}
    output=markdown_report(result)
    assert 'sent=5' in output and 'cited=2' in output and 'paragraphs=7' in output
    assert 'copy=2' in output and 'stop=2' in output and 'steer_unknown=1' in output
    assert len([line for line in output.splitlines() if line.startswith('| ') and not line.startswith(('| metric','| ---'))])==7



def test_actual_sqlite_authority_and_bad_marker_fail_closed(ask_env):
    from tests.memory_app.v2.test_workbench_ask import add_document
    from tools.signal_report import open_signal_copy
    from core.aggregate_repository_factory import AggregateRepositoryFactoryError
    from core.document_engine import SQLiteDocumentRepository
    add_document(ask_env);root=analysis_copy(ask_env)
    before=inventory(root)
    with open_signal_copy(root) as opened:
        assert isinstance(opened.documents,SQLiteDocumentRepository)
        assert opened.documents.namespace_id=='default'
        assert opened.documents.list()
        assert opened.sources.sqlite_active is False
    assert inventory(root)==before
    records=SQLiteStructuredRecordStore(root/'.rebuild-data/structured-records.sqlite3')
    marker=records.read('aggregate_authority_targets','default~documents')
    with records.begin() as tx:
        tx.put('aggregate_authority_targets','default~documents',{**marker.payload,'verified_record_count':999},expected_revision=marker.revision);tx.commit()
    before=inventory(root)
    with pytest.raises(AggregateRepositoryFactoryError):
        with open_signal_copy(root):pass
    assert inventory(root)==before


def test_explicit_launch_authority_aliases_and_invalid_contract(tmp_path,monkeypatch):
    from tools.signal_report import open_signal_copy
    from backend.api.runtime_root_config import RuntimeRootConfigError
    formal=tmp_path/'formal';formal.mkdir()
    for key,value in {'VERSION':'1','REVISION':'synthetic','VAULT_ROOT':str(formal),'MODEL_ROOT':str(formal),'MEDIA_ROOT':str(formal)}.items():
        monkeypatch.setenv('CHRIPTMAS_RUNTIME_ROOT_'+key,value) if key in ('VERSION','REVISION') else monkeypatch.setenv('CHRIPTMAS_RUNTIME_'+key,value)
    alias=formal/'..'/'formal'
    with pytest.raises(ValueError,match='formal_root_forbidden'):
        with open_signal_copy(alias):pass
    monkeypatch.delenv('CHRIPTMAS_RUNTIME_MEDIA_ROOT')
    with pytest.raises(RuntimeRootConfigError):
        with open_signal_copy(tmp_path/'otherwise-copy'):pass
    assert list(formal.iterdir())==[]


def test_copy_changes_are_rejected_and_scratch_cleaned(copy_root):
    from tools.signal_report import open_signal_copy
    with pytest.raises(ValueError,match='copy_changed'):
        with open_signal_copy(copy_root) as opened:
            scratch=opened.scratch_root
            (copy_root/'changed').write_text('Synthetic changed copy')
    assert not scratch.exists()


def test_readonly_query_missing_index_injection_fails_before_files(copy_root):
    from backend.memory_app.workspace_query import WorkspaceQuery
    from backend.recognition import RecognitionService
    from core.document_engine import SQLiteDocumentRepository
    from core.storage_provider import JsonObjectStore
    from tools.memory_eval import NoModels
    from tools.signal_report import ReadOnlyRecords
    records=ReadOnlyRecords(copy_root/'recognition.sqlite3')
    sources=JsonObjectStore(copy_root/'.rebuild-data')
    before=inventory(copy_root)
    with pytest.raises(ValueError,match='readonly_source_index_required'):
        WorkspaceQuery(records,SQLiteDocumentRepository(records),sources,NoModels(),RecognitionService(records),read_only=True)
    assert inventory(copy_root)==before
    assert not sources.root.exists()



def test_readonly_invalid_projection_is_unavailable_not_repaired(ask_env):
    from tests.memory_app.v2.test_workbench_ask import add_document
    from core.document_engine.retrieval_index import COLLECTION
    from tools.signal_report import open_signal_copy
    identity,_=add_document(ask_env)
    row=ask_env.records.read(COLLECTION,identity)
    with ask_env.records.begin() as tx:
        tx.put(COLLECTION,identity,{**row.payload,'entry':None},expected_revision=row.revision);tx.commit()
    root=analysis_copy(ask_env);before=inventory(root)
    with open_signal_copy(root) as opened:
        query=opened.query()
        assert ('document',identity) in query.retrieval_index.unavailable
        query.collect_candidates('alpha','alpha')
        assert opened.records.read(COLLECTION,identity).payload['entry'] is None
        assert query.retrieval_index.worker is None
    assert inventory(root)==before


def test_source_asset_sqlite_authority_never_falls_back_to_json(ask_env):
    from tests.memory_app.v2.test_workbench_ask import add_document
    from tests.rebuild.test_aggregate_repository_factory import _activate_source_asset_compound
    from tools.signal_report import open_signal_copy
    from core.storage_provider import JsonObjectStore
    from core.storage_provider.source_asset_runtime import SourceAssetRuntimeStore
    add_document(ask_env);root=analysis_copy(ask_env)
    records=SQLiteStructuredRecordStore(root/'.rebuild-data/structured-records.sqlite3')
    _activate_source_asset_compound(root,records)
    json_store=JsonObjectStore(root/'.rebuild-data')
    runtime=SourceAssetRuntimeStore(json_store=json_store,sqlite_records=records,library_root=root/'library',authority_identity='synthetic')
    payload={'id':'asset-a','asset_ref':'asset-a','sha256':'a'*64,'vault_ref':'synthetic.txt','byte_count':0,'metadata':{'kind':'sql'}}
    runtime.write('workbench_original_assets','asset-a',payload,expected_revision=0)
    json_store.write('workbench_original_assets','asset-a',{**payload,'metadata':{'kind':'json'}},expected_revision=0)
    before=inventory(root)
    with open_signal_copy(root) as opened:
        assert opened.sources.sqlite_active
        assert opened.sources.read('workbench_original_assets','asset-a')['metadata']=={'kind':'sql'}
        assert opened.sources._json.read('workbench_original_assets','asset-a')['metadata']=={'kind':'json'}
        assert opened.sources.list('sources')  # sources remain original JSON owner
    assert inventory(root)==before


def test_replay_ninety_days_clear_off_and_admin_gates(ask_env):
    from tests.memory_app.v2.test_workbench_ask import add_document
    from tools.memory_eval import replay
    from backend.memory_app.v2.signals import SignalService
    from datetime import datetime,timedelta,timezone
    add_document(ask_env);clock=datetime.now(timezone.utc)
    with ask_env.records.begin() as tx:
        for identity,age,by in [('recent',1,'user'),('old',91,'user'),('admin',1,'admin'),('future',-1,'user')]:
            tx.put('v2_turns',identity,{'project_id':'alpha','intent':'ask','by':by,
                'created_at':(clock-timedelta(days=age)).isoformat(),'user_text':'alpha',
                'receipt':{'ask':{'answer':'Synthetic','citations':[]}}},expected_revision=0)
        tx.commit()
    root=analysis_copy(ask_env)
    result=replay(root,policies=(['scope=@1'],['scope=@2']),now=clock)
    assert [row['turn_id'] for row in result['turns']]==['recent']
    records=SQLiteStructuredRecordStore(root/'.rebuild-data/structured-records.sqlite3')
    owner=SignalService(records)
    owner.clear(expected_revision=owner.settings()['revision'])
    assert replay(root,policies=(['scope=@1'],['scope=@2']),now=clock)['turns']==[]
    owner.set_enabled(False,expected_revision=owner.settings()['revision'])
    assert replay(root,policies=(['scope=@1'],['scope=@2']),now=clock)=={}


def test_replay_real_unused_and_citation_rank_metrics(ask_env):
    from tests.memory_app.v2.test_workbench_ask import publish,ask,add_document
    from tools.memory_eval import replay
    from tools.signal_report import signal_report
    insight,_=publish(ask_env)
    ask_env.model.numbers=[]
    identities=[]
    for _ in range(5):
        response=ask(ask_env)
        assert response.status_code==200
        identities.append(response.json()['turn']['id'])
    add_document(ask_env)
    root=analysis_copy(ask_env)
    before=inventory(root)
    statistics=signal_report(root)
    assert any(row['object_id']==insight.id and row['sent']==5 and row['cited']==0 for row in statistics['unused']['objects'])
    result=replay(root,policies=(['scope=@1'],['scope=@2']))
    assert len(result['turns'])==5
    for row in result['turns']:
        assert row['before']['cited_total']==0 and row['before']['cited_top_k']==0
        assert any(rank['object_id']==insight.id and rank['rank'] is not None for rank in row['before']['unused_ranks'])
    assert result['model_attempts']==0 and inventory(root)==before


def test_cli_report_and_replay_actual_argv_body_free(copy_root):
    import subprocess,json
    from tools.signal_report import ROOT
    # New tool's fixed derived output path is absent before this synthetic CLI.
    from datetime import datetime,timezone
    directory=copy_root.parent/'cli-output'
    destination=directory/f'{datetime.now(timezone.utc):%Y-%m-%d}'
    assert not destination.with_suffix('.json').exists()
    command=[__import__('sys').executable,str(ROOT/'tools/signal_report.py'),'--root',str(copy_root),'--output-dir',str(directory)]
    completed=subprocess.run(command,capture_output=True,text=True)
    assert completed.returncode==0,completed.stderr
    payload=json.loads(destination.with_suffix('.json').read_text(encoding='utf-8'))
    assert len(payload)==7 and 'Synthetic' not in json.dumps(payload)
    assert len([line for line in destination.with_suffix('.md').read_text().splitlines() if line.startswith('| ') and not line.startswith(('| metric','| ---'))])==7
    assert 'Synthetic' not in completed.stdout+completed.stderr
    # Persist exact CLI evidence as derived test data; do not delete unknown files.
    output=copy_root.parent/'cli-proof.json'
    output.write_text(json.dumps({'argv':command,'exit':completed.returncode}),encoding='utf-8')


def test_cli_output_refuses_input_formal_and_unknown_targets(copy_root):
    import subprocess,sys
    from tools.signal_report import ROOT
    from datetime import datetime,timezone
    base=[sys.executable,str(ROOT/'tools/signal_report.py'),'--root',str(copy_root)]
    before=inventory(copy_root)
    for target in (copy_root,copy_root.parent/'formal-absent',copy_root.parent):
        result=subprocess.run(base+['--output-dir',str(target)],capture_output=True,text=True)
        assert result.returncode==2 and 'report_output_forbidden' in result.stderr
    assert inventory(copy_root)==before
    unknown=copy_root.parent/'unknown-output';unknown.mkdir()
    output=unknown/f'{datetime.now(timezone.utc):%Y-%m-%d}.json';output.write_text('Synthetic existing file')
    result=subprocess.run(base+['--output-dir',str(unknown)],capture_output=True,text=True)
    assert result.returncode==2 and 'report_output_exists' in result.stderr
    assert output.read_text()=='Synthetic existing file'
    assert len(list(unknown.iterdir()))==1


def test_replay_same_document_different_layer_does_not_count_hit(ask_env):
    from tests.memory_app.v2.test_workbench_ask import add_document,ask
    from tools.memory_eval import replay
    identity,_=add_document(ask_env,summary='summaryneedle',body='bodyneedle')
    response=ask(ask_env,text='bodyneedle?',intent='ask')
    assert response.status_code==200
    assert any(c['id']==identity and c['layer']=='note' for c in response.json()['turn']['receipt']['ask']['citations'])
    doc=ask_env.documents.read(identity)
    ask_env.documents.save_user_edit(identity,expected_revision=doc['revision'],markdown='# Synthetic\n\n## 摘要\nbodyneedle\n\n## 正文\ndifferent material')
    root=analysis_copy(ask_env)
    result=replay(root,policies=(['scope=@1'],['scope=@2']))
    row=next(row for row in result['turns'] if row['turn_id']==response.json()['turn']['id'])
    assert row['before']['cited_top_k']==0
    assert row['before']['rank_space']=='selected_context'


def test_replay_cold_citation_is_unknown_even_other_object_selected(ask_env):
    from tests.memory_app.v2.test_workbench_ask import add_document,ask,publish
    from core.document_engine.retrieval_index import COLLECTION
    from tools.memory_eval import replay
    identity,_=add_document(ask_env,summary='summaryneedle',body='bodyneedle')
    response=ask(ask_env,text='bodyneedle?',intent='ask');assert response.status_code==200
    publish(ask_env,text='bodyneedle')
    projection=ask_env.records.read(COLLECTION,identity)
    with ask_env.records.begin() as tx:
        tx.delete(COLLECTION,identity,expected_revision=projection.revision);tx.commit()
    root=analysis_copy(ask_env)
    result=replay(root,policies=(['scope=@1'],['scope=@2']))
    row=next(row for row in result['turns'] if row['turn_id']==response.json()['turn']['id'])
    assert row['before']['selected']>0
    assert row['before']['cited_top_k'] is None
    assert row['before']['citation_unknown']>0


def test_replay_private_project_has_unavailable_local_baseline(ask_env):
    from tests.memory_app.v2.test_workbench_ask import add_document,ask
    from backend.memory_app.v2.privacy import set_private_project
    from tools.memory_eval import replay
    add_document(ask_env)
    response=ask(ask_env);assert response.status_code==200
    set_private_project(ask_env.records,'alpha',True,0)
    root=analysis_copy(ask_env)
    result=replay(root,policies=(['scope=@1'],['scope=@2']))
    row=next(row for row in result['turns'] if row['turn_id']==response.json()['turn']['id'])
    assert row['before']['status']=='unavailable'
    assert row['before']['selected'] is None and row['before']['cited_top_k'] is None
    assert row['before']['reason']=='private_no_models'


def test_replay_cli_output_refuses_input_and_unknown_file(ask_env):
    import subprocess, sys
    from tools.signal_report import ROOT
    from tests.memory_app.v2.test_workbench_ask import add_document, ask
    add_document(ask_env); assert ask(ask_env).status_code == 200
    copy_root = analysis_copy(ask_env)
    before = inventory(copy_root)
    original = (copy_root/'.rebuild-data'/'structured-records.sqlite3').read_bytes()
    base = [sys.executable, '-m', 'tools.memory_eval', '--replay', str(copy_root),
            '--policy', 'scope=@1', '--policy', 'scope=@2']
    for target in (copy_root/'.rebuild-data'/'structured-records.sqlite3', Path(__import__('os').environ['CHRIPTMAS_APP_ROOT'])/'report.json'):
        result = subprocess.run(base+['--output', str(target)], capture_output=True, text=True)
        assert result.returncode == 2 and 'report_output_forbidden' in result.stderr
    assert inventory(copy_root) == before
    assert (copy_root/'.rebuild-data'/'structured-records.sqlite3').read_bytes() == original
    existing = copy_root.parent/'existing.json'; existing.write_text('Synthetic existing')
    result = subprocess.run(base+['--output', str(existing)], capture_output=True, text=True)
    assert result.returncode == 2 and 'report_output_exists' in result.stderr
    assert existing.read_text() == 'Synthetic existing'


def test_replay_json_source_cold_projection_is_unknown(ask_env):
    from tests.memory_app.v2.test_workbench_ask import ask, publish
    from core.storage_provider.source_retrieval_index import COLLECTION
    from tools.memory_eval import replay
    query=ask_env.domains.query
    query.source_store.write('sources','standalone',{'id':'standalone','title':'Synthetic',
        'project_id':'alpha','metadata':{'content':'alpha beta gamma'}},expected_revision=0)
    response=ask(ask_env);assert response.status_code==200
    citations=response.json()['turn']['receipt']['ask']['citations']
    assert any(c['layer']=='source' and c['id']=='standalone' and c['locator']['coordinate_space']=='source_content_v1' for c in citations)
    publish(ask_env)
    query.retrieval_index.wait_for_repairs()
    records=query.retrieval_index.source_records
    projection=records.read(COLLECTION,'standalone');assert projection is not None
    with records.begin() as tx:
        tx.delete(COLLECTION,'standalone',expected_revision=projection.revision);tx.commit()
    result=replay(analysis_copy(ask_env),policies=(['scope=@1'],['scope=@2']))
    row=next(r for r in result['turns'] if r['turn_id']==response.json()['turn']['id'])
    assert row['before']['selected']>0
    assert row['before']['cited_top_k'] is None and row['before']['citation_unknown']>0


def test_replay_direct_script_safe_output_and_layer_rank_delta(ask_env):
    import subprocess,sys,json
    from tools.signal_report import ROOT
    from tests.memory_app.v2.test_workbench_ask import ask,publish
    insight,_=publish(ask_env)
    ask_env.model.numbers=[]
    for _ in range(5):
        assert ask(ask_env).status_code==200
    root=analysis_copy(ask_env)
    target=root.parent/'replay-output'/'report.json'
    result=subprocess.run([sys.executable,str(ROOT/'tools/memory_eval.py'),'--replay',str(root),
        '--policy','scope=@1','--policy','scope=@2','--output',str(target)],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    report=json.loads(target.read_text())
    assert report['model_attempts']==0 and 'Synthetic' not in target.read_text()
    assert all(any(v['object_id']==insight.id and v['layer']=='insight' and v['rank_delta']==0
                   and v['selection_change']=='retained' for v in row['unused_changes']) for row in report['turns'])


def test_replay_future_usage_marks_only_affected_project_unknown(ask_env):
    from datetime import datetime,timezone
    from tests.memory_app.v2.test_workbench_ask import ask,publish
    from tools.memory_eval import replay
    publish(ask_env);publish(ask_env,project='beta')
    ask_env.model.numbers=[]
    other=ask(ask_env,project_id='beta');assert other.status_code==200
    for _ in range(5):
        assert ask(ask_env).status_code==200
    clock=datetime.now(timezone.utc)
    future=ask(ask_env);assert future.status_code==200
    root=analysis_copy(ask_env)
    result=replay(root,policies=(['scope=@1'],['scope=@2']),now=clock)
    assert future.json()['turn']['id'] not in {r['turn_id'] for r in result['turns']}
    alpha=[r for r in result['turns'] if r['project_id']=='alpha']
    assert alpha and all(r['unused_coverage']=='unknown_future_history' and r['unused_changes']==[] for r in alpha)
    beta=next(r for r in result['turns'] if r['project_id']=='beta')
    assert beta['unused_coverage']=='current_present_facts'


def test_replay_unused_source_owner_proof_and_cold_coverage(ask_env):
    from tests.memory_app.v2.test_workbench_ask import ask,publish
    from core.storage_provider.source_retrieval_index import COLLECTION
    from tools.memory_eval import replay
    from tools.signal_report import open_signal_copy
    query=ask_env.domains.query
    query.source_store.write('sources','unused-source',{'id':'unused-source','title':'Synthetic',
        'project_id':'alpha','metadata':{'content':'alpha beta gamma'}},expected_revision=0)
    ask_env.model.numbers=[]
    for _ in range(5):
        response=ask(ask_env);assert response.status_code==200
        assert response.json()['turn']['receipt']['ask']['citations']==[]
    root=analysis_copy(ask_env)
    with open_signal_copy(root) as opened:
        from backend.memory_app.v2.signals import SignalService
        objects=SignalService(opened.records).report(kernel_groups=opened.groups,read_model_input=opened.answer_input)['unused']['objects']
        assert any(o['layer']=='source' and o['object_id']=='unused-source' and o['sent']==5 and o['cited']==0 for o in objects)
    result=replay(root,policies=(['scope=@1'],['scope=@2']))
    assert result['model_attempts']==0
    assert all(any(v['object_id']=='unused-source' and v['layer']=='source' and v['coverage']=='known'
                   and v['selection_change']=='retained' and v['rank_delta']==0 for v in row['unused_changes']) for row in result['turns'])
    publish(ask_env)
    records=query.retrieval_index.source_records
    projection=records.read(COLLECTION,'unused-source');assert projection is not None
    with records.begin() as tx:
        tx.delete(COLLECTION,'unused-source',expected_revision=projection.revision);tx.commit()
    # Second independent copy preserves the original first-copy evidence.
    cold=replay(analysis_copy(ask_env,suffix='-cold-copy'),policies=(['scope=@1'],['scope=@2']))
    assert cold['model_attempts']==0
    assert all(row['before']['selected']>0 for row in cold['turns'])
    assert all(any(v['object_id']=='unused-source' and v['coverage']=='unknown' and v['rank_delta'] is None
                   and v['selection_change']=='unknown' for v in row['unused_changes']) for row in cold['turns'])


def test_replay_unused_workspace_original_preserves_owner_priority(ask_env):
    from tests.memory_app.v2.test_workbench_ask import ask,add_document
    from tools.memory_eval import replay
    _,item=add_document(ask_env,summary='different summary',body='different body',original='alpha beta gamma')
    ask_env.model.numbers=[]
    for _ in range(5):
        assert ask(ask_env).status_code==200
    result=replay(analysis_copy(ask_env),policies=(['scope=@1'],['scope=@2']))
    assert result['model_attempts']==0
    assert all(any(v['object_id']==item and v['layer']=='source' and v['coverage']=='known'
                   and v['selection_change']=='retained' and v['rank_delta']==0 for v in row['unused_changes']) for row in result['turns'])
