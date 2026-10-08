"""Core-owned materialization boundary for the local Session Placement signer."""

from __future__ import annotations

import hashlib

from backend.security.secrets import SecretStore
from core.product_core.session_placement import DeviceIdentity, HostSigningIdentity


_SIGNING_KEY_REF = "session-placement:host-signing-key"


class HostSigningIdentityStore:
    """Load or create the host signer without exposing its private key."""

    def __init__(self, secret_store: SecretStore) -> None:
        self._secret_store = secret_store

    def load_or_create(self) -> HostSigningIdentity:
        snapshot = self._secret_store.get_snapshot(_SIGNING_KEY_REF)
        if snapshot.value:
            temporary = HostSigningIdentity.from_private_key(
                device_id="host-pending", private_key=snapshot.value,
            )
            device_id = _device_id(temporary.public_identity.public_key)
            return HostSigningIdentity.from_private_key(
                device_id=device_id,
                private_key=snapshot.value,
            )
        temporary = HostSigningIdentity(device_id="host-pending")
        encoded = temporary.export_private_key()
        self._secret_store.set(_SIGNING_KEY_REF, encoded)
        return HostSigningIdentity.from_private_key(
            device_id=_device_id(temporary.public_identity.public_key),
            private_key=encoded,
        )

    def public_identity(self) -> DeviceIdentity:
        return self.load_or_create().public_identity


def _device_id(public_key: str) -> str:
    # DeviceIdentity derives from the public half; this deliberately stays
    # inside the same capability so callers never receive private material.
    return "host-" + hashlib.sha256(public_key.encode("ascii")).hexdigest()[:24]
