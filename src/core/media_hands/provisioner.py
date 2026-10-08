from __future__ import annotations

from dataclasses import dataclass
import json
import re
from types import MappingProxyType
from typing import Callable, Mapping

from core.effect_log import EFFECT_V2
from core.job_runner import (
    SQLiteJobAdmissionCommand,
    SQLiteJobRecord,
    SQLiteJobStore,
    SQLiteMediaAdmissionConflict,
)
from core.source_processing import SourceManifest

from .effect_contract import EFFECT_KIND
from .job_admission import (
    MediaHandsAdmissionCommand,
    MediaHandsJobAdmissionFactory,
)

LANES = ("download", "asr", "vision", "model", "media_cpu")
OPERATIONS = ("analyze_source", "extract_audio_track", "transcribe_video", "extract_images")


@dataclass(frozen=True, slots=True)
class MediaHandsPolicy:
    revision: str
    lane_max_queue: Mapping[str, int]
    lane_max_concurrency: Mapping[str, int]
    operation_profiles: Mapping[str, "MediaOperationProfile"]

    def __post_init__(self) -> None:
        if not self.revision or set(self.lane_max_queue) != set(LANES) or set(self.lane_max_concurrency) != set(LANES):
            raise ValueError("media policy requires every fixed lane and a revision")
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in (*self.lane_max_queue.values(), *self.lane_max_concurrency.values())):
            raise ValueError("media policy queue limits must be non-negative integers")
        object.__setattr__(self, "lane_max_queue", MappingProxyType(dict(self.lane_max_queue)))
        object.__setattr__(self, "lane_max_concurrency", MappingProxyType(dict(self.lane_max_concurrency)))
        if not self.operation_profiles or any(operation not in OPERATIONS or not isinstance(profile, MediaOperationProfile) for operation, profile in self.operation_profiles.items()):
            raise ValueError("media policy requires valid operation profiles")
        object.__setattr__(self, "operation_profiles", MappingProxyType(dict(self.operation_profiles)))


@dataclass(frozen=True, slots=True)
class MediaHandsAdmission:
    record: SQLiteJobRecord
    replayed: bool


@dataclass(frozen=True, slots=True)
class SourcePermissionSnapshot:
    """The immutable content-processing grant frozen at Media Hands admission.

    This is deliberately narrower than a Tool or network grant.  It identifies
    the project-owned Source Manifest and the exact permission revision that a
    worker must still find active immediately before it invokes a provider.
    """

    project_id: str
    manifest_ref: str
    manifest_revision: str
    grant_ref: str
    grant_revision: str
    revocation_generation: int

    def __post_init__(self) -> None:
        if (
            not self.project_id
            or not self.manifest_ref.startswith("crp://")
            or not self.manifest_revision
            or not self.grant_ref.startswith("crp://")
            or not self.grant_revision
            or not isinstance(self.revocation_generation, int)
            or isinstance(self.revocation_generation, bool)
            or self.revocation_generation < 0
        ):
            raise ValueError("media source permission snapshot is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "manifest_ref": self.manifest_ref,
            "manifest_revision": self.manifest_revision,
            "grant_ref": self.grant_ref,
            "grant_revision": self.grant_revision,
            "revocation_generation": self.revocation_generation,
        }

    @classmethod
    def from_mapping(cls, value: object) -> "SourcePermissionSnapshot":
        if not isinstance(value, Mapping) or set(value) != {
            "project_id", "manifest_ref", "manifest_revision", "grant_ref",
            "grant_revision", "revocation_generation",
        }:
            raise ValueError("media source permission snapshot fields are not exact")
        try:
            return cls(
                project_id=_required_string(value, "project_id"),
                manifest_ref=_required_string(value, "manifest_ref"),
                manifest_revision=_required_string(value, "manifest_revision"),
                grant_ref=_required_string(value, "grant_ref"),
                grant_revision=_required_string(value, "grant_revision"),
                revocation_generation=value["revocation_generation"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("media source permission snapshot is invalid") from exc


@dataclass(frozen=True, slots=True)
class MediaResourceBudget:
    max_download_bytes: int
    max_media_cpu_ms: int
    max_asr_audio_ms: int
    max_vision_frames: int
    max_model_input_tokens: int
    max_model_output_tokens: int
    max_wall_ms: int

    def __post_init__(self) -> None:
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in self.as_dict().values()):
            raise ValueError("media resource budgets must be non-negative integers")

    def as_dict(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class MediaOperationProfile:
    required_lanes: tuple[str, ...]
    budget: MediaResourceBudget

    def __post_init__(self) -> None:
        if not self.required_lanes or len(set(self.required_lanes)) != len(self.required_lanes) or any(lane not in LANES for lane in self.required_lanes):
            raise ValueError("operation profile lanes must be fixed, non-empty and unique")
        if tuple(sorted(self.required_lanes, key=LANES.index)) != self.required_lanes:
            raise ValueError("operation profile lanes must use fixed acquisition order")


def build_media_hands_job(*, manifest: SourceManifest, manifest_ref: str, manifest_revision: str, operation: str, idempotency_key: str, policy: MediaHandsPolicy, created_at: str, permission_snapshot: SourcePermissionSnapshot) -> dict[str, object]:
    if not manifest_ref.startswith("crp://") or "/source-manifests/" not in manifest_ref or manifest_ref == manifest.source_ref or not manifest_revision or operation not in policy.operation_profiles or len(idempotency_key) < 16 or not created_at:
        raise ValueError("media job identity, revision and timestamp are required")
    profile = policy.operation_profiles[operation]
    required_lanes, budget = profile.required_lanes, profile.budget
    if not required_lanes or len(set(required_lanes)) != len(required_lanes) or any(lane not in LANES for lane in required_lanes):
        raise ValueError("media job lanes must be fixed, non-empty and unique")
    if tuple(sorted(required_lanes, key=LANES.index)) != required_lanes:
        raise ValueError("media job lanes must use the fixed acquisition order")
    if re.fullmatch(r"[A-Za-z0-9._~-]+", manifest.source_id) is None:
        raise ValueError("media source id is not portable")
    # Identity is source + operation only: no source body/content hash participates.
    job_id = f"media_hands:{manifest.source_id}:{operation}"
    if (
        permission_snapshot.manifest_ref != manifest_ref
        or permission_snapshot.manifest_revision != manifest_revision
    ):
        raise ValueError("media source permission snapshot must bind the admitted manifest revision")
    credential_use_binding = None
    if manifest.credential_binding is not None:
        binding = manifest.credential_binding
        credential_use_binding = {
            "provider": binding.provider,
            "authorization_revision": binding.authorization_revision,
            "secret_generation": binding.secret_generation,
        }
    media = {"manifest": {"ref": manifest_ref, "revision": manifest_revision, "source_ref": manifest.source_ref}, "credential_use_binding": credential_use_binding, "permission_snapshot": permission_snapshot.as_dict(), "operation": operation, "policy": {"revision": policy.revision, "lane_max_queue": dict(policy.lane_max_queue), "lane_max_concurrency": dict(policy.lane_max_concurrency)}, "required_lanes": list(required_lanes), "budget": budget.as_dict(), "admission_state": "queued", "consumed": {key: 0 for key in budget.as_dict()}, "concurrency": {"state": "idle", "acquired_at": None}}
    return {"schema_version": "1.0.0", "id": job_id, "source_id": manifest.source_id, "job_type": "media_hands", "idempotency_key": idempotency_key, "status": "pending", "attempt": 0, "max_attempts": 1, "lease": None, "progress": {"current": 0, "total": 1, "percent": 0, "message": None}, "steps": [{"name": "execute_operation", "status": "pending", "attempt": 0, "started_at": None, "completed_at": None, "progress": 0, "input_refs": [manifest.source_ref, manifest_ref], "staged_output_refs": [], "log_refs": [], "error": None}], "error": None, "checkpoint": None, "staged_outputs": [], "published_outputs": [], "log_refs": [], "created_at": created_at, "updated_at": created_at, "media_hands": media}


@dataclass(slots=True)
class MediaHandsProvisioner:
    store: SQLiteJobStore
    policy: MediaHandsPolicy
    policy_admission_fence: Callable[[object, str], None] | None = None

    def provision(
        self,
        *,
        manifest: SourceManifest,
        manifest_ref: str,
        manifest_revision: str,
        operation: str,
        idempotency_key: str,
        created_at: str,
        permission_snapshot: SourcePermissionSnapshot,
    ) -> MediaHandsAdmission:
        job = build_media_hands_job(
            manifest=manifest,
            manifest_ref=manifest_ref,
            manifest_revision=manifest_revision,
            operation=operation,
            idempotency_key=idempotency_key,
            policy=self.policy,
            created_at=created_at,
            permission_snapshot=permission_snapshot,
        )
        record, replayed = self.store.create_media_admitted(
            job,
            policy_admission_fence=self.policy_admission_fence,
        )
        return MediaHandsAdmission(record, replayed)


@dataclass(slots=True)
class MediaHandsV2Provisioner:
    """Admit new Media work without creating a legacy Job execution authority."""

    admission_command: SQLiteJobAdmissionCommand
    policy: MediaHandsPolicy
    handler: object
    admitted_at: Callable[[], int]
    policy_admission_fence: Callable[[object, str], None] | None = None

    def provision(
        self,
        *,
        manifest: SourceManifest,
        manifest_ref: str,
        manifest_revision: str,
        operation: str,
        idempotency_key: str,
        created_at: str,
        permission_snapshot: SourcePermissionSnapshot,
        command: MediaHandsAdmissionCommand,
    ) -> MediaHandsAdmission:
        """Persist Gate, Effect and read projection in one caller-owned transaction."""

        if not isinstance(command, MediaHandsAdmissionCommand):
            raise TypeError("Media Hands v2 provision requires an admission command")
        job = build_media_hands_job(
            manifest=manifest,
            manifest_ref=manifest_ref,
            manifest_revision=manifest_revision,
            operation=operation,
            idempotency_key=idempotency_key,
            policy=self.policy,
            created_at=created_at,
            permission_snapshot=permission_snapshot,
        )
        media = dict(job["media_hands"])
        media["selection"] = {
            "ref": command.selection_ref,
            "revision": command.selection_revision,
            "mode": command.selection_mode,
        }
        # These fields belonged to the legacy Job-owned execution state.  The
        # v2 fact retains only immutable admission inputs; Core Effect and its
        # domain Receipt own all later progress and terminal truth.
        media.pop("admission_state", None)
        media.pop("consumed", None)
        media.pop("concurrency", None)
        job["media_hands"] = media
        job["execution_version"] = EFFECT_V2

        admitted = MediaHandsJobAdmissionFactory(
            admitted_at=self.admitted_at(),
        ).build(
            job_payload=job,
            command=command,
            handler=self.handler,
        )

        def preflight(connection, replay: bool) -> None:
            if self.policy_admission_fence is not None:
                self.policy_admission_fence(connection, self.policy.revision)
            if not replay:
                _assert_v2_media_capacity(
                    connection,
                    required_lanes=tuple(media["required_lanes"]),
                    lane_limits=self.policy.lane_max_queue,
                )

        result = self.admission_command.admit(
            payload=job,
            authorization=admitted.authorization,
            intent=admitted.intent,
            preflight=preflight,
        )
        return MediaHandsAdmission(
            SQLiteJobRecord(result.record.payload, result.record.revision),
            replayed=not result.created,
        )


def _assert_v2_media_capacity(
    connection: object,
    *,
    required_lanes: tuple[str, ...],
    lane_limits: Mapping[str, int],
) -> None:
    """Count active Media Effects without consulting Job status or Job lease."""

    execute = getattr(connection, "execute", None)
    if not callable(execute):
        raise TypeError("Media Hands admission requires a SQLite transaction")
    used = {lane: 0 for lane in required_lanes}
    rows = execute(
        "SELECT fact.payload_json FROM job_effect_fact AS fact "
        "JOIN effect AS execution ON execution.operation_id=fact.effect_operation_id "
        "WHERE execution.kind=? AND execution.contract_version=? "
        "AND execution.state IN ('PLANNED','INFLIGHT','UNKNOWN','WAITING_USER')",
        (EFFECT_KIND, EFFECT_V2),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(str(row[0]))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise SQLiteMediaAdmissionConflict(
                "media Effect capacity evidence is invalid"
            ) from error
        media = payload.get("media_hands") if isinstance(payload, Mapping) else None
        lanes = media.get("required_lanes") if isinstance(media, Mapping) else None
        if not isinstance(lanes, list):
            raise SQLiteMediaAdmissionConflict(
                "media Effect capacity evidence is invalid"
            )
        for lane in lanes:
            if lane in used:
                used[lane] += 1
    blocked = [lane for lane in required_lanes if used[lane] >= lane_limits[lane]]
    if blocked:
        raise SQLiteMediaAdmissionConflict(
            f"media lane queue is full: {', '.join(blocked)}"
        )


def _required_string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ValueError(f"{key} is required")
    return item
