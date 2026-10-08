"""Operation-scoped, non-secret network egress decision facts.

The persistent object is an immutable fact for one governed operation.  It is
not a mutable process or workspace proxy preference and it never reads proxy
environment variables or stores credentials.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass
from pathlib import Path

from backend.security.network_adapter import LoopbackHttpConnectProxy


_SCHEMA_VERSION = "1.0.0"
_REVISION = re.compile(r"^network-egress-decision-v1\.([0-9a-f]{64})$")
_SOURCE_REVISION = re.compile(r"^[0-9a-f]{40}$")
_CAPABILITY = "anonymous_public_media"


class NetworkEgressDecisionError(ValueError):
    """The operation-scoped egress fact is invalid or has drifted."""


@dataclass(frozen=True, slots=True)
class NetworkEgressDecisionFact:
    decision_ref: str
    decision_revision: str
    scope_ref: str
    capability: str
    mode: str
    literal_address: str | None
    port: int | None
    source_revision: str
    manifest_revision: str
    generation: int

    def connect_proxy(self) -> LoopbackHttpConnectProxy | None:
        if self.mode == "direct":
            return None
        if self.mode != "loopback_http_connect" or self.literal_address is None or self.port is None:
            raise NetworkEgressDecisionError("network egress decision is inconsistent")
        return LoopbackHttpConnectProxy(self.literal_address, self.port)


class NetworkEgressDecisionStore:
    """Immutable fact store used by planning, Handler execution and recovery."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root).resolve(strict=False)

    def decide(
        self,
        *,
        project_id: str,
        source_revision: str,
        manifest_revision: str,
        generation: int,
        network_egress: object = None,
        confirm_loopback: bool = False,
    ) -> NetworkEgressDecisionFact:
        scope_ref = _scope_ref(project_id)
        mode, address, port = _selection(network_egress, confirm_loopback=confirm_loopback)
        material = {
            "schema_version": _SCHEMA_VERSION,
            "scope_ref": scope_ref,
            "capability": _CAPABILITY,
            "mode": mode,
            "literal_address": address,
            "port": port,
            "source_revision": _source_revision(source_revision),
            "manifest_revision": _manifest_revision(manifest_revision),
            "generation": _generation(generation),
        }
        digest = hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        payload = {
            **material,
            "decision_ref": f"facts:network-egress-decision/{digest}",
            "decision_revision": f"network-egress-decision-v1.{digest}",
        }
        fact = _decode(payload)
        self._write(fact)
        return fact

    def get(self, decision_revision: str) -> NetworkEgressDecisionFact:
        match = _REVISION.fullmatch(decision_revision) if isinstance(decision_revision, str) else None
        if match is None:
            raise NetworkEgressDecisionError("network egress decision revision is invalid")
        path = self._path(match.group(1))
        if path.is_symlink() or not path.is_file():
            raise NetworkEgressDecisionError("network egress decision fact is unavailable")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise NetworkEgressDecisionError("network egress decision fact is unreadable") from error
        fact = _decode(payload)
        if fact.decision_revision != decision_revision:
            raise NetworkEgressDecisionError("network egress decision revision drifted")
        return fact

    def _write(self, fact: NetworkEgressDecisionFact) -> None:
        digest = _REVISION.fullmatch(fact.decision_revision)
        if digest is None:
            raise NetworkEgressDecisionError("network egress decision revision is invalid")
        path = self._path(digest.group(1))
        payload = _encode(fact)
        if path.exists():
            if path.is_symlink() or _read_existing(path) != payload:
                raise NetworkEgressDecisionError("network egress decision fact drifted")
            return
        self._root.mkdir(parents=True, exist_ok=True)
        if self._root.is_symlink():
            raise NetworkEgressDecisionError("network egress decision root cannot be a link")
        pending = self._root / f".{path.stem}.pending"
        if pending.exists():
            if pending.is_symlink() or _read_existing(pending) != payload:
                raise NetworkEgressDecisionError("pending network egress decision fact drifted")
            pending.replace(path)
            return
        pending.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        pending.replace(path)

    def _path(self, digest: str) -> Path:
        candidate = (self._root / f"{digest}.json").resolve(strict=False)
        if candidate.parent != self._root:
            raise NetworkEgressDecisionError("network egress decision path escaped its root")
        return candidate


def _selection(value: object, *, confirm_loopback: bool) -> tuple[str, str | None, int | None]:
    if value is None:
        return "direct", None, None
    if not isinstance(value, dict) or set(value) != {"mode", "literal_address", "port"}:
        raise NetworkEgressDecisionError("network egress decision fields are invalid")
    mode = value.get("mode")
    if mode == "direct":
        if value.get("literal_address") is not None or value.get("port") is not None:
            raise NetworkEgressDecisionError("direct egress cannot name an endpoint")
        return "direct", None, None
    if mode != "loopback_http_connect" or confirm_loopback is not True:
        raise NetworkEgressDecisionError("loopback egress requires explicit operation confirmation")
    address = value.get("literal_address")
    port = value.get("port")
    try:
        parsed = ipaddress.ip_address(address) if isinstance(address, str) else None
    except ValueError as error:
        raise NetworkEgressDecisionError("network egress endpoint must be a loopback literal") from error
    if parsed is None or not parsed.is_loopback or not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise NetworkEgressDecisionError("network egress endpoint must be a loopback literal and valid port")
    return "loopback_http_connect", str(parsed), port


def _decode(value: object) -> NetworkEgressDecisionFact:
    fields = {
        "schema_version", "decision_ref", "decision_revision", "scope_ref", "capability", "mode",
        "literal_address", "port", "source_revision", "manifest_revision", "generation",
    }
    if not isinstance(value, dict) or set(value) != fields or value.get("schema_version") != _SCHEMA_VERSION:
        raise NetworkEgressDecisionError("network egress decision fact fields are invalid")
    revision = value.get("decision_revision")
    match = _REVISION.fullmatch(revision) if isinstance(revision, str) else None
    if match is None or value.get("decision_ref") != f"facts:network-egress-decision/{match.group(1)}":
        raise NetworkEgressDecisionError("network egress decision identity is invalid")
    material = {key: value[key] for key in (
        "schema_version", "scope_ref", "capability", "mode", "literal_address", "port",
        "source_revision", "manifest_revision", "generation",
    )}
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if digest != match.group(1) or value.get("capability") != _CAPABILITY:
        raise NetworkEgressDecisionError("network egress decision digest drifted")
    mode, address, port = _selection(
        {"mode": value.get("mode"), "literal_address": value.get("literal_address"), "port": value.get("port")},
        confirm_loopback=True,
    )
    return NetworkEgressDecisionFact(
        decision_ref=str(value["decision_ref"]), decision_revision=revision,
        scope_ref=_scope_ref_from_value(value.get("scope_ref")), capability=_CAPABILITY,
        mode=mode, literal_address=address, port=port,
        source_revision=_source_revision(value.get("source_revision")),
        manifest_revision=_manifest_revision(value.get("manifest_revision")),
        generation=_generation(value.get("generation")),
    )


def _encode(fact: NetworkEgressDecisionFact) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION, "decision_ref": fact.decision_ref,
        "decision_revision": fact.decision_revision, "scope_ref": fact.scope_ref,
        "capability": fact.capability, "mode": fact.mode, "literal_address": fact.literal_address,
        "port": fact.port, "source_revision": fact.source_revision,
        "manifest_revision": fact.manifest_revision, "generation": fact.generation,
    }


def _read_existing(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise NetworkEgressDecisionError("network egress decision fact is unreadable") from error
    if not isinstance(value, dict):
        raise NetworkEgressDecisionError("network egress decision fact must be an object")
    return value


def _scope_ref(project_id: object) -> str:
    if not isinstance(project_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", project_id):
        raise NetworkEgressDecisionError("network egress project id is invalid")
    return f"scope:project/{project_id}"


def _scope_ref_from_value(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("scope:project/"):
        raise NetworkEgressDecisionError("network egress scope is invalid")
    _scope_ref(value.removeprefix("scope:project/"))
    return value


def _source_revision(value: object) -> str:
    if not isinstance(value, str) or not _SOURCE_REVISION.fullmatch(value):
        raise NetworkEgressDecisionError("network egress source revision is invalid")
    return value


def _manifest_revision(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 160 or any(character.isspace() for character in value):
        raise NetworkEgressDecisionError("network egress manifest revision is invalid")
    return value


def _generation(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise NetworkEgressDecisionError("network egress generation is invalid")
    return value
