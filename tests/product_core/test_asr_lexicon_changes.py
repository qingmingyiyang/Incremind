import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.product_core.asr_lexicon_service import AsrLexiconError, AsrLexiconService
from core.storage_provider import SQLiteStructuredRecordStore


def services(path):
    records = SQLiteStructuredRecordStore(path / '.rebuild-data' / 'structured-records.sqlite3')
    return AsrLexiconService(records, runtime_root=path), AsrLexiconService(records, runtime_root=path)


def apply(service, operations):
    return service.change_terms(operations=operations, expected_revision=service.current_revision(), preview=False)


def test_real_sqlite_preview_apply_rename_weight_retire_restore_and_shared_reader(tmp_path):
    service, settings = services(tmp_path)
    operations = [{'action': 'add', 'term': 'Incremind', 'weight': 5}, {'action': 'add', 'term': '词语甲'}]
    preview = service.change_terms(operations=operations, expected_revision=0)
    assert preview['preview'] is True and preview['revision'] == 0
    assert settings.read()['terms'] == [] and service.current_revision() == 0
    saved = apply(service, operations)
    assert saved['changes'] == preview['changes'] and saved['preview'] is False
    assert settings.read() == service.read()
    word = next(row for row in saved['terms'] if row['term'] == '词语甲')
    selected = service.select_for_session()
    result = apply(service, [{'action': 'rename', 'term': '词语甲', 'new_term': '词语乙', 'weight': 3}])
    renamed = next(row for row in result['terms'] if row['id'] == word['id'])
    assert renamed['term'] == '词语乙' and renamed['weight'] == 3 and renamed['status'] == 'active'
    assert any(term.term == '词语甲' for term in selected.terms)
    apply(service, [{'action': 'set_weight', 'term': '词语乙', 'weight': 4}])
    apply(service, [{'action': 'retire', 'term': '词语乙'}])
    assert all(term.term != '词语乙' for term in service.select_for_session().terms)
    apply(service, [{'action': 'restore', 'term': '词语乙'}])
    restored = next(row for row in settings.read()['terms'] if row['id'] == word['id'])
    assert restored['term'] == '词语乙' and restored['weight'] == 4 and restored['status'] == 'active'
    reopened, _ = services(tmp_path)
    assert reopened.read() == service.read()
    other, _ = services(tmp_path / 'other-user')
    assert other.read()['terms'] == []


def test_invalid_second_operation_and_revision_conflict_leave_real_database_unchanged(tmp_path):
    service, settings = services(tmp_path)
    apply(service, [{'action': 'add', 'term': '已有甲'}, {'action': 'add', 'term': '已有乙'}])
    original = settings.read()
    with pytest.raises(AsrLexiconError, match='asr_lexicon_term_exists'):
        apply(service, [{'action': 'add', 'term': '尚未保存'},
            {'action': 'rename', 'term': '已有甲', 'new_term': '已有乙'}])
    assert settings.read() == original
    with pytest.raises(AsrLexiconError, match='asr_lexicon_revision_conflict'):
        service.change_terms(operations=[{'action': 'retire', 'term': '已有甲'}], expected_revision=0, preview=False)
    assert settings.read() == original


@pytest.mark.parametrize('operations', [[], [{'action': 'sql', 'term': 'x'}],
    [{'action': 'add', 'term': 'x', 'weight': True}],
    [{'action': 'add', 'term': 'x', 'user_id': 'other'}],
    [{'action': 'add', 'term': 'x'}, {'action': 'retire', 'term': 'X'}],
    [{'action': 'rename', 'term': 'x'}]])
def test_strict_operation_schema_and_conflicts_do_not_write(tmp_path, operations):
    service, _ = services(tmp_path)
    with pytest.raises(AsrLexiconError):
        service.change_terms(operations=operations, expected_revision=0, preview=False)
    assert service.read()['revision'] == 0 and service.read()['terms'] == []


def test_retired_super_term_can_be_renamed_with_fifty_active_super_terms(tmp_path):
    service, _ = services(tmp_path)
    apply(service, [{'action': 'add', 'term': 'RetiredSuper', 'weight': 50}])
    apply(service, [{'action': 'retire', 'term': 'RetiredSuper'}])
    service.import_text(text=json.dumps([{'term': f'Super{number}', 'weight': 50} for number in range(50)]),
        format='json', expected_revision=service.current_revision(), preview=False)
    before = next(row for row in service.read()['terms'] if row['term'] == 'RetiredSuper')
    result = apply(service, [{'action': 'rename', 'term': 'RetiredSuper', 'new_term': 'RenamedSuper'}])
    after = next(row for row in result['terms'] if row['id'] == before['id'])
    assert after['term'] == 'RenamedSuper' and after['status'] == 'retired' and after['weight'] == 50
    assert len(service.select_for_session().terms) == 50
    original = service.read()
    with pytest.raises(AsrLexiconError, match='asr_lexicon_capacity_exceeded'):
        apply(service, [{'action': 'restore', 'term': 'RenamedSuper'}])
    assert service.read() == original


def test_actual_simultaneous_revision_claims_allow_one_change(tmp_path):
    first, second = services(tmp_path)
    def save(service, term):
        try:
            service.change_terms(operations=[{'action': 'add', 'term': term}], expected_revision=0, preview=False)
            return 'saved'
        except AsrLexiconError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda args: save(*args), [(first, 'First'), (second, 'Second')]))
    assert sorted(outcomes) == ['asr_lexicon_revision_conflict', 'saved']
    assert len(first.read()['terms']) == 1
