"""Freeze the workbench's public model choice for the preserved Turn runtime.

This is a separate configuration contract, not a ProviderRegistry route. The
key stays in ModelConfiguration; only its monotonically revised public choice
and the host-verified execution identities enter the old immutable store.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from core.ai_kernel.contracts import _validate_internal_agent_binding

from .model_config import ModelConfigurationError


CONFIGURATION_FIELDS = ("purpose", "provider", "base_url", "model", "allow_remote",
                        "revision", "configured", "has_api_key")
SNAPSHOT_KIND = "recognition-model-routing-snapshot-v1"


def _revision(payload: Mapping[str, object]) -> str:
    # This is the existing Turn protocol's canonical in-memory identity format,
    # not a filesystem checksum or a second secret/configuration authority.
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RecognitionRoutingSnapshot:
    payload_ref: str
    revision: str
    payload: Mapping[str, object]

    def generation_binding(self) -> dict[str, object]:
        return {"payload_ref": self.payload_ref, "revision": self.revision,
                "prompt_cache_scope_identity": self.payload["prompt_cache_scope_identity"],
                "configuration": dict(self.payload["configuration"]),
                "execution_location": self.payload["execution_location"]}


class RecognitionModelRoutingSnapshotAuthority:
    def __init__(self, models, payloads):
        self.models, self.payloads = models, payloads

    def acquire(self, *, turn_id: str, project_id: str, context_packet_id: str,
                project_profile_id: str, project_profile_revision: int,
                boundary_profile_id: str, boundary_profile_revision: int,
                capability_ids: tuple[str, ...], agent_binding: Mapping[str, object] | None,
                allow_remote: bool, expected_execution_location: str | None = None) -> RecognitionRoutingSnapshot:
        """Host composition supplies verified profile/Agent identities.

        Re-acquiring an accepted Turn must retain its exact immutable choice;
        changing configuration or ownership requires a new confirmed task.
        """
        public = self.models.public()["generation"]
        configuration = {key: public.get(key) for key in CONFIGURATION_FIELDS}
        if (configuration["purpose"] != "generation" or configuration["provider"] != "openai"
                or configuration["configured"] is not True or configuration["has_api_key"] is not True):
            raise ModelConfigurationError("model_not_configured")
        endpoint = urlsplit(str(configuration["base_url"]))
        local = endpoint.hostname in {"localhost", "127.0.0.1", "::1"}
        if expected_execution_location is not None and expected_execution_location != ("local_loopback" if local else "remote"):
            raise ModelConfigurationError("recognition_turn_routing_location_changed")
        if (not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
                or (local and endpoint.scheme not in {"http", "https"})
                or (not local and (endpoint.scheme != "https" or configuration["allow_remote"] is not True or allow_remote is not True))):
            raise ModelConfigurationError("model_egress_remote_not_consented")
        # Reuse the preserved exact Agent contract, including cancel epoch and
        # budget identity. Host verification is still required before acquire.
        agent = None if agent_binding is None else _validate_internal_agent_binding(agent_binding)
        identity = {
            "turn_id": turn_id, "project_id": project_id, "context_packet_id": context_packet_id,
            "project_profile_id": project_profile_id, "project_profile_revision": project_profile_revision,
            "boundary_profile_id": boundary_profile_id, "boundary_profile_revision": boundary_profile_revision,
            "capability_ids": sorted(set(capability_ids)), "agent": agent,
            "configuration": configuration,
            "execution_location": "local_loopback" if local else "remote",
        }
        payload = {"schema_version": "1.0.0", "kind": SNAPSHOT_KIND, **identity,
                   "catalog_revision": _revision({"configuration": configuration}),
                   "prompt_cache_scope_identity": _revision(identity)}
        try:
            reference = self.payloads.get_or_create_immutable_payload(turn_id, SNAPSHOT_KIND, payload)
        except ValueError:
            raise ModelConfigurationError("recognition_turn_routing_changed") from None
        return RecognitionRoutingSnapshot(reference, _revision(payload), payload)
