from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from urllib.parse import urlsplit, urlunsplit
from collections.abc import Callable

from backend.shared.filesystem import atomic_write_text
from core.product_core.model_dispatch_authority import model_dispatch_authority_fence


_PROVIDER_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_PURPOSE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_CATEGORY = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SCHEMA_VERSION = "1.0.0"
_MANIFEST_REVISION = "provider-egress-v1"
DEFAULT_PROVIDER_EGRESS_PURPOSES = (
    "agent_interaction",
    "companion_ambient",
    "companion_chat",
    "companion_diary",
    "companion_vision",
    "connection_test",
    "document_draft",
    "image_generation",
    "intake_classification",
    "knowledge_card",
    "memory_candidate",
    "mindmap",
    "model_discovery",
    "project_routing",
    "search_answer",
    "series_intake_organize",
    "transcript_enhancement",
    "video_processing",
    "video_summary",
    "vision_analysis",
    "web_search",
)
DEFAULT_PROVIDER_EGRESS_CATEGORIES = ("image_frame", "instructions", "provider_metadata", "source_excerpt")
# A confirmed 2 MiB image expands when encoded for an OpenAI-compatible
# request. Text routes still enforce their smaller route-specific limits.
DEFAULT_PROVIDER_EGRESS_MAX_BYTES = 4 * 1024 * 1024


class ProviderEgressError(ValueError):
    """Raised before an external Provider request when consent is not valid."""


@dataclass(frozen=True, slots=True)
class ProviderEgressManifest:
    manifest_id: str
    revision: str
    provider_id: str
    endpoint: str
    purposes: tuple[str, ...]
    payload_categories: tuple[str, ...]
    max_payload_bytes: int
    external: bool

    def public_dict(self, *, consented: bool) -> dict[str, object]:
        return {
            "manifest_id": self.manifest_id,
            "revision": self.revision,
            "provider_id": self.provider_id,
            "endpoint": self.endpoint,
            "purposes": list(self.purposes),
            "payload_categories": list(self.payload_categories),
            "max_payload_bytes": self.max_payload_bytes,
            "external": self.external,
            "consented": consented,
            "local_only_default": True,
        }


class ProviderEgressLease:
    def __init__(
        self,
        policy: ProviderEgressPolicyStore,
        *,
        manifest: ProviderEgressManifest,
        purpose: str,
        payload_categories: tuple[str, ...],
        payload_bytes: int,
    ) -> None:
        self._policy = policy
        self._manifest = manifest
        self._purpose = purpose
        self._payload_categories = payload_categories
        self._payload_bytes = payload_bytes
        self._finished = False

    def finish(self, status: str, *, error_code: str | None = None) -> None:
        if self._finished:
            return
        self._finished = True
        self._policy._append_audit(
            manifest=self._manifest,
            purpose=self._purpose,
            payload_categories=self._payload_categories,
            payload_bytes=self._payload_bytes,
            decision="allowed",
            outcome=status,
            error_code=error_code,
        )


class ProviderEgressPolicyStore:
    """Persists manifest-bound consent and append-only redacted decisions."""

    def __init__(self, root_dir: Path) -> None:
        self._root_dir = root_dir.resolve()
        directory = self._root_dir / "library" / "global" / "providers"
        self._policy_path = directory / "provider-egress-policy.json"
        self._audit_path = directory / "provider-egress-audit.jsonl"
        self._lock = Lock()

    def manifest(
        self,
        *,
        provider_id: str,
        endpoint: str,
        purposes: tuple[str, ...],
        payload_categories: tuple[str, ...],
        max_payload_bytes: int = 256 * 1024,
    ) -> ProviderEgressManifest:
        provider = _required_token(provider_id, _PROVIDER_ID, "provider_id")
        canonical_endpoint, external = _canonical_endpoint(endpoint)
        normalized_purposes = _tokens(purposes, _PURPOSE, "purpose")
        normalized_categories = _tokens(payload_categories, _CATEGORY, "payload category")
        if max_payload_bytes < 1:
            raise ProviderEgressError("provider egress payload budget must be positive")
        identity = {
            "revision": _MANIFEST_REVISION,
            "provider_id": provider,
            "endpoint": canonical_endpoint,
            "purposes": normalized_purposes,
            "payload_categories": normalized_categories,
            "max_payload_bytes": max_payload_bytes,
            "external": external,
        }
        digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return ProviderEgressManifest(
            manifest_id=f"egress-{digest}",
            revision=_MANIFEST_REVISION,
            provider_id=provider,
            endpoint=canonical_endpoint,
            purposes=normalized_purposes,
            payload_categories=normalized_categories,
            max_payload_bytes=max_payload_bytes,
            external=external,
        )

    def is_consented(self, manifest: ProviderEgressManifest) -> bool:
        if not manifest.external:
            return True
        with model_dispatch_authority_fence(self._root_dir), self._lock:
            payload = self._read_policy()
            consent = payload.get("consents", {}).get(manifest.provider_id)
            return isinstance(consent, dict) and consent.get("manifest_id") == manifest.manifest_id

    def grant(self, manifest: ProviderEgressManifest, *, manifest_id: str, confirm: bool) -> None:
        if not confirm or manifest_id != manifest.manifest_id:
            raise ProviderEgressError("provider egress consent requires the current manifest")
        if not manifest.external:
            return
        with model_dispatch_authority_fence(self._root_dir), self._lock:
            payload = self._read_policy()
            consents = payload.setdefault("consents", {})
            consents[manifest.provider_id] = {
                "manifest_id": manifest.manifest_id,
                "revision": manifest.revision,
                "granted_at": _now(),
            }
            self._write_policy(payload)

    def revoke(self, provider_id: str) -> None:
        provider = _required_token(provider_id, _PROVIDER_ID, "provider_id")
        with model_dispatch_authority_fence(self._root_dir), self._lock:
            payload = self._read_policy()
            consents = payload.setdefault("consents", {})
            consents.pop(provider, None)
            self._write_policy(payload)

    def authorize(
        self,
        manifest: ProviderEgressManifest,
        *,
        purpose: str,
        payload_categories: tuple[str, ...],
        payload_bytes: int,
    ) -> ProviderEgressLease:
        normalized_purpose, normalized_categories = self.validate(
            manifest,
            purpose=purpose,
            payload_categories=payload_categories,
            payload_bytes=payload_bytes,
        )
        return ProviderEgressLease(
            self,
            manifest=manifest,
            purpose=normalized_purpose,
            payload_categories=normalized_categories,
            payload_bytes=payload_bytes,
        )

    def validate(
        self,
        manifest: ProviderEgressManifest,
        *,
        purpose: str,
        payload_categories: tuple[str, ...],
        payload_bytes: int,
    ) -> tuple[str, tuple[str, ...]]:
        """Evaluate the Gate without issuing an execution Lease."""

        with model_dispatch_authority_fence(self._root_dir):
            normalized_purpose = _required_token(purpose, _PURPOSE, "purpose")
            normalized_categories = _tokens(payload_categories, _CATEGORY, "payload category")
            error_code: str | None = None
            if normalized_purpose not in manifest.purposes:
                error_code = "purpose_not_manifested"
            elif not set(normalized_categories).issubset(manifest.payload_categories):
                error_code = "payload_category_not_manifested"
            elif payload_bytes < 0 or payload_bytes > manifest.max_payload_bytes:
                error_code = "payload_budget_exceeded"
            elif manifest.external and not self.is_consented(manifest):
                error_code = "consent_required"
            if error_code:
                self._append_audit(
                    manifest=manifest,
                    purpose=normalized_purpose,
                    payload_categories=normalized_categories,
                    payload_bytes=max(0, payload_bytes),
                    decision="denied",
                    outcome="not_sent",
                    error_code=error_code,
                )
                raise ProviderEgressError(error_code)
            return normalized_purpose, normalized_categories


    def _read_policy(self) -> dict[str, object]:
        if not self._policy_path.exists():
            return {"schema_version": _SCHEMA_VERSION, "consents": {}}
        try:
            payload = json.loads(self._policy_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProviderEgressError("provider egress policy is unreadable") from error
        if not isinstance(payload, dict) or payload.get("schema_version") != _SCHEMA_VERSION:
            raise ProviderEgressError("provider egress policy is invalid")
        if not isinstance(payload.get("consents"), dict):
            raise ProviderEgressError("provider egress consents are invalid")
        return payload

    def _write_policy(self, payload: dict[str, object]) -> None:
        payload["schema_version"] = _SCHEMA_VERSION
        self._policy_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self._policy_path, json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")

    def _append_audit(
        self,
        *,
        manifest: ProviderEgressManifest,
        purpose: str,
        payload_categories: tuple[str, ...],
        payload_bytes: int,
        decision: str,
        outcome: str,
        error_code: str | None,
    ) -> None:
        record = {
            "schema_version": _SCHEMA_VERSION,
            "event_id": "egress-event-" + hashlib.sha256(
                f"{manifest.manifest_id}\0{purpose}\0{_now()}\0{os.urandom(16).hex()}".encode()
            ).hexdigest(),
            "recorded_at": _now(),
            "manifest_id": manifest.manifest_id,
            "provider_id": manifest.provider_id,
            "endpoint": manifest.endpoint,
            "purpose": purpose,
            "payload_categories": list(payload_categories),
            "payload_bytes": payload_bytes,
            "decision": decision,
            "outcome": outcome,
            "error_code": error_code,
        }
        encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        with model_dispatch_authority_fence(self._root_dir), self._lock:
            self._audit_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(self._audit_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                os.write(descriptor, encoded.encode("utf-8"))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)


def build_provider_egress_guard(
    root_dir: Path,
    *,
    provider_id: str,
    endpoint: str,
    purposes: tuple[str, ...] = DEFAULT_PROVIDER_EGRESS_PURPOSES,
    payload_categories: tuple[str, ...] = DEFAULT_PROVIDER_EGRESS_CATEGORIES,
    max_payload_bytes: int = DEFAULT_PROVIDER_EGRESS_MAX_BYTES,
) -> Callable[[str, tuple[str, ...], int], ProviderEgressLease]:
    policy = ProviderEgressPolicyStore(root_dir)
    manifest = policy.manifest(
        provider_id=provider_id,
        endpoint=endpoint,
        purposes=purposes,
        payload_categories=payload_categories,
        max_payload_bytes=max_payload_bytes,
    )

    def guard(purpose: str, categories: tuple[str, ...], payload_bytes: int) -> ProviderEgressLease:
        return policy.authorize(
            manifest,
            purpose=purpose,
            payload_categories=categories,
            payload_bytes=payload_bytes,
        )

    return guard


def build_active_provider_egress_guard(
    root_dir: Path,
    *,
    endpoint: str,
    purposes: tuple[str, ...] = DEFAULT_PROVIDER_EGRESS_PURPOSES,
    max_payload_bytes: int = DEFAULT_PROVIDER_EGRESS_MAX_BYTES,
) -> Callable[[str, tuple[str, ...], int], ProviderEgressLease]:
    from backend.providers import ProviderRegistry

    records = ProviderRegistry(root_dir).list_readonly(
        fallback={
            "name": "默认供应商",
            "llm_provider": "openai",
            "base_url": endpoint,
            "api_path": "/chat/completions",
            "model": "",
            "models": [],
            "enabled": True,
        }
    )
    active = next((record for record in records if record.get("is_active")), records[0])
    return build_provider_egress_guard(
        root_dir,
        provider_id=str(active["provider_id"]),
        endpoint=endpoint,
        purposes=purposes,
        payload_categories=DEFAULT_PROVIDER_EGRESS_CATEGORIES,
        max_payload_bytes=max_payload_bytes,
    )


def _canonical_endpoint(value: str) -> tuple[str, bool]:
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except (AttributeError, ValueError) as error:
        raise ProviderEgressError("provider endpoint is invalid") from error
    if parsed.scheme.lower() not in {"http", "https", "ws", "wss"} or not parsed.hostname:
        raise ProviderEgressError("provider endpoint must use http, https, ws or wss")
    if parsed.username is not None or parsed.password is not None:
        raise ProviderEgressError("provider endpoint cannot contain credentials")
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    display_host = f"[{host}]" if ":" in host else host
    if port is not None:
        display_host += f":{port}"
    endpoint = urlunsplit((parsed.scheme.lower(), display_host, parsed.path.rstrip("/") or "/", "", ""))
    external = not _is_local_host(host)
    return endpoint, external


def _is_local_host(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback


def _tokens(values: tuple[str, ...], pattern: re.Pattern[str], label: str) -> tuple[str, ...]:
    result = tuple(sorted({_required_token(value, pattern, label) for value in values}))
    if not result:
        raise ProviderEgressError(f"provider egress {label} list is required")
    return result


def _required_token(value: str, pattern: re.Pattern[str], label: str) -> str:
    normalized = str(value).strip().lower()
    if not pattern.fullmatch(normalized):
        raise ProviderEgressError(f"provider egress {label} is invalid")
    return normalized


def _now() -> str:
    return datetime.now(UTC).isoformat()
