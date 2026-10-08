"""Repeated migration must retain unresolved evidence from historical imports."""
from copy import deepcopy
import pytest
from backend.memory_app.migration_bundle import MigrationBundleService
from backend.recognition import RecognitionConflict
from tests.memory_app.test_migration_evidence_eligibility import _import
from tests.recognition.test_artifact_dependencies import env, _publish

def test_revision_mismatch_cannot_be_laundered_through_second_import(env, tmp_path):
    basis = env.service.stage_experience(scope=env.scope, content='basis revision one',
        provenance={'kind': 'user_statement'})
    with env.records.begin() as tx:
        original = tx.read('recognition_experiences', basis)
        tx.put('recognition_experiences', basis, {**original.payload, 'content': 'basis revision two'},
               expected_revision=original.revision)
        tx.commit()
    resolved_basis = env.service.stage_experience(scope=env.scope, content='independent resolved basis',
        provenance={'kind': 'user_statement'})
    statement = env.service.stage_experience(scope=env.scope, content='Descriptive reference to the older basis.',
        provenance={'kind': 'user_statement', 'source_refs': [
            {'type': 'experience', 'id': basis, 'revision': 1},
            {'type': 'experience', 'id': resolved_basis, 'revision': 1}]})
    source = _publish(env, 'descriptive-reference', experiences=[basis, statement, resolved_basis])
    assert source.authorized  # Live descriptive refs intentionally retain existing semantics.
    bundle = MigrationBundleService(env.records).export(env.scope, [source.id], [basis, statement, resolved_basis])['bundle']
    records1, service1, scope1, plan1 = _import(env, tmp_path / 'first', bundle)
    assert any(issue['code'] == 'source_revision_mismatch' and issue['location'] == 'provenance'
               for issue in plan1['issues'])
    first_statement = plan1['mapping']['experiences'][statement]
    with pytest.raises(RecognitionConflict):
        service1.propose(scope=scope1, content='must remain blocked', source_experience_ids=[first_statement])

    first_rid = plan1['mapping']['recognitions'][source.id]
    before = records1.list_all()
    exported = MigrationBundleService(records1).export(scope1, [first_rid],
        list(plan1['mapping']['experiences'].values()))['bundle']
    assert records1.list_all() == before
    refs = next(row for row in exported['experiences'] if row['id'] == first_statement)['payload']['provenance']['source_refs']
    assert {'type': 'experience', 'id': plan1['mapping']['experiences'][resolved_basis], 'revision': 1} in refs
    records2, service2, scope2, plan2 = _import(env, tmp_path / 'second', exported)
    second_statement = plan2['mapping']['experiences'][first_statement]
    print('second import provenance issues:', [i for i in plan2['issues'] if i.get('location') == 'provenance'])
    # Even though the imported recognition stays stale, its experience must
    # not become a route for publishing a fresh authorized recognition.
    with pytest.raises(RecognitionConflict):
        candidate = service2.propose(scope=scope2, content='fresh conclusion from unresolved import',
            source_experience_ids=[second_statement])
        result = service2.publish(scope=scope2, candidate_id=candidate.id,
            expected_revision=candidate.revision, reviewer='user')
        print('unexpected new recognition eligibility:', result.authorized)

def test_resolved_revision_two_refs_remain_eligible_across_migration(env, tmp_path):
    basis = env.service.stage_experience(scope=env.scope, content='basis', provenance={'kind': 'user_statement'})
    with env.records.begin() as tx:
        row = tx.read('recognition_experiences', basis)
        tx.put('recognition_experiences', basis, {**row.payload, 'content': 'second revision'}, expected_revision=row.revision)
        tx.commit()
    statement = env.service.stage_experience(scope=env.scope, content='Resolved descriptive reference.',
        provenance={'kind': 'user_statement', 'source_refs': [{'type': 'experience', 'id': basis, 'revision': 2}]})
    current = _publish(env, 'resolved', experiences=[basis, statement])
    records, scope = env.records, env.scope
    experiences = [basis, statement]
    for hop in ('first', 'second'):
        bundle = MigrationBundleService(records).export(scope, [current.id], experiences)['bundle']
        records, service, scope, plan = _import(env, tmp_path / hop, bundle)
        current = service.get_recognition(scope=scope, recognition_id=plan['mapping']['recognitions'][current.id])
        experiences = list(plan['mapping']['experiences'].values())
        assert current.authorized and current.state == 'active'
        assert not any(issue.get('code') in {'external_provenance_reference', 'source_revision_mismatch'}
            and issue.get('location') == 'provenance' for issue in plan['issues'])

def test_legacy_persisted_normalization_cannot_be_laundered(env, tmp_path, monkeypatch):
    """First import deliberately models pre-repair persisted rows; later paths use production."""
    import sys
    from backend.memory_app import migration_plan
    original_import = _import
    calls = []
    def old_map(provenance, mapping, record_id, import_id, issues):
        result = deepcopy(dict(provenance))
        refs = []
        for index, ref in enumerate(result.get('source_refs', ())):
            updated = dict(ref)
            group = 'experiences' if ref.get('type') == 'experience' else 'recognitions' if ref.get('type') == 'recognition' else None
            if group and ref.get('id') in mapping[group]:
                updated['id'] = mapping[group][ref['id']]
                if 'revision' in updated:
                    updated['revision'] = 1
            else:
                updated['id'] = migration_plan._placeholder(import_id, 'provenance', index)
                issues.append({'code': 'external_provenance_reference', 'record_id': record_id,
                    'source_type': ref.get('type'), 'source_id': ref.get('id'), 'location': 'provenance'})
            refs.append(updated)
        result['source_refs'] = refs
        return result
    def first_import_with_legacy_mapping(*args, **kwargs):
        if not calls:
            with monkeypatch.context() as first:
                first.setattr(migration_plan, '_map_provenance', old_map)
                result = original_import(*args, **kwargs)
        else:
            result = original_import(*args, **kwargs)
        calls.append(result)
        return result
    monkeypatch.setattr(sys.modules[__name__], '_import', first_import_with_legacy_mapping)
    test_revision_mismatch_cannot_be_laundered_through_second_import(env, tmp_path)


def test_same_source_good_and_bad_revisions_remain_distinct_on_reexport(env, tmp_path):
    basis = env.service.stage_experience(scope=env.scope, content='basis', provenance={'kind': 'user_statement'})
    with env.records.begin() as tx:
        row = tx.read('recognition_experiences', basis)
        tx.put('recognition_experiences', basis, {**row.payload, 'content': 'second revision'}, expected_revision=row.revision)
        tx.commit()
    statement = env.service.stage_experience(scope=env.scope, content='Compare the previous and current basis.',
        provenance={'kind': 'user_statement', 'source_refs': [
            {'type': 'experience', 'id': basis, 'revision': 1},
            {'type': 'experience', 'id': basis, 'revision': 2}]})
    current = _publish(env, 'mixed-revisions', experiences=[basis, statement])
    assert current.authorized
    bundle = MigrationBundleService(env.records).export(env.scope, [current.id], [basis, statement])['bundle']
    records, service, scope, plan = _import(env, tmp_path / 'first', bundle)
    first_statement = plan['mapping']['experiences'][statement]
    expected_good = {'type': 'experience', 'id': plan['mapping']['experiences'][basis], 'revision': 1}
    stored_refs = records.read('recognition_experiences', first_statement).payload['provenance']['source_refs']
    assert expected_good in stored_refs
    before = records.list_all()
    exported = MigrationBundleService(records).export(scope, [plan['mapping']['recognitions'][current.id]],
        list(plan['mapping']['experiences'].values()))['bundle']
    assert records.list_all() == before
    exported_refs = next(row for row in exported['experiences'] if row['id'] == first_statement)['payload']['provenance']['source_refs']
    assert expected_good in exported_refs
    assert len(exported_refs) == 2 and exported_refs[0]['id'] != exported_refs[1]['id']
    records2, service2, scope2, plan2 = _import(env, tmp_path / 'second', exported)
    with pytest.raises(RecognitionConflict):
        service2.propose(scope=scope2, content='must stay blocked',
            source_experience_ids=[plan2['mapping']['experiences'][first_statement]])
