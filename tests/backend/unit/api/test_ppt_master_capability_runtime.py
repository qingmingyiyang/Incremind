from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from backend.api.ppt_master_capability_runtime import (
    FixedPptxGenerationCapability, PPT_MASTER_FIXED_CAPABILITY,
    ppt_master_fixed_capability_definition,
)
from core.effect_log import build_effect_runtime
from core.plugin_host.presentation_artifact import HostArtifactRoot, PresentationArtifactManifest, PresentationArtifactService


def _service(tmp_path: Path):
    upstream = tmp_path / "upstream"
    (upstream / "scripts").mkdir(parents=True)
    for name in ("attribution_guard.py", "svg_quality_checker.py", "svg_to_pptx.py", "pptx_delivery_check.py"):
        (upstream / "scripts" / name).write_text("# fixture", encoding="utf-8")
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    artifact = PresentationArtifactService(
        roots={"ppt-master-root": HostArtifactRoot(upstream, True)}, jobs_root=jobs,
        python_executable=Path(__file__).resolve(),
    )
    manifest = PresentationArtifactManifest("ppt-master-managed", "5.0.0", "abcdef0123456789abcdef0123456789abcdef01", "ppt-master-root")
    runtime = build_effect_runtime(tmp_path / "effects.sqlite3", owner_id="test")
    calls = []

    def runner(command, *, cwd, env, stdout):
        calls.append((tuple(command), cwd, dict(env), stdout))
        if command[1].endswith("pptx_delivery_check.py"):
            out, validation = cwd / "out", cwd / "validation"
            with zipfile.ZipFile(out / "presentation.pptx", "w") as archive:
                for name in ("[Content_Types].xml", "_rels/.rels", "ppt/presentation.xml", "ppt/slides/slide1.xml"):
                    archive.writestr(name, "<x/>")
            (validation / "presentation.report.json").write_text(json.dumps({"schema": "ppt-master.pptx-postflight-report.v1", "status": "passed"}), encoding="utf-8")
            (validation / "svg_quality_report.json").write_text(json.dumps({"schema": "ppt-master.svg-quality-report.v1", "stage": "final", "summary": {"errors": 0}, "files": [{"passed": True, "errors": []}]}), encoding="utf-8")
            stdout.write_text(json.dumps({"schema": "ppt-master.pptx-delivery-check.v1", "status": "passed", "file": {}, "package": {}, "slides": {"count": 1}, "fonts": {}, "media": {}, "motion": {}, "errors": [], "advisories": []}), encoding="utf-8")

    return FixedPptxGenerationCapability(effect_runtime=runtime, artifacts=artifact, manifest=manifest, jobs_root=jobs, clock=lambda: 100, process_runner=runner), runtime, calls


def _request():
    return {"turn_id": "turn-1", "scope": {"project_id": "project-1"},
            "authorization_facts_ref": "crp://session/turn-1/frozen-authorization-facts/facts-v1", "authorization_facts_revision": "policy-v1",
            "arguments": {"schema_version": "1.0.0", "operation_id": "operation-1", "slides": [{"title": "Hello", "body": "World"}]}}


def test_fixed_capability_effect_receipt_and_private_paths(tmp_path: Path) -> None:
    capability, runtime, calls = _service(tmp_path)
    response = capability.invoke(_request())
    with runtime.log._connect() as connection:
        operation_id = connection.execute("SELECT operation_id FROM effect WHERE kind=?", ("presentation_pptx_fixed_generate",)).fetchone()[0]
    effect = runtime.log.get(operation_id)
    assert effect.state.value == "SETTLED_OK" and effect.result_ref == "receipt:ppt-master-fixed/" + response["result"]["receipt_id"]
    assert effect.operation_id != response["result"]["operation_id"]
    assert effect.contract_version == "effect-v2"
    with runtime.log._connect() as connection:
        assert connection.execute("SELECT 1 FROM effect_gate_fact WHERE decision_id=?", (effect.gate_decision_id,)).fetchone()
        assert connection.execute("SELECT 1 FROM effect_intent_fact WHERE operation_id=?", (effect.operation_id,)).fetchone()
    assert response["result"]["replayed"] is False and len(calls) == 4
    assert calls[0][0][1].endswith("attribution_guard.py")
    assert calls[1][0][1].endswith("svg_quality_checker.py")
    assert calls[2][0][1].endswith("svg_to_pptx.py")
    assert calls[3][0][1].endswith("pptx_delivery_check.py")
    assert all(set(env) == {"PATH", "PYTHONNOUSERSITE", "PYTHONDONTWRITEBYTECODE", "PYTHONUTF8", "TMP", "TEMP", "USERPROFILE"} for _, _, env, _ in calls)
    encoded = json.dumps(response)
    assert "\\" not in encoded and "/jobs/" not in encoded
    assert response["result"]["artifact_ref"] == f"crp://presentations/{response['result']['result_id']}"
    assert capability.invoke(_request())["result"]["replayed"] is True and len(calls) == 4


def test_rejects_paths_commands_and_unapproved_input(tmp_path: Path) -> None:
    capability, _, _ = _service(tmp_path)
    for key, value in (("path", "C:/escape"), ("command", "python no"), ("slides", [{"title": "x", "path": "no"}])):
        request = _request()
        request["arguments"][key] = value
        with pytest.raises(Exception):
            capability.invoke(request)
    request = _request()
    request.pop("authorization_facts_ref")
    with pytest.raises(Exception):
        capability.invoke(request)
    for key in ("turn_id", "scope"):
        request = _request()
        request.pop(key)
        with pytest.raises(Exception):
            capability.invoke(request)
    request = _request()
    request["authorization_facts_ref"] = "gate:caller-controlled"
    with pytest.raises(Exception):
        capability.invoke(request)
    request = _request()
    request["project_id"] = "other-project"  # top-level project is not provider authority
    assert capability.invoke(request)["result"]["operation_id"].startswith("pptx-op-")


def test_definition_is_exclusive_receipt_backed() -> None:
    definition = ppt_master_fixed_capability_definition()
    assert definition.capability_id == PPT_MASTER_FIXED_CAPABILITY and definition.requires_approval
    assert definition.tool_definition and definition.tool_definition.execution_mode == "exclusive"
    assert definition.tool_definition.idempotency == "verify_before_retry"


def test_recovery_backfills_receipt_and_retries_only_partial_output(tmp_path: Path) -> None:
    capability, runtime, calls = _service(tmp_path)
    response = capability.invoke(_request())
    operation = response["result"]["operation_id"]
    with runtime.log._connect() as connection:
        effect_id = connection.execute("SELECT operation_id FROM effect WHERE kind=?", ("presentation_pptx_fixed_generate",)).fetchone()[0]
    effect = runtime.log.get(effect_id)
    receipt = capability._receipt_root / f"{operation}.json"
    receipt.unlink()
    assert capability._handle(effect).receipt_ref == "receipt:ppt-master-fixed/" + response["result"]["receipt_id"]
    assert len(calls) == 4, "valid output is recovered without rerunning subprocesses"
    receipt.unlink()
    job = capability._jobs_root / capability._job_id(operation) if hasattr(capability, "_job_id") else None
    job = capability._jobs_root / ("pptx-" + __import__("hashlib").sha256(operation.encode()).hexdigest()[:32])
    (job / "out").mkdir(exist_ok=True)
    (job / "out" / "partial.txt").write_text("partial", encoding="utf-8")
    assert capability._handle(effect).receipt_ref == "receipt:ppt-master-fixed/" + response["result"]["receipt_id"]
    assert len(calls) == 8, "partial managed outputs are cleared then rerun"
    state, receipt_ref = capability._probe(effect)
    assert state.value == "SETTLED_OK" and receipt_ref == "receipt:ppt-master-fixed/" + response["result"]["receipt_id"]
    assert len(calls) == 8, "probe never repeats the formal write"
    with runtime.log._connect() as connection:
        connection.execute(
            "UPDATE effect SET state='INFLIGHT', result_ref=NULL, settled_at=NULL, "
            "lease_owner='expired-runner', lease_expires_at=1 WHERE operation_id=?",
            (effect.operation_id,),
        )
        connection.commit()
    runtime.recover_expired(now=100, limit=10)
    recovered = runtime.log.get(effect.operation_id)
    assert recovered.state.value == "SETTLED_OK"
    assert recovered.result_ref == "receipt:ppt-master-fixed/" + response["result"]["receipt_id"]
    assert len(calls) == 8, "reaper probe settles receipt without repeating writer"


def test_dynamic_resolver_drift_is_fail_closed(tmp_path: Path) -> None:
    capability, _runtime, _calls = _service(tmp_path)
    state = {"commit": "abcdef0123456789abcdef0123456789abcdef01"}
    original = capability._artifact_resolver
    def resolver(project_id):
        artifacts, manifest = original(project_id)
        if state["commit"] == manifest.source_commit:
            return artifacts, manifest
        return artifacts, PresentationArtifactManifest(manifest.artifact_id, manifest.version, state["commit"], manifest.root_locator)
    def drift_runner(*args, **kwargs):
        state["commit"] = "0123456789abcdef0123456789abcdef01234567"
    dynamic = FixedPptxGenerationCapability(effect_runtime=build_effect_runtime(tmp_path / "dynamic.sqlite3", owner_id="dynamic"), jobs_root=tmp_path / "jobs", clock=lambda: 100, artifact_resolver=resolver, process_runner=drift_runner)
    request = _request()
    with pytest.raises(Exception):
        dynamic.invoke(request)


def test_result_resolution_requires_matching_current_receipt_and_verified_output(tmp_path: Path) -> None:
    capability, _runtime, _calls = _service(tmp_path)
    response = capability.invoke(_request())
    result_id = response["result"]["result_id"]
    output = capability.resolve_result(project_id="project-1", result_id=result_id)
    assert output.name == "presentation.pptx" and output.is_file()
    with pytest.raises(Exception):
        capability.resolve_result(project_id="other-project", result_id=result_id)
    with pytest.raises(Exception):
        capability.resolve_result(project_id="project-1", result_id="../presentation.pptx")
    receipt_path = capability._receipt_root / f"{response['result']['operation_id']}.json"
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload["artifact_commit"] = "0" * 40
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(Exception):
        capability.resolve_result(project_id="project-1", result_id=result_id)
