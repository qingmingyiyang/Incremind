import pytest

from backend.memory_app import workspace_intake as intake


def test_dispatch_table_owns_all_existing_acquisition_types():
    assert set(intake.SOURCE_READERS) == {"text", "file", "link", "audio", "bilibili", "xiaohongshu", "image", "video"}
    assert all(callable(reader) for reader in intake.SOURCE_READERS.values())


@pytest.mark.parametrize("kind", ["text", "file", "link", "unknown"])
def test_existing_text_is_read_without_model_or_external_acquisition(kind, tmp_path):
    import asyncio
    owner = intake.WorkspaceIntake(tmp_path, None, None)
    item = {"input_kind": kind, "source_text": "原文"}
    result, text = asyncio.run(owner.acquire_source(item, "alpha", "item", "run", None, None, lambda purpose: None))
    assert result is item
    assert text == "原文"


def test_audio_checkpoint_is_reused_without_asr(tmp_path):
    import asyncio
    owner = intake.WorkspaceIntake(tmp_path, None, None)
    item = {"input_kind": "audio", "source_text": "", "original_path": "missing.wav"}
    result, text = asyncio.run(owner.acquire_source(item, "alpha", "item", "run", None, {"text": "已转写原文"}, lambda purpose: None))
    assert result is item
    assert text == "已转写原文"
