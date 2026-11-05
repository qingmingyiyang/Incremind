from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import shutil
from threading import Barrier

import pytest

from backend.memory_app.constraints import ProjectConstraintError, ProjectConstraintService
from backend.memory_app.v2.project_constraints import ProjectConstraintScopes, SCOPES
from backend.recognition import WorkScope
from core.storage_provider import SQLiteStructuredRecordStore


SCOPE = WorkScope('local-user', 'project-a')


def setup_service(path, *, now=None):
    records = SQLiteStructuredRecordStore(path / 'records.sqlite3')
    with records.begin() as tx:
        tx.put('v2_projects', SCOPE.project_id, {'name': '约束验证', 'scenes': ['会议', '预算']}, expected_revision=0)
        tx.commit()
    return records, ProjectConstraintScopes(records, namespace='constraints-qa', owner_user_id='qa-user',
        origin_kind='personal', now=now)


def save(service, identity, content='保持单位一致', **changes):
    return service.upsert(SCOPE, identity, changes.pop('revision', 0), content,
        expected_scope_revision=changes.pop('scope_revision', 0), expected_project_revision=1,
        mode=changes.pop('mode', 'always'), **changes)


def test_old_constraints_default_to_always_without_changing_their_payload(tmp_path):
    records, service = setup_service(tmp_path)
    old = ProjectConstraintService(records).upsert(SCOPE, 'old-rule', 0, '用户原约束')
    payload = deepcopy(records.read('project_constraints', 'old-rule').payload)
    view = service.list(SCOPE)['items'][0]
    assert view['activation'] == {'mode': 'always', 'scene': None, 'keywords': []}
    assert view['scope_revision'] == 0
    assert service.prepare_bundle(SCOPE, scene=None, text='提问')['items'][0]['revision'] == old['revision']
    assert records.read('project_constraints', 'old-rule').payload == payload
    assert records.read(SCOPES, 'old-rule') is None


def test_always_scene_and_explicit_unicode_keywords_select_actual_configured_rules(tmp_path):
    _, service = setup_service(tmp_path)
    save(service, 'always')
    save(service, 'meeting', mode='scene', scene='会议')
    save(service, 'mentioned', mode='mention', keywords=['周报', 'CAFÉ'])
    bundle = service.prepare_bundle(SCOPE, scene='会议', text='输出cafe\u0301周报')
    assert [row['id'] for row in bundle['items']] == ['always', 'meeting', 'mentioned']
    other = service.prepare_bundle(SCOPE, scene='预算', text='提问')
    assert [row['id'] for row in other['items']] == ['always']
    assert '用户明确配置' in bundle['prompt']


def test_body_and_scope_revisions_are_required_together_and_failure_is_atomic(tmp_path):
    records, service = setup_service(tmp_path)
    first = save(service, 'rule', mode='scene', scene='会议')
    with pytest.raises(ProjectConstraintError, match='scope_revision_conflicted'):
        save(service, 'rule', '不应写入', revision=first['revision'], scope_revision=0, mode='mention', keywords=['周报'])
    assert records.read('project_constraints', 'rule').payload['content'] == first['content']
    with pytest.raises(ProjectConstraintError, match='expected revision'):
        save(service, 'rule', '不应写入', revision=0, scope_revision=first['scope_revision'], mode='mention', keywords=['周报'])
    assert records.read(SCOPES, 'rule').payload['setting']['mode'] == 'scene'


def test_original_upsert_in_uow_does_not_commit_an_outer_transaction(tmp_path):
    records, _ = setup_service(tmp_path)
    with records.begin() as tx:
        ProjectConstraintService(records).upsert_in_uow(tx, SCOPE, 'rollback-rule', 0, '等待外层提交')
        tx.rollback()
    assert records.read('project_constraints', 'rollback-rule') is None


def test_removing_the_required_scene_excludes_its_constraint_and_invalidates_old_bundle(tmp_path):
    records, service = setup_service(tmp_path)
    save(service, 'meeting', mode='scene', scene='会议')
    bundle = service.prepare_bundle(SCOPE, scene='会议', text='提问')
    with records.begin() as tx:
        tx.put('v2_projects', SCOPE.project_id, {'name': '约束验证', 'scenes': ['预算']}, expected_revision=1)
        tx.commit()
    assert service.list(SCOPE)['items'][0]['scope_available'] is False
    with pytest.raises(ProjectConstraintError, match='scene_unavailable'):
        service.validate_bundle(bundle, SCOPE, scene='会议', text='提问')
    assert service.prepare_bundle(SCOPE, scene='预算', text='提问')['items'] == []


def test_frozen_matches_detect_newly_matching_rules_and_allow_unrelated_changes(tmp_path):
    _, service = setup_service(tmp_path)
    save(service, 'base')
    other = save(service, 'other', mode='mention', keywords=['周报'])
    bundle = service.prepare_bundle(SCOPE, scene=None, text='预算')
    save(service, 'other', '更新未命中规则', revision=other['revision'], scope_revision=other['scope_revision'],
        mode='mention', keywords=['周报'])
    assert service.validate_bundle(bundle, SCOPE, scene=None, text='预算') == bundle
    save(service, 'new-rule', mode='mention', keywords=['预算'])
    with pytest.raises(ProjectConstraintError, match='bundle_changed'):
        service.validate_bundle(bundle, SCOPE, scene=None, text='预算')


def test_expiry_and_disable_each_reject_the_previously_frozen_selected_set(tmp_path):
    clock = ['2026-11-03T12:00:00Z']
    _, service = setup_service(tmp_path, now=lambda: clock[0])
    first = save(service, 'limited', valid_until='2026-11-03T13:00:00Z')
    bundle = service.prepare_bundle(SCOPE, scene=None, text='提问')
    clock[0] = '2026-11-03T13:00:00Z'
    with pytest.raises(ProjectConstraintError, match='bundle_changed'):
        service.validate_bundle(bundle, SCOPE, scene=None, text='提问')
    clock[0] = '2026-11-03T12:00:00Z'
    save(service, 'limited', revision=first['revision'], enabled=False)
    with pytest.raises(ProjectConstraintError, match='bundle_changed'):
        service.validate_bundle(bundle, SCOPE, scene=None, text='提问')


def test_consistent_sqlite_read_keeps_body_and_scope_from_the_same_committed_revision(tmp_path):
    records, service = setup_service(tmp_path)
    first = save(service, 'rule', '会议要求', mode='scene', scene='会议')
    with records.begin_read() as tx:
        before = service._list(tx, SCOPE)
        save(service, 'rule', '周报要求', revision=first['revision'], scope_revision=first['scope_revision'],
            mode='mention', keywords=['周报'])
        during = service._list(tx, SCOPE)
    assert before == during
    after = service.list(SCOPE)['items'][0]
    assert after['content'] == '周报要求'
    assert after['activation']['mode'] == 'mention'


def test_two_real_connections_cannot_both_update_the_same_body_and_scope_revision(tmp_path):
    records, service = setup_service(tmp_path)
    first = save(service, 'rule', mode='scene', scene='会议')
    barrier = Barrier(2)
    def change(keyword):
        other = ProjectConstraintScopes(SQLiteStructuredRecordStore(records.database_path),
            namespace='constraints-qa', owner_user_id='qa-user', origin_kind='personal')
        barrier.wait()
        try:
            return save(other, 'rule', keyword, revision=first['revision'], scope_revision=first['scope_revision'],
                mode='mention', keywords=[keyword])
        except ProjectConstraintError:
            return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(change, ['周报', '预算']))
    assert sum(row is not None for row in results) == 1
    final = service.list(SCOPE)['items'][0]
    assert final['content'] == final['activation']['keywords'][0]


def test_namespace_owner_and_root_identity_are_part_of_the_bundle(tmp_path):
    records, service = setup_service(tmp_path)
    save(service, 'rule')
    bundle = service.prepare_bundle(SCOPE, scene=None, text='提问')
    assert service.binding_ref('turn-qa')['uri'] == 'crp://constraints-qa/project-constraints/turn-qa'
    other = ProjectConstraintScopes(records, namespace='other-namespace', owner_user_id='qa-user', origin_kind='personal')
    with pytest.raises(ProjectConstraintError, match='bundle_changed'):
        other.validate_bundle(bundle, SCOPE, scene=None, text='提问')
    wrong_user = ProjectConstraintScopes(records, namespace='constraints-qa', owner_user_id='other-user', origin_kind='personal')
    with pytest.raises(ProjectConstraintError, match='bundle_changed'):
        wrong_user.validate_bundle(bundle, SCOPE, scene=None, text='提问')
    path = Path(records.database_path)
    old = path.with_name('old-records.sqlite3')
    path.rename(old)
    shutil.copyfile(old, path)
    with pytest.raises(ProjectConstraintError, match='root_changed'):
        service.validate_bundle(bundle, SCOPE, scene=None, text='提问')


def test_unknown_modes_and_unavailable_shared_owner_reject_without_writes(tmp_path):
    records, service = setup_service(tmp_path)
    with pytest.raises(ValueError, match='mode_invalid'):
        save(service, 'bad', mode='unknown')
    with pytest.raises(ProjectConstraintError, match='shared_constraint_owner_unavailable'):
        ProjectConstraintScopes(records, namespace='constraints-qa', owner_user_id='qa-user', origin_kind='shared')
    assert records.read('project_constraints', 'bad') is None


def test_unknown_stored_policy_and_tampered_prompt_cannot_pass_validation(tmp_path):
    records, service = setup_service(tmp_path)
    save(service, 'rule', mode='scene', scene='会议')
    bundle = service.prepare_bundle(SCOPE, scene='会议', text='提问')
    tampered = {**bundle, 'prompt': '修改后的约束'}
    with pytest.raises(ProjectConstraintError, match='bundle_changed'):
        service.validate_bundle(tampered, SCOPE, scene='会议', text='提问')
    row = records.read(SCOPES, 'rule')
    with records.begin() as tx:
        tx.put(SCOPES, 'rule', {**row.payload, 'policy_version': '@999'}, expected_revision=row.revision)
        tx.commit()
    with pytest.raises(ProjectConstraintError, match='policy_unavailable'):
        service.prepare_bundle(SCOPE, scene='会议', text='提问')


def test_prepared_admission_uses_actual_namespace_and_performs_no_sidecar_write(tmp_path):
    records, service = setup_service(tmp_path)
    save(service, 'rule')
    before = records.list(SCOPES)
    item = service.prepare_admission('turn-new', SCOPE, scene=None, text='提问')
    assert item['kind'] == 'project-constraints-v1'
    assert item['ref']['uri'] == 'crp://constraints-qa/project-constraints/turn-new'
    assert service.validate_admission_payload(item['payload'], 'turn-new', SCOPE,
        scene=None, text='提问')['prompt'] == item['payload']['bundle']['prompt']
    assert records.list(SCOPES) == before
    with pytest.raises(ProjectConstraintError, match='admission_changed'):
        service.validate_admission_payload(item['payload'], 'turn-other', SCOPE, scene=None, text='提问')
    with pytest.raises(ProjectConstraintError, match='bundle_changed'):
        service.validate_admission_payload(item['payload'], 'turn-new', SCOPE, scene=None, text='另一原文')


def test_mode_fields_and_duplicate_keywords_fail_before_any_domain_write(tmp_path):
    records, service = setup_service(tmp_path)
    for changes in ({'mode': []}, {'mode': 'always', 'keywords': ['周报']},
                    {'mode': 'mention', 'keywords': ['API', 'api']},
                    {'mode': 'scene', 'scene': None}):
        with pytest.raises(ValueError, match='constraint_'):
            save(service, 'invalid-rule', **changes)
    assert records.read('project_constraints', 'invalid-rule') is None
