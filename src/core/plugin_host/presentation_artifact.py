"""Fail-closed planning and smoke verification for the managed ppt-master artifact.

The module does not discover skills, execute subprocesses, or accept caller
paths.  The host supplies an opaque root locator and resolves it through its
own fixed mapping; a job can only consume ``svg_output/*.svg`` and publish the
single PPTX plus the delivery-check JSON beneath its owned root.
"""
from __future__ import annotations

import json
import re
import stat
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType


_SOURCE = "ppt-master"
_LOCATOR = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
_COMMIT = re.compile(r"^[0-9a-f]{7,64}$")
_JOB = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
_REPORT_SCHEMA = "ppt-master.pptx-delivery-check.v1"
_OUTPUT = "presentation.pptx"
_REPORT = "delivery-report.json"


class PresentationArtifactError(ValueError):
    """Raised when a presentation artifact request exceeds the host contract."""


class PresentationArtifactConflict(PresentationArtifactError):
    """Raised when a host-owned artifact root or job tree has drifted."""


@dataclass(frozen=True, slots=True)
class PresentationArtifactManifest:
    """Durable identity only; the locator is resolved by the host, never a path."""

    artifact_id: str
    version: str
    source_commit: str
    root_locator: str
    source: str = _SOURCE

    def __post_init__(self) -> None:
        if self.source != _SOURCE:
            raise PresentationArtifactError("presentation artifact source is not allowed")
        _token(self.artifact_id, "artifact_id", 96)
        _token(self.version, "version", 64)
        if not isinstance(self.source_commit, str) or not re.fullmatch(r"[0-9a-f]{40}", self.source_commit):
            raise PresentationArtifactError("source_commit is invalid")
        if not isinstance(self.root_locator, str) or not _LOCATOR.fullmatch(self.root_locator):
            raise PresentationArtifactError("root_locator is invalid")

    def payload(self) -> dict[str, str]:
        return {"artifact_id": self.artifact_id, "version": self.version, "source": self.source,
                "source_commit": self.source_commit, "root_locator": self.root_locator}


@dataclass(frozen=True, slots=True)
class HostArtifactRoot:
    """Host-resolved root.  The read-only assertion is not package-controlled."""

    root: Path
    read_only: bool


@dataclass(frozen=True, slots=True)
class PresentationCommand:
    """One host-spawned process with no caller-controlled command fields."""

    executable: Path
    argv: tuple[str, ...]
    cwd: Path
    environment: Mapping[str, str]
    stdout: Path | None = None


@dataclass(frozen=True, slots=True)
class PresentationExecutionPlan:
    """A fixed guard → converter → checker plan; this module never spawns it."""

    commands: tuple[PresentationCommand, ...]
    output_pptx: Path
    validation_report: Path
    postflight_report: Path
    required_directories: tuple[Path, Path]


@dataclass(frozen=True, slots=True)
class PresentationSmokeReceipt:
    job_id: str
    output_pptx: Path
    validation_report: Path
    slide_count: int
    delivery_status: str


class PresentationArtifactService:
    """Resolve one reviewed ppt-master artifact and constrain presentation jobs."""

    def __init__(self, *, roots: Mapping[str, HostArtifactRoot], jobs_root: Path,
                 python_executable: Path) -> None:
        if not jobs_root.is_absolute() or not jobs_root.is_dir() or _is_link(jobs_root):
            raise PresentationArtifactError("controlled jobs root is invalid")
        if not python_executable.is_absolute() or not python_executable.is_file() or _is_link(python_executable):
            raise PresentationArtifactError("python executable is invalid")
        self._roots = MappingProxyType(dict(roots))
        self._jobs_root = jobs_root.resolve(strict=True)
        self._python = python_executable.resolve(strict=True)

    def plan(self, manifest: PresentationArtifactManifest, *, job_id: str) -> PresentationExecutionPlan:
        root = self._resolve(manifest)
        job = self._job(job_id)
        self._validate_inputs(job)
        out = job / "out"
        validation = job / "validation"
        output = out / _OUTPUT
        report = out / _REPORT
        postflight = validation / "presentation.report.json"
        environment = MappingProxyType({
            "PATH": str(self._python.parent), "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1",
            "TMP": str(out), "TEMP": str(out), "USERPROFILE": str(job),
        })
        converter = root / "scripts" / "svg_to_pptx.py"
        checker = root / "scripts" / "pptx_delivery_check.py"
        guard = root / "scripts" / "attribution_guard.py"
        quality = root / "scripts" / "svg_quality_checker.py"
        if not guard.is_file() or _is_link(guard):
            raise PresentationArtifactConflict("required ppt-master integrity guard is missing")
        if not quality.is_file() or _is_link(quality):
            raise PresentationArtifactConflict("required ppt-master quality checker is missing")
        return PresentationExecutionPlan(
            commands=(
                PresentationCommand(self._python, (str(guard),), root, environment),
                PresentationCommand(self._python, (str(quality), str(job), "--quick-generate", "--stage", "final", "--json"), job, environment),
                PresentationCommand(self._python, (str(converter), str(job), "--output", str(output), "--quick-generate", "--no-notes", "--no-animations", "--quiet"), job, environment),
                PresentationCommand(self._python, (str(checker), str(output)), job, environment, report),
            ),
            output_pptx=output,
            validation_report=report,
            postflight_report=postflight,
            required_directories=(out, validation),
        )

    def verify_smoke(self, *, job_id: str) -> PresentationSmokeReceipt:
        job = self._job(job_id)
        self._validate_job_boundary(job, require_outputs=True)
        output, report = job / "out" / _OUTPUT, job / "out" / _REPORT
        quality_report = job / "validation" / "svg_quality_report.json"
        postflight = job / "validation" / "presentation.report.json"
        try:
            with zipfile.ZipFile(output) as archive:
                if archive.testzip() is not None:
                    raise PresentationArtifactConflict("PPTX ZIP integrity failed")
                names = set(archive.namelist())
        except (OSError, zipfile.BadZipFile) as exc:
            raise PresentationArtifactConflict("PPTX output is not a valid ZIP package") from exc
        required = {"[Content_Types].xml", "_rels/.rels", "ppt/presentation.xml"}
        if not required <= names or not any(re.fullmatch(r"ppt/slides/slide[1-9][0-9]*\.xml", item) for item in names):
            raise PresentationArtifactConflict("PPTX package structure is incomplete")
        try:
            payload = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PresentationArtifactConflict("delivery report is invalid") from exc
        required_report = {"schema", "status", "file", "package", "slides", "fonts", "media", "motion", "errors", "advisories"}
        if not isinstance(payload, dict) or set(payload) != required_report or payload.get("schema") != _REPORT_SCHEMA:
            raise PresentationArtifactConflict("delivery report schema is invalid")
        if payload.get("status") not in {"passed", "passed-with-advisories"} or not isinstance(payload.get("slides"), dict):
            raise PresentationArtifactConflict("delivery report did not pass")
        slides = payload["slides"].get("count")
        if not isinstance(slides, int) or isinstance(slides, bool) or slides < 1:
            raise PresentationArtifactConflict("delivery report slide count is invalid")
        try:
            quality_payload = json.loads(quality_report.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PresentationArtifactConflict("SVG quality report is invalid") from exc
        _validate_quality_report(quality_payload)
        try:
            postflight_payload = json.loads(postflight.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PresentationArtifactConflict("postflight report is invalid") from exc
        if not isinstance(postflight_payload, dict) or postflight_payload.get("schema") != "ppt-master.pptx-postflight-report.v1" or postflight_payload.get("status") not in {"passed", "passed-with-warnings"}:
            raise PresentationArtifactConflict("postflight report schema is invalid")
        return PresentationSmokeReceipt(job_id, output, report, slides, payload["status"])

    def rollback_projection(self, *, job_id: str) -> dict[str, object]:
        """Return the bounded removal projection for a later host-owned rollback."""
        job = self._job(job_id)
        return {"job_id": job_id, "status": "rolled_back", "remove": (str(job / "out" / _OUTPUT), str(job / "out" / _REPORT))}

    def _resolve(self, manifest: PresentationArtifactManifest) -> Path:
        resolved = self._roots.get(manifest.root_locator)
        if not isinstance(resolved, HostArtifactRoot) or resolved.read_only is not True:
            raise PresentationArtifactConflict("host artifact root is unavailable")
        root = resolved.root
        if not root.is_absolute() or not root.is_dir() or _is_link(root):
            raise PresentationArtifactConflict("host artifact root is invalid")
        root = root.resolve(strict=True)
        for relative in ("scripts/attribution_guard.py", "scripts/svg_quality_checker.py", "scripts/svg_to_pptx.py", "scripts/pptx_delivery_check.py"):
            entry = root / relative
            if not entry.is_file() or _is_link(entry):
                raise PresentationArtifactConflict("required ppt-master entrypoint is missing")
        return root

    def _job(self, job_id: str) -> Path:
        if not isinstance(job_id, str) or not _JOB.fullmatch(job_id):
            raise PresentationArtifactError("job_id is invalid")
        job = self._jobs_root / job_id
        if not job.is_dir() or _is_link(job) or job.resolve(strict=True).parent != self._jobs_root:
            raise PresentationArtifactConflict("controlled job root is invalid")
        return job.resolve(strict=True)

    def _validate_inputs(self, job: Path) -> None:
        self._validate_job_boundary(job, require_outputs=False)
        svg = job / "svg_output"
        files = tuple(svg.iterdir())
        if not files or any(not item.is_file() or _is_link(item) or item.suffix.lower() != ".svg" for item in files):
            raise PresentationArtifactConflict("presentation inputs must be svg_output/*.svg only")

    def _validate_job_boundary(self, job: Path, *, require_outputs: bool) -> None:
        allowed = {"svg_output", "out", "validation"}
        entries = {entry.name: entry for entry in job.iterdir()}
        if set(entries) - allowed or "svg_output" not in entries or not entries["svg_output"].is_dir() or _is_link(entries["svg_output"]):
            raise PresentationArtifactConflict("controlled job boundary drifted")
        out, validation = entries.get("out"), entries.get("validation")
        if ((out is not None and (not out.is_dir() or _is_link(out)))
                or (validation is not None and (not validation.is_dir() or _is_link(validation)))):
            raise PresentationArtifactConflict("controlled job output boundary drifted")
        if require_outputs:
            if out is None or validation is None or {item.name for item in out.iterdir()} != {_OUTPUT, _REPORT}:
                raise PresentationArtifactConflict("presentation outputs exceed the allowed boundary")
            if any(not item.is_file() or _is_link(item) for item in out.iterdir()):
                raise PresentationArtifactConflict("presentation outputs are invalid")
            if {item.name for item in validation.iterdir()} != {"svg_quality_report.json", "presentation.report.json"} or any(not item.is_file() or _is_link(item) for item in validation.iterdir()):
                raise PresentationArtifactConflict("presentation validation boundary drifted")


def _token(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum or "\x00" in value:
        raise PresentationArtifactError(f"{label} is invalid")
    return value.strip()


def _is_link(path: Path) -> bool:
    try:
        details = path.lstat()
        return stat.S_ISLNK(details.st_mode) or bool(getattr(details, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    except FileNotFoundError:
        return False


def _validate_quality_report(payload: object) -> None:
    """Accept the upstream v5 quality report's warning-tolerant pass shape."""
    if not isinstance(payload, dict) or payload.get("schema") != "ppt-master.svg-quality-report.v1" or payload.get("stage") != "final":
        raise PresentationArtifactConflict("SVG quality report schema is invalid")
    summary, files = payload.get("summary"), payload.get("files")
    if not isinstance(summary, dict) or summary.get("errors") != 0 or not isinstance(files, list) or not files:
        raise PresentationArtifactConflict("SVG quality report did not pass")
    if any(not isinstance(item, dict) or item.get("passed") is not True or item.get("errors") not in ([], ()) for item in files):
        raise PresentationArtifactConflict("SVG quality report did not pass")
