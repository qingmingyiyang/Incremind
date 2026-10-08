"""Host-owned Resume Bundle and paired-device placement contracts.

The bundle is deliberately a *resume pointer*, not a portable session archive:
the originating host remains the authority for Turn/Event, Secret, Effect,
Receipt and workspace writes.  A paired device can verify and project a
bounded resume packet, then must return every side-effect request to that
host's normal admission path.
"""
from __future__ import annotations

from base64 import b64decode, b64encode
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sqlite3
from typing import Mapping, Sequence
from uuid import uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from core.product_core.file_authority_lock import (
    FileAuthorityLockPort,
    interprocess_file_lock,
)
from core.product_core.workspace_resume_reconcile import (
    WorkspaceResumeReconcileError,
    validate_workspace_relative_path,
)


_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_OPAQUE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
_HEX_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_FORBIDDEN_KEY = re.compile(
    r"(?:secret|credential|password|token|cookie|api[_-]?key|authorization|dpapi|desktop[_-]?nonce|"
    r"receipt(?:_payload)?|lease|vault|executable|command|argument|argv|environment)",
    re.IGNORECASE,
)
_SECRET_TEXT = re.compile(r"(?:api[_-]?key|secret|token|password|cookie|authorization)\s*[:=]", re.IGNORECASE)
_EFFECT_UNKNOWN = "UNKNOWN"
_MAX_BUNDLE_LIFETIME = timedelta(hours=24)


class SessionPlacementError(ValueError):
    """Raised when a packet cannot cross the host/paired-device boundary."""


class SessionPlacementConflict(SessionPlacementError):
    """Raised when a one-time nonce or trust revision has changed."""


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    device_id: str
    public_key: str


@dataclass(frozen=True, slots=True)
class WorkspaceManifestEntry:
    relative_path: str
    digest: str
    size_bytes: int
    base_revision: str


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    capability_id: str
    revision: int
    effect_classes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PlacementSafetyState:
    """Authoritative preflight facts supplied by the host Effect authority."""

    active_lease_count: int = 0
    effect_states: tuple[str, ...] = ()

    def validate(self) -> None:
        if self.active_lease_count:
            raise SessionPlacementError("session placement is blocked by an active Effect lease")
        if _EFFECT_UNKNOWN in self.effect_states:
            raise SessionPlacementError("session placement is blocked by an UNKNOWN Effect")


@dataclass(frozen=True, slots=True)
class ResumeBundle:
    schema_version: str
    bundle_id: str
    source_device_id: str
    target_device_id: str
    project_id: str
    session_ref: str
    turn_refs: tuple[str, ...]
    context_manifest_ref: str
    context_manifest_revision: str
    last_event_cursor: str
    display_summary: str
    workspace_base_manifest_ref: str
    workspace_manifest: tuple[WorkspaceManifestEntry, ...]
    capability_descriptors: tuple[CapabilityDescriptor, ...]
    expires_at: str
    source_trust_revision: int | None
    target_trust_revision: int | None
    nonce: str
    signature: str

    def unsigned_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version, "bundle_id": self.bundle_id,
            "source_device_id": self.source_device_id, "target_device_id": self.target_device_id,
            "project_id": self.project_id,
            "session_ref": self.session_ref, "turn_refs": list(self.turn_refs),
            "context_manifest_ref": self.context_manifest_ref,
            "context_manifest_revision": self.context_manifest_revision,
            "last_event_cursor": self.last_event_cursor, "display_summary": self.display_summary,
            "workspace_base_manifest_ref": self.workspace_base_manifest_ref,
            "workspace_manifest": [asdict(item) for item in self.workspace_manifest],
            "capability_descriptors": [
                {"capability_id": item.capability_id, "revision": item.revision,
                 "effect_classes": list(item.effect_classes)}
                for item in self.capability_descriptors
            ],
            "expires_at": self.expires_at,
            **({} if self.schema_version == "resume_bundle.v2" else {
                "source_trust_revision": self.source_trust_revision,
                "target_trust_revision": self.target_trust_revision,
            }),
            "nonce": self.nonce,
        }

    def wire(self) -> dict[str, object]:
        return {**self.unsigned_payload(), "signature": self.signature}


@dataclass(frozen=True, slots=True)
class ResumedSessionProjection:
    bundle_id: str
    project_id: str
    session_ref: str
    turn_refs: tuple[str, ...]
    context_manifest_ref: str
    context_manifest_revision: str
    last_event_cursor: str
    display_summary: str
    workspace_base_manifest_ref: str
    workspace_manifest: tuple[WorkspaceManifestEntry, ...]
    capability_descriptors: tuple[CapabilityDescriptor, ...]
    source_device_id: str
    target_device_id: str
    bundle_source_trust_revision: int | None
    bundle_target_trust_revision: int | None
    local_source_trust_revision: int
    local_target_trust_revision: int
    expires_at: str
    created_at: str
    host_authorization_required: bool = True
    read_only: bool = True


class HostSigningIdentity:
    """Host-held Ed25519 signer.  Only its public identity enters pairing state."""

    def __init__(self, *, device_id: str, private_key: Ed25519PrivateKey | None = None) -> None:
        self._device_id = _identity(device_id, "host device identity")
        self._private_key = private_key or Ed25519PrivateKey.generate()

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def public_identity(self) -> DeviceIdentity:
        raw = self._private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        return DeviceIdentity(self._device_id, b64encode(raw).decode("ascii"))

    @classmethod
    def from_private_key(cls, *, device_id: str, private_key: str) -> "HostSigningIdentity":
        try:
            raw = b64decode(private_key.encode("ascii"), validate=True)
            key = Ed25519PrivateKey.from_private_bytes(raw)
        except (ValueError, TypeError) as error:
            raise SessionPlacementError("host signing key is invalid") from error
        return cls(device_id=device_id, private_key=key)

    def export_private_key(self) -> str:
        raw = self._private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        return b64encode(raw).decode("ascii")

    def sign(self, payload: Mapping[str, object]) -> str:
        return b64encode(self._private_key.sign(_canonical(payload))).decode("ascii")


class PairedDeviceRegistry:
    """Durable public-device trust registry and one-time resume nonce ledger."""

    _DATABASE = "paired-devices.sqlite3"

    def __init__(
        self,
        root_dir: Path,
        *,
        file_authority_lock: FileAuthorityLockPort = interprocess_file_lock,
    ) -> None:
        root = Path(root_dir)
        self._path = root / ".rebuild-data" / "session-placement" / self._DATABASE
        self._file_authority_lock = file_authority_lock
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def trust(self, identity: DeviceIdentity, *, expected_revision: int = 0) -> int:
        identity = _device_identity(identity)
        with self._transaction() as conn:
            row = conn.execute("SELECT public_key, trust_revision, trust_state FROM paired_devices WHERE device_id=?", (identity.device_id,)).fetchone()
            if row is None:
                if expected_revision != 0:
                    raise SessionPlacementConflict("paired device does not yet exist")
                conn.execute(
                    "INSERT INTO paired_devices (device_id, public_key, trust_revision, trusted_at, trust_state, revoked_at) VALUES (?,?,?,?,?,?)",
                    (identity.device_id, identity.public_key, 1, _now(), "active", None),
                )
                return 1
            if int(row["trust_revision"]) != expected_revision:
                raise SessionPlacementConflict("paired device trust revision changed")
            if str(row["public_key"]) != identity.public_key:
                raise SessionPlacementConflict("paired device identity key changed")
            if str(row["trust_state"]) == "active":
                return int(row["trust_revision"])
            revision = int(row["trust_revision"]) + 1
            conn.execute(
                "UPDATE paired_devices SET trust_revision=?, trust_state='active', trusted_at=?, revoked_at=NULL WHERE device_id=?",
                (revision, _now(), identity.device_id),
            )
            return revision

    def revoke(self, device_id: str, *, expected_revision: int) -> int:
        """CAS-revoke a pairing while retaining its public audit record."""

        device_id = _identity(device_id, "device identity")
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 1:
            raise SessionPlacementError("paired device trust revision is invalid")
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT trust_revision, trust_state FROM paired_devices WHERE device_id=?", (device_id,)
            ).fetchone()
            if row is None or int(row["trust_revision"]) != expected_revision:
                raise SessionPlacementConflict("paired device trust revision changed")
            if str(row["trust_state"]) != "active":
                raise SessionPlacementConflict("paired device is already revoked")
            revision = expected_revision + 1
            conn.execute(
                "UPDATE paired_devices SET trust_revision=?, trust_state='revoked', revoked_at=? WHERE device_id=?",
                (revision, _now(), device_id),
            )
            return revision

    def identity(self, device_id: str) -> DeviceIdentity | None:
        device_id = _identity(device_id, "device identity")
        with closing(self._connection()) as conn:
            row = conn.execute(
                "SELECT device_id, public_key FROM paired_devices WHERE device_id=? AND trust_state='active'", (device_id,)
            ).fetchone()
        return None if row is None else DeviceIdentity(str(row["device_id"]), str(row["public_key"]))

    def trust_record(self, device_id: str) -> tuple[DeviceIdentity, int, str, str, str | None] | None:
        """Return auditable state, including revoked pairings, without exposing secrets."""

        device_id = _identity(device_id, "device identity")
        with closing(self._connection()) as conn:
            row = conn.execute(
                "SELECT device_id, public_key, trust_revision, trust_state, trusted_at, revoked_at FROM paired_devices WHERE device_id=?",
                (device_id,),
            ).fetchone()
        if row is None:
            return None
        return (
            DeviceIdentity(str(row["device_id"]), str(row["public_key"])), int(row["trust_revision"]),
            str(row["trust_state"]), str(row["trusted_at"]),
            None if row["revoked_at"] is None else str(row["revoked_at"]),
        )

    def list_identities(self) -> tuple[tuple[str, int, str], ...]:
        with closing(self._connection()) as conn:
            rows = conn.execute(
                "SELECT device_id, trust_revision, trusted_at FROM paired_devices WHERE trust_state='active' ORDER BY device_id"
            ).fetchall()
        return tuple((str(row["device_id"]), int(row["trust_revision"]), str(row["trusted_at"])) for row in rows)

    def consume_nonce(self, *, source_device_id: str, target_device_id: str, nonce: str, expires_at: str) -> None:
        source_device_id = _identity(source_device_id, "source device identity")
        target_device_id = _identity(target_device_id, "target device identity")
        nonce = _identity(nonce, "bundle nonce")
        _future(expiry=expires_at)
        with self._transaction() as conn:
            try:
                conn.execute("INSERT INTO consumed_resume_nonces VALUES (?,?,?,?,?)", (nonce, source_device_id, target_device_id, expires_at, _now()))
            except sqlite3.IntegrityError as error:
                raise SessionPlacementConflict("resume bundle nonce has already been consumed") from error

    def _initialize(self) -> None:
        with self._transaction() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS paired_devices (device_id TEXT PRIMARY KEY, public_key TEXT NOT NULL, trust_revision INTEGER NOT NULL, trusted_at TEXT NOT NULL, trust_state TEXT NOT NULL DEFAULT 'active', revoked_at TEXT)")
            columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(paired_devices)").fetchall()}
            if "trust_state" not in columns:
                conn.execute("ALTER TABLE paired_devices ADD COLUMN trust_state TEXT NOT NULL DEFAULT 'active'")
            if "revoked_at" not in columns:
                conn.execute("ALTER TABLE paired_devices ADD COLUMN revoked_at TEXT")
            conn.execute("UPDATE paired_devices SET trust_state='active' WHERE trust_state IS NULL OR trust_state NOT IN ('active', 'revoked')")
            conn.execute("CREATE TABLE IF NOT EXISTS consumed_resume_nonces (nonce TEXT PRIMARY KEY, source_device_id TEXT NOT NULL, target_device_id TEXT NOT NULL, expires_at TEXT NOT NULL, consumed_at TEXT NOT NULL)")

    def _connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _transaction(self):
        return _Transaction(self._path, self._connection, self._file_authority_lock)


class ResumeBundleService:
    """Build and import signed read/resume-only packets for one paired device."""

    def __init__(self, *, host: HostSigningIdentity, pairs: PairedDeviceRegistry) -> None:
        self._host = host
        self._pairs = pairs

    def create(
        self, *, target_device_id: str, project_id: str, session_ref: str, turn_refs: Sequence[str],
        context_manifest_ref: str, context_manifest_revision: str, last_event_cursor: str,
        display_summary: str, workspace_base_manifest_ref: str,
        workspace_manifest: Sequence[WorkspaceManifestEntry],
        capability_descriptors: Sequence[CapabilityDescriptor], expires_at: str,
        safety: PlacementSafetyState = PlacementSafetyState(), nonce: str | None = None,
    ) -> ResumeBundle:
        safety.validate()
        target_device_id = _identity(target_device_id, "target device identity")
        target = self._pairs.trust_record(target_device_id)
        source = self._pairs.trust_record(self._host.device_id)
        if target is None or target[2] != "active":
            raise SessionPlacementError("target device is not paired")
        if source is None or source[2] != "active":
            raise SessionPlacementError("host device is not paired")
        payload = _unsigned_payload(
            source_device_id=self._host.device_id, target_device_id=target_device_id, project_id=project_id,
            session_ref=session_ref, turn_refs=turn_refs, context_manifest_ref=context_manifest_ref,
            context_manifest_revision=context_manifest_revision, last_event_cursor=last_event_cursor,
            display_summary=display_summary, workspace_base_manifest_ref=workspace_base_manifest_ref,
            workspace_manifest=workspace_manifest,
            capability_descriptors=capability_descriptors, expires_at=expires_at,
            source_trust_revision=source[1], target_trust_revision=target[1],
            bundle_id=f"resume-{uuid4().hex}", nonce=nonce or f"nonce-{uuid4().hex}",
        )
        _validate_payload(payload)
        signature = self._host.sign(payload)
        return _bundle_from_wire({**payload, "signature": signature})

    def import_for_paired_device(
        self, wire: Mapping[str, object], *, local_device_id: str,
        safety: PlacementSafetyState = PlacementSafetyState(), now: datetime | None = None,
    ) -> ResumedSessionProjection:
        _reject_forbidden(wire)
        bundle = _bundle_from_wire(wire)
        _validate_payload(bundle.unsigned_payload())
        safety.validate()
        local_device_id = _identity(local_device_id, "local device identity")
        if bundle.target_device_id != local_device_id:
            raise SessionPlacementError("resume bundle target device does not match")
        host_record = self._pairs.trust_record(bundle.source_device_id)
        target_record = self._pairs.trust_record(bundle.target_device_id)
        if host_record is None or host_record[2] != "active":
            raise SessionPlacementError("resume bundle host is not paired")
        if target_record is None or target_record[2] != "active":
            raise SessionPlacementError("resume bundle target is not paired")
        host = host_record[0]
        try:
            Ed25519PublicKey.from_public_bytes(_decode_key(host.public_key)).verify(
                _decode_signature(bundle.signature), _canonical(bundle.unsigned_payload())
            )
        except (ValueError, InvalidSignature) as error:
            raise SessionPlacementError("resume bundle signature is invalid") from error
        if _future(expiry=bundle.expires_at, now=now) is None:  # satisfies type check and keeps time gate explicit
            raise AssertionError("unreachable")
        self._pairs.consume_nonce(
            source_device_id=bundle.source_device_id, target_device_id=bundle.target_device_id,
            nonce=bundle.nonce, expires_at=bundle.expires_at,
        )
        return ResumedSessionProjection(
            bundle.bundle_id, bundle.project_id, bundle.session_ref, bundle.turn_refs,
            bundle.context_manifest_ref, bundle.context_manifest_revision,
            bundle.last_event_cursor, bundle.display_summary,
            bundle.workspace_base_manifest_ref, bundle.workspace_manifest,
            bundle.capability_descriptors, bundle.source_device_id,
            bundle.target_device_id,
            bundle.source_trust_revision, bundle.target_trust_revision,
            host_record[1], target_record[1], bundle.expires_at, _now(),
        )

    def validate_projection_access(
        self,
        projection: ResumedSessionProjection,
        *,
        now: datetime | None = None,
    ) -> None:
        """Revalidate local trust and expiry for every recovered-session use.

        Trust revisions are local CAS facts.  A revision recorded by the
        source host is evidence for a later host-side admission, but it is not
        comparable with the target device's independent trust ledger.
        """

        if not isinstance(projection, ResumedSessionProjection):
            raise SessionPlacementError("resumed session projection is invalid")
        source = self._pairs.trust_record(projection.source_device_id)
        target = self._pairs.trust_record(projection.target_device_id)
        if source is None or source[2] != "active":
            raise SessionPlacementError("resumed session source pairing is unavailable")
        if target is None or target[2] != "active":
            raise SessionPlacementError("resumed session target pairing is unavailable")
        if (
            source[1] != projection.local_source_trust_revision
            or target[1] != projection.local_target_trust_revision
        ):
            raise SessionPlacementConflict("resumed session trust revision changed")
        _future(expiry=projection.expires_at, now=now)


def _unsigned_payload(**values: object) -> dict[str, object]:
    return {
        "schema_version": "resume_bundle.v3", **values,
        "turn_refs": list(values["turn_refs"]),
        "workspace_manifest": [asdict(_workspace_entry(item)) for item in values["workspace_manifest"]],
        "capability_descriptors": [
            {"capability_id": _capability(item).capability_id, "revision": _capability(item).revision,
             "effect_classes": list(_capability(item).effect_classes)}
            for item in values["capability_descriptors"]
        ],
    }


def _bundle_from_wire(wire: Mapping[str, object]) -> ResumeBundle:
    if not isinstance(wire, Mapping): raise SessionPlacementError("resume bundle must be an object")
    schema_version = wire.get("schema_version")
    required = {"schema_version", "bundle_id", "source_device_id", "target_device_id", "project_id", "session_ref", "turn_refs", "context_manifest_ref", "context_manifest_revision", "last_event_cursor", "display_summary", "workspace_base_manifest_ref", "workspace_manifest", "capability_descriptors", "expires_at", "nonce", "signature"}
    if schema_version == "resume_bundle.v3":
        required |= {"source_trust_revision", "target_trust_revision"}
    elif schema_version != "resume_bundle.v2":
        raise SessionPlacementError("resume bundle schema is invalid")
    if set(wire) != required: raise SessionPlacementError("resume bundle shape is invalid")
    try:
        return ResumeBundle(
            _string(wire["schema_version"], "schema version"), _string(wire["bundle_id"], "bundle id"),
            _string(wire["source_device_id"], "source device id"), _string(wire["target_device_id"], "target device id"),
            _string(wire["project_id"], "project id"),
            _string(wire["session_ref"], "session ref"), tuple(_string(item, "turn reference") for item in _array(wire["turn_refs"], "turn refs")),
            _string(wire["context_manifest_ref"], "context manifest ref"), _string(wire["context_manifest_revision"], "context manifest revision"),
            _string(wire["last_event_cursor"], "last event cursor"), _string(wire["display_summary"], "display summary"),
            _string(wire["workspace_base_manifest_ref"], "workspace base manifest ref"),
            tuple(_workspace_entry(item) for item in _array(wire["workspace_manifest"], "workspace manifest")),
            tuple(_capability(item) for item in _array(wire["capability_descriptors"], "capability descriptors")),
            _string(wire["expires_at"], "expires at"),
            _revision(wire["source_trust_revision"], "source trust revision") if schema_version == "resume_bundle.v3" else None,
            _revision(wire["target_trust_revision"], "target trust revision") if schema_version == "resume_bundle.v3" else None,
            _string(wire["nonce"], "nonce"), _string(wire["signature"], "signature"),
        )
    except SessionPlacementError:
        raise
    except (TypeError, ValueError) as error:
        raise SessionPlacementError("resume bundle values are invalid") from error


def _validate_payload(payload: Mapping[str, object]) -> None:
    schema_version = payload.get("schema_version")
    if schema_version not in {"resume_bundle.v2", "resume_bundle.v3"}: raise SessionPlacementError("resume bundle schema is invalid")
    if schema_version == "resume_bundle.v3":
        _revision(payload.get("source_trust_revision"), "source trust revision")
        _revision(payload.get("target_trust_revision"), "target trust revision")
    for field in ("bundle_id", "source_device_id", "target_device_id", "project_id", "nonce"):
        _identity(payload.get(field), field.replace("_", " "))
    for field in ("session_ref", "context_manifest_ref", "last_event_cursor", "workspace_base_manifest_ref"):
        _opaque_ref(payload.get(field), field.replace("_", " "))
    _opaque_ref(payload.get("context_manifest_revision"), "context manifest revision")
    turns = _array(payload.get("turn_refs"), "turn refs")
    if not turns or len(turns) > 64: raise SessionPlacementError("turn references are invalid")
    for item in turns: _opaque_ref(item, "turn reference")
    summary = payload.get("display_summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 512 or _SECRET_TEXT.search(summary):
        raise SessionPlacementError("display summary is invalid")
    workspace = _array(payload.get("workspace_manifest"), "workspace manifest")
    if len(workspace) > 4096: raise SessionPlacementError("workspace manifest is too large")
    paths = [_workspace_entry(item).relative_path for item in workspace]
    if len(paths) != len(set(paths)): raise SessionPlacementError("workspace manifest contains duplicate paths")
    capabilities = _array(payload.get("capability_descriptors"), "capability descriptors")
    if len(capabilities) > 128: raise SessionPlacementError("capability descriptors are too large")
    for item in capabilities: _capability(item)
    _future(expiry=payload.get("expires_at"))


def _workspace_entry(value: object) -> WorkspaceManifestEntry:
    if isinstance(value, WorkspaceManifestEntry): result = value
    elif isinstance(value, Mapping): result = WorkspaceManifestEntry(
        _string(value.get("relative_path"), "workspace relative path"),
        _string(value.get("digest"), "workspace digest"), value.get("size_bytes"),
        _string(value.get("base_revision"), "workspace base revision"),
    )
    else: raise SessionPlacementError("workspace manifest entry is invalid")
    try: validate_workspace_relative_path(result.relative_path)
    except WorkspaceResumeReconcileError as error: raise SessionPlacementError("workspace path must be relative and normalized") from error
    if not _HEX_DIGEST.fullmatch(result.digest): raise SessionPlacementError("workspace digest is invalid")
    if not isinstance(result.size_bytes, int) or isinstance(result.size_bytes, bool) or result.size_bytes < 0:
        raise SessionPlacementError("workspace size is invalid")
    _opaque_ref(result.base_revision, "workspace base revision")
    return result


def _capability(value: object) -> CapabilityDescriptor:
    if isinstance(value, CapabilityDescriptor): result = value
    elif isinstance(value, Mapping):
        raw = value.get("effect_classes", ())
        result = CapabilityDescriptor(
            _string(value.get("capability_id"), "capability identity"),
            _revision(value.get("revision"), "capability revision"),
            tuple(_string(item, "capability effect class") for item in _array(raw, "effect classes")),
        )
    else: raise SessionPlacementError("capability descriptor is invalid")
    _identity(result.capability_id, "capability identity")
    if not isinstance(result.revision, int) or isinstance(result.revision, bool) or result.revision < 1:
        raise SessionPlacementError("capability revision is invalid")
    if len(result.effect_classes) > 16: raise SessionPlacementError("capability effect classes are too large")
    for value in result.effect_classes: _identity(value, "capability effect class")
    return result


def _device_identity(value: DeviceIdentity) -> DeviceIdentity:
    if not isinstance(value, DeviceIdentity): raise SessionPlacementError("paired device identity is invalid")
    _identity(value.device_id, "device identity")
    _decode_key(value.public_key)
    return value


def _reject_forbidden(value: object) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str) or _FORBIDDEN_KEY.search(key):
                raise SessionPlacementError("resume bundle contains protected material")
            _reject_forbidden(child)
    elif isinstance(value, (list, tuple)):
        for child in value: _reject_forbidden(child)
    elif isinstance(value, str) and _SECRET_TEXT.search(value):
        raise SessionPlacementError("resume bundle contains protected material")


def _canonical(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _array(value: object, label: str) -> Sequence[object]:
    if not isinstance(value, (list, tuple)): raise SessionPlacementError(f"{label} must be an array")
    return value


def _identity(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTITY.fullmatch(value): raise SessionPlacementError(f"{label} is invalid")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise SessionPlacementError(f"{label} is invalid")
    return value


def _revision(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise SessionPlacementError(f"{label} is invalid")
    return value


def _opaque_ref(value: object, label: str) -> str:
    if not isinstance(value, str) or not _OPAQUE_REF.fullmatch(value) or _absolute(value) or ".." in value.split("/"):
        raise SessionPlacementError(f"{label} is invalid")
    return value


def _absolute(value: str) -> bool:
    return PureWindowsPath(value).is_absolute() or PurePosixPath(value).is_absolute() or value.startswith("\\\\")


def _decode_key(value: object) -> bytes:
    if not isinstance(value, str): raise SessionPlacementError("device public key is invalid")
    try: raw = b64decode(value.encode("ascii"), validate=True)
    except Exception as error: raise SessionPlacementError("device public key is invalid") from error
    if len(raw) != 32: raise SessionPlacementError("device public key is invalid")
    return raw


def _decode_signature(value: str) -> bytes:
    try: raw = b64decode(value.encode("ascii"), validate=True)
    except Exception as error: raise SessionPlacementError("resume bundle signature is invalid") from error
    if len(raw) != 64: raise SessionPlacementError("resume bundle signature is invalid")
    return raw


def _future(*, expiry: object, now: datetime | None = None) -> datetime:
    if not isinstance(expiry, str): raise SessionPlacementError("resume bundle expiry is invalid")
    try: parsed = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
    except ValueError as error: raise SessionPlacementError("resume bundle expiry is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None: raise SessionPlacementError("resume bundle expiry is invalid")
    current = now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)
    if parsed.astimezone(timezone.utc) <= current: raise SessionPlacementError("resume bundle has expired")
    if parsed.astimezone(timezone.utc) - current > _MAX_BUNDLE_LIFETIME:
        raise SessionPlacementError("resume bundle expiry exceeds the allowed lifetime")
    return parsed.astimezone(timezone.utc)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _Transaction:
    def __init__(self, path: Path, connection, file_authority_lock: FileAuthorityLockPort):
        self.path, self.connection, self.file_authority_lock = path, connection, file_authority_lock
    def __enter__(self):
        self.lock = self.file_authority_lock(self.path); self.lock.__enter__()
        self.conn = self.connection(); self.conn.execute("BEGIN IMMEDIATE"); return self.conn
    def __exit__(self, kind, value, trace):
        try: self.conn.commit() if kind is None else self.conn.rollback()
        finally: self.conn.close(); self.lock.__exit__(kind, value, trace)
