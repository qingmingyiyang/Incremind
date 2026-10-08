"""Independent old receipt compatibility probes, using existing fixtures."""
from copy import deepcopy
import pytest
from tests.memory_app import test_migration_roundtrip_eligibility as cases
from tests.recognition.test_artifact_dependencies import env


@pytest.mark.parametrize('scenario', ['mixed', 'mixed-reversed', 'legacy-normalized'])
def test_old_issues_without_revision_keep_only_unresolved_references(env, tmp_path, monkeypatch, scenario):
    original_import = cases._import
    imported = []

    def import_with_historical_receipt(test_env, target_path, bundle):
        value = deepcopy(bundle)
        if not imported and scenario == 'mixed-reversed':
            for row in value['experiences']:
                row['payload'].get('provenance', {}).get('source_refs', []).reverse()
        result = original_import(test_env, target_path, value)
        records, service, scope, plan = result
        if not imported:
            with records.begin() as tx:
                archive = tx.list('recognition_migration_imports')[0]
                payload = deepcopy(archive.payload)
                changed = 0
                for issue in payload['receipt']['issues']:
                    if issue.get('location') == 'provenance':
                        if issue.pop('source_revision', None) is not None:
                            changed += 1
                assert changed > 0
                tx.put('recognition_migration_imports', archive.object_id, payload,
                       expected_revision=archive.revision)
                tx.commit()
        imported.append(result)
        return result

    monkeypatch.setattr(cases, '_import', import_with_historical_receipt)
    if scenario == 'legacy-normalized':
        cases.test_legacy_persisted_normalization_cannot_be_laundered(env, tmp_path, monkeypatch)
    else:
        cases.test_same_source_good_and_bad_revisions_remain_distinct_on_reexport(env, tmp_path)
    assert len(imported) == 2
