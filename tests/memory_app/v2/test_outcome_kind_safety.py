"""A damaged feedback fact cannot stop the existing learning checkpoint."""
import pytest

from backend.memory_app.v2.learning_events import checkpoint, events
from core.storage_provider import SQLiteStructuredRecordStore
from tests.memory_app.v2.test_outcome_accumulation import _synthetic_division, _put


@pytest.mark.parametrize('kind', [[], {}], ids=['list', 'mapping'])
def test_unknown_unhashable_kind_preserves_facts_and_valid_learning(tmp_path, kind):
    records = SQLiteStructuredRecordStore(tmp_path / 'facts.sqlite3')
    damaged = _put(records, 'damaged-kind', {**_synthetic_division(), 'kind': kind})
    valid = _put(records, 'real-division', _synthetic_division())
    assert events(records) == {'alpha': {'outcome:real-division'}}
    state = checkpoint(records, 0)['alpha']
    assert state['score'] == 1 and state['seen_event_ids'] == ['outcome:real-division']
    assert checkpoint(records, 0)['alpha'] == state
    assert records.read('v2_outcome_corrections', damaged.object_id) == damaged
    assert records.read('v2_outcome_corrections', valid.object_id) == valid
