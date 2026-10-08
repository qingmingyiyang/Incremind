from __future__ import annotations

from pathlib import Path

import pytest

from core.aggregate_repository_factory import (
    AUTHORITY_DATABASE_NAME,
    STRUCTURED_DATABASE_NAME,
    TARGET_IDENTITY,
    AggregateRepositoryFactory,
    AggregateRepositoryFactoryError,
)
from core.memory_core import (
    shared_trust_audit_activation_id,
    shared_trust_audit_activation_payload,
)
from core.product_core.project_series_scope import (
    ProjectSeriesScopeError,
    ProjectSeriesScopeResolver,
)
from core.storage_provider import (
    AggregateAuthorityEvidence,
    JsonObjectStore,
    SQLiteAggregateAuthorityStore,
    SQLiteStructuredRecordStore,
)


_MEMBERS = (
    "memory_atoms", "memory_publications", "memory_scenarios",
    "memory_series_memory", "memory_transitions", "project_skills",
)


def _parts(tmp_path: Path) -> tuple[AggregateRepositoryFactory, JsonObjectStore]:
    store = JsonObjectStore(tmp_path / ".rebuild-data")
    return (
        AggregateRepositoryFactory(runtime_root=tmp_path, namespace_id="default", json_store=store),
        store,
    )


def _payload(object_id: str = "series-memory-alpha", **changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": object_id,
        "series_id": "series-alpha",
        "project_ids": ["project-alpha"],
        "revision": 7,
        "stale": False,
        "trust_status": "trusted",
    }
    value.update(changes)
    return value


def _resolver(tmp_path: Path) -> tuple[ProjectSeriesScopeResolver, JsonObjectStore]:
    factory, store = _parts(tmp_path)
    return ProjectSeriesScopeResolver(factory, store), store


def _assert_code(exc: pytest.ExceptionInfo[ProjectSeriesScopeError], code: str) -> None:
    assert exc.value.code == code
    assert str(exc.value) == code


def test_json_authority_returns_portable_frozen_scope_snapshot(tmp_path: Path) -> None:
    resolver, store = _resolver(tmp_path)
    store.write("memory_series_memory", "series-memory-alpha", _payload(), expected_revision=0)

    result = resolver.resolve("series-alpha")

    assert result.namespace_id == "default"
    assert result.project_id == "project-alpha"
    assert result.object_id == "series-memory-alpha"
    assert result.payload_revision == 7
    assert result.storage_revision == 1
    assert result.authority_identity == "json:object-store-v1"
    assert result.authority_ref == "crp://default/memory/series/series-memory-alpha"
    assert "\\" not in result.authority_ref
    assert ":\\" not in result.authority_ref


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"project_ids": []}, "project_scope_ambiguous"),
        ({"project_ids": ["project-alpha", "project-beta"]}, "project_scope_ambiguous"),
        ({"stale": True}, "series_scope_stale"),
        ({"trust_status": "system_generated"}, "series_scope_untrusted"),
        ({"trust_status": "imported_unverified"}, "series_scope_untrusted"),
        ({"trust_status": "failed"}, "series_scope_untrusted"),
        ({"id": "bad/id"}, "series_scope_record_invalid"),
        ({"revision": 0}, "series_scope_record_invalid"),
    ],
)
def test_json_authority_rejects_non_authoritative_mapping(
    tmp_path: Path, changes: dict[str, object], code: str,
) -> None:
    resolver, store = _resolver(tmp_path)
    value = _payload(**changes)
    store.write(
        "memory_series_memory",
        "safe-series-record" if value["id"] == "bad/id" else str(value["id"]),
        value,
        expected_revision=0,
    )

    with pytest.raises(ProjectSeriesScopeError) as exc:
        resolver.resolve("series-alpha")

    _assert_code(exc, code)


def test_user_confirmed_json_mapping_is_authoritative(tmp_path: Path) -> None:
    resolver, store = _resolver(tmp_path)
    store.write(
        "memory_series_memory",
        "series-memory-confirmed",
        _payload("series-memory-confirmed", trust_status="user_confirmed"),
        expected_revision=0,
    )

    assert resolver.resolve("series-alpha").project_id == "project-alpha"


def test_json_physical_key_and_payload_id_mismatch_fails_closed(tmp_path: Path) -> None:
    resolver, store = _resolver(tmp_path)
    store.write(
        "memory_series_memory",
        "physical-record-key",
        _payload("payload-series-id"),
        expected_revision=0,
    )

    with pytest.raises(ProjectSeriesScopeError) as exc:
        resolver.resolve("series-alpha")

    _assert_code(exc, "series_scope_record_invalid")


def test_json_payload_id_collision_is_ambiguous_even_when_one_key_looks_canonical(tmp_path: Path) -> None:
    resolver, store = _resolver(tmp_path)
    payload = _payload("series-memory-alpha")
    store.write("memory_series_memory", "series-memory-alpha", payload, expected_revision=0)
    store.write("memory_series_memory", "other-physical-key", payload, expected_revision=0)

    with pytest.raises(ProjectSeriesScopeError) as exc:
        resolver.resolve("series-alpha")

    _assert_code(exc, "series_scope_ambiguous")


def test_json_authority_rejects_missing_or_duplicate_series_mapping(tmp_path: Path) -> None:
    resolver, store = _resolver(tmp_path)
    with pytest.raises(ProjectSeriesScopeError) as missing:
        resolver.resolve("series-alpha")
    _assert_code(missing, "series_scope_not_found")

    store.write("memory_series_memory", "series-memory-a", _payload("series-memory-a"), expected_revision=0)
    store.write("memory_series_memory", "series-memory-b", _payload("series-memory-b"), expected_revision=0)
    with pytest.raises(ProjectSeriesScopeError) as duplicate:
        resolver.resolve("series-alpha")
    _assert_code(duplicate, "series_scope_ambiguous")


def _evidence() -> AggregateAuthorityEvidence:
    return AggregateAuthorityEvidence(
        migration_id="scope-fixture-v1", source_fingerprint="a" * 64,
        target_fingerprint="b" * 64, target_identity=TARGET_IDENTITY,
    )


def _activate_compound(tmp_path: Path, records: SQLiteStructuredRecordStore) -> None:
    authority = SQLiteAggregateAuthorityStore(tmp_path / ".rebuild-data" / AUTHORITY_DATABASE_NAME)
    for member in _MEMBERS:
        initial = authority.create_json_active(namespace_id="default", aggregate=member, reason="fixture")
        staged = authority.transition(namespace_id="default", aggregate=member, expected_revision=initial.revision, to_state="sqlite_staged", evidence=_evidence(), reason="fixture")
        authority.transition(namespace_id="default", aggregate=member, expected_revision=staged.revision, to_state="sqlite_active", evidence=_evidence(), reason="fixture")
        with records.begin() as uow:
            uow.put("aggregate_authority_targets", f"default~{member}", {
                "namespace_id": "default", "aggregate": member, "target_identity": TARGET_IDENTITY,
                "source_fingerprint": "a" * 64, "target_fingerprint": "b" * 64, "migration_id": "scope-fixture-v1",
            }, expected_revision=0)
            uow.commit()
    activation = shared_trust_audit_activation_payload(
        namespace_id="default", target_identity=TARGET_IDENTITY, activation_id="scope-activation-v1",
        member_migrations={member: "scope-fixture-v1" for member in _MEMBERS},
        source_fingerprint="a" * 64, target_fingerprint="b" * 64, activated_at="2026-08-25T10:00:00+08:00",
    )
    with records.begin() as uow:
        uow.put("aggregate_authority_compound_activations", shared_trust_audit_activation_id("default"), activation, expected_revision=0)
        uow.commit()


def test_complete_sqlite_compound_authority_is_used_instead_of_json(tmp_path: Path) -> None:
    factory, json_store = _parts(tmp_path)
    json_store.write("memory_series_memory", "json-series", _payload("json-series", project_ids=["wrong-project"]), expected_revision=0)
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME)
    _activate_compound(tmp_path, records)
    sqlite_payload = _payload("sqlite-series", project_ids=["sqlite-project"], revision=3, trust_status="user_confirmed")
    with records.begin() as uow:
        uow.put("memory_series_memory", "sqlite-series", sqlite_payload, expected_revision=0)
        uow.commit()

    result = ProjectSeriesScopeResolver(factory, json_store).resolve("series-alpha")

    assert result.project_id == "sqlite-project"
    assert result.object_id == "sqlite-series"
    assert result.payload_revision == 3
    assert result.storage_revision == 1
    assert result.authority_identity == TARGET_IDENTITY
    assert result.authority_ref == "crp://default/memory/series/sqlite-series"


def test_sqlite_physical_key_and_payload_id_mismatch_fails_closed(tmp_path: Path) -> None:
    factory, json_store = _parts(tmp_path)
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME)
    _activate_compound(tmp_path, records)
    with records.begin() as uow:
        uow.put(
            "memory_series_memory",
            "sqlite-physical-key",
            _payload("sqlite-payload-id"),
            expected_revision=0,
        )
        uow.commit()

    with pytest.raises(ProjectSeriesScopeError) as exc:
        ProjectSeriesScopeResolver(factory, json_store).resolve("series-alpha")

    _assert_code(exc, "series_scope_record_invalid")


def test_partial_authority_maps_factory_failure_to_stable_unavailable_code(tmp_path: Path) -> None:
    factory, store = _parts(tmp_path)
    authority = SQLiteAggregateAuthorityStore(tmp_path / ".rebuild-data" / AUTHORITY_DATABASE_NAME)
    initial = authority.create_json_active(namespace_id="default", aggregate="memory_atoms", reason="fixture")
    staged = authority.transition(namespace_id="default", aggregate="memory_atoms", expected_revision=initial.revision, to_state="sqlite_staged", evidence=_evidence(), reason="fixture")
    authority.transition(namespace_id="default", aggregate="memory_atoms", expected_revision=staged.revision, to_state="sqlite_active", evidence=_evidence(), reason="fixture")

    with pytest.raises(ProjectSeriesScopeError) as exc:
        ProjectSeriesScopeResolver(factory, store).resolve("series-alpha")
    _assert_code(exc, "authority_unavailable")


def test_factory_exception_is_mapped_without_exposing_authority_detail(tmp_path: Path) -> None:
    factory, store = _parts(tmp_path)
    resolver = ProjectSeriesScopeResolver(factory, store)
    object.__setattr__(resolver, "_factory", _FailingFactory())

    with pytest.raises(ProjectSeriesScopeError) as exc:
        resolver.resolve("series-alpha")
    _assert_code(exc, "authority_unavailable")


class _FailingFactory:
    namespace_id = "default"

    def memory_publication_authority_resolution(self) -> object:
        raise AggregateRepositoryFactoryError("private filesystem detail")
