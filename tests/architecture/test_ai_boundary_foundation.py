from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[2]
BOUNDARY_ROOT = ROOT / "src" / "core" / "ai_boundary"
TOOLING_ROOT = ROOT / "src" / "core" / "ai_tooling"


def test_boundary_and_tooling_are_framework_and_provider_neutral() -> None:
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for root in (BOUNDARY_ROOT, TOOLING_ROOT)
        for path in sorted(root.glob("*.py"))
    )
    for forbidden in (
        "fastapi",
        "backend.",
        "litellm",
        "httpx",
        "urllib",
        "ProviderRegistry",
        "JsonObjectStore",
        "secret_store",
    ):
        assert forbidden not in text


def test_v2_foundation_schemas_are_strict_and_secret_free() -> None:
    names = (
        "tool-definition.schema.json",
        "boundary-request.schema.json",
        "boundary-decision.schema.json",
        "boundary-grant.schema.json",
        "project-boundary-profile.schema.json",
    )
    for name in names:
        payload = json.loads((ROOT / "core-contracts" / "ai" / name).read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(payload)
        assert payload["additionalProperties"] is False
        encoded = json.dumps(payload).lower()
        for forbidden in ("api_key", "authorization", "cookie", "local_path", "windows_path"):
            assert forbidden not in encoded


def test_boundary_engine_keeps_hard_rules_before_grant_resolution() -> None:
    policy = (BOUNDARY_ROOT / "policy.py").read_text(encoding="utf-8")
    hard_rule = policy.index("hard_sensitive_remote_denied")
    grant_resolution = policy.index("_matching_grant(request")
    assert hard_rule < grant_resolution
