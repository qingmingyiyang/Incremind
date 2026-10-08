from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from core.application_skill import (
    ApplicationSkillBindingConflict,
    ApplicationSkillBindingError,
    ApplicationSkillBindingRegistry,
    ApplicationSkillCatalog,
    ApplicationSkillCatalogSnapshot,
    ApplicationSkillSource,
)
from core.storage_provider import JsonObjectStore


NOW = "2026-07-18T12:00:00+00:00"


def _write_skill(source: Path, skill_id: str, *, body: str = "# Workflow\n\nUse evidence.\n") -> Path:
    root = source / skill_id
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(
        "---\n"
        f"name: {skill_id}\n"
        f"description: Use {skill_id} for an explicitly bound project task.\n"
        "---\n\n"
        f"{body}",
        encoding="utf-8",
    )
    return root


def _catalog(source: Path):
    return ApplicationSkillCatalog().discover(
        [ApplicationSkillSource("test-user", source, "user")]
    )


def _registry(tmp_path: Path) -> tuple[ApplicationSkillBindingRegistry, JsonObjectStore]:
    store = JsonObjectStore(tmp_path / "store", legacy_root=tmp_path / "legacy")
    return ApplicationSkillBindingRegistry(store, now=NOW), store


def _activate(
    registry: ApplicationSkillBindingRegistry,
    package,
    *,
    project_id: str = "project-alpha",
    consumers: tuple[str, ...] = ("answer.model-request",),
    priority: int = 500,
    terms: tuple[str, ...] = ("interview",),
    reason: str = "Bind the reviewed project method.",
):
    preview = registry.preview_bind(
        package,
        project_id=project_id,
        allowed_consumers=consumers,
        priority=priority,
        trigger_terms=terms,
    )
    return registry.activate(
        package,
        project_id=project_id,
        allowed_consumers=consumers,
        priority=priority,
        trigger_terms=terms,
        expected_registry_revision=preview["registry_revision"],
        preview_token=preview["preview_token"],
        confirm=True,
        reason=reason,
    )


def test_preview_is_read_only_and_activation_survives_restart(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    package = _catalog(source).get("missing")
    assert package is None
    package = _catalog_after_write(source, "evidence-review")
    registry, store = _registry(tmp_path)

    preview = registry.preview_bind(
        package,
        project_id="project-alpha",
        allowed_consumers=("answer.model-request",),
        priority=700,
        trigger_terms=("Evidence Review", " evidence   review "),
    )

    assert preview["write_effect"] == "none"
    assert preview["registry_revision"] == 0
    assert store.revision(registry.collection, registry.registry_id) == 0
    result = registry.activate(
        package,
        project_id="project-alpha",
        allowed_consumers=("answer.model-request",),
        priority=700,
        trigger_terms=("Evidence Review", " evidence   review "),
        expected_registry_revision=0,
        preview_token=preview["preview_token"],
        confirm=True,
        reason="User confirmed the reviewed binding.",
    )
    restarted = ApplicationSkillBindingRegistry(store, now="2026-07-18T12:01:00+00:00")

    assert result["status"] == "activated"
    assert result["registry_revision"] == 1
    assert restarted.status(_catalog(source))["bindings"][0]["effective_status"] == "active"


def test_two_projects_and_consumer_allowlists_are_isolated(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    alpha = _catalog_after_write(source, "alpha-method")
    beta = _catalog_after_write(source, "beta-method")
    catalog = _catalog(source)
    registry, _ = _registry(tmp_path)
    _activate(registry, alpha, project_id="project-alpha", priority=800)
    _activate(
        registry,
        beta,
        project_id="project-beta",
        consumers=("document.generate",),
        priority=600,
    )

    alpha_answer = registry.effective_bindings(
        catalog, project_id="project-alpha", consumer="answer.model-request"
    )
    beta_document = registry.effective_bindings(
        catalog, project_id="project-beta", consumer="document.generate"
    )

    assert [item.package.skill_id for item in alpha_answer] == ["alpha-method"]
    assert [item.package.skill_id for item in beta_document] == ["beta-method"]
    assert registry.effective_bindings(
        catalog, project_id="project-alpha", consumer="document.generate"
    ) == ()
    assert registry.effective_bindings(
        catalog, project_id="project-beta", consumer="answer.model-request"
    ) == ()


def test_exact_activation_replay_does_not_advance_revisions(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    package = _catalog_after_write(source, "stable-method")
    registry, store = _registry(tmp_path)
    first = _activate(registry, package)
    second = _activate(registry, package)

    assert first["registry_revision"] == 1
    assert second["status"] == "already_active"
    assert second["replayed"] is True
    assert second["registry_revision"] == 1
    assert second["bindings"][0]["binding_revision"] == 1
    assert len(second["history"]) == 1
    assert store.revision(registry.collection, registry.registry_id) == 1


def test_rebind_tracks_new_fingerprint_and_old_catalog_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    root = _write_skill(source, "changing-method")
    old_catalog = _catalog(source)
    old_package = old_catalog.packages[0]
    registry, _ = _registry(tmp_path)
    _activate(registry, old_package)

    (root / "SKILL.md").write_text(
        "---\nname: changing-method\n"
        "description: Use the changed reviewed method.\n---\n\n# Changed\n",
        encoding="utf-8",
    )
    new_catalog = _catalog(source)
    new_package = new_catalog.packages[0]

    assert registry.status(new_catalog)["bindings"][0]["effective_status"] == "drifted"
    assert registry.effective_bindings(
        new_catalog, project_id="project-alpha", consumer="answer.model-request"
    ) == ()
    rebound = _activate(registry, new_package)

    assert rebound["status"] == "rebound"
    assert rebound["registry_revision"] == 2
    assert rebound["bindings"][0]["binding_revision"] == 2
    assert [event["action"] for event in rebound["history"]] == ["activated", "rebound"]
    assert registry.effective_bindings(
        old_catalog, project_id="project-alpha", consumer="answer.model-request"
    ) == ()
    assert [item.package.fingerprint for item in registry.effective_bindings(
        new_catalog, project_id="project-alpha", consumer="answer.model-request"
    )] == [new_package.fingerprint]


def test_missing_and_duplicate_catalog_packages_are_not_effective(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    package = _catalog_after_write(source, "shared-method")
    registry, _ = _registry(tmp_path)
    _activate(registry, package)
    empty = _catalog(tmp_path / "empty-skills")

    other = tmp_path / "other-skills"
    _write_skill(other, "shared-method")
    duplicate = ApplicationSkillCatalog().discover(
        [
            ApplicationSkillSource("first", source),
            ApplicationSkillSource("second", other),
        ]
    )

    assert registry.status(empty)["bindings"][0]["effective_status"] == "missing"
    assert registry.status(duplicate)["bindings"][0]["effective_status"] == "missing"
    assert registry.effective_bindings(
        duplicate, project_id="project-alpha", consumer="answer.model-request"
    ) == ()


def test_deactivate_is_durable_idempotent_and_can_be_reactivated(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    package = _catalog_after_write(source, "toggle-method")
    catalog = _catalog(source)
    registry, store = _registry(tmp_path)
    _activate(registry, package)

    deactivated = registry.deactivate(
        project_id="project-alpha",
        skill_id="toggle-method",
        expected_registry_revision=1,
        confirm=True,
        reason="Project no longer uses this method.",
    )
    replay = registry.deactivate(
        project_id="project-alpha",
        skill_id="toggle-method",
        expected_registry_revision=2,
        confirm=True,
        reason="Project no longer uses this method.",
    )

    assert deactivated["status"] == "deactivated"
    assert deactivated["bindings"][0]["effective_status"] == "inactive"
    assert replay["status"] == "already_inactive"
    assert replay["registry_revision"] == 2
    restarted = ApplicationSkillBindingRegistry(store, now="2026-07-18T12:02:00+00:00")
    assert restarted.effective_bindings(
        catalog, project_id="project-alpha", consumer="answer.model-request"
    ) == ()
    reactivated = _activate(restarted, package)
    assert reactivated["status"] == "rebound"
    assert reactivated["bindings"][0]["binding_revision"] == 3


def test_stale_preview_and_registry_revision_fail_closed(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    first = _catalog_after_write(source, "first-method")
    second = _catalog_after_write(source, "second-method")
    registry, _ = _registry(tmp_path)
    stale = registry.preview_bind(
        second,
        project_id="project-beta",
        allowed_consumers=("document.generate",),
    )
    _activate(registry, first)

    with pytest.raises(ApplicationSkillBindingConflict, match="revision conflict"):
        registry.activate(
            second,
            project_id="project-beta",
            allowed_consumers=("document.generate",),
            priority=500,
            trigger_terms=(),
            expected_registry_revision=stale["registry_revision"],
            preview_token=stale["preview_token"],
            confirm=True,
            reason="Use the document method.",
        )

    fresh = registry.preview_bind(
        second,
        project_id="project-beta",
        allowed_consumers=("document.generate",),
    )
    with pytest.raises(ApplicationSkillBindingConflict, match="preview drifted"):
        registry.activate(
            second,
            project_id="project-beta",
            allowed_consumers=("document.generate",),
            priority=500,
            trigger_terms=(),
            expected_registry_revision=fresh["registry_revision"],
            preview_token="0" * 64,
            confirm=True,
            reason="Use the document method.",
        )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"project_id": "../other"}, "project id"),
        ({"consumers": ("unknown.consumer",)}, "unsupported"),
        ({"priority": 1001}, "priority"),
        ({"terms": ("api_key=abcdefghijklmnop",)}, "sensitive"),
    ],
)
def test_invalid_binding_configuration_fails_closed(
    tmp_path: Path, kwargs: dict[str, object], message: str
) -> None:
    source = tmp_path / "skills"
    package = _catalog_after_write(source, "safe-method")
    registry, _ = _registry(tmp_path)
    values = {
        "project_id": "project-alpha",
        "consumers": ("answer.model-request",),
        "priority": 500,
        "terms": ("safe",),
    }
    values.update(kwargs)

    with pytest.raises(ApplicationSkillBindingError, match=message):
        registry.preview_bind(
            package,
            project_id=values["project_id"],
            allowed_consumers=values["consumers"],
            priority=values["priority"],
            trigger_terms=values["terms"],
        )


def test_confirmation_and_sensitive_reason_are_required(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    package = _catalog_after_write(source, "safe-method")
    registry, _ = _registry(tmp_path)
    preview = registry.preview_bind(
        package,
        project_id="project-alpha",
        allowed_consumers=("answer.model-request",),
    )

    with pytest.raises(ApplicationSkillBindingError, match="explicit confirmation"):
        registry.activate(
            package,
            project_id="project-alpha",
            allowed_consumers=("answer.model-request",),
            priority=500,
            trigger_terms=(),
            expected_registry_revision=0,
            preview_token=preview["preview_token"],
            confirm=False,
            reason="Reviewed.",
        )
    with pytest.raises(ApplicationSkillBindingError, match="sensitive"):
        registry.activate(
            package,
            project_id="project-alpha",
            allowed_consumers=("answer.model-request",),
            priority=500,
            trigger_terms=(),
            expected_registry_revision=0,
            preview_token=preview["preview_token"],
            confirm=True,
            reason="authorization: bearer secret-value",
        )


def test_tampered_registry_is_rejected_on_restart(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    package = _catalog_after_write(source, "tamper-method")
    registry, store = _registry(tmp_path)
    _activate(registry, package)
    raw = dict(store.read(registry.collection, registry.registry_id) or {})
    raw["bindings"] = [{**raw["bindings"][0], "project_id": "other-project"}]
    store.write(
        registry.collection,
        registry.registry_id,
        raw,
        expected_revision=store.revision(registry.collection, registry.registry_id),
    )

    restarted = ApplicationSkillBindingRegistry(store, now=NOW)
    with pytest.raises(ApplicationSkillBindingError, match="identity drifted"):
        restarted.status(_catalog(source))


def test_tampered_registry_cannot_create_unbounded_binding_set(tmp_path: Path) -> None:
    registry, store = _registry(tmp_path)
    raw = {
        "schema_version": "1.0.0",
        "registry_revision": 1,
        "bindings": [{} for _ in range(257)],
        "history": [],
        "updated_at": NOW,
    }
    store.write(registry.collection, registry.registry_id, raw, expected_revision=0)

    with pytest.raises(ApplicationSkillBindingError, match="bindings are unbounded"):
        registry.status()


def _activation_request(package, *, project_id: str = "project-alpha", priority: int = 500) -> dict[str, object]:
    return {
        "package": package,
        "project_id": project_id,
        "allowed_consumers": ("answer.model-request",),
        "priority": priority,
        "trigger_terms": (package.skill_id,),
    }


def test_activate_batch_is_one_cas_write_and_preserves_per_binding_history(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    first = _catalog_after_write(source, "batch-first")
    second = _catalog_after_write(source, "batch-second")
    registry, store = _registry(tmp_path)
    requests = [_activation_request(first, priority=800), _activation_request(second, priority=600)]

    preview = registry.preview_bind_batch(requests)
    result = registry.activate_batch(
        requests,
        expected_registry_revision=preview["registry_revision"],
        preview_token=preview["preview_token"],
        confirm=True,
        reason="Bind the reviewed batch.",
    )

    assert result["actions"] == ["activated", "activated"]
    assert result["registry_revision"] == 2
    assert [event["registry_revision"] for event in result["history"]] == [1, 2]
    assert store.revision(registry.collection, registry.registry_id) == 1


def test_activate_batch_validates_all_requests_before_any_write(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    package = _catalog_after_write(source, "valid-batch-skill")
    invalid_package = _catalog_after_write(source, "invalid-batch-skill")
    registry, store = _registry(tmp_path)
    invalid = _activation_request(invalid_package)
    invalid["allowed_consumers"] = ("unsupported.consumer",)

    with pytest.raises(ApplicationSkillBindingError, match="unsupported"):
        registry.preview_bind_batch([_activation_request(package), invalid])

    assert store.revision(registry.collection, registry.registry_id) == 0
    assert registry.status()["bindings"] == []


def test_activate_batch_rebinds_and_replays_without_partial_changes(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    changing_root = _write_skill(source, "batch-changing")
    old_package = _catalog(source).get("batch-changing")
    assert old_package is not None
    stable = _catalog_after_write(source, "batch-stable")
    registry, store = _registry(tmp_path)
    _activate(registry, old_package, terms=(old_package.skill_id,))
    _activate(registry, stable, terms=(stable.skill_id,))
    (changing_root / "SKILL.md").write_text(
        "---\nname: batch-changing\ndescription: Changed reviewed method.\n---\n\n# Changed\n",
        encoding="utf-8",
    )
    changed = _catalog(source).get("batch-changing")
    assert changed is not None
    requests = [_activation_request(changed), _activation_request(stable)]
    preview = registry.preview_bind_batch(requests)

    result = registry.activate_batch(
        requests,
        expected_registry_revision=preview["registry_revision"],
        preview_token=preview["preview_token"],
        confirm=True,
        reason="Refresh the reviewed batch.",
    )

    assert result["actions"] == ["rebound", "already_active"]
    assert result["registry_revision"] == 3
    assert store.revision(registry.collection, registry.registry_id) == 3
    replay = registry.preview_bind_batch(requests)
    replayed = registry.activate_batch(
        requests,
        expected_registry_revision=replay["registry_revision"],
        preview_token=replay["preview_token"],
        confirm=True,
        reason="Refresh the reviewed batch.",
    )
    assert replayed["actions"] == ["already_active", "already_active"]
    assert replayed["replayed"] is True
    assert store.revision(registry.collection, registry.registry_id) == 3


def test_deactivate_batch_requires_matching_fingerprints_and_is_atomic(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    first = _catalog_after_write(source, "deactivate-first")
    second = _catalog_after_write(source, "deactivate-second")
    registry, store = _registry(tmp_path)
    _activate(registry, first)
    _activate(registry, second)
    requests = [
        {"project_id": "project-alpha", "skill_id": first.skill_id, "skill_fingerprint": first.fingerprint},
        {"project_id": "project-alpha", "skill_id": second.skill_id, "skill_fingerprint": "0" * 64},
    ]

    with pytest.raises(ApplicationSkillBindingConflict, match="fingerprint conflict"):
        registry.deactivate_batch(
            requests, expected_registry_revision=2, confirm=True, reason="Retire reviewed methods."
        )

    assert [binding["status"] for binding in registry.status()["bindings"]] == ["active", "active"]
    assert store.revision(registry.collection, registry.registry_id) == 2
    result = registry.deactivate_batch(
        [
            {"project_id": "project-alpha", "skill_id": first.skill_id, "skill_fingerprint": first.fingerprint},
            {"project_id": "project-alpha", "skill_id": second.skill_id, "skill_fingerprint": second.fingerprint},
        ],
        expected_registry_revision=2,
        confirm=True,
        reason="Retire reviewed methods.",
    )
    assert result["actions"] == ["deactivated", "deactivated"]
    assert result["registry_revision"] == 4
    assert store.revision(registry.collection, registry.registry_id) == 3


def test_stale_activate_batch_cas_leaves_entire_batch_unwritten(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    existing = _catalog_after_write(source, "existing-skill")
    first = _catalog_after_write(source, "stale-first")
    second = _catalog_after_write(source, "stale-second")
    registry, _ = _registry(tmp_path)
    requests = [_activation_request(first), _activation_request(second)]
    preview = registry.preview_bind_batch(requests)
    _activate(registry, existing)

    with pytest.raises(ApplicationSkillBindingConflict, match="revision conflict"):
        registry.activate_batch(
            requests,
            expected_registry_revision=preview["registry_revision"],
            preview_token=preview["preview_token"],
            confirm=True,
            reason="Bind stale batch.",
        )

    assert [binding["skill_id"] for binding in registry.status()["bindings"]] == ["existing-skill"]


def test_legacy_binding_without_provenance_remains_readable_as_unknown(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    package = _catalog_after_write(source, "legacy-source")
    registry, store = _registry(tmp_path)
    _activate(registry, package)
    raw = store.read(registry.collection, registry.registry_id)
    assert raw is not None

    def legacy_binding(binding: dict[str, object]) -> dict[str, object]:
        return {
            key: value for key, value in binding.items()
            if key not in {"source_id", "source_kind"}
        }

    legacy = {
        **raw,
        "bindings": [legacy_binding(dict(item)) for item in raw["bindings"]],
        "history": [
            {
                **dict(event),
                "before": [legacy_binding(dict(item)) for item in event["before"]],
                "after": [legacy_binding(dict(item)) for item in event["after"]],
            }
            for event in raw["history"]
        ],
    }
    store.write(
        registry.collection, registry.registry_id, legacy,
        expected_revision=store.revision(registry.collection, registry.registry_id),
    )

    restarted = ApplicationSkillBindingRegistry(store, now=NOW)
    status = restarted.status(_catalog(source))
    assert status["registry_revision"] == 1
    assert status["bindings"][0]["source_id"] == "unknown"
    assert status["bindings"][0]["source_kind"] == "unknown"
    assert status["history"][0]["after"][0]["source_kind"] == "unknown"
    assert status["bindings"][0]["effective_status"] == "legacy_provenance_untrusted"
    assert restarted.effective_bindings(
        _catalog(source), project_id="project-alpha", consumer="answer.model-request"
    ) == ()

    external = replace(package, source_id="external-legacy-source", source_kind="external")
    external_catalog = ApplicationSkillCatalogSnapshot(
        packages=(external,), issues=(), scanned_source_count=1,
    )
    external_status = restarted.status(external_catalog)
    assert external_status["bindings"][0]["effective_status"] == "legacy_provenance_untrusted"
    assert restarted.effective_bindings(
        external_catalog,
        project_id="project-alpha",
        consumer="answer.model-request",
    ) == ()


@pytest.mark.parametrize(
    ("source_id", "source_kind"),
    (
        ("other-user-source", "user"),
        ("plugin-source", "plugin"),
        ("bundled-source", "bundled"),
        ("external-source", "external"),
    ),
)
def test_legacy_binding_rejects_all_same_bytes_source_replacements(
    tmp_path: Path,
    source_id: str,
    source_kind: str,
) -> None:
    source = tmp_path / "skills"
    package = _catalog_after_write(source, "legacy-replacement")
    registry, store = _registry(tmp_path)
    _activate(registry, package)
    raw = store.read(registry.collection, registry.registry_id)
    assert raw is not None
    legacy = {
        **raw,
        "bindings": [
            {key: value for key, value in item.items() if key not in {"source_id", "source_kind"}}
            for item in raw["bindings"]
        ],
        "history": [
            {
                **event,
                "before": [
                    {key: value for key, value in item.items() if key not in {"source_id", "source_kind"}}
                    for item in event["before"]
                ],
                "after": [
                    {key: value for key, value in item.items() if key not in {"source_id", "source_kind"}}
                    for item in event["after"]
                ],
            }
            for event in raw["history"]
        ],
    }
    store.write(
        registry.collection, registry.registry_id, legacy,
        expected_revision=store.revision(registry.collection, registry.registry_id),
    )
    replacement = replace(package, source_id=source_id, source_kind=source_kind)
    replacement_catalog = ApplicationSkillCatalogSnapshot(
        packages=(replacement,), issues=(), scanned_source_count=1,
    )

    restarted = ApplicationSkillBindingRegistry(store, now=NOW)

    assert restarted.status(replacement_catalog)["bindings"][0]["effective_status"] == (
        "legacy_provenance_untrusted"
    )
    assert restarted.effective_bindings(
        replacement_catalog, project_id="project-alpha", consumer="answer.model-request"
    ) == ()


def test_same_bytes_from_a_different_source_are_not_an_effective_binding(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    package = _catalog_after_write(source, "source-isolated")
    registry, _ = _registry(tmp_path)
    _activate(registry, package)
    other_source_package = replace(package, source_id="other-source", source_kind="external")
    other_source_catalog = ApplicationSkillCatalogSnapshot(
        packages=(other_source_package,), issues=(), scanned_source_count=1,
    )

    status = registry.status(other_source_catalog)

    assert status["bindings"][0]["effective_status"] == "source_drifted"
    assert registry.effective_bindings(
        other_source_catalog, project_id="project-alpha", consumer="answer.model-request"
    ) == ()


def _catalog_after_write(source: Path, skill_id: str):
    _write_skill(source, skill_id)
    catalog = _catalog(source)
    package = catalog.get(skill_id)
    assert package is not None
    return package
