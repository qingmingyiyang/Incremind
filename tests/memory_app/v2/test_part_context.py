"""Source facts and egress policy remain authoritative across processing."""
import pytest
from backend.recognition import WorkScope, RecognitionConflict
from backend.memory_app.source_egress import SourceEgressService
from core.storage_provider import SQLiteStructuredRecordStore


@pytest.fixture
def original(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    payload = {'id':'workspace-one', 'project_id':'alpha', 'input_kind':'text',
               'created_at':'2026-10-03T00:00:00Z', 'title':'预算', 'source_text':'预算十万元', 'status':'staged'}
    with records.begin() as tx:
        tx.put('workspace_items', 'workspace-one', payload, expected_revision=0)
        tx.commit()
    yield records, SourceEgressService(records), WorkScope('local-user', 'alpha')


def test_original_input_snapshot_retains_authority_across_processing(original):
    records, authority, scope = original
    snapshot = authority.snapshot_original_content(scope, 'workspace-one', span='十万元')
    strict = authority.snapshot(scope, [{'type':'original_item', 'id':'workspace-one', 'revision':1}])
    with records.begin() as tx:
        row = tx.read('workspace_items', 'workspace-one')
        tx.put('workspace_items', row.object_id, {**row.payload, 'status':'processing', 'run_id':'one'}, expected_revision=row.revision)
        tx.commit()
    authority.validate_original_content(scope, snapshot, 'generation')
    with pytest.raises(RecognitionConflict):
        authority.validate_snapshot(scope, strict)


@pytest.mark.parametrize('change', ['body', 'project', 'scene', 'private', 'policy', 'readmit'])
def test_original_input_snapshot_rejects_changed_identity_and_authority(original, change):
    records, authority, scope = original
    snapshot = authority.snapshot_original_content(scope, 'workspace-one', span='十万元')
    if change == 'private':
        from backend.memory_app.v2.privacy import set_private_project
        set_private_project(records, 'alpha', True, 0)
        set_private_project(records, 'alpha', False, 1)
    elif change == 'policy':
        authority.set_policy(scope=scope, source_type='original_item', source_id='workspace-one',
            allowed_purposes=[], expected_source_revision=1, expected_policy_revision=0)
    elif change == 'scene':
        from backend.memory_app.v2.projects import assign_scene
        assign_scene(records, 'item', 'workspace-one', 'alpha', 'changed')
    else:
        changes = {'body':{'source_text':'预算二十万元'}, 'project':{'project_id':'other'},
                   'readmit':{'created_at':'2026-10-03T00:01:00Z'}}[change]
        with records.begin() as tx:
            row = tx.read('workspace_items', 'workspace-one')
            tx.put('workspace_items', row.object_id, {**row.payload, **changes}, expected_revision=row.revision)
            tx.commit()
    with pytest.raises(RecognitionConflict):
        authority.validate_original_content(scope, snapshot, 'generation')


@pytest.mark.parametrize('kind', ['file', 'link'])
@pytest.mark.parametrize('change', [None, 'source', 'span', 'parent'])
def test_nontext_original_uses_frozen_turn_coordinates(original, kind, change):
    records, authority, scope = original
    span = '记住这个附件。' if kind == 'file' else 'https://example.test/budget'
    parent_text = span + '预算多少？'
    with records.begin() as tx:
        row = tx.read('workspace_items', 'workspace-one')
        tx.put('workspace_items', row.object_id, {**row.payload, 'input_kind':kind}, expected_revision=row.revision)
        tx.put('v2_turns', 'parent', {'project_id':'alpha', 'user_text':parent_text}, expected_revision=0)
        tx.put('v2_turns', 'child', {'project_id':'alpha', 'parent_turn_id':'parent',
            'user_text':span, 'item_id':'workspace-one'}, expected_revision=0)
        tx.commit()
    binding = {'child_id':'child', 'parent_id':'parent', 'parent_text':parent_text, 'start':0, 'end':len(span)}
    snapshot = authority.snapshot_original_content(scope, 'workspace-one', span=span, input_binding=binding)
    assert snapshot['content']['coordinate_space'] == 'child_turn_user_text_v1'
    assert snapshot['content']['identity']['source_text'] != span
    if change:
        with records.begin() as tx:
            collection, identity, changes = ('workspace_items', 'workspace-one', {'source_text':'改动正文'}) if change == 'source' else (
                'v2_turns', 'child', {'user_text':'改动输入'} if change == 'span' else {'parent_turn_id':'other'})
            row = tx.read(collection, identity)
            tx.put(collection, identity, {**row.payload, **changes}, expected_revision=row.revision)
            tx.commit()
        with pytest.raises(RecognitionConflict):
            authority.validate_original_content(scope, snapshot, 'generation')
    else:
        authority.validate_original_content(scope, snapshot, 'generation')
