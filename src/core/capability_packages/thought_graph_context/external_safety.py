from __future__ import annotations

import re
import json
from collections.abc import Iterable

from ...context_graph.validation import ContextGraphValidationError


_BUILTIN_CANARY = re.compile(
    r"linemap[-_ ]?secret[-_ ]?canary|do[-_ ]?not[-_ ]?leak",
    re.IGNORECASE,
)
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?:api[_-]?key|access[_-]?token|secret|password)\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{8,}",
    re.IGNORECASE,
)


def reject_external_secret_material(
    raw: str,
    *,
    secret_canaries: Iterable[str] = (),
) -> None:
    """Reject explicit canaries and credential-shaped material locally.

    This deliberately performs no SecretStore lookup and never resolves a
    lease.  It is a conservative file/export boundary filter, shared by every
    Thought Graph importer and exporter.
    """
    for canary in secret_canaries:
        if not isinstance(canary, str) or not canary:
            raise ContextGraphValidationError(("invalid_secret_canary_policy",))
        if canary in raw:
            raise ContextGraphValidationError(("secret_canary_detected",))
    if _BUILTIN_CANARY.search(raw):
        raise ContextGraphValidationError(("secret_canary_detected",))
    if _CREDENTIAL_ASSIGNMENT.search(raw):
        raise ContextGraphValidationError(("external_secret_material_detected",))


def reject_external_json_value(
    value: object,
    *,
    secret_canaries: Iterable[str] = (),
) -> None:
    """Scan parsed JSON so Unicode escapes cannot hide sensitive strings."""
    try:
        canonical = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ContextGraphValidationError(("invalid_external_json_value",)) from exc
    reject_external_secret_material(canonical, secret_canaries=secret_canaries)
