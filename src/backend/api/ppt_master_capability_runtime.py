"""Fixed, receipt-backed PPTX generation over the managed ppt-master artifact."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from html import escape
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
from typing import Protocol

from core.ai_kernel import CapabilityDefinition
from core.ai_kernel.dispatcher import ToolProviderFailure
from core.ai_tooling import ToolDefinition, ToolRetryPolicy
from core.effect_log import Effect, EffectClass, EffectHandlerRegistration, EffectIntent, EffectState
from core.effect_log.core import EFFECT_V2, V2_REVISION_KEYS, EffectReceipt, GateDecision, GateDecisionFact
from core.plugin_host.presentation_artifact import PresentationArtifactManifest, PresentationArtifactService, PresentationExecutionPlan

PPT_MASTER_FIXED_CAPABILITY = "presentation.pptx.fixed"
PPT_MASTER_FIXED_EFFECT_KIND = "presentation_pptx_fixed_generate"
PPT_MASTER_FIXED_INTENT_SCHEMA = "ppt-master-fixed-intent.v2"
PPT_MASTER_FIXED_RECEIPT_SCHEMA = "ppt-master-fixed-receipt.v1"
PPT_MASTER_FIXED_RECEIPT_KIND = "presentation_pptx_fixed_generate-receipt"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_PROJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_JOB = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
_OPERATION = re.compile(r"^pptx-op-[a-f0-9]{40}$")
_RESULT = re.compile(r"^pptx-result-[a-f0-9]{24}$")


class FixedPptxCapabilityError(ValueError):
    pass


class ProcessRunner(Protocol):
    def __call__(self, command: Sequence[str], *, cwd: Path, env: Mapping[str, str], stdout: Path | None) -> None: ...


class ArtifactResolver(Protocol):
    def __call__(self, project_id: str) -> tuple[PresentationArtifactService, PresentationArtifactManifest]: ...


@dataclass(frozen=True, slots=True)
class FixedPptxReceipt:
    operation_id: str
    project_id: str
    request_id: str
    input_digest: str
    artifact_commit: str
    result_id: str
    receipt_id: str
    artifact_ref: str
    slide_count: int


def ppt_master_fixed_capability_definition() -> CapabilityDefinition:
    input_schema = "crp://default/contracts/ppt-master-fixed-request.schema.json"
    output_schema = "crp://default/contracts/ppt-master-fixed-result.schema.json"
    return CapabilityDefinition(
        PPT_MASTER_FIXED_CAPABILITY, 1, "write", True, "receipt_required", input_schema, output_schema,
        tool_definition=ToolDefinition(
            tool_id=PPT_MASTER_FIXED_CAPABILITY, version=1, display_name="Fixed PPTX generation",
            # The provider is host-owned, while its availability is governed
            # by the installed PPT Master plugin/profile.  Classifying it as
            # ``core`` would expose it to new Turns before installation.
            description="Host-governed editable PPTX generation", source="plugin", owner_id="ppt-master",
            effect="write", data_classes=("project_content",), destination="local",
            input_schema_uri=input_schema, output_schema_uri=output_schema,
            receipt_schema_uri="crp://default/contracts/ppt-master-fixed-receipt.schema.json",
            operation_semantics="receipt_required", execution_mode="exclusive",
            resource_locks=("presentation:pptx",), idempotency="verify_before_retry",
            retry_policy=ToolRetryPolicy(1, 0, ()), verification_tool_id="presentation.pptx.fixed.probe",
            compensation_tool_id=None, mutability="reversible", egress_class="local",
            network_scope=(), data_egress_scope=(), timeout_ms=300_000, required_scopes=(),
            boundary_requirements=("project_plugin_enabled",),
        ),
    )


class FixedPptxGenerationCapability:
    """Text-only provider: host owns SVG, commands, paths and durable receipts."""

    def __init__(
        self, *, effect_runtime, jobs_root: Path, clock: Callable[[], int],
        artifacts: PresentationArtifactService | None = None, manifest: PresentationArtifactManifest | None = None,
        artifact_resolver: ArtifactResolver | None = None, process_runner: ProcessRunner | None = None,
    ) -> None:
        if artifact_resolver is None:
            if not isinstance(artifacts, PresentationArtifactService) or not isinstance(manifest, PresentationArtifactManifest):
                raise ValueError("fixed PPTX capability requires an artifact resolver or static artifact")
            artifact_resolver = lambda _project: (artifacts, manifest)
        elif artifacts is not None or manifest is not None:
            raise ValueError("fixed PPTX artifact resolver cannot be mixed with static artifact")
        self._effects, self._artifact_resolver = effect_runtime, artifact_resolver
        self._jobs_root, self._clock = Path(jobs_root).resolve(strict=False), clock
        self._run = process_runner or _run_process
        self._receipt_root = self._jobs_root / ".receipts"
        self._request_root = self._jobs_root / ".requests"
        self._effects.handlers.register(EffectHandlerRegistration(
            PPT_MASTER_FIXED_EFFECT_KIND, EffectClass.QUERYABLE, self._handle, self._probe,
            contract_version=EFFECT_V2, intent_schema_version=PPT_MASTER_FIXED_INTENT_SCHEMA,
            receipt_kind=PPT_MASTER_FIXED_RECEIPT_KIND,
            receipt_schema_version=PPT_MASTER_FIXED_RECEIPT_SCHEMA,
        ))

    def invoke(self, request: Mapping[str, object]) -> Mapping[str, object]:
        try:
            request_id, turn, project, slides, authorization = _request(request)
            _artifacts, manifest = self._resolve(project)
            input_digest = _input_digest(slides)
            operation = _operation_id(project, request_id, input_digest)
            receipt = self._receipt(operation, project=project, request_id=request_id, input_digest=input_digest, artifact_commit=manifest.source_commit)
            if receipt:
                return _public(receipt, replayed=True)
            job_id = _job_id(operation)
            self._prepare(job_id, project, request_id, input_digest, slides)
            gate_id, gate_fact = _gate(authorization, project=project, domain_operation=operation)
            intent = EffectIntent(
                session_id=turn, turn_id=turn, root_id=project, step_key=f"pptx:{operation}",
                kind=PPT_MASTER_FIXED_EFFECT_KIND, effect_class=EffectClass.QUERYABLE,
                intent_ref=f"intent:ppt-master-fixed/{operation}", gate_decision_id=gate_id,
                rev_set=_revisions(manifest.source_commit, authorization["revision"]),
                payload={"domain_operation_id": operation, "job_id": job_id,
                         "request_ref": f"intent:ppt-master-request/{operation}",
                         "authorization_facts_ref": authorization["ref"],
                         "artifact_digest": manifest.source_commit, "slide_count": len(slides)},
                idem_key=operation, contract_version=EFFECT_V2,
                intent_schema_version=PPT_MASTER_FIXED_INTENT_SCHEMA,
                expected_receipt_kind=PPT_MASTER_FIXED_RECEIPT_KIND,
                expected_receipt_schema_version=PPT_MASTER_FIXED_RECEIPT_SCHEMA,
            )
            planned, _ = self._effects.log.plan_v2(
                intent, gate_decision_id=gate_id, gate_fact=gate_fact, now=self._clock(),
            )
            done = self._effects.dispatch_operation(planned.operation_id, now=self._clock())
            receipt = self._receipt(operation, project=project, request_id=request_id, input_digest=input_digest, artifact_commit=manifest.source_commit)
            if done.state is not EffectState.SETTLED_OK or receipt is None:
                raise FixedPptxCapabilityError("PPTX generation did not settle")
            return _public(receipt, replayed=False)
        except ToolProviderFailure:
            raise
        except Exception as error:
            raise ToolProviderFailure("presentation.pptx.fixed_failed", effect_certainty="unknown") from error

    def _prepare(self, job_id: str, project: str, request_id: str, input_digest: str, slides: tuple[tuple[str, str], ...]) -> None:
        target, staging = self._jobs_root / job_id, self._jobs_root / f".{job_id}.pending"
        if staging.exists():
            raise FixedPptxCapabilityError("controlled presentation job conflicts")
        expected = {f"slide-{index:02d}.svg": _svg(title, body) for index, (title, body) in enumerate(slides, 1)}
        self._write_request(job_id, project, request_id, input_digest)
        if target.exists():
            actual = target / "svg_output"
            found = {item.name: item.read_text(encoding="utf-8") for item in actual.glob("*.svg")} if actual.is_dir() else {}
            if found != expected:
                raise FixedPptxCapabilityError("controlled presentation job input drifted")
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        output = staging / "svg_output"
        output.mkdir(parents=True)
        for name, body in expected.items():
            (output / name).write_text(body, encoding="utf-8")
        staging.replace(target)

    def _handle(self, effect: Effect) -> EffectReceipt:
        operation = _effect_domain_operation(effect)
        job_id = _job_id(operation)
        receipt = self._receipt(operation)
        if receipt:
            return _effect_receipt(receipt)
        job = self._jobs_root / job_id / "svg_output"
        count = len(tuple(job.glob("*.svg"))) if job.is_dir() else 0
        if not _JOB.fullmatch(job_id) or not 1 <= count <= 12:
            raise FixedPptxCapabilityError("PPTX Effect inputs are unavailable")
        facts = self._request_facts(job_id)
        artifacts, manifest = self._resolve(effect.root_id)
        if effect.rev_set.get("bundle") != manifest.source_commit:
            raise FixedPptxCapabilityError("PPTX artifact revision drifted")
        try:
            verified = artifacts.verify_smoke(job_id=job_id)
        except Exception:
            self._clear_partial_outputs(job_id)
            verified = None
        if verified is not None:
            return _effect_receipt(self._store_verified_receipt(operation, facts, manifest.source_commit, verified.slide_count))
        plan = artifacts.plan(manifest, job_id=job_id)
        self._execute(plan)
        verified = artifacts.verify_smoke(job_id=job_id)
        if verified.slide_count != count:
            raise FixedPptxCapabilityError("PPTX delivery report slide count drifted")
        return _effect_receipt(self._store_verified_receipt(operation, facts, manifest.source_commit, count))

    def _execute(self, plan: PresentationExecutionPlan) -> None:
        for directory in plan.required_directories:
            directory.mkdir(exist_ok=False)
        for command in plan.commands:
            self._run((str(command.executable), *command.argv), cwd=command.cwd, env=command.environment, stdout=command.stdout)

    def _probe(self, effect: Effect) -> tuple[EffectState, str | None]:
        operation = _effect_domain_operation(effect)
        receipt = self._receipt(operation)
        if not receipt:
            return EffectState.PLANNED, None
        try:
            artifacts, manifest = self._resolve(effect.root_id)
            if effect.rev_set.get("bundle") != manifest.source_commit:
                return EffectState.PLANNED, None
            valid = artifacts.verify_smoke(job_id=_job_id(operation))
        except Exception:
            return EffectState.PLANNED, None
        return (EffectState.SETTLED_OK, _effect_receipt(receipt).receipt_ref) if valid.slide_count == receipt.slide_count else (EffectState.PLANNED, None)

    def _receipt(self, operation_id: str, *, project: str | None = None, request_id: str | None = None, input_digest: str | None = None, artifact_commit: str | None = None) -> FixedPptxReceipt | None:
        path = self._receipt_root / f"{operation_id}.json"
        if not path.is_file() or path.is_symlink():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            receipt = FixedPptxReceipt(
                operation_id=str(payload["operation_id"]), project_id=str(payload["project_id"]), request_id=str(payload["request_id"]),
                input_digest=str(payload["input_digest"]), artifact_commit=str(payload["artifact_commit"]),
                result_id=str(payload["result_id"]), receipt_id=str(payload["receipt_id"]), artifact_ref=str(payload["artifact_ref"]),
                slide_count=payload["slide_count"],
            )
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise FixedPptxCapabilityError("PPTX receipt is invalid") from error
        if set(payload) != {"operation_id", "project_id", "request_id", "input_digest", "artifact_commit", "result_id", "receipt_id", "artifact_ref", "slide_count"} or receipt.operation_id != operation_id or not _PROJECT.fullmatch(receipt.project_id) or not _ID.fullmatch(receipt.request_id) or not re.fullmatch(r"[a-f0-9]{64}", receipt.input_digest) or not re.fullmatch(r"[a-f0-9]{40}", receipt.artifact_commit) or not _ID.fullmatch(receipt.result_id) or not _ID.fullmatch(receipt.receipt_id) or not isinstance(receipt.slide_count, int) or isinstance(receipt.slide_count, bool) or not 1 <= receipt.slide_count <= 12 or receipt.artifact_ref != f"crp://presentations/{receipt.result_id}":
            raise FixedPptxCapabilityError("PPTX receipt identity drifted")
        if project is not None and (receipt.project_id != project or receipt.request_id != request_id or receipt.input_digest != input_digest or receipt.artifact_commit != artifact_commit):
            raise FixedPptxCapabilityError("PPTX receipt request identity drifted")
        return receipt

    def _store_receipt(self, receipt: FixedPptxReceipt) -> None:
        self._receipt_root.mkdir(exist_ok=True)
        target = self._receipt_root / f"{receipt.operation_id}.json"
        if target.exists():
            raise FixedPptxCapabilityError("PPTX receipt already exists")
        pending = self._receipt_root / f".{receipt.operation_id}.pending"
        pending.write_text(json.dumps({"operation_id": receipt.operation_id, "project_id": receipt.project_id, "request_id": receipt.request_id, "input_digest": receipt.input_digest, "artifact_commit": receipt.artifact_commit, "result_id": receipt.result_id, "receipt_id": receipt.receipt_id, "artifact_ref": receipt.artifact_ref, "slide_count": receipt.slide_count}, sort_keys=True), encoding="utf-8")
        pending.replace(target)

    def _write_request(self, job_id: str, project: str, request_id: str, input_digest: str) -> None:
        self._request_root.mkdir(exist_ok=True)
        target = self._request_root / f"{job_id}.json"
        expected = {"project_id": project, "request_id": request_id, "input_digest": input_digest}
        if target.exists():
            try: current = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error: raise FixedPptxCapabilityError("PPTX request identity is invalid") from error
            if current != expected: raise FixedPptxCapabilityError("PPTX request identity drifted")
            return
        pending = self._request_root / f".{job_id}.pending"; pending.write_text(json.dumps(expected, sort_keys=True), encoding="utf-8"); pending.replace(target)

    def _request_facts(self, job_id: str) -> Mapping[str, str]:
        try: facts = json.loads((self._request_root / f"{job_id}.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error: raise FixedPptxCapabilityError("PPTX request identity is unavailable") from error
        if not isinstance(facts, dict) or set(facts) != {"project_id", "request_id", "input_digest"} or not _PROJECT.fullmatch(str(facts.get("project_id"))) or not _ID.fullmatch(str(facts.get("request_id"))) or not re.fullmatch(r"[a-f0-9]{64}", str(facts.get("input_digest"))): raise FixedPptxCapabilityError("PPTX request identity is invalid")
        return facts

    def _store_verified_receipt(self, operation: str, facts: Mapping[str, str], artifact_commit: str, count: int) -> str:
        result = "pptx-result-" + hashlib.sha256(operation.encode()).hexdigest()[:24]
        receipt = FixedPptxReceipt(operation, facts["project_id"], facts["request_id"], facts["input_digest"], artifact_commit, result, "pptx-receipt-" + result[12:], f"crp://presentations/{result}", count)
        self._store_receipt(receipt)
        return receipt.receipt_id

    def _clear_partial_outputs(self, job_id: str) -> None:
        job = self._jobs_root / job_id
        for name in ("out", "validation"):
            candidate = job / name
            if candidate.exists():
                if not candidate.is_dir() or candidate.is_symlink(): raise FixedPptxCapabilityError("PPTX partial output boundary is invalid")
                shutil.rmtree(candidate)

    def _resolve(self, project_id: str) -> tuple[PresentationArtifactService, PresentationArtifactManifest]:
        resolved = self._artifact_resolver(project_id)
        if not isinstance(resolved, tuple) or len(resolved) != 2 or not isinstance(resolved[0], PresentationArtifactService) or not isinstance(resolved[1], PresentationArtifactManifest):
            raise FixedPptxCapabilityError("PPTX artifact resolver is invalid")
        return resolved

    def resolve_result(self, *, project_id: str, result_id: str) -> Path:
        """Resolve a host-owned PPTX only when its current receipt still verifies.

        `result_id` is a durable public handle, never a filename or user path.
        The dynamic artifact resolver is deliberately consulted first: a rolled-back
        installation, project mismatch, or artifact revision change therefore makes
        every older result unavailable.
        """
        if not isinstance(project_id, str) or not _PROJECT.fullmatch(project_id):
            raise FixedPptxCapabilityError("PPTX result is unavailable")
        if not isinstance(result_id, str) or not _RESULT.fullmatch(result_id):
            raise FixedPptxCapabilityError("PPTX result is unavailable")
        artifacts, manifest = self._resolve(project_id)
        if not self._receipt_root.is_dir() or self._receipt_root.is_symlink():
            raise FixedPptxCapabilityError("PPTX result is unavailable")
        matches: list[FixedPptxReceipt] = []
        for candidate in self._receipt_root.glob("pptx-op-*.json"):
            if candidate.is_symlink() or not candidate.is_file() or not _OPERATION.fullmatch(candidate.stem):
                continue
            receipt = self._receipt(candidate.stem)
            if receipt is not None and receipt.result_id == result_id:
                matches.append(receipt)
        if len(matches) != 1:
            raise FixedPptxCapabilityError("PPTX result is unavailable")
        receipt = matches[0]
        if (receipt.project_id != project_id
                or receipt.artifact_commit != manifest.source_commit
                or receipt.operation_id != _operation_id(receipt.project_id, receipt.request_id, receipt.input_digest)):
            raise FixedPptxCapabilityError("PPTX result is unavailable")
        job_id = _job_id(receipt.operation_id)
        try:
            verified = artifacts.verify_smoke(job_id=job_id)
        except Exception as error:
            raise FixedPptxCapabilityError("PPTX result is unavailable") from error
        output = verified.output_pptx
        if verified.slide_count != receipt.slide_count or output.name != "presentation.pptx" or output.is_symlink() or not output.is_file():
            raise FixedPptxCapabilityError("PPTX result is unavailable")
        return output


def _run_process(command: Sequence[str], *, cwd: Path, env: Mapping[str, str], stdout: Path | None) -> None:
    with (stdout.open("wb") if stdout else _Null()) as stream:
        completed = subprocess.run(list(command), cwd=str(cwd), env=dict(env), stdout=stream,
            stderr=subprocess.PIPE, shell=False, check=False, timeout=300)
    if completed.returncode:
        raise FixedPptxCapabilityError("fixed PPTX command failed")


class _Null:
    def __enter__(self): return None
    def __exit__(self, *_): return False


def _request(value: Mapping[str, object]) -> tuple[str, str, str, tuple[tuple[str, str], ...], Mapping[str, str]]:
    if not isinstance(value, Mapping):
        raise FixedPptxCapabilityError("PPTX request is invalid")
    args = value.get("arguments")
    args = args if isinstance(args, Mapping) else value
    if set(args) - {"schema_version", "operation_id", "slides"}:
        raise FixedPptxCapabilityError("PPTX request contains unsupported fields")
    if args.get("schema_version") != "1.0.0":
        raise FixedPptxCapabilityError("PPTX request schema version is invalid")
    operation = _required(args.get("operation_id") or value.get("operation_id"), "operation id")
    turn = _required(value.get("turn_id"), "turn id")
    scope = value.get("scope")
    project = scope.get("project_id") if isinstance(scope, Mapping) else None
    if not isinstance(project, str) or not _PROJECT.fullmatch(project):
        raise FixedPptxCapabilityError("project id is invalid")
    raw = args.get("slides")
    if not isinstance(raw, list) or not 1 <= len(raw) <= 12:
        raise FixedPptxCapabilityError("slides must contain between 1 and 12 items")
    slides: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, Mapping) or set(item) - {"title", "body"} or "title" not in item:
            raise FixedPptxCapabilityError("slide structure is invalid")
        title = _text(item.get("title"), "slide title", 600)
        body = item.get("body", "")
        if not isinstance(body, str) or len(body) > 2400:
            raise FixedPptxCapabilityError("slide body is invalid")
        slides.append((title, body))
    return operation, turn, project, tuple(slides), _authorization(value)


def _required(value: object, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise FixedPptxCapabilityError(f"{label} is invalid")
    return value


def _text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise FixedPptxCapabilityError(f"{label} is invalid")
    return value.strip()


def _authorization(request: Mapping[str, object]) -> Mapping[str, str]:
    """Require the frozen authority handle carried by the AI-kernel provider request."""
    ref, revision = request.get("authorization_facts_ref"), request.get("authorization_facts_revision")
    is_kernel_fact = isinstance(ref, str) and re.fullmatch(
        r"crp://session/[A-Za-z0-9._:@+\\-]{1,160}/frozen-authorization-facts/[A-Za-z0-9._:@+\\-]{1,160}", ref,
    )
    is_host_fact = isinstance(ref, str) and re.fullmatch(r"(?:facts|scope):[A-Za-z0-9._:/@+\\-]{1,160}", ref)
    if not (is_kernel_fact or is_host_fact):
        raise FixedPptxCapabilityError("frozen authorization facts are required")
    if not isinstance(revision, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@/+?&=\\-]{0,159}", revision):
        raise FixedPptxCapabilityError("frozen authorization revision is required")
    return {"ref": ref, "revision": revision}


def _gate(authorization: Mapping[str, str], *, project: str, domain_operation: str) -> tuple[str, GateDecisionFact]:
    material = f"{authorization['ref']}\\0{authorization['revision']}\\0{project}\\0{domain_operation}".encode()
    digest = hashlib.sha256(material).hexdigest()
    return "gate:ppt-master-fixed/" + digest, GateDecisionFact(
        decision=GateDecision.ALLOW,
        rule_ref="rule:ppt-master-fixed/authorization-frozen-v1",
        scope_ref=f"scope:project/{project}",
        budget_after={"authorization_ref": authorization["ref"], "operation_id": domain_operation},
        secret_scope="scope:local/ppt-master",
        policy_revision=authorization["revision"],
    )


def _revisions(artifact_commit: str, authorization_revision: str) -> dict[str, str]:
    values = {key: "not_applicable" for key in V2_REVISION_KEYS}
    values.update({"policy": authorization_revision, "boundary": "ppt-master-boundary-v1",
                   "capability": "ppt-master-fixed-v1", "context_manifest": authorization_revision,
                   "provider": "ppt-master-host-v1", "bundle": artifact_commit,
                   "handler": "ppt-master-fixed-handler-v2"})
    return values


def _effect_domain_operation(effect: Effect) -> str:
    prefix = "intent:ppt-master-fixed/"
    operation = effect.intent_ref[len(prefix):] if effect.intent_ref.startswith(prefix) else ""
    if not _OPERATION.fullmatch(operation):
        raise FixedPptxCapabilityError("PPTX Effect domain operation is invalid")
    return operation


def _effect_receipt(receipt: FixedPptxReceipt | str) -> EffectReceipt:
    receipt_ref = receipt if isinstance(receipt, str) else receipt.receipt_id
    return EffectReceipt("receipt:ppt-master-fixed/" + receipt_ref, PPT_MASTER_FIXED_RECEIPT_KIND,
                         PPT_MASTER_FIXED_RECEIPT_SCHEMA, PPT_MASTER_FIXED_INTENT_SCHEMA)


def _job_id(operation: str) -> str:
    return "pptx-" + hashlib.sha256(operation.encode()).hexdigest()[:32]


def _input_digest(slides: tuple[tuple[str, str], ...]) -> str:
    return hashlib.sha256(json.dumps(slides, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _operation_id(project: str, request_id: str, input_digest: str) -> str:
    material = f"{project}\0{request_id}\0{input_digest}".encode("utf-8")
    return "pptx-op-" + hashlib.sha256(material).hexdigest()[:40]


def _svg(title: str, body: str) -> str:
    lines = "".join(f'<text x="100" y="{330+i*54}" font-family="Aptos" font-size="28">{escape(line)}</text>' for i, line in enumerate(body.splitlines()[:12]))
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="900" viewBox="0 0 1600 900"><rect width="1600" height="900" fill="#ffffff"/><text x="100" y="190" font-family="Aptos Display" font-size="60" font-weight="700">{escape(title)}</text>{lines}</svg>'


def _public(receipt: FixedPptxReceipt, *, replayed: bool) -> dict[str, object]:
    return {"summary": "Fixed PPTX generation completed", "receipt_ref": receipt.receipt_id,
        "payload_ref": None, "evidence_refs": [], "result": {"schema_version": "1.0.0",
        "operation_id": receipt.operation_id, "receipt_id": receipt.receipt_id, "result_id": receipt.result_id, "artifact_ref": receipt.artifact_ref,
        "slide_count": receipt.slide_count, "replayed": replayed}}
