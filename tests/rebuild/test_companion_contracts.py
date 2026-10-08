from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "core-contracts" / "rebuild" / "companion"
MANIFEST_PATH = CONTRACT_ROOT / "manifest.json"
EXPECTED_CONTRACTS = {
    "action-request.schema.json",
    "overlay-event.schema.json",
    "state-projection.schema.json",
    "chat-request.schema.json",
    "chat-response.schema.json",
    "provider-job.schema.json",
    "forget-receipt.schema.json",
}
ENVELOPE_FIELDS = {"schema_version", "occurred_at", "source", "payload"}
FORBIDDEN_DECLARED_FIELDS = {
    "api_key",
    "backend_base_url",
    "command",
    "delta",
    "endpoint",
    "method",
    "path",
    "secret",
    "system_prompt",
    "url",
}


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _schema(file_name: str) -> dict[str, Any]:
    return _load(CONTRACT_ROOT / file_name)


def _validator(file_name: str) -> Draft202012Validator:
    return Draft202012Validator(_schema(file_name), format_checker=FormatChecker())


def _fixture_paths(file_name: str, prefix: str) -> list[Path]:
    fixture_dir = CONTRACT_ROOT / "fixtures" / file_name.removesuffix(".schema.json")
    return sorted(fixture_dir.glob(f"{prefix}-*.json"))


def _declared_property_names(value: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(value, dict):
        properties = value.get("properties")
        if isinstance(properties, dict):
            names.update(str(item) for item in properties)
        for nested in value.values():
            names.update(_declared_property_names(nested))
    elif isinstance(value, list):
        for nested in value:
            names.update(_declared_property_names(nested))
    return names


def _object_schema_paths(value: Any, path: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], dict[str, Any]]]:
    found: list[tuple[tuple[str, ...], dict[str, Any]]] = []
    if isinstance(value, dict):
        if value.get("type") == "object":
            found.append((path, value))
        for key, nested in value.items():
            found.extend(_object_schema_paths(nested, (*path, str(key))))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            found.extend(_object_schema_paths(nested, (*path, str(index))))
    return found


def test_manifest_inventory_matches_exact_schema_set() -> None:
    manifest = _load(MANIFEST_PATH)
    actual = {path.name for path in CONTRACT_ROOT.glob("*.schema.json")}
    registered = {item["file"] for item in manifest["contracts"]}

    assert actual == EXPECTED_CONTRACTS
    assert registered == EXPECTED_CONTRACTS
    assert len(manifest["contracts"]) == len(EXPECTED_CONTRACTS)
    assert len({item["name"] for item in manifest["contracts"]}) == len(EXPECTED_CONTRACTS)


def test_manifest_freezes_current_only_until_a_previous_version_exists() -> None:
    manifest = _load(MANIFEST_PATH)

    assert manifest["current_version"] == "1.0.0"
    assert manifest["previous_version"] is None
    assert manifest["accepted_versions"] == ["1.0.0"]
    assert manifest["unknown_version_policy"] == "reject"
    assert manifest["compatibility_policy"] == "accept_current_and_immediately_previous_published_version"


@pytest.mark.parametrize("file_name", sorted(EXPECTED_CONTRACTS))
def test_companion_schemas_are_valid_draft_2020_12(file_name: str) -> None:
    schema = _schema(file_name)

    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"].endswith(f"/companion/{file_name}")
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize("file_name", sorted(EXPECTED_CONTRACTS))
def test_each_contract_has_strict_envelope(file_name: str) -> None:
    schema = _schema(file_name)
    required = set(schema["required"])

    assert ENVELOPE_FIELDS <= required
    assert {"request_id", "event_id"} & required
    assert schema["properties"]["schema_version"] == {"type": "string", "const": "1.0.0"}
    assert schema["properties"]["occurred_at"] == {"type": "string", "format": "date-time"}
    assert schema["properties"]["source"]["enum"]


@pytest.mark.parametrize("file_name", sorted(EXPECTED_CONTRACTS))
def test_valid_fixtures_pass_and_invalid_fixtures_fail(file_name: str) -> None:
    validator = _validator(file_name)
    valid_paths = _fixture_paths(file_name, "valid")
    invalid_paths = _fixture_paths(file_name, "invalid")

    assert valid_paths, file_name
    assert invalid_paths, file_name
    for path in valid_paths:
        errors = sorted(validator.iter_errors(_load(path)), key=lambda item: list(item.path))
        assert errors == [], f"{file_name}:{path.name}: {errors}"
    for path in invalid_paths:
        assert validator.is_valid(_load(path)) is False, f"{file_name}:{path.name}"


@pytest.mark.parametrize("file_name", sorted(EXPECTED_CONTRACTS))
@pytest.mark.parametrize("unsupported_version", ["0.9.0", "2.0.0", "future"])
def test_contracts_fail_closed_for_unsupported_versions(file_name: str, unsupported_version: str) -> None:
    instance = deepcopy(_load(_fixture_paths(file_name, "valid")[0]))
    instance["schema_version"] = unsupported_version

    assert _validator(file_name).is_valid(instance) is False


@pytest.mark.parametrize("file_name", sorted(EXPECTED_CONTRACTS))
def test_contracts_reject_unknown_top_level_fields(file_name: str) -> None:
    instance = deepcopy(_load(_fixture_paths(file_name, "valid")[0]))
    instance["backend_base_url"] = "http://127.0.0.1:8001"

    errors = list(_validator(file_name).iter_errors(instance))

    assert any(error.validator == "additionalProperties" for error in errors)


def test_contract_property_names_exclude_ambient_authority_and_secret_fields() -> None:
    for file_name in sorted(EXPECTED_CONTRACTS):
        declared = _declared_property_names(_schema(file_name))
        assert declared.isdisjoint(FORBIDDEN_DECLARED_FIELDS), (file_name, declared & FORBIDDEN_DECLARED_FIELDS)


@pytest.mark.parametrize("file_name", sorted(EXPECTED_CONTRACTS))
def test_every_declared_object_is_closed(file_name: str) -> None:
    for path, object_schema in _object_schema_paths(_schema(file_name)):
        assert object_schema.get("additionalProperties") is False, (file_name, path)


def test_action_request_freezes_known_action_variants() -> None:
    schema = _schema("action-request.schema.json")
    no_argument_actions = set(schema["$defs"]["noArgumentAction"]["properties"]["action_id"]["enum"])

    assert {
        "companion.context_menu.open",
        "companion.open_center",
        "companion.pet.hide",
        "companion.pet.show",
        "app.quit",
    } <= no_argument_actions
    assert schema["$defs"]["openPanelAction"]["properties"]["action_id"]["const"] == "companion.open_panel"
    assert schema["$defs"]["chatSubmitAction"]["properties"]["action_id"]["const"] == "companion.chat.submit"
    assert schema["$defs"]["historyDeleteAction"]["properties"]["action_id"]["const"] == "companion.history.delete"
    assert set(schema["$defs"]["overlayBoundAction"]["properties"]["action_id"]["enum"]) == {
        "companion.open_panel",
        "companion.reminder.acknowledge",
        "companion.overlay.dismiss",
        "companion.chat.retry",
    }


def test_pet_projection_cannot_carry_conversation_or_profile_text() -> None:
    projection = deepcopy(_load(_fixture_paths("state-projection.schema.json", "valid")[0]))
    projection["payload"]["text"] = "private conversation"
    projection["payload"]["nickname"] = "private profile"

    assert _validator("state-projection.schema.json").is_valid(projection) is False


def test_forget_receipt_cannot_return_deleted_text() -> None:
    receipt = deepcopy(_load(_fixture_paths("forget-receipt.schema.json", "valid")[0]))
    receipt["payload"]["deleted_content"] = "canary that must be forgotten"

    assert _validator("forget-receipt.schema.json").is_valid(receipt) is False
