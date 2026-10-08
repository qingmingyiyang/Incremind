"""Evidence-gated, proposal-only learning for Application Skills.

This module deliberately does not contain package, binding, or catalog write
operations.  It turns one completed task's immutable Skill selection evidence
into a pending-review proposal only.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from .consumer_runtime import ObjectStoreApplicationSkillTraceRepository
from .management import ApplicationSkillProposalRegistry
from .package_catalog import ApplicationSkillCatalogSnapshot


class SkillLearningWorkshopError(ValueError):
    """Raised when task evidence is insufficient or unsafe to learn from."""


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SIGNAL_KINDS = frozenset({"complex_task", "explicit_correction", "reliable_recovery", "repeated_flow"})
_SECRET = re.compile(r"(?i)(?:authorization\s*:|cookie\s*:|\bsk-[A-Za-z0-9_-]{16,}\b|(?:api[_-]?key|secret|token|password)\s*[:=])")
_ABSOLUTE_PATH = re.compile(r"(?:(?:[A-Za-z]:[\\/])|(?:^|\s)/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*)")
_DANGEROUS_COMMAND = re.compile(r"(?i)(?:\brm\s+-[a-z]*r[a-z]*f?\b|\bdel\s+/[a-z]*[fq]|\brmdir\s+/s|\bshutil\.rmtree\b|\b(?:subprocess|os)\.system\b)")
_OVERBROAD = re.compile(r"(?i)(?:\b(?:always|never)\b.{0,40}\b(?:any|all)\s+(?:task|project|file|command)s?\b|\bfor\s+(?:every|all)\s+tasks?\b)")
_MAX_CONTENT_BYTES = 16 * 1024


@dataclass(frozen=True, slots=True)
class SkillLearningWorkshop:
    """Create one review-only proposal from a completed, traced invocation."""

    store: object
    snapshot: ApplicationSkillCatalogSnapshot

    def propose(
        self,
        *,
        turn_receipt: Mapping[str, object],
        resolution_id: str,
        skill_id: str,
        expected_fingerprint: str,
        reusable_signal: Mapping[str, object],
        proposed_content: Mapping[str, object],
    ) -> dict[str, object]:
        receipt = _receipt(turn_receipt)
        clean_resolution = _identifier(resolution_id, "resolution_id")
        clean_skill = _identifier(skill_id, "skill_id")
        fingerprint = _fingerprint(expected_fingerprint)
        signal = _signal(reusable_signal)
        content = _content(proposed_content)

        trace = ObjectStoreApplicationSkillTraceRepository(self.store).get_trace(clean_resolution)
        if trace is None:
            raise SkillLearningWorkshopError("Application Skill resolution trace is unavailable")
        if trace.get("project_id") != receipt["project_id"]:
            raise SkillLearningWorkshopError("Application Skill receipt project differs from resolution trace")
        selected = next(
            (item for item in trace["selected"]
             if isinstance(item, Mapping) and item.get("skill_id") == clean_skill),
            None,
        )
        if selected is None:
            raise SkillLearningWorkshopError("Application Skill was not selected for this task")
        if selected.get("skill_fingerprint") != fingerprint:
            raise SkillLearningWorkshopError("Application Skill selected fingerprint drifted")

        package = self.snapshot.get(clean_skill)
        if package is None or package.fingerprint != fingerprint:
            raise SkillLearningWorkshopError("Application Skill package fingerprint drifted")

        action = "skill.update" if package.source_kind == "user" else "skill.fork"
        payload = {
            "turn_id": receipt["turn_id"],
            "receipt_id": receipt["receipt_id"],
            "project_id": receipt["project_id"],
            "resolution_id": clean_resolution,
            "skill_id": clean_skill,
            "skill_fingerprint": fingerprint,
            "source_kind": package.source_kind,
            "target_source_kind": "user",
            "reusable_signal": signal,
            "proposed_content": content,
            "safety_diagnostics": [
                {"code": "credential_path_command_and_scope_scan", "status": "passed"},
            ],
            "mutation_mode": "update_existing_user_skill" if action == "skill.update" else "fork_to_new_user_skill",
            "write_effect": "proposal_only",
        }
        proposal = ApplicationSkillProposalRegistry(self.store).propose(action, payload)
        return {
            "status": proposal["status"],
            "proposal_id": proposal["proposal_id"],
            "action": action,
            "skill_id": clean_skill,
            "skill_fingerprint": fingerprint,
            "write_effect": "proposal_only",
        }


def _receipt(value: Mapping[str, object]) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {"turn_id", "receipt_id", "project_id", "status"}:
        raise SkillLearningWorkshopError("completed Turn receipt evidence has an invalid schema")
    if value.get("status") != "completed":
        raise SkillLearningWorkshopError("Skill learning requires a completed Turn receipt")
    return {field: _identifier(value.get(field), field) for field in ("turn_id", "receipt_id", "project_id")}


def _signal(value: Mapping[str, object]) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {"kind", "evidence"}:
        raise SkillLearningWorkshopError("reusable signal has an invalid schema")
    kind = value.get("kind")
    evidence = value.get("evidence")
    if kind not in _SIGNAL_KINDS or not isinstance(evidence, str) or not 8 <= len(evidence.strip()) <= 1000:
        raise SkillLearningWorkshopError("reusable signal is insufficient")
    _scan(evidence, "reusable signal")
    return {"kind": str(kind), "evidence": evidence.strip()}


def _content(value: Mapping[str, object]) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {"summary", "instructions"}:
        raise SkillLearningWorkshopError("proposed Skill content has an invalid schema")
    clean: dict[str, str] = {}
    for field, maximum in (("summary", 2048), ("instructions", _MAX_CONTENT_BYTES)):
        text = value.get(field)
        if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > maximum:
            raise SkillLearningWorkshopError(f"proposed Skill {field} is invalid or exceeds its budget")
        _scan(text, f"proposed Skill {field}")
        clean[field] = text.strip()
    return clean


def _scan(text: str, label: str) -> None:
    if _SECRET.search(text):
        raise SkillLearningWorkshopError(f"{label} contains secret-like material")
    if _ABSOLUTE_PATH.search(text):
        raise SkillLearningWorkshopError(f"{label} contains an absolute path")
    if _DANGEROUS_COMMAND.search(text):
        raise SkillLearningWorkshopError(f"{label} contains a dangerous command")
    if _OVERBROAD.search(text):
        raise SkillLearningWorkshopError(f"{label} contains an overbroad rule")


def _identifier(value: object, label: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not _SAFE_ID.fullmatch(text):
        raise SkillLearningWorkshopError(f"invalid {label}")
    return text


def _fingerprint(value: object) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not _SHA256.fullmatch(text):
        raise SkillLearningWorkshopError("invalid Application Skill fingerprint")
    return text
