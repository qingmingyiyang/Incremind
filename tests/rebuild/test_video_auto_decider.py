from types import SimpleNamespace

from core.product_core.video_auto_decider import (
    VideoAutoFacts,
    decide_video_auto_workflow,
)


READY = {"status": "ready", "provider_name": "local", "reason": None, "next_step": None}


def test_video_decider_emits_steps_without_executing_handlers() -> None:
    facts = VideoAutoFacts(source_id="source-1", project_id="project-1", summary_readiness=READY)
    assert decide_video_auto_workflow(facts).next_step == "extract_audio"

    facts = VideoAutoFacts(
        source_id="source-1", project_id="project-1", summary_readiness=READY,
        audio=SimpleNamespace(status="completed", audio_asset_id="asset-1", job_id="job-a", output_id="out-a", error=None),
    )
    assert decide_video_auto_workflow(facts).next_step == "transcribe_audio"


def test_video_decider_completes_from_immutable_outcome_facts() -> None:
    decision = decide_video_auto_workflow(VideoAutoFacts(
        source_id="source-1", project_id=None, summary_readiness=READY,
        audio=SimpleNamespace(status="completed", audio_asset_id="asset-1", job_id="job-a", output_id="out-a", error=None),
        transcript=SimpleNamespace(status="completed", audio_asset_id="asset-1", job_id="job-t", output_id="transcript-1", error=None),
        summary=SimpleNamespace(status="completed", job_id="job-s", output_id="summary-1", error=None, creates_memory_candidate=False, candidate_ids=()),
        candidate=SimpleNamespace(status="candidate_created", candidate_id="candidate-1"),
    ))

    assert decision.next_step is None
    assert decision.result is not None
    assert decision.result.status == "completed"
    assert decision.result.memory_candidate_id == "candidate-1"
    assert decision.result.memory_publication == "candidate_created_not_published"


def test_video_decider_records_domain_failure_without_retrying() -> None:
    decision = decide_video_auto_workflow(VideoAutoFacts(
        source_id="source-1", project_id=None, summary_readiness=READY,
        failed_step="extract_audio", failure="provider unavailable",
    ))

    assert decision.result is not None
    assert decision.result.status == "blocked"
    assert decision.result.error == "provider unavailable"
    assert decision.result.steps[0].name == "extract_audio"
