"""Retired remote paths leave the retained local series service available."""
from backend.replay.series_workspace import SeriesWorkspace
from backend.replay.contracts import MemoryQuestionRequest


def test_retired_series_generation_methods_are_absent():
    assert not hasattr(SeriesWorkspace, "organize_intake")
    assert not hasattr(SeriesWorkspace, "merge_report")
    assert not hasattr(SeriesWorkspace, "merge_intake_to_report")


def test_series_memory_remains_local_and_records_the_session(tmp_path):
    workspace = SeriesWorkspace(tmp_path)
    answer = workspace.ask_memory("default", MemoryQuestionRequest(question="合成问题"))
    assert not answer.evidence_sufficient
    assert len(workspace.memory_session("default").messages) == 2
    call = workspace.memory_session("default").messages[-1].tool_calls[0]
    assert call["model_synthesis"] is False
    assert call["prompt_version"] == "local-retrieval-only"
