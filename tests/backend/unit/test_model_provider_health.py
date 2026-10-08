from __future__ import annotations

from pathlib import Path

import pytest

from backend.model_provider_health import (
    ModelProviderHealthError,
    ModelProviderHealthStore,
    ProviderFailureClass,
    ProviderHealthScope,
    ProviderHealthState,
    classify_provider_failure,
)


def _scope(**overrides: object) -> ProviderHealthScope:
    values: dict[str, object] = {
        "project_id": "project-a", "boundary_profile_id": "boundary-a",
        "boundary_revision": 1, "route_key": "search.answer",
        "provider_id": "openai", "provider_revision": "provider-rev-1", "model_name": "gpt-5",
    }
    values.update(overrides)
    return ProviderHealthScope(**values)  # type: ignore[arg-type]


def test_unknown_then_success_is_revision_scoped(tmp_path: Path) -> None:
    store = ModelProviderHealthStore(tmp_path)
    scope = _scope()

    assert store.get(scope).state is ProviderHealthState.UNKNOWN
    assert store.observe_success(scope).state is ProviderHealthState.HEALTHY
    assert store.get(_scope(provider_revision="provider-rev-2")).state is ProviderHealthState.UNKNOWN
    assert store.get(_scope(boundary_revision=2)).state is ProviderHealthState.UNKNOWN


def test_transient_failure_opens_then_expires(tmp_path: Path) -> None:
    now = [1000.0]
    store = ModelProviderHealthStore(tmp_path, transient_open_ttl_seconds=30, clock=lambda: now[0])

    record = store.observe_failure(_scope(), ProviderFailureClass.RATE_LIMITED)

    assert record.state is ProviderHealthState.OPEN
    assert record.failure_class is ProviderFailureClass.RATE_LIMITED
    now[0] += 31
    assert store.get(_scope()).state is ProviderHealthState.UNKNOWN


def test_auth_and_quota_block_only_current_revision(tmp_path: Path) -> None:
    store = ModelProviderHealthStore(tmp_path)
    scope = _scope()

    assert store.observe_failure(scope, "auth").state is ProviderHealthState.BLOCKED
    assert store.get(_scope(provider_revision="provider-rev-2")).state is ProviderHealthState.UNKNOWN
    assert store.observe_failure(scope, "quota").state is ProviderHealthState.BLOCKED


def test_stable_exception_classification_does_not_keep_exception_text(tmp_path: Path) -> None:
    class ResponseError(Exception):
        status_code = 503

    store = ModelProviderHealthStore(tmp_path)
    record = store.observe_failure(_scope(), ResponseError("prompt=private response body"))

    assert classify_provider_failure(TimeoutError("body")) is ProviderFailureClass.TIMEOUT
    assert record.state is ProviderHealthState.OPEN
    database = (tmp_path / ".rebuild-data" / "model-provider-health.sqlite3").read_bytes()
    assert b"private" not in database
    assert b"response body" not in database


@pytest.mark.parametrize("field, value", [
    ("project_id", "prompt=private"),
    ("model_name", "C:/private/body"),
    ("provider_revision", "prompt=private"),
])
def test_scope_rejects_non_metadata_or_sensitive_like_input(field: str, value: object) -> None:
    with pytest.raises(ModelProviderHealthError):
        _scope(**{field: value})


def test_degraded_failure_does_not_imply_a_retry(tmp_path: Path) -> None:
    store = ModelProviderHealthStore(tmp_path)
    record = store.observe_failure(_scope(), "client_error")

    assert record.state is ProviderHealthState.DEGRADED
    assert record.is_unavailable is False


def test_credential_change_can_invalidate_only_one_provider(tmp_path: Path) -> None:
    store = ModelProviderHealthStore(tmp_path)
    store.observe_failure(_scope(), "auth")
    other = _scope(provider_id="other-provider")
    store.observe_failure(other, "auth")

    store.invalidate_provider("openai")

    assert store.get(_scope()).state is ProviderHealthState.UNKNOWN
    assert store.get(other).state is ProviderHealthState.BLOCKED
