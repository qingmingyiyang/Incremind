from core.memory_core import SQLiteMemoryReader
from core.storage_provider import SQLiteStructuredRecordStore


def test_sqlite_memory_reader_reads_current_projection_without_json(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / "structured.sqlite3")
    with records.begin() as transaction:
        transaction.put("memory_series_memory", "series-1", {"id": "series-1", "project_ids": ["project-1"], "source_refs": [{"source_id": "source-1", "locator": "0"}], "updated_at": "3"}, expected_revision=0)
        transaction.put("memory_scenarios", "scenario-1", {"id": "scenario-1", "project_id": "project-1", "atom_ids": ["atom-1"], "source_refs": [{"source_id": "source-1", "locator": "1"}], "updated_at": "2"}, expected_revision=0)
        transaction.put("memory_atoms", "atom-1", {"id": "atom-1", "source_refs": [{"source_id": "source-1", "locator": "2"}], "updated_at": "1"}, expected_revision=0)
        transaction.commit()
    reader = SQLiteMemoryReader(records)
    assert reader.get("series_memory", "series-1")["id"] == "series-1"
    assert [item["id"] for item in reader.list_by_source("source-1")] == ["atom-1", "scenario-1", "series-1"]
    assert [item["id"] for item in reader.list_by_project("project-1")] == ["series-1", "scenario-1", "atom-1"]
