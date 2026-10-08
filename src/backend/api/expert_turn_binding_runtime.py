from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from core.ai_kernel import CapabilityDefinition, CapabilityManifest, ContextManifest
from core.product_core.expert_binding_snapshot import (
    ExpertBindingSnapshotError,
    freeze_expert_binding_snapshot,
    verify_snapshot_replay,
)
from core.product_core.expert_catalog import (
    ExpertCatalog,
    ExpertProjectBindingStore,
    ExpertConfigurationResolver,
)


class ExpertAwarePlanner:
    """Routes a frozen expert snapshot to its bounded deterministic recipe."""

    def __init__(
        self,
        delegate: object,
        *,
        job_reader: Callable[[str], Mapping[str, object] | None] | None = None,
        job_resume_supported: bool | Callable[[], bool] = False,
    ) -> None:
        self._delegate = delegate
        self._job_reader = job_reader
        self._job_resume_supported = job_resume_supported

    def plan(self, request, events, capabilities, payloads, execution_control=None):
        resolved = payloads.get_immutable_payload(
            str(request.get("turn_id") or ""), "expert-binding-snapshot-v1"
        )
        snapshot = resolved[1] if resolved is not None else None
        if not isinstance(snapshot, Mapping):
            return self._delegate.plan(
                request, events, capabilities, payloads, execution_control
            )
        if snapshot.get("expert_id") != "video-research-expert":
            return self._delegate.plan(
                request, events, capabilities, payloads, execution_control
            )
        if not all(
            isinstance(snapshot.get(field), str) and snapshot.get(field)
            for field in ("role", "method", "output_contract")
        ):
            raise ExpertBindingSnapshotError("expert execution profile is incomplete")
        terminal_resolved = payloads.get_immutable_payload(
            str(request.get("turn_id") or ""), "expert-job-terminal-snapshot-v1"
        )
        terminal = terminal_resolved[1] if terminal_resolved is not None else None
        if isinstance(terminal, Mapping) and terminal.get("status") == "completed":
            if (
                terminal.get("turn_id") != request.get("turn_id")
                or terminal.get("source_manifest_revision") is None
                or not isinstance(terminal.get("receipt_ref"), str)
            ):
                raise ExpertBindingSnapshotError("expert terminal evidence drifted")
            recalled = next(
                (
                    item for item in reversed(events)
                    if item.get("type") == "tool.completed"
                    and isinstance(item.get("data"), Mapping)
                    and item["data"].get("capability_id") == "memory.recall"
                ),
                None,
            )
            if recalled is None:
                if "memory.recall" not in {item.capability_id for item in capabilities}:
                    raise ExpertBindingSnapshotError(
                        "video research expert requires memory.recall for project grounding"
                    )
                input_payload = request.get("input")
                text = input_payload.get("text") if isinstance(input_payload, Mapping) else None
                query = str(text or "video research project context").strip()
                return {
                    "type": "tool",
                    "capability_id": "memory.recall",
                    "arguments": {
                        "query": query,
                        "allowed_trust_statuses": ["verified", "published"],
                        "limit": 12,
                    },
                }
            recalled_data = recalled.get("data")
            recalled_values = recalled_data if isinstance(recalled_data, Mapping) else {}
            return {
                "type": "complete",
                "summary": "video research evidence and project context grounded",
                "payload_ref": recalled_values.get("payload_ref") or terminal_resolved[0],
                "evidence_refs": list(dict.fromkeys([
                    str(terminal["receipt_ref"]),
                    str(terminal["source_manifest_ref"]),
                    *[
                        str(ref) for ref in recalled_values.get("evidence_refs", [])
                        if isinstance(ref, str)
                    ],
                ])),
            }
        completed = next(
            (
                item for item in reversed(events)
                if item.get("type") == "tool.completed"
                and isinstance(item.get("data"), Mapping)
                and item["data"].get("capability_id") == "analyze_source"
            ),
            None,
        )
        if completed is not None:
            data = completed.get("data")
            values = data if isinstance(data, Mapping) else {}
            outcome_ref = values.get("payload_ref")
            outcome = payloads.get(outcome_ref) if isinstance(outcome_ref, str) else None
            # Tool Runtime stores a capability's returned ``result`` as the
            # canonical ``tool-result`` payload referenced by tool.completed.
            # Older focused providers may still retain the outer envelope, so
            # accept that shape only as a compatibility read.  Do not infer
            # any Job identity from the event itself.
            result = outcome.get("result") if isinstance(outcome, Mapping) else None
            result_values = (
                result if isinstance(result, Mapping)
                else outcome if isinstance(outcome, Mapping) else {}
            )
            if result_values.get("status") == "admitted":
                return self._job_decision(result_values, snapshot)
            return {
                "type": "complete",
                "summary": str(values.get("summary") or "video research completed"),
                "payload_ref": values.get("payload_ref"),
                "evidence_refs": list(values.get("evidence_refs") or ()),
            }
        available = {item.capability_id for item in capabilities}
        if "analyze_source" not in available:
            raise ExpertBindingSnapshotError(
                "video research expert requires analyze_source in the frozen capability set"
            )
        if not self._resume_supported():
            raise ExpertBindingSnapshotError(
                "video research expert execution awaits the durable Media Job resume bridge"
            )
        return {
            "type": "tool",
            "capability_id": "analyze_source",
            "arguments": self._analyze_source_arguments(request, snapshot),
        }

    def _job_decision(
        self,
        admission: Mapping[str, object],
        snapshot: Mapping[str, object],
    ) -> dict[str, object]:
        job_id = admission.get("job_id")
        job_ref = admission.get("canonical_job_ref", admission.get("job_ref"))
        if not isinstance(job_id, str) or not job_id or not isinstance(job_ref, str):
            raise ExpertBindingSnapshotError("analyze_source admission is missing Job identity")
        job = self._job_reader(job_id) if self._job_reader is not None else None
        if not isinstance(job, Mapping):
            raise ExpertBindingSnapshotError("admitted Media Hands Job is unavailable")
        status = job.get("status")
        if status in {"pending", "queued", "running", "retrying", "waiting_user"}:
            return {
                "type": "wait_job",
                "job_id": job_id,
                "job_ref": job_ref,
                "job_revision": int(job.get("revision") or admission.get("job_revision") or 1),
                "observed_job_revision": int(job.get("revision") or admission.get("job_revision") or 1),
                "expert_snapshot_id": str(snapshot["snapshot_id"]),
            }
        if status == "completed":
            outputs = job.get("published_outputs")
            if not isinstance(outputs, list) or not outputs:
                raise ExpertBindingSnapshotError("completed Media Hands Job has no published output")
            evidence_refs = [
                str(item.get("uri")) for item in outputs
                if isinstance(item, Mapping) and isinstance(item.get("uri"), str)
            ]
            if not evidence_refs:
                raise ExpertBindingSnapshotError("completed Media Hands Job has no output evidence")
            return {
                "type": "complete",
                "summary": "video research source analysis completed",
                "payload_ref": evidence_refs[0],
                "evidence_refs": evidence_refs,
            }
        if status in {"failed", "cancelled"}:
            raise ExpertBindingSnapshotError(f"Media Hands Job ended with status {status}")
        raise ExpertBindingSnapshotError("Media Hands Job status is invalid")

    def _resume_supported(self) -> bool:
        value = (
            self._job_resume_supported()
            if callable(self._job_resume_supported)
            else self._job_resume_supported
        )
        return value is True

    @staticmethod
    def _analyze_source_arguments(
        request: Mapping[str, object], snapshot: Mapping[str, object],
    ) -> dict[str, object]:
        input_payload = request.get("input")
        input_value = input_payload if isinstance(input_payload, Mapping) else {}
        refs = input_value.get("refs")
        first = refs[0] if isinstance(refs, list) and refs else None
        source_ref = first.get("uri") if isinstance(first, Mapping) else None
        if isinstance(source_ref, str) and source_ref.startswith("crp://"):
            source_input = {"kind": "source_ref", "text": None, "source_ref": source_ref}
        else:
            text = input_value.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ExpertBindingSnapshotError(
                    "video research expert requires source text or a controlled source ref"
                )
            source_input = {"kind": "text", "text": text.strip(), "source_ref": None}
        return {
            "input": source_input,
            "intent": "summarize",
            "output_profile": {
                "profile_id": str(snapshot["expert_id"]),
                "revision": str(snapshot["expert_revision"]),
            },
            "resource_budget": {
                "max_assets": 64,
                "max_bytes": 536_870_912,
                "max_seconds": 3_600,
            },
        }


class ExpertTurnBindingRuntime:
    """Production adapter from AI Kernel Turn facts to the expert domain layer."""

    def __init__(self, root_dir: Path) -> None:
        self._catalog = ExpertCatalog(root_dir)
        self._bindings = ExpertProjectBindingStore(root_dir)
        self._selection = ExpertConfigurationResolver(self._catalog, self._bindings)

    @property
    def catalog(self) -> ExpertCatalog:
        """Expose the same read authority used for Turn expert selection."""

        return self._catalog

    @property
    def bindings(self) -> ExpertProjectBindingStore:
        """Expose the same project-binding authority used for selection."""

        return self._bindings

    def select(
        self,
        request: Mapping[str, object],
        capability_manifest: CapabilityManifest,
        capabilities: Sequence[CapabilityDefinition],
    ) -> Mapping[str, object]:
        del capability_manifest, capabilities
        scope = request.get("scope")
        project_id = scope.get("project_id") if isinstance(scope, Mapping) else None
        expert_request = request.get("expert_request")
        explicit = expert_request if isinstance(expert_request, Mapping) else {}
        intents_value = explicit.get("task_intents")
        intents = (
            [str(item) for item in intents_value]
            if isinstance(intents_value, list)
            else [str(request.get("desired_outcome") or "")]
        )
        intents = [item for item in intents if item]
        return self._selection.select(
            str(project_id or ""),
            intents,
            requested_expert_id=(
                str(explicit["expert_id"])
                if isinstance(explicit.get("expert_id"), str) else None
            ),
            budget=(
                str(explicit["budget"])
                if isinstance(explicit.get("budget"), str) else None
            ),
        )

    def resolve_dispatch_assignment(
        self, project_id: str, proposal: Mapping[str, object], kind: str,
    ) -> tuple[str, int] | None:
        """Resolve one agent-plan expert DTO to a frozen, provider-free ref.

        The returned integer is the project binding revision: it is the CAS
        revision governing whether this project may use the selected immutable
        expert revision.  The canonical ref also encodes the expert revision,
        so both identities remain visible without carrying profile text,
        provider/model configuration, or credentials.
        """
        if kind != "expert":
            raise ExpertBindingSnapshotError("dispatch assignment kind is invalid")
        if not isinstance(project_id, str) or not project_id.strip():
            raise ExpertBindingSnapshotError("dispatch assignment project is invalid")
        if not isinstance(proposal, Mapping) or set(proposal) != {
            "expert_id", "task_intents", "budget",
        }:
            raise ExpertBindingSnapshotError("dispatch expert proposal shape is invalid")
        expert_id = proposal.get("expert_id")
        intents = proposal.get("task_intents")
        budget = proposal.get("budget")
        if (
            not isinstance(expert_id, str) or not expert_id.strip()
            or not isinstance(intents, list) or not intents
            or any(not isinstance(item, str) or not item.strip() for item in intents)
            or not isinstance(budget, str) or not budget.strip()
        ):
            raise ExpertBindingSnapshotError("dispatch expert proposal is invalid")
        receipt = self._selection.select(
            project_id.strip(), [item.strip() for item in intents],
            requested_expert_id=expert_id.strip(), budget=budget.strip(),
        )
        selected = receipt.get("selected")
        if not isinstance(selected, Mapping):
            raise ExpertBindingSnapshotError("dispatch expert is unavailable")
        if selected.get("requires_confirmation") is True:
            raise ExpertBindingSnapshotError("dispatch expert selection requires confirmation")
        selected_id = selected.get("expert_id")
        expert_revision = selected.get("expert_revision")
        binding_revision = selected.get("binding_revision")
        if (
            selected_id != expert_id.strip()
            or not isinstance(expert_revision, int) or isinstance(expert_revision, bool) or expert_revision < 1
            or not isinstance(binding_revision, int) or isinstance(binding_revision, bool) or binding_revision < 1
        ):
            raise ExpertBindingSnapshotError("dispatch expert selection is invalid")
        verdict = self._selection.verify_receipt(receipt)
        if verdict.get("status") != "ok":
            raise ExpertBindingSnapshotError(
                "dispatch expert selection drifted: "
                + "; ".join(str(item) for item in verdict.get("reasons", []))
            )
        reference = (
            f"crp://expert-dispatch/{project_id.strip()}/experts/{selected_id}"
            f"/expert-revisions/{expert_revision}/binding-revisions/{binding_revision}"
        )
        return reference, binding_revision

    def freeze(
        self,
        request: Mapping[str, object],
        selection_receipt: Mapping[str, object],
        capability_manifest: CapabilityManifest,
        context_manifest: ContextManifest,
        capabilities: Sequence[CapabilityDefinition],
    ) -> Mapping[str, object] | None:
        selected = selection_receipt.get("selected")
        if not isinstance(selected, Mapping):
            return None
        if selected.get("requires_confirmation") is True:
            raise ExpertBindingSnapshotError(
                "expert selection requires a durable confirmation action"
            )
        self._assert_scope(request, context_manifest)
        model_revision = capability_manifest.model_routing_snapshot_revision
        if not isinstance(model_revision, str) or not model_revision:
            raise ExpertBindingSnapshotError(
                "expert binding requires a frozen model routing snapshot revision"
            )
        return freeze_expert_binding_snapshot(
            selection_receipt,
            catalog=self._catalog,
            bindings=self._bindings,
            context_manifest_revision=context_manifest.manifest_id,
            boundary_revision=context_manifest.boundary_profile_revision,
            model_route_revision=model_revision,
            tool_capability_revisions=self._selected_tool_revisions(
                selection_receipt, capabilities
            ),
        )

    def verify_replay(
        self,
        request: Mapping[str, object],
        selection_receipt: Mapping[str, object],
        snapshot: Mapping[str, object],
        capability_manifest: CapabilityManifest,
        context_manifest: ContextManifest,
        capabilities: Sequence[CapabilityDefinition],
    ) -> None:
        self._assert_scope(request, context_manifest)
        verdict = self._selection.verify_receipt(selection_receipt)
        if verdict.get("status") != "ok":
            raise ExpertBindingSnapshotError(
                "expert selection receipt drifted during replay: "
                + "; ".join(str(item) for item in verdict.get("reasons", []))
            )
        model_revision = capability_manifest.model_routing_snapshot_revision
        if not isinstance(model_revision, str) or not model_revision:
            raise ExpertBindingSnapshotError(
                "expert replay requires a frozen model routing snapshot revision"
            )
        verdict = verify_snapshot_replay(
            snapshot,
            catalog=self._catalog,
            bindings=self._bindings,
            context_manifest_revision=context_manifest.manifest_id,
            boundary_revision=context_manifest.boundary_profile_revision,
            model_route_revision=model_revision,
            tool_capability_revisions=self._snapshot_tool_revisions(
                snapshot, capabilities
            ),
        )
        if verdict.get("status") != "ok":
            raise ExpertBindingSnapshotError(
                "expert binding snapshot drifted during replay: "
                + "; ".join(str(item) for item in verdict.get("reasons", []))
            )

    @staticmethod
    def _selected_tool_revisions(
        selection_receipt: Mapping[str, object],
        capabilities: Sequence[CapabilityDefinition],
    ) -> dict[str, int]:
        frozen_refs = selection_receipt.get("frozen_refs")
        tools = frozen_refs.get("tools") if isinstance(frozen_refs, Mapping) else None
        return ExpertTurnBindingRuntime._required_tool_revisions(tools, capabilities)

    @staticmethod
    def _snapshot_tool_revisions(
        snapshot: Mapping[str, object],
        capabilities: Sequence[CapabilityDefinition],
    ) -> dict[str, int]:
        return ExpertTurnBindingRuntime._required_tool_revisions(
            snapshot.get("tools"), capabilities
        )

    @staticmethod
    def _required_tool_revisions(
        tools: object, capabilities: Sequence[CapabilityDefinition],
    ) -> dict[str, int]:
        if not isinstance(tools, list) or not tools or any(
            not isinstance(item, str) or not item for item in tools
        ):
            raise ExpertBindingSnapshotError("expert frozen tool set is invalid")
        available = {item.capability_id: item.version for item in capabilities}
        missing = [item for item in tools if item not in available]
        if missing:
            raise ExpertBindingSnapshotError(
                "expert tools are absent from the Turn capability manifest: "
                + ", ".join(sorted(missing))
            )
        return {item: available[item] for item in tools}

    @staticmethod
    def _assert_scope(
        request: Mapping[str, object], context_manifest: ContextManifest,
    ) -> None:
        scope = request.get("scope")
        project_id = scope.get("project_id") if isinstance(scope, Mapping) else None
        if project_id != context_manifest.project_id:
            raise ExpertBindingSnapshotError("expert binding crossed project scope")


def build_expert_turn_binding_runtime(root_dir: Path) -> ExpertTurnBindingRuntime:
    return ExpertTurnBindingRuntime(Path(root_dir))
