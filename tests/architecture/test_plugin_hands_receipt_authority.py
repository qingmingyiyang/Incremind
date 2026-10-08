from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_plugin_hands_reaper_verifier_reads_immutable_receipt_not_lifecycle_state() -> None:
    source = (ROOT / "src" / "core" / "plugin_hands" / "durable_lifecycle.py").read_text(encoding="utf-8")
    verifier = source.split("def verify_plugin_hands_effect", 1)[1].split("def backfill_plugin_hands_execution_effects", 1)[0]

    assert "_RECEIPTS" in verifier
    assert "_COLLECTION" not in verifier
    assert "record.state" not in verifier
    assert "raw.revision != 1" in verifier


def test_plugin_hands_outcome_fact_and_cleanup_projection_share_one_transaction() -> None:
    source = (ROOT / "src" / "core" / "plugin_hands" / "durable_lifecycle.py").read_text(encoding="utf-8")
    public = source.split("    def record_outcome", 1)[1].split("    def _record_outcome_fact", 1)[0]
    method = source.split("    def _record_outcome_fact", 1)[1].split("    def mark_cleaned", 1)[0]

    assert "with self._records.begin() as uow" in method
    assert "uow.put(" in method and "_RECEIPTS" in method and "_COLLECTION" in method
    assert "_RESULT_PAYLOADS" in method
    assert "result_payload_ref=result_payload_ref" in method
    assert "uow.commit()" in method
    assert public.index("self._record_outcome_fact(") < public.index("self._runner.settle_ok")


def test_plugin_hands_live_execution_is_claimed_and_settled_by_core_runner() -> None:
    source = (ROOT / "src" / "core" / "plugin_hands" / "durable_lifecycle.py").read_text(encoding="utf-8")
    method = source.split("    def execute(self, host:", 1)[1].split("    def handle_claimed", 1)[0]
    handler = source.split("    def handle_claimed", 1)[1].split("    def _resolve", 1)[0]

    assert "self._runner.execute(" in method
    assert "def handle(effect" not in method
    assert "self.handle_claimed(" in method
    assert "self._runner.begin_planned(" not in method
    assert "self._runner.settle_ok(" not in method
    assert "self.record_outcome(" not in method
    assert "self._record_outcome_fact(" in handler
    assert "_workspace_cleanup_intent(" in handler
    assert 'kind="plugin_hands_workspace_cleanup"' in source
    assert "parent_id=parent_id" in source
