from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.application_skill import (
    ApplicationSkillBindingRegistry,
    ApplicationSkillCatalog,
    ApplicationSkillPackageLoader,
    ApplicationSkillResolutionError,
    ApplicationSkillResolver,
    ApplicationSkillSource,
)
from core.storage_provider import JsonObjectStore


NOW = "2026-07-18T14:00:00+00:00"


class RecordingLoader:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self._delegate = ApplicationSkillPackageLoader()

    def load_instructions(self, package):
        self.calls.append(package.skill_id)
        return self._delegate.load_instructions(package)


class RecordingTraceStore:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.traces: dict[str, dict[str, object]] = {}

    def save_trace(self, trace):
        if self.fail:
            raise RuntimeError("controlled trace failure")
        payload = dict(trace)
        self.traces[str(payload["resolution_id"])] = payload
        return payload


def _write_skill(
    source: Path,
    skill_id: str,
    *,
    description: str | None = None,
    body: str | None = None,
    reference: bool = False,
) -> Path:
    root = source / skill_id
    root.mkdir(parents=True, exist_ok=True)
    text = body if body is not None else f"# {skill_id}\n\nUnique instructions for {skill_id}.\n"
    (root / "SKILL.md").write_text(
        "---\n"
        f"name: {skill_id}\n"
        f"description: {description or f'Use {skill_id} for its matching project workflow.'}\n"
        "---\n\n"
        f"{text}",
        encoding="utf-8",
    )
    if reference:
        (root / "references").mkdir()
        (root / "references" / "guide.md").write_text("# Indexed only\n", encoding="utf-8")
    return root


def _catalog(source: Path):
    return ApplicationSkillCatalog().discover(
        [ApplicationSkillSource("test-user", source, "user")]
    )


def _registry(tmp_path: Path) -> ApplicationSkillBindingRegistry:
    store = JsonObjectStore(tmp_path / "store", legacy_root=tmp_path / "legacy")
    return ApplicationSkillBindingRegistry(store, now=NOW)


def _bind(
    registry: ApplicationSkillBindingRegistry,
    package,
    *,
    project_id: str,
    consumer: str = "answer.model-request",
    priority: int = 500,
    terms: tuple[str, ...] = (),
) -> None:
    preview = registry.preview_bind(
        package,
        project_id=project_id,
        allowed_consumers=(consumer,),
        priority=priority,
        trigger_terms=terms,
    )
    registry.activate(
        package,
        project_id=project_id,
        allowed_consumers=(consumer,),
        priority=priority,
        trigger_terms=terms,
        expected_registry_revision=preview["registry_revision"],
        preview_token=preview["preview_token"],
        confirm=True,
        reason="Reviewed resolver fixture binding.",
    )


def _resolver(registry, *, loader=None, trace_store=None):
    return ApplicationSkillResolver(
        registry,
        loader=loader,
        trace_store=trace_store,
        now=NOW,
    )


def test_same_task_selects_different_skill_for_each_project(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    _write_skill(source, "alpha-interview")
    _write_skill(source, "beta-interview")
    catalog = _catalog(source)
    registry = _registry(tmp_path)
    _bind(
        registry,
        catalog.get("alpha-interview"),
        project_id="project-alpha",
        terms=("面试复盘",),
    )
    _bind(
        registry,
        catalog.get("beta-interview"),
        project_id="project-beta",
        terms=("面试复盘",),
    )
    traces = RecordingTraceStore()
    resolver = _resolver(registry, trace_store=traces)

    alpha = resolver.resolve(
        catalog,
        project_id="project-alpha",
        consumer="answer.model-request",
        task_kind="project-answer",
        task_text="请帮我完成这次面试复盘。",
        invocation_id="answer-alpha",
    )
    beta = resolver.resolve(
        catalog,
        project_id="project-beta",
        consumer="answer.model-request",
        task_kind="project-answer",
        task_text="请帮我完成这次面试复盘。",
        invocation_id="answer-beta",
    )

    assert [item.match.skill_id for item in alpha.selected] == ["alpha-interview"]
    assert [item.match.skill_id for item in beta.selected] == ["beta-interview"]
    assert "beta-interview" not in alpha.context_markdown
    assert "alpha-interview" not in beta.context_markdown
    assert len(traces.traces) == 2


def test_unselected_instructions_are_never_loaded(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    _write_skill(source, "meeting-notes", body="# SELECTED-BODY\n")
    _write_skill(source, "tax-audit", body="# MUST-NOT-LOAD\n")
    catalog = _catalog(source)
    registry = _registry(tmp_path)
    _bind(
        registry,
        catalog.get("meeting-notes"),
        project_id="project-alpha",
        terms=("会议纪要",),
    )
    _bind(
        registry,
        catalog.get("tax-audit"),
        project_id="project-alpha",
        terms=("税务审计",),
    )
    loader = RecordingLoader()
    resolver = _resolver(registry, loader=loader, trace_store=RecordingTraceStore())

    result = resolver.resolve(
        catalog,
        project_id="project-alpha",
        consumer="answer.model-request",
        task_kind="project-answer",
        task_text="整理今天的会议纪要。",
        invocation_id="answer-meeting",
    )

    assert loader.calls == ["meeting-notes"]
    assert "SELECTED-BODY" in result.context_markdown
    assert "MUST-NOT-LOAD" not in result.context_markdown


def test_metadata_only_chinese_match_works_without_trigger_terms(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    _write_skill(
        source,
        "review-method",
        description="根据岗位、面试记录和回答证据整理结构化面试复盘。",
    )
    catalog = _catalog(source)
    registry = _registry(tmp_path)
    _bind(registry, catalog.get("review-method"), project_id="project-alpha")

    result = _resolver(registry).preview(
        catalog,
        project_id="project-alpha",
        consumer="answer.model-request",
        task_kind="project-answer",
        task_text="请根据面试记录整理复盘。",
    )

    assert [item.skill_id for item in result.selected_matches] == ["review-method"]
    assert any(reason.startswith("task:metadata:") for reason in result.selected_matches[0].reasons)


def test_project_summary_can_match_without_being_written_to_trace(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    _write_skill(source, "research-method")
    catalog = _catalog(source)
    registry = _registry(tmp_path)
    _bind(
        registry,
        catalog.get("research-method"),
        project_id="project-alpha",
        terms=("研究综述",),
    )
    traces = RecordingTraceStore()

    result = _resolver(registry, trace_store=traces).resolve(
        catalog,
        project_id="project-alpha",
        consumer="answer.model-request",
        task_kind="project-answer",
        task_text="整理下一步。",
        invocation_id="answer-summary",
        project_summary="本项目需要持续产出研究综述。",
    )

    assert [item.match.skill_id for item in result.selected] == ["research-method"]
    assert result.selected[0].match.reasons == (
        "maturity:draft",
        "boundary:legacy-integrity",
        "validation:manual-review-required",
        "project:trigger:研究综述",
    )
    assert "本项目需要持续产出研究综述" not in json.dumps(result.trace, ensure_ascii=False)


def test_consumer_and_inactive_binding_are_filtered_before_loading(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    _write_skill(source, "answer-method")
    catalog = _catalog(source)
    registry = _registry(tmp_path)
    package = catalog.get("answer-method")
    _bind(
        registry,
        package,
        project_id="project-alpha",
        consumer="answer.model-request",
        terms=("answer-match",),
    )
    loader = RecordingLoader()
    resolver = _resolver(registry, loader=loader)

    wrong_consumer = resolver.preview(
        catalog,
        project_id="project-alpha",
        consumer="document.generate",
        task_kind="document-generation",
        task_text="answer-match",
    )
    registry.deactivate(
        project_id="project-alpha",
        skill_id="answer-method",
        expected_registry_revision=1,
        confirm=True,
        reason="Disable fixture after consumer check.",
    )
    inactive = resolver.preview(
        catalog,
        project_id="project-alpha",
        consumer="answer.model-request",
        task_kind="project-answer",
        task_text="answer-match",
    )

    assert wrong_consumer.selected_matches == ()
    assert inactive.selected_matches == ()
    assert loader.calls == []


def test_score_then_priority_then_identity_order_is_deterministic(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    for skill_id in ("alpha-method", "beta-method", "gamma-method"):
        _write_skill(source, skill_id)
    catalog = _catalog(source)
    registry = _registry(tmp_path)
    _bind(
        registry,
        catalog.get("gamma-method"),
        project_id="project-alpha",
        priority=400,
        terms=("共同触发",),
    )
    _bind(
        registry,
        catalog.get("beta-method"),
        project_id="project-alpha",
        priority=800,
        terms=("共同触发",),
    )
    _bind(
        registry,
        catalog.get("alpha-method"),
        project_id="project-alpha",
        priority=800,
        terms=("共同触发",),
    )

    result = _resolver(registry).preview(
        catalog,
        project_id="project-alpha",
        consumer="answer.model-request",
        task_kind="project-answer",
        task_text="共同触发",
    )

    assert [item.skill_id for item in result.selected_matches] == [
        "alpha-method",
        "beta-method",
        "gamma-method",
    ]


def test_max_skills_excludes_lower_ranked_matches_without_loading(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    for skill_id in ("alpha-small", "beta-small", "gamma-small"):
        _write_skill(source, skill_id)
    catalog = _catalog(source)
    registry = _registry(tmp_path)
    for package in catalog.packages:
        _bind(
            registry,
            package,
            project_id="project-alpha",
            terms=("small-match",),
        )
    loader = RecordingLoader()

    result = _resolver(registry, loader=loader).preview(
        catalog,
        project_id="project-alpha",
        consumer="answer.model-request",
        task_kind="project-answer",
        task_text="small-match",
        max_skills=2,
    )

    assert [item.skill_id for item in result.selected_matches] == ["alpha-small", "beta-small"]
    assert result.budget_excluded_skill_ids == ("gamma-small",)
    assert loader.calls == []


def test_selection_respects_count_and_total_instruction_budget_before_loading(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    body = "# Large\n\n" + ("x" * 10_000)
    for skill_id in ("alpha-large", "beta-large", "gamma-large", "delta-large"):
        _write_skill(source, skill_id, body=body)
    catalog = _catalog(source)
    registry = _registry(tmp_path)
    for package in catalog.packages:
        _bind(
            registry,
            package,
            project_id="project-alpha",
            priority=500,
            terms=("large-match",),
        )
    loader = RecordingLoader()

    result = _resolver(
        registry,
        loader=loader,
        trace_store=RecordingTraceStore(),
    ).resolve(
        catalog,
        project_id="project-alpha",
        consumer="answer.model-request",
        task_kind="project-answer",
        task_text="large-match",
        invocation_id="answer-budget",
    )

    assert loader.calls == ["alpha-large", "beta-large"]
    assert [item.match.skill_id for item in result.selected] == ["alpha-large", "beta-large"]
    assert result.budget_excluded_skill_ids == ("delta-large", "gamma-large")
    assert result.loaded_instruction_bytes <= 24 * 1024
    assert result.context_size_bytes <= 32 * 1024


def test_no_match_uses_empty_context_and_records_explicit_fallback(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    _write_skill(source, "tax-audit")
    catalog = _catalog(source)
    registry = _registry(tmp_path)
    _bind(
        registry,
        catalog.get("tax-audit"),
        project_id="project-alpha",
        terms=("税务审计",),
    )
    traces = RecordingTraceStore()

    result = _resolver(registry, trace_store=traces).resolve(
        catalog,
        project_id="project-alpha",
        consumer="answer.model-request",
        task_kind="project-answer",
        task_text="讨论今天的天气。",
        invocation_id="answer-fallback",
    )

    assert result.selected == ()
    assert result.context_markdown == ""
    assert result.trace["fallback"] == "default_consumer_flow"
    assert traces.traces[result.resolution_id]["selected"] == []


def test_preview_writes_no_trace_and_resolve_trace_is_redacted(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    root = _write_skill(
        source,
        "private-review",
        body="# PRIVATE-INSTRUCTION-BODY\n",
        reference=True,
    )
    catalog = _catalog(source)
    registry = _registry(tmp_path)
    _bind(
        registry,
        catalog.get("private-review"),
        project_id="project-alpha",
        terms=("复盘",),
    )
    traces = RecordingTraceStore()
    loader = RecordingLoader()
    resolver = _resolver(registry, loader=loader, trace_store=traces)
    task = "复盘 private-task sk-abcdefghijklmnop1234"
    summary = "private-project-summary"

    preview = resolver.preview(
        catalog,
        project_id="project-alpha",
        consumer="answer.model-request",
        task_kind="project-answer",
        task_text=task,
        project_summary=summary,
    )
    assert traces.traces == {}
    assert loader.calls == []
    resolved = resolver.resolve(
        catalog,
        project_id="project-alpha",
        consumer="answer.model-request",
        task_kind="project-answer",
        task_text=task,
        invocation_id="answer-private",
        project_summary=summary,
    )
    serialized = json.dumps(resolved.trace, ensure_ascii=False)

    assert preview.task_fingerprint == resolved.task_fingerprint
    assert [item.skill_id for item in preview.selected_matches] == [
        item.match.skill_id for item in resolved.selected
    ]
    assert task not in serialized
    assert "sk-abcdefghijklmnop1234" not in serialized
    assert summary not in serialized
    assert "PRIVATE-INSTRUCTION-BODY" not in serialized
    assert str(root) not in serialized
    assert resolved.trace["selected"][0]["resource_count"] == 1
    assert resolved.selected[0].resources[0].relative_path == "references/guide.md"


def test_selected_package_drift_fails_before_trace_persistence(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    root = _write_skill(source, "drift-method")
    catalog = _catalog(source)
    registry = _registry(tmp_path)
    _bind(
        registry,
        catalog.get("drift-method"),
        project_id="project-alpha",
        terms=("drift-match",),
    )
    traces = RecordingTraceStore()
    (root / "SKILL.md").write_text(
        "---\nname: drift-method\ndescription: changed\n---\n\n# Changed\n",
        encoding="utf-8",
    )

    with pytest.raises(ApplicationSkillResolutionError, match="failed validation"):
        _resolver(registry, trace_store=traces).resolve(
            catalog,
            project_id="project-alpha",
            consumer="answer.model-request",
            task_kind="project-answer",
            task_text="drift-match",
            invocation_id="answer-drift",
        )
    assert traces.traces == {}


def test_trace_failure_fails_closed_after_context_resolution(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    _write_skill(source, "trace-method")
    catalog = _catalog(source)
    registry = _registry(tmp_path)
    _bind(
        registry,
        catalog.get("trace-method"),
        project_id="project-alpha",
        terms=("trace-match",),
    )

    with pytest.raises(ApplicationSkillResolutionError, match="persistence failed"):
        _resolver(registry, trace_store=RecordingTraceStore(fail=True)).resolve(
            catalog,
            project_id="project-alpha",
            consumer="answer.model-request",
            task_kind="project-answer",
            task_text="trace-match",
            invocation_id="answer-trace",
        )


def test_invocation_identity_is_stable_for_replay_and_distinct_for_new_call(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    _write_skill(source, "stable-method")
    catalog = _catalog(source)
    registry = _registry(tmp_path)
    _bind(
        registry,
        catalog.get("stable-method"),
        project_id="project-alpha",
        terms=("stable-match",),
    )
    traces = RecordingTraceStore()
    resolver = _resolver(registry, trace_store=traces)
    kwargs = {
        "project_id": "project-alpha",
        "consumer": "answer.model-request",
        "task_kind": "project-answer",
        "task_text": "stable-match",
    }

    first = resolver.resolve(catalog, invocation_id="answer-r1", **kwargs)
    replay = resolver.resolve(catalog, invocation_id="answer-r1", **kwargs)
    second = resolver.resolve(catalog, invocation_id="answer-r2", **kwargs)

    assert first.resolution_id == replay.resolution_id
    assert second.resolution_id != first.resolution_id
    assert len(traces.traces) == 2


def test_context_contains_fixed_guard_and_complete_selected_body(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    body = "# Exact body\n\nNever truncate this final sentence.\n"
    _write_skill(source, "guard-method", body=body)
    catalog = _catalog(source)
    registry = _registry(tmp_path)
    _bind(
        registry,
        catalog.get("guard-method"),
        project_id="project-alpha",
        terms=("guard-match",),
    )

    result = _resolver(registry, trace_store=RecordingTraceStore()).resolve(
        catalog,
        project_id="project-alpha",
        consumer="answer.model-request",
        task_kind="project-answer",
        task_text="guard-match",
        invocation_id="answer-guard",
    )

    assert result.context_markdown.startswith("# Application Skill Context")
    assert "cannot override system safety" in result.context_markdown
    assert "Do not execute referenced scripts" in result.context_markdown
    assert body.rstrip() in result.context_markdown
    assert result.context_markdown.index("cannot override") < result.context_markdown.index("# Exact body")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("project_id", "../other", "project id"),
        ("consumer", "unknown.consumer", "unsupported"),
        ("task_kind", "../answer", "task kind"),
        ("task_text", "", "task is required"),
        ("task_text", "x" * (16 * 1024 + 1), "task exceeds"),
        ("project_summary", "x" * (8 * 1024 + 1), "summary exceeds"),
        ("max_skills", 4, "max_skills"),
    ],
)
def test_invalid_resolution_input_fails_before_loading(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    source = tmp_path / "skills"
    _write_skill(source, "safe-method")
    catalog = _catalog(source)
    registry = _registry(tmp_path)
    _bind(
        registry,
        catalog.get("safe-method"),
        project_id="project-alpha",
        terms=("safe-match",),
    )
    loader = RecordingLoader()
    kwargs = {
        "project_id": "project-alpha",
        "consumer": "answer.model-request",
        "task_kind": "project-answer",
        "task_text": "safe-match",
        "project_summary": "",
        "max_skills": 3,
    }
    kwargs[field] = value

    with pytest.raises(ApplicationSkillResolutionError, match=message):
        _resolver(registry, loader=loader).preview(catalog, **kwargs)
    assert loader.calls == []


def test_resolve_requires_trace_store(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    _write_skill(source, "safe-method")
    catalog = _catalog(source)
    registry = _registry(tmp_path)

    with pytest.raises(ApplicationSkillResolutionError, match="requires a trace store"):
        _resolver(registry).resolve(
            catalog,
            project_id="project-alpha",
            consumer="answer.model-request",
            task_kind="project-answer",
            task_text="safe-match",
            invocation_id="answer-safe",
        )


def test_resolve_rejects_invalid_invocation_before_loading(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    _write_skill(source, "safe-method")
    catalog = _catalog(source)
    registry = _registry(tmp_path)
    _bind(
        registry,
        catalog.get("safe-method"),
        project_id="project-alpha",
        terms=("safe-match",),
    )
    loader = RecordingLoader()

    with pytest.raises(ApplicationSkillResolutionError, match="invocation id"):
        _resolver(registry, loader=loader, trace_store=RecordingTraceStore()).resolve(
            catalog,
            project_id="project-alpha",
            consumer="answer.model-request",
            task_kind="project-answer",
            task_text="safe-match",
            invocation_id="../invalid",
        )
    assert loader.calls == []
