from backend.api.task_reference_projection import task_ref_for_workbench_content_transform
from core.task_reference_contract import task_updated_utc_key, workbench_transform_task_tie_key


def test_transform_candidate_tie_key_has_the_exact_public_reference_order_for_any_project() -> None:
    job_ids = ("-", "0", "9", "A", "Z", "_", "a", "a-", "a_", "aa", "job-0001", "job-0002")
    indexed_order = sorted(job_ids, key=workbench_transform_task_tie_key, reverse=True)

    for project_id in ("a", "project-a", "project.with_~chars"):
        public_order = sorted(
            job_ids,
            key=lambda job_id: task_ref_for_workbench_content_transform(
                project_id=project_id, job_id=job_id,
            ),
            reverse=True,
        )
        assert public_order == indexed_order


def test_task_updated_utc_key_matches_public_cursor_contract() -> None:
    assert task_updated_utc_key("2026-09-06T08:00:00+08:00") == "2026-09-06T00:00:00.000000+00:00"
    assert task_updated_utc_key("2026-09-06T00:00:00Z") == "2026-09-06T00:00:00.000000+00:00"
    assert task_updated_utc_key("2026-09-06T00:00:00") == "2026-09-06T00:00:00.000000+00:00"
    assert task_updated_utc_key("not-a-time") == "0001-01-01T00:00:00.000000+00:00"
    assert task_updated_utc_key(None) == "0001-01-01T00:00:00.000000+00:00"
