from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from core.plugin_host.presentation_artifact import (
    HostArtifactRoot,
    PresentationArtifactConflict,
    PresentationArtifactError,
    PresentationArtifactManifest,
    PresentationArtifactService,
)


def _service(tmp_path: Path) -> tuple[PresentationArtifactService, PresentationArtifactManifest, Path]:
    skill = tmp_path / "skill"
    (skill / "scripts").mkdir(parents=True)
    (skill / "scripts" / "attribution_guard.py").write_text("# guard", encoding="utf-8")
    (skill / "scripts" / "svg_quality_checker.py").write_text("# quality", encoding="utf-8")
    (skill / "scripts" / "svg_to_pptx.py").write_text("# converter", encoding="utf-8")
    (skill / "scripts" / "pptx_delivery_check.py").write_text("# checker", encoding="utf-8")
    jobs = tmp_path / "jobs"
    job = jobs / "job-001"
    (job / "svg_output").mkdir(parents=True)
    (job / "svg_output" / "slide-01.svg").write_text("<svg/>", encoding="utf-8")
    python = Path(__file__).resolve()
    manifest = PresentationArtifactManifest("ppt-master-managed", "5.0.0", "abcdef0123456789abcdef0123456789abcdef01", "ppt-master-root")
    return PresentationArtifactService(roots={"ppt-master-root": HostArtifactRoot(skill, True)}, jobs_root=jobs, python_executable=python), manifest, job


def _write_smoke(job: Path, *, status: str = "passed") -> None:
    out = job / "out"
    out.mkdir()
    validation = job / "validation"
    validation.mkdir()
    with zipfile.ZipFile(out / "presentation.pptx", "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("_rels/.rels", "<Relationships/>")
        archive.writestr("ppt/presentation.xml", "<p:presentation/>")
        archive.writestr("ppt/slides/slide1.xml", "<p:sld/>")
    (out / "delivery-report.json").write_text(json.dumps({
        "schema": "ppt-master.pptx-delivery-check.v1", "status": status,
        "file": {}, "package": {}, "slides": {"count": 1}, "fonts": {}, "media": {},
        "motion": {}, "errors": [], "advisories": [],
    }), encoding="utf-8")
    (validation / "presentation.report.json").write_text(json.dumps({
        "schema": "ppt-master.pptx-postflight-report.v1", "status": "passed", "output": {},
    }), encoding="utf-8")
    (validation / "svg_quality_report.json").write_text(json.dumps({
        "schema": "ppt-master.svg-quality-report.v1", "stage": "final",
        "summary": {"errors": 0}, "files": [{"passed": True, "errors": []}],
    }), encoding="utf-8")


def test_manifest_is_opaque_and_plan_is_fixed_to_ppt_master_boundaries(tmp_path: Path) -> None:
    service, manifest, job = _service(tmp_path)
    plan = service.plan(manifest, job_id="job-001")
    assert manifest.payload() == {"artifact_id": "ppt-master-managed", "version": "5.0.0", "source": "ppt-master", "source_commit": "abcdef0123456789abcdef0123456789abcdef01", "root_locator": "ppt-master-root"}
    guard, quality, converter, checker = plan.commands
    assert guard.argv[0].endswith("scripts\\attribution_guard.py") and guard.cwd.name == "skill"
    assert quality.argv[1:] == (str(job), "--quick-generate", "--stage", "final", "--json")
    assert converter.cwd == job and converter.argv[1:4] == (str(job), "--output", str(job / "out" / "presentation.pptx"))
    assert converter.argv[-4:] == ("--quick-generate", "--no-notes", "--no-animations", "--quiet")
    assert checker.argv[-1] == str(job / "out" / "presentation.pptx") and checker.stdout == job / "out" / "delivery-report.json"
    assert dict(converter.environment) == {"PATH": str(converter.executable.parent), "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1", "TMP": str(job / "out"), "TEMP": str(job / "out"), "USERPROFILE": str(job)}
    assert plan.required_directories == (job / "out", job / "validation")
    assert service.rollback_projection(job_id="job-001")["remove"] == (str(job / "out" / "presentation.pptx"), str(job / "out" / "delivery-report.json"))


def test_plan_rejects_unresolved_root_and_non_svg_or_extra_job_input(tmp_path: Path) -> None:
    service, manifest, job = _service(tmp_path)
    with pytest.raises(PresentationArtifactError, match="source"):
        PresentationArtifactManifest("x", "1", "abcdef0123456789abcdef0123456789abcdef01", "ppt-master-root", source="other")
    with pytest.raises(PresentationArtifactConflict, match="unavailable"):
        service.plan(PresentationArtifactManifest("x", "1", "abcdef0123456789abcdef0123456789abcdef01", "missing-root"), job_id="job-001")
    (job / "svg_output" / "notes.txt").write_text("no", encoding="utf-8")
    with pytest.raises(PresentationArtifactConflict, match="svg_output"):
        service.plan(manifest, job_id="job-001")


def test_smoke_checks_zip_report_schema_and_exact_output_boundary(tmp_path: Path) -> None:
    service, _manifest, job = _service(tmp_path)
    _write_smoke(job, status="passed-with-advisories")
    receipt = service.verify_smoke(job_id="job-001")
    assert receipt.slide_count == 1 and receipt.delivery_status == "passed-with-advisories"
    (job / "out" / "escaped.txt").write_text("no", encoding="utf-8")
    with pytest.raises(PresentationArtifactConflict, match="allowed boundary"):
        service.verify_smoke(job_id="job-001")
