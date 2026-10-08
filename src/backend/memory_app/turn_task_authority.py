"""Recognition-owned input and atomic result boundary for old Turn execution."""

from collections.abc import Mapping

from backend.memory_app.kernel.ai_execution_control import execution_control_from
from backend.recognition import RecognitionConflict, WorkScope
from backend.recognition.restructuring import RestructureProposalService
from core.document_engine import DocumentDraft, SQLiteDocumentRepository
from core.document_engine.runtime import ObjectStoreDocumentRepository
from core.document_engine.sqlite_runtime import _SQLiteTransactionObjectStore, _document_snapshot

from .constraints import ProjectConstraintService
from .packet_egress import validate_packet_egress
from .restructure_generation import STEP_VERSION, parse_proposal
from .restructure_tasks import verify_restructure_snapshot
from .turn_routing import RecognitionRoutingSnapshot, SNAPSHOT_KIND, _revision


class RecognitionTurnTaskAuthority:
    def __init__(self, *, service, models, payloads, mutation_lock, verify_current, now,
                 document_namespace="recognition"):
        self.service, self.models, self.payloads = service, models, payloads
        self.lock, self.verify_current, self.now = mutation_lock, verify_current, now
        self.document_namespace = document_namespace

    def load_task(self, request: Mapping[str, object]) -> dict:
        with self.lock:
            task, packet, scope = self._current(request)
            frozen = self.payloads.get_immutable_payload(str(request["turn_id"]), SNAPSHOT_KIND)
            if frozen is None:
                raise RecognitionConflict("task model routing is unavailable")
            reference, payload = frozen
            if (payload.get("turn_id") != request["turn_id"] or payload.get("project_id") != scope.project_id
                    or payload.get("context_packet_id") != packet.object_id):
                raise RecognitionConflict("task routing identity changed")
            result = {"task_id": task.object_id, "project_id": scope.project_id,
                      "context_packet_id": packet.object_id, "messages": packet.payload["messages"],
                      "routing_snapshot": RecognitionRoutingSnapshot(reference, _revision(payload), payload).generation_binding()}
            packet_revision = packet.revision

            def validate_current():
                with self.lock:
                    current_task, current_packet, current_scope = self._current(request)
                    if (current_task.object_id != task.object_id or current_packet.object_id != packet.object_id
                            or current_packet.revision != packet_revision or current_scope != scope):
                        raise RecognitionConflict("task input changed during model execution")

            # This callable stays in-process; only IDs enter Turn receipts.
            # The same existing authority checks now also run at wire time.
            result["validate_current"] = validate_current
            if task.payload.get("state") in {"result_ready", "completed"}:
                result["existing_result"] = self._existing_result(task, scope)
            elif task.payload.get("state") in {"queued", "waiting_approval"}:
                with self.service.records.begin() as tx:
                    tx.put("recognition_tasks", task.object_id, {**task.payload, "state": "running"},
                           expected_revision=task.revision)
                    tx.commit()
            return result

    def commit_result(self, request, loaded, answer, metadata) -> dict:
        if not isinstance(answer, str) or not answer.strip() or len(answer) > 40000:
            raise RecognitionConflict("task output is invalid")
        with self.lock:
            control = execution_control_from(request)
            if control is None:
                raise RecognitionConflict("task execution control is unavailable")
            control.checkpoint()
            validator = loaded.get("validate_current")
            if validator is not None:
                validator()
            task, packet, scope = self._current(request)
            if (loaded.get("task_id") != task.object_id or loaded.get("context_packet_id") != packet.object_id
                    or loaded.get("project_id") != scope.project_id):
                raise RecognitionConflict("task result identity changed")
            if task.payload.get("state") in {"result_ready", "completed"}:
                return self._existing_result(task, scope)
            if task.payload.get("state") != "running":
                raise RecognitionConflict("task is not running")
            if metadata.get("configuration_revision") != packet.payload.get("model_revision"):
                raise RecognitionConflict("task result model changed")
            # Re-run the shared admission validation before beginning the
            # atomic write.  save_in_uow fences the same snapshot once more
            # within that write transaction.
            restructure = self._restructure_packet(task, packet, scope)
            refs = ({"source_id": task.object_id, "locator": f"task://{task.object_id}"},) + tuple(
                {"source_id": item["id"], "locator": f"recognition://{item['id']}?revision={item['revision']}"}
                for item in packet.payload["items"])
            # Reuse the old Document domain and transaction adapter. Document
            # body, revisions and task reference commit together or not at all.
            with self.service.records.begin() as tx:
                document_store = _SQLiteTransactionObjectStore(tx, _document_snapshot(tx))
                repository = ObjectStoreDocumentRepository(document_store, namespace_id=self.document_namespace, now=self.now())
                proposal = None
                if restructure is not None:
                    parsed = parse_proposal(
                        scope=scope,
                        snapshot=restructure["snapshot"],
                        requested_operation=restructure["operation"],
                        response=answer,
                    )
                document = repository.create(DocumentDraft(
                    title=str(task.payload["input"])[:80] + " · " + task.object_id[-8:],
                    document_type=("restructure-internal" if restructure is not None else "agent-result"),
                    markdown=answer, project_id=scope.project_id, source_refs=refs))
                if restructure is not None:
                    proposal = RestructureProposalService(self.service).save_in_uow(
                        tx,
                        scope=scope,
                        proposal_id=restructure["proposal_id"],
                        snapshot=restructure["snapshot"],
                        operation=parsed["operation"],
                        outputs=parsed["outputs"],
                        reason=parsed["reason"],
                        origin_task_id=task.object_id,
                        step_metadata={
                            "source": "model",
                            "implementation_version": STEP_VERSION,
                            "model": metadata.get("model", ""),
                            "configuration_revision": metadata["configuration_revision"],
                        },
                    )
                tx.put("recognition_tasks", task.object_id, {**task.payload, "state": "result_ready",
                    "document_id": document["id"], "configuration_revision": metadata["configuration_revision"],
                    "model": metadata.get("model", ""), "usage": metadata.get("usage", {}),
                    **({"proposal_id": proposal["id"]} if proposal is not None else {})}, expected_revision=task.revision)
                control.checkpoint()
                tx.commit()
            return {"task_id": task.object_id, "document_id": document["id"], "document_revision": document["revision"]}

    def _current(self, request):
        arguments, scope_payload = request.get("arguments"), request.get("scope")
        if (not isinstance(arguments, Mapping) or set(arguments) != {"task_id", "context_packet_id"}
                or not isinstance(scope_payload, Mapping) or scope_payload.get("kind") != "project"):
            raise RecognitionConflict("task execution identity is invalid")
        scope = WorkScope("local-user", scope_payload.get("project_id"))
        task = self.service.records.read("recognition_tasks", arguments["task_id"])
        packet = self.service.records.read("recognition_context_packets", arguments["context_packet_id"])
        if (task is None or packet is None or task.payload.get("project_id") != scope.project_id
                or packet.payload.get("project_id") != scope.project_id
                or task.payload.get("turn_id") != request.get("turn_id")
                or task.payload.get("context_packet_id") != packet.object_id
                or packet.payload.get("task_id") != task.object_id or packet.payload.get("state") != "consumed"
                or task.payload.get("state") not in {"queued", "waiting_approval", "running", "result_ready", "completed"}
                or task.payload.get("input") != packet.payload.get("query")):
            raise RecognitionConflict("task is unavailable in this project")
        self._restructure_packet(task, packet, scope)
        self.verify_current(self.service, scope, packet.payload["items"])
        validate_packet_egress(self.service, self.models, scope, packet.payload)
        ProjectConstraintService(self.service.records).validate_snapshot(scope, packet.payload.get("constraints", ()))
        if packet.payload.get("model_revision") != self.models.public()["generation"]["revision"]:
            raise RecognitionConflict("task model configuration changed")
        return task, packet, scope

    def _restructure_packet(self, task, packet, scope):
        """Validate the frozen, model-only restructuring input before use.

        Missing kinds remain the legacy context task for compatibility.  A
        restructure task deliberately carries all of its data in the consumed
        packet: no model output can select a different operation or evidence
        graph after approval.
        """
        task_kind = task.payload.get("kind", "context")
        packet_kind = packet.payload.get("kind", "context")
        if task_kind != packet_kind or task_kind not in {"context", "restructure"}:
            raise RecognitionConflict("task context kind changed")
        if task_kind == "context":
            return None
        required = {
            "proposal_id", "snapshot", "operation", "step_version", "instruction",
            "query", "messages", "items", "constraints", "model_revision",
        }
        if not required.issubset(packet.payload):
            raise RecognitionConflict("restructure packet is incomplete")
        proposal_id = task.payload.get("proposal_id")
        if (not isinstance(proposal_id, str) or not proposal_id.strip()
                or packet.payload.get("proposal_id") != proposal_id):
            raise RecognitionConflict("restructure proposal identity changed")
        if packet.payload.get("step_version") != STEP_VERSION:
            raise RecognitionConflict("restructure implementation changed")
        if packet.payload.get("query") != task.payload.get("input"):
            raise RecognitionConflict("restructure task input changed")
        if not isinstance(packet.payload.get("instruction"), str) or not packet.payload["instruction"].strip():
            raise RecognitionConflict("restructure instruction is invalid")
        if not isinstance(packet.payload.get("messages"), list) or not packet.payload["messages"]:
            raise RecognitionConflict("restructure messages are invalid")
        snapshot = verify_restructure_snapshot(self.service, scope, packet.payload)
        if packet.payload.get("operation") not in {"revise", "split", "merge", "supersede", "revoke"}:
            raise RecognitionConflict("restructure operation is invalid")
        expected_items = {(row["id"], row["revision"]) for row in snapshot["recognitions"]}
        items = packet.payload["items"]
        if (not isinstance(items, list) or len(items) != len(expected_items)
                or {(item.get("id"), item.get("revision")) for item in items if isinstance(item, Mapping)} != expected_items):
            raise RecognitionConflict("restructure evidence items changed")
        return {"proposal_id": proposal_id, "snapshot": snapshot, "operation": packet.payload["operation"]}

    def _existing_result(self, task, scope):
        document = SQLiteDocumentRepository(self.service.records, namespace_id=self.document_namespace).read(task.payload["document_id"])
        if document is None or document.get("project_id") != scope.project_id:
            raise RecognitionConflict("task result is unavailable")
        return {"task_id": task.object_id, "document_id": document["id"], "document_revision": document["revision"]}

    def observe_terminal(self, receipt) -> None:
        """Project the existing runner's terminal receipt; never start execution."""
        if receipt.status not in {"completed", "failed", "cancelled"}:
            return
        request = self.payloads.get_request(receipt.turn_id)
        exact = request.get("capability_request", {})
        if exact.get("capability_id") not in {"recognition.task.execute", "recognition.task.execute.local"}:
            return
        task_id = exact.get("arguments", {}).get("task_id")
        with self.lock:
            task = self.service.records.read("recognition_tasks", task_id)
            if task is None or task.payload.get("turn_id") != receipt.turn_id:
                return
            if task.payload.get("state") in {"completed", "failed", "cancelled", "stale", "interrupted"}:
                return
            status = receipt.status
            reason = "turn_receipt"
            if status in {"completed", "failed"}:
                # A source/configuration conflict can make the tool fail after
                # the model has returned. Preserve that distinction for the UI
                # without publishing the rejected answer or provider details.
                try:
                    self._current({**request, "arguments": exact["arguments"]})
                except RecognitionConflict:
                    status = "stale"
                    reason = "authority_conflict"
            if status == "completed" and task.payload.get("kind") == "restructure":
                proposal = self.service.records.read(
                    "recognition_restructure_proposals", task.payload.get("proposal_id"),
                )
                if proposal is None or proposal.payload.get("origin_task_id") != task.object_id:
                    # Do not project a visible completed Turn when its
                    # internal proposal was absent or belongs to another task.
                    status = "stale"
                    reason = "proposal_unavailable"
            if status == "completed":
                if task.payload.get("state") != "result_ready":
                    status = "failed"
                    reason = "result_not_committed"
            with self.service.records.begin() as tx:
                tx.put("recognition_tasks", task.object_id, {**task.payload, "state": status,
                    "terminal_projection": {"turn_status": receipt.status,
                        "prior_state": task.payload.get("state"), "reason": reason},
                    "finished_at": self.now()}, expected_revision=task.revision)
                tx.commit()
