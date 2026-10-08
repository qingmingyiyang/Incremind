from __future__ import annotations

from dataclasses import dataclass, replace
import json
import re
from typing import Mapping, Sequence

from core.context_graph import (
    ContextBinding,
    ContextCompiler,
    FrozenContextRevisions,
    TurnModelObservation,
    context_binding_model_projection,
    estimate_model_projection_tokens,
)
from core.context_graph import context_binding_to_payload

from .evaluation import run_structural_benchmark


_CONTEXT_EVALUATE_OUTCOME = "context.evaluate"
_DECODING_REVISION = "model-planner-json-t0-v1"
_MINIMUM_MEAN_QUALITY_GAIN = 0.15
_MAXIMUM_TOKEN_RATIO = 1.5


class ModelBenchmarkError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FrozenModelBenchmarkCase:
    case_id: str
    task_kind: str
    fixture_revision: str
    evaluator_revision: str
    decoding_revision: str
    instruction: str
    linear_context: str
    binding_template: ContextBinding
    required_terms: tuple[str, ...]
    required_source_refs: tuple[str, ...]
    forbidden_terms: tuple[str, ...]
    metric_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ModelBenefitResult:
    case_id: str
    linear_metrics: Mapping[str, float]
    linemap_metrics: Mapping[str, float]
    same_frozen_route: bool
    model_quality_verified: bool
    benefit_gate_passed: bool
    evidence: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ModelBenchmarkTurnPair:
    suite_run_id: str
    replicate_index: int
    case_id: str
    binding_creation: Mapping[str, object]
    linear_turn: Mapping[str, object]
    linemap_turn: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ModelBenchmarkSuiteResult:
    suite_run_id: str
    case_results: tuple[ModelBenefitResult, ...]
    all_required_cases_present: bool
    all_model_outputs_verified: bool
    all_case_gates_passed: bool
    native_canvas_eligible: bool
    aggregate_quality_gain: float
    aggregate_token_ratio: float
    hard_failures: tuple[str, ...]


_SPECS = {
    "project_skill": {
        "task_kind": "project_skill",
        "instruction": (
            "Produce a reviewable Project Skill proposal. The Model Planner completion summary must contain exactly one JSON object "
            "with fields proposal_status, rules, source_refs, excluded_option_ids, usage_boundaries, "
            "failure_conditions, validation_steps and maintenance_actions. proposal_status must be a string; "
            "all other fields must be arrays of strings. Use graph node IDs for excluded options when available. "
            "Assume platform_evidence changed and maintenance_actions must contain only affected node IDs in dependency "
            "order. "
            "Return a Model Planner decision envelope with type complete, payload_ref null and evidence_refs []; "
            "its summary string must be the compact case JSON, with no Markdown or extra prose."
        ),
        "required_terms": ("proposal-only", "Gate", "Effect Runner", "source revision", "failure condition"),
        "required_source_refs": ("source:requirement", "source:architecture", "source:decision"),
        "forbidden_terms": ("directly publish", "bypass the shared Effect path"),
        "metric_names": ("rule_completeness", "source_traceability", "excluded_option_identification", "usage_boundary_accuracy", "maintenance_cost", "token_cost"),
        "quality_thresholds": {
            "rule_completeness": 1.0, "source_traceability": 1.0,
            "excluded_option_identification": 1.0, "usage_boundary_accuracy": 1.0,
            "maintenance_cost": 3.0,
        },
    },
    "document": {
        "task_kind": "document",
        "instruction": (
            "Produce a two-paragraph evidence-backed draft after Evidence A changes to: Evidence A no longer verifies "
            "Fact A. Preserve every unrelated paragraph exactly and determine the local update. "
            "The Model Planner completion summary must contain exactly one JSON object with fields paragraphs, affected_paragraph_ids, "
            "regeneration_order and unchanged_paragraph_ids. paragraphs must be an array of objects containing exactly "
            "paragraph_id, text and source_refs; the other fields must be arrays of strings. Return no Markdown fence or "
            "prose outside the JSON object. Return a Model Planner decision envelope with type complete, payload_ref null "
            "and evidence_refs []; its summary string must be the compact case JSON."
        ),
        "required_terms": ("Paragraph A", "Paragraph B", "Evidence A", "Fact B"),
        "required_source_refs": ("source:a", "source:a-evidence", "source:b"),
        "forbidden_terms": (),
        "metric_names": ("argument_structure", "paragraph_source_coverage", "affected_paragraph_identification", "local_regeneration_accuracy", "unrelated_paragraph_stability", "token_cost"),
        "quality_thresholds": {
            "argument_structure": 1.0, "paragraph_source_coverage": 1.0,
            "affected_paragraph_identification": 1.0, "local_regeneration_accuracy": 1.0,
            "unrelated_paragraph_stability": 1.0,
        },
    },
    "research_turn": {
        "task_kind": "research_turn",
        "instruction": (
            "Determine the supported hypothesis, cite evidence, identify excluded claims and stale conclusions, preserve "
            "open questions, and give the dependency replay order. The Model Planner completion summary must contain exactly one JSON "
            "object with fields supported_hypothesis, evidence_refs, excluded_claim_ids, stale_conclusion_ids, "
            "replay_order, open_questions, included_node_ids, budget_note and selection_explanation. "
            "supported_hypothesis, budget_note and selection_explanation must be strings; all other fields must be "
            "arrays of strings. Use graph node IDs when available. Return no "
            "Markdown fence or prose outside the JSON object. Return a Model Planner decision envelope with type complete, "
            "payload_ref null and evidence_refs []; its summary string must be the compact case JSON."
        ),
        "required_terms": ("Hypothesis A", "supports A", "replication", "open"),
        "required_source_refs": ("source:evidence-a", "source:open"),
        "forbidden_terms": ("B is proven", "False claim"),
        "metric_names": ("wrong_context_recovery", "evidence_merge_quality", "citation_accuracy", "stale_conclusion_identification", "dependency_replay_order", "selection_transparency", "token_cost"),
        "quality_thresholds": {
            "wrong_context_recovery": 1.0, "evidence_merge_quality": 1.0,
            "citation_accuracy": 1.0, "stale_conclusion_identification": 1.0,
            "dependency_replay_order": 1.0, "selection_transparency": 1.0,
        },
    },
}


def build_model_benchmark_cases() -> tuple[FrozenModelBenchmarkCase, ...]:
    cases = []
    for result in run_structural_benchmark():
        spec = _SPECS[result.evaluation_id]
        binding = result.evidence.get("binding")
        linear = result.evidence.get("linear_context")
        if not isinstance(binding, ContextBinding) or not isinstance(linear, str):
            raise ModelBenchmarkError("structural fixture lacks model benchmark evidence")
        cases.append(FrozenModelBenchmarkCase(
            case_id=result.evaluation_id,
            task_kind=str(spec["task_kind"]),
            fixture_revision="2.0.0",
            evaluator_revision="2.0.0",
            decoding_revision=_DECODING_REVISION,
            instruction=str(spec["instruction"]),
            linear_context=linear,
            binding_template=binding,
            required_terms=tuple(spec["required_terms"]),
            required_source_refs=tuple(spec["required_source_refs"]),
            forbidden_terms=tuple(spec["forbidden_terms"]),
            metric_names=tuple(spec["metric_names"]),
        ))
    return tuple(cases)


def build_model_benchmark_definition() -> Mapping[str, object]:
    """Declare the pure benchmark contract a capability contributes to Core.

    This contribution intentionally exposes fixture and scoring callables only.
    Turn submission, recovery, credential use and evidence persistence remain
    platform responsibilities.
    """
    return {
        "schema_version": "1.0.0",
        "case_builder": build_model_benchmark_cases,
        "pair_builder": build_model_benchmark_turn_pair,
        "case_scorer": score_model_benchmark,
        "suite_scorer": score_model_benchmark_suite,
    }


def freeze_case_binding(
    case: FrozenModelBenchmarkCase, revisions: FrozenContextRevisions,
) -> ContextBinding:
    return freeze_benchmark_binding(case.binding_template, revisions)


def freeze_benchmark_binding(
    template: ContextBinding, revisions: FrozenContextRevisions,
) -> ContextBinding:
    """Freeze revisions and reprice the exact canonical model entry."""

    if revisions.compiler_revision != ContextCompiler.compiler_revision:
        raise ModelBenchmarkError("model benchmark compiler revision drifted")
    frozen = replace(
        template,
        capability_revision=revisions.capability_revision,
        compiler_revision=revisions.compiler_revision,
        boundary_revision=revisions.boundary_revision,
        provider_revision=revisions.provider_revision,
        model_route_revision=revisions.model_route_revision,
    )
    total = estimate_model_projection_tokens(context_binding_model_projection(frozen))
    explanation = dict(frozen.budget_explanation)
    hard_budget = explanation.get("hard_budget")
    if type(hard_budget) is not int or hard_budget < total:
        raise ModelBenchmarkError("model benchmark hard budget exceeded after revision freeze")
    original = explanation.get("original_token_estimate")
    if type(original) is not int:
        raise ModelBenchmarkError("model benchmark budget evidence is invalid")
    explanation.update({
        "original_token_estimate": max(original, total),
        "final_token_estimate": total,
        "estimator_revision": "canonical-model-entry-v2",
    })
    return replace(
        frozen, total_token_cost=total, budget_explanation=explanation,
    )


def build_model_benchmark_turn_pair(
    case: FrozenModelBenchmarkCase,
    *,
    project_id: str,
    session_id: str,
    linear_turn_id: str,
    linemap_turn_id: str,
    binding_id: str,
    suite_run_id: str,
    revisions: FrozenContextRevisions,
    created_at: str,
    consent_refs: tuple[str, ...],
    replicate_index: int = 0,
    max_context_bytes: int = 262_144,
) -> ModelBenchmarkTurnPair:
    if linear_turn_id == linemap_turn_id:
        raise ModelBenchmarkError("model benchmark Turn identities must differ")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", suite_run_id):
        raise ModelBenchmarkError("model benchmark suite run identity is invalid")
    if isinstance(replicate_index, bool) or not isinstance(replicate_index, int) or replicate_index < 0:
        raise ModelBenchmarkError("model benchmark replicate index is invalid")
    if not consent_refs:
        raise ModelBenchmarkError("remote model benchmark requires explicit consent refs")
    binding = freeze_case_binding(case, revisions)
    binding_ref = f"crp://context-bindings/{project_id}/{binding_id}"
    if replicate_index > 9_999:
        raise ModelBenchmarkError("model benchmark replicate index is invalid")
    common = {
        "schema_version": "1.0.0",
        "session_id": session_id,
        "scope": {"kind": "project", "project_id": project_id, "series_id": None},
        "desired_outcome": _CONTEXT_EVALUATE_OUTCOME,
        "privacy": {
            "mode": "remote_allowed", "allow_remote": True, "pii": "possible",
            "consent_refs": list(consent_refs), "retention": "local_durable",
        },
        "capability_policy": {"allowed": [], "denied": [], "require_approval": []},
        "context_policy": {
            "include_project_skill": False, "include_memory": False,
            "include_session_history": False, "max_context_bytes": max_context_bytes,
        },
        "approval_policy": {"mode": "risk_based", "auto_approve_read_only": True},
        "created_at": created_at,
    }
    linear = {
        **common,
        "turn_id": linear_turn_id,
        "operation_id": _operation_id(suite_run_id, case.case_id, replicate_index, "linear"),
        "idempotency_key": _idempotency_key(suite_run_id, case.case_id, replicate_index, "linear"),
        "input": {
            "kind": "text",
            "text": f"{case.instruction}\n\nLinear context:\n{case.linear_context}",
            "refs": [],
        },
    }
    linemap = {
        **common,
        "turn_id": linemap_turn_id,
        "operation_id": _operation_id(suite_run_id, case.case_id, replicate_index, "linemap"),
        "idempotency_key": _idempotency_key(suite_run_id, case.case_id, replicate_index, "linemap"),
        "input": {
            "kind": "text", "text": case.instruction,
            "refs": [{
                "kind": "context_binding", "object_id": binding_id,
                "uri": binding_ref,
            }],
        },
    }
    return ModelBenchmarkTurnPair(
        suite_run_id=suite_run_id,
        replicate_index=replicate_index,
        case_id=case.case_id,
        binding_creation={
            "binding_id": binding_id,
            "project_id": project_id,
            "capability_id": "thought_graph_context",
            "capability_revision": revisions.capability_revision,
            "expected_revision": 0,
            "binding": context_binding_to_payload(binding),
        },
        linear_turn=linear,
        linemap_turn=linemap,
    )


def score_model_benchmark(
    case: FrozenModelBenchmarkCase,
    linear: TurnModelObservation,
    linemap: TurnModelObservation,
    *,
    suite_run_id: str,
) -> ModelBenefitResult:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", suite_run_id):
        raise ModelBenchmarkError("model benchmark suite run identity is invalid")
    _validate_observation(case, linear, "linear", suite_run_id)
    _validate_observation(case, linemap, "linemap", suite_run_id)
    same_route = _route_identity(linear) == _route_identity(linemap)
    if not same_route:
        raise ModelBenchmarkError("model benchmark variants used different frozen routes")
    if linear.turn_id == linemap.turn_id:
        raise ModelBenchmarkError("model benchmark variants must use distinct Turns")
    if linear.replicate_index != linemap.replicate_index:
        raise ModelBenchmarkError("model benchmark replicate identity drifted")
    linear_metrics, linear_valid = _metrics(case, linear)
    linemap_metrics, linemap_valid = _metrics(case, linemap)
    quality_names = tuple(name for name in case.metric_names if name not in {"token_cost", "maintenance_cost"})
    quality_gain = round(
        sum(linemap_metrics[name] - linear_metrics[name] for name in quality_names)
        / len(quality_names),
        4,
    ) if quality_names else 0.0
    token_ratio = round(linemap.total_tokens / linear.total_tokens, 4) if linear.total_tokens else float("inf")
    thresholds = _quality_thresholds(case)
    no_quality_regression = all(
        linemap_metrics[name] >= linear_metrics[name] for name in quality_names
    )
    threshold_satisfied = all(
        linemap_metrics[name] >= thresholds[name] for name in quality_names
    )
    maintenance_satisfied = True
    if "maintenance_cost" in case.metric_names:
        maintenance_satisfied = (
            linemap_metrics["maintenance_cost"] <= linear_metrics["maintenance_cost"]
            and linemap_metrics["maintenance_cost"] <= thresholds["maintenance_cost"]
        )
    token_satisfied = token_ratio <= _MAXIMUM_TOKEN_RATIO
    return ModelBenefitResult(
        case_id=case.case_id,
        linear_metrics=linear_metrics,
        linemap_metrics=linemap_metrics,
        same_frozen_route=True,
        model_quality_verified=linear_valid and linemap_valid,
        benefit_gate_passed=(
            linear_valid and linemap_valid and no_quality_regression
            and threshold_satisfied and maintenance_satisfied
            and quality_gain >= _MINIMUM_MEAN_QUALITY_GAIN and token_satisfied
        ),
        evidence={
            "suite_run_id": suite_run_id,
            "route_key": linemap.route_key,
            "route_revision": linemap.route_revision,
            "provider_id": linemap.provider_id,
            "provider_revision": linemap.provider_revision,
            "model_name": linemap.model_name,
            "execution_location": linemap.execution_location,
            "capability_revision": linemap.capability_revision,
            "compiler_revision": linemap.compiler_revision,
            "boundary_revision": linemap.boundary_revision,
            "linear_turn_id": linear.turn_id,
            "linemap_turn_id": linemap.turn_id,
            "linear_turn_terminal_event_id": linear.turn_terminal_event_id,
            "linemap_turn_terminal_event_id": linemap.turn_terminal_event_id,
            "linear_model_receipt_ref": linear.model_receipt_ref,
            "linemap_model_receipt_ref": linemap.model_receipt_ref,
            "linear_routing_snapshot_ref": linear.routing_snapshot_ref,
            "linemap_routing_snapshot_ref": linemap.routing_snapshot_ref,
            "linear_routing_snapshot_revision": linear.routing_snapshot_revision,
            "linemap_routing_snapshot_revision": linemap.routing_snapshot_revision,
            "linear_model_request_id": linear.model_request_id,
            "linemap_model_request_id": linemap.model_request_id,
            "linear_model_attempt_id": linear.model_attempt_id,
            "linemap_model_attempt_id": linemap.model_attempt_id,
            "linear_operation_id": linear.operation_id,
            "linemap_operation_id": linemap.operation_id,
            "replicate_index": linemap.replicate_index,
            "linear_usage": _usage(linear),
            "linemap_usage": _usage(linemap),
            "mean_quality_gain": quality_gain,
            "token_ratio": token_ratio,
            "linear_output_contract_valid": linear_valid,
            "linemap_output_contract_valid": linemap_valid,
            "metric_contract_revision": "2.0.0",
            "fixture_revision": case.fixture_revision,
            "evaluator_revision": case.evaluator_revision,
            "decoding_revision": case.decoding_revision,
            "frozen_gate": {
                "minimum_mean_quality_gain": _MINIMUM_MEAN_QUALITY_GAIN,
                "maximum_token_ratio": _MAXIMUM_TOKEN_RATIO,
                "quality_thresholds": thresholds,
                "no_quality_regression": no_quality_regression,
                "maintenance_cost_satisfied": maintenance_satisfied,
            },
        },
    )


def score_model_benchmark_suite(
    suite_run_id: str, results: Sequence[ModelBenefitResult],
) -> ModelBenchmarkSuiteResult:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", suite_run_id):
        raise ModelBenchmarkError("model benchmark suite run identity is invalid")
    expected = tuple(_SPECS)
    by_case = {result.case_id: result for result in results}
    if len(by_case) != len(results):
        raise ModelBenchmarkError("model benchmark suite contains duplicate cases")
    if set(by_case) - set(expected):
        raise ModelBenchmarkError("model benchmark suite contains unknown cases")
    ordered = tuple(by_case[case_id] for case_id in expected if case_id in by_case)
    if any(result.evidence.get("suite_run_id") != suite_run_id for result in ordered):
        raise ModelBenchmarkError("model benchmark suite run identity drifted")
    present = set(by_case) == set(expected)
    identities = {
        _suite_route_identity(result)
        for result in ordered
    }
    frozen_identity = present and len(identities) == 1
    verified = present and all(result.model_quality_verified for result in ordered)
    passed = present and frozen_identity and all(result.benefit_gate_passed for result in ordered)
    quality = [float(result.evidence["mean_quality_gain"]) for result in ordered]
    ratios = [float(result.evidence["token_ratio"]) for result in ordered]
    failures = []
    if not present:
        failures.append("missing_required_case")
    if present and not frozen_identity:
        failures.append("suite_frozen_identity_drift")
    failures.extend(
        f"{result.case_id}:output_contract_unverified"
        for result in ordered if not result.model_quality_verified
    )
    failures.extend(
        f"{result.case_id}:benefit_gate_failed"
        for result in ordered if not result.benefit_gate_passed
    )
    return ModelBenchmarkSuiteResult(
        suite_run_id=suite_run_id,
        case_results=ordered,
        all_required_cases_present=present,
        all_model_outputs_verified=verified,
        all_case_gates_passed=passed,
        native_canvas_eligible=verified and passed,
        aggregate_quality_gain=round(sum(quality) / len(quality), 4) if quality else 0.0,
        aggregate_token_ratio=round(sum(ratios) / len(ratios), 4) if ratios else 0.0,
        hard_failures=tuple(failures),
    )


def _metrics(
    case: FrozenModelBenchmarkCase, observation: TurnModelObservation,
) -> tuple[Mapping[str, float], bool]:
    output = _structured_output(case.case_id, observation.output_text)
    valid = output is not None
    if case.case_id == "project_skill":
        values = _project_skill_metrics(case, output, observation.total_tokens)
    elif case.case_id == "document":
        values = _document_metrics(output, observation.total_tokens)
    elif case.case_id == "research_turn":
        values = _research_metrics(output, observation.total_tokens)
    else:
        raise ModelBenchmarkError("unsupported model benchmark case")
    if not valid:
        values = {
            key: (float(observation.total_tokens) if key in {"token_cost", "maintenance_cost"} else 0.0)
            for key in values
        }
    return {name: values[name] for name in case.metric_names}, valid


def _project_skill_metrics(
    case: FrozenModelBenchmarkCase, output: Mapping[str, object] | None, total_tokens: int,
) -> dict[str, float]:
    if output is None:
        return {name: 0.0 for name in case.metric_names}
    rules_text = " ".join(
        [*_strings(output["rules"]), *_strings(output["failure_conditions"]),
         *_strings(output["validation_steps"])]
    ).casefold()
    boundaries = " ".join(_strings(output["usage_boundaries"])).casefold()
    sources = set(_strings(output["source_refs"]))
    boundary_checks = (
        output["proposal_status"] == "proposal_only",
        "gate" in boundaries,
        "effect runner" in boundaries,
    )
    return {
        "rule_completeness": _ratio(
            sum(term.casefold() in rules_text for term in case.required_terms),
            len(case.required_terms),
        ),
        "source_traceability": _set_f1(sources, set(case.required_source_refs)),
        "excluded_option_identification": _set_f1(
            set(_strings(output["excluded_option_ids"])), {"rejected_direct_write"},
        ),
        "usage_boundary_accuracy": _ratio(sum(boundary_checks), len(boundary_checks)),
        "maintenance_cost": float(
            len(_strings(output["maintenance_actions"]))
            + (10 * (1 - _set_f1(
                set(_strings(output["maintenance_actions"])),
                {"platform_evidence", "decision", "skill_proposal"},
            )))
        ),
        "token_cost": float(total_tokens),
    }


def _document_metrics(
    output: Mapping[str, object] | None, total_tokens: int,
) -> dict[str, float]:
    if output is None:
        return {
            "argument_structure": 0.0, "paragraph_source_coverage": 0.0,
            "affected_paragraph_identification": 0.0, "local_regeneration_accuracy": 0.0,
            "unrelated_paragraph_stability": 0.0, "token_cost": float(total_tokens),
        }
    paragraphs = {
        str(item["paragraph_id"]): item for item in output["paragraphs"]  # type: ignore[index]
    }
    expected_sources = {
        "paragraph_a": {"source:a", "source:a-evidence"},
        "paragraph_b": {"source:b"},
    }
    paragraph_ids = set(paragraphs)
    source_scores = [
        _set_f1(set(_strings(paragraphs[node_id]["source_refs"])), refs)
        for node_id, refs in expected_sources.items() if node_id in paragraphs
    ]
    affected = set(_strings(output["affected_paragraph_ids"]))
    unchanged = set(_strings(output["unchanged_paragraph_ids"]))
    return {
        "argument_structure": _set_f1(paragraph_ids, set(expected_sources)),
        "paragraph_source_coverage": round(sum(source_scores) / len(expected_sources), 4),
        "affected_paragraph_identification": _set_f1(affected, {"paragraph_a"}),
        "local_regeneration_accuracy": (
            1.0 if _strings(output["regeneration_order"]) == ("paragraph_a",)
            and "no longer verifies" in str(paragraphs.get("paragraph_a", {}).get("text", "")).casefold()
            else 0.0
        ),
        "unrelated_paragraph_stability": (
            1.0 if unchanged == {"paragraph_b"} and not (unchanged & affected)
            and str(paragraphs.get("paragraph_b", {}).get("text", "")).strip().casefold()
            == "paragraph b follows only from fact b."
            else 0.0
        ),
        "token_cost": float(total_tokens),
    }


def _research_metrics(
    output: Mapping[str, object] | None, total_tokens: int,
) -> dict[str, float]:
    if output is None:
        return {
            "wrong_context_recovery": 0.0, "evidence_merge_quality": 0.0,
            "citation_accuracy": 0.0, "stale_conclusion_identification": 0.0,
            "dependency_replay_order": 0.0, "selection_transparency": 0.0,
            "token_cost": float(total_tokens),
        }
    refs = set(_strings(output["evidence_refs"]))
    expected_refs = {"source:evidence-a", "source:open"}
    excluded = set(_strings(output["excluded_claim_ids"]))
    stale = set(_strings(output["stale_conclusion_ids"]))
    explanation = str(output["selection_explanation"]).casefold()
    transparency_checks = (
        _set_f1(
            set(_strings(output["included_node_ids"])),
            {"evidence_a", "open_question", "stale_conclusion", "synthesis"},
        ),
        _set_f1(excluded, {"wrong_evidence"}),
        1.0 if _strings(output["open_questions"]) else 0.0,
        1.0 if "excluded" in explanation and "stale" in explanation else 0.0,
        1.0 if str(output["budget_note"]).strip() else 0.0,
    )
    supported = str(output["supported_hypothesis"]).strip().casefold()
    return {
        "wrong_context_recovery": round((
            (1.0 if supported == "hypothesis a" else 0.0)
            + _set_f1(excluded, {"wrong_evidence"})
        ) / 2, 4),
        "evidence_merge_quality": _set_recall(refs, expected_refs),
        "citation_accuracy": _set_precision(refs, expected_refs),
        "stale_conclusion_identification": _set_f1(stale, {"stale_conclusion"}),
        "dependency_replay_order": (
            1.0 if _strings(output["replay_order"]) == ("stale_conclusion", "synthesis") else 0.0
        ),
        "selection_transparency": round(sum(transparency_checks) / len(transparency_checks), 4),
        "token_cost": float(total_tokens),
    }


def _structured_output(case_id: str, text: str) -> Mapping[str, object] | None:
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    if case_id == "project_skill":
        fields = {
            "proposal_status", "rules", "source_refs", "excluded_option_ids",
            "usage_boundaries", "failure_conditions", "validation_steps",
            "maintenance_actions",
        }
        if set(value) != fields or not isinstance(value["proposal_status"], str):
            return None
        if not all(_string_list(value[field]) for field in fields - {"proposal_status"}):
            return None
    elif case_id == "document":
        if set(value) != {
            "paragraphs", "affected_paragraph_ids", "regeneration_order",
            "unchanged_paragraph_ids",
        } or not isinstance(value["paragraphs"], list) or not value["paragraphs"]:
            return None
        if not all(_paragraph(item) for item in value["paragraphs"]):
            return None
        paragraph_ids = [item["paragraph_id"] for item in value["paragraphs"]]
        if len(paragraph_ids) != len(set(paragraph_ids)):
            return None
        if not all(_string_list(value[field]) for field in (
            "affected_paragraph_ids", "regeneration_order", "unchanged_paragraph_ids",
        )):
            return None
    elif case_id == "research_turn":
        fields = {
            "supported_hypothesis", "evidence_refs", "excluded_claim_ids",
            "stale_conclusion_ids", "replay_order", "open_questions",
            "included_node_ids", "budget_note", "selection_explanation",
        }
        if set(value) != fields or not all(
            isinstance(value[field], str) and value[field].strip()
            for field in ("supported_hypothesis", "budget_note", "selection_explanation")
        ):
            return None
        if not all(_string_list(value[field]) for field in fields - {
            "supported_hypothesis", "budget_note", "selection_explanation",
        }):
            return None
    else:
        return None
    return value


def _paragraph(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"paragraph_id", "text", "source_refs"}
        and isinstance(value["paragraph_id"], str) and bool(value["paragraph_id"].strip())
        and isinstance(value["text"], str) and bool(value["text"].strip())
        and _string_list(value["source_refs"])
    )


def _string_list(value: object) -> bool:
    return (
        isinstance(value, list)
        and all(isinstance(item, str) and bool(item.strip()) for item in value)
        and len(value) == len(set(value))
    )


def _strings(value: object) -> tuple[str, ...]:
    return tuple(str(item).strip() for item in value) if isinstance(value, list) else ()


def _set_f1(actual: set[str], expected: set[str]) -> float:
    if not actual and not expected:
        return 1.0
    if not actual or not expected:
        return 0.0
    intersection = len(actual & expected)
    precision = intersection / len(actual)
    recall = intersection / len(expected)
    return round(2 * precision * recall / (precision + recall), 4) if intersection else 0.0


def _set_precision(actual: set[str], expected: set[str]) -> float:
    return round(len(actual & expected) / len(actual), 4) if actual else 0.0


def _set_recall(actual: set[str], expected: set[str]) -> float:
    return round(len(actual & expected) / len(expected), 4) if expected else 1.0


def _validate_observation(
    case: FrozenModelBenchmarkCase,
    value: TurnModelObservation,
    variant: str,
    suite_run_id: str,
) -> None:
    if (
        value.suite_run_id != suite_run_id
        or value.case_id != case.case_id
        or value.variant != variant
        or value.decoding_revision != case.decoding_revision
        or value.operation_id
        != _operation_id(suite_run_id, case.case_id, value.replicate_index, variant)
    ):
        raise ModelBenchmarkError("model benchmark observation identity drifted")
    prefix = f"crp://session/{value.turn_id}/"
    if (
        value.status != "completed"
        or not value.turn_id.strip()
        or not value.turn_terminal_event_id.strip()
        or not value.model_receipt_ref.startswith(prefix)
        or not value.routing_snapshot_ref.startswith(prefix)
        or not value.model_request_id.strip()
        or not value.model_attempt_id.startswith("model-wire-attempt-")
        or re.fullmatch(r"[0-9a-f]{64}", value.routing_snapshot_revision) is None
    ):
        raise ModelBenchmarkError("model benchmark Turn evidence is incomplete")
    if not value.output_text.strip() or any(number < 0 for number in _usage(value).values()):
        raise ModelBenchmarkError("model benchmark observation is incomplete")
    if value.total_tokens != value.input_tokens + value.output_tokens:
        raise ModelBenchmarkError("model benchmark usage total drifted")
    if not all(_route_identity(value)):
        raise ModelBenchmarkError("model benchmark frozen route evidence is incomplete")
    if value.execution_location != "remote":
        raise ModelBenchmarkError(
            "model benchmark requires a remote execution location"
        )


def _route_identity(value: TurnModelObservation) -> tuple[str, ...]:
    return (
        value.route_key, value.route_revision, value.provider_id,
        value.provider_revision, value.model_name, value.capability_revision,
        value.execution_location, value.compiler_revision, value.boundary_revision,
        value.decoding_revision,
    )


def _suite_route_identity(result: ModelBenefitResult) -> tuple[str, ...]:
    return tuple(str(result.evidence.get(field) or "") for field in (
        "route_key", "route_revision", "provider_id", "provider_revision",
        "model_name", "execution_location", "capability_revision", "compiler_revision",
        "boundary_revision", "decoding_revision",
    ))


def _quality_thresholds(case: FrozenModelBenchmarkCase) -> Mapping[str, float]:
    raw = _SPECS[case.case_id].get("quality_thresholds")
    if not isinstance(raw, Mapping):
        raise ModelBenchmarkError("model benchmark quality thresholds are unavailable")
    thresholds = {str(key): float(value) for key, value in raw.items()}
    required = set(case.metric_names) - {"token_cost"}
    if set(thresholds) != required:
        raise ModelBenchmarkError("model benchmark quality thresholds drifted")
    return thresholds


def _operation_id(
    suite_run_id: str,
    case_id: str,
    replicate_index: int,
    variant: str,
) -> str:
    return f"op-lm-{suite_run_id}-{case_id}-r{replicate_index}-{variant}"


def _idempotency_key(
    suite_run_id: str,
    case_id: str,
    replicate_index: int,
    variant: str,
) -> str:
    return f"idem-lm-{suite_run_id}-{case_id}-r{replicate_index}-{variant}"


def _usage(value: TurnModelObservation) -> dict[str, int]:
    return {
        "input_tokens": value.input_tokens,
        "output_tokens": value.output_tokens,
        "total_tokens": value.total_tokens,
    }


def _ratio(found: int, total: int) -> float:
    return round(found / total, 4) if total else 1.0
