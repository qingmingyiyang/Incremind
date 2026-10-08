from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from shutil import rmtree
from types import SimpleNamespace

import pytest

from core.application_skill import (
    ApplicationSkillBindingRegistry,
    ApplicationSkillCatalog,
    ApplicationSkillConsumerRuntime,
    ApplicationSkillConsumerRuntimeError,
    ApplicationSkillError,
    ApplicationSkillPackage,
    ApplicationSkillResolver,
    ApplicationSkillSource,
    ObjectStoreApplicationSkillTraceRepository,
)
from core.storage_provider import JsonObjectStore


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / "store", legacy_root=tmp_path / "legacy")


def _fallback_trace() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "resolution_id": "skill-resolution-" + ("a" * 32),
        "invocation_id": "model-request-alpha",
        "project_id": "project-alpha",
        "consumer": "answer.model-request",
        "task_kind": "project-answer",
        "task_fingerprint": "b" * 64,
        "matched": [],
        "selected": [],
        "budget_excluded_skill_ids": [],
        "loaded_instruction_bytes": 0,
        "context_size_bytes": 0,
        "fallback": "default_consumer_flow",
        "recorded_at": "2026-07-18T15:00:00+00:00",
    }


def test_trace_repository_is_idempotent_across_replay_timestamp(tmp_path: Path) -> None:
    repository = ObjectStoreApplicationSkillTraceRepository(_store(tmp_path))
    first = repository.save_trace(_fallback_trace())
    replay = {**_fallback_trace(), "recorded_at": "2026-07-18T15:01:00+00:00"}

    saved = repository.save_trace(replay)

    assert saved == first
    assert saved["recorded_at"] == "2026-07-18T15:00:00+00:00"
    assert repository.get_trace(str(saved["resolution_id"])) == first


def test_trace_repository_rejects_same_identity_with_different_evidence(tmp_path: Path) -> None:
    repository = ObjectStoreApplicationSkillTraceRepository(_store(tmp_path))
    repository.save_trace(_fallback_trace())
    drifted = {**_fallback_trace(), "task_kind": "document-generation"}

    with pytest.raises(ApplicationSkillConsumerRuntimeError, match="identity conflict"):
        repository.save_trace(drifted)


def test_trace_repository_rejects_body_fields_and_selection_drift(tmp_path: Path) -> None:
    repository = ObjectStoreApplicationSkillTraceRepository(_store(tmp_path))
    body = {**_fallback_trace(), "context_markdown": "private body"}
    selected = {
        **_fallback_trace(),
        "selected": [
            {
                "skill_id": "alpha-method",
                "skill_fingerprint": "c" * 64,
                "binding_id": "skill-binding-alpha",
                "binding_revision": 1,
                "score": 100,
                "priority": 500,
                "instruction_bytes": 10,
                "resource_count": 0,
            }
        ],
        "loaded_instruction_bytes": 10,
        "context_size_bytes": 100,
        "fallback": "none",
    }

    with pytest.raises(ApplicationSkillConsumerRuntimeError, match="trace schema"):
        repository.save_trace(body)
    with pytest.raises(ApplicationSkillConsumerRuntimeError, match="selected trace drifted"):
        repository.save_trace(selected)


def test_trace_repository_revalidates_tampered_storage_on_read(tmp_path: Path) -> None:
    store = _store(tmp_path)
    repository = ObjectStoreApplicationSkillTraceRepository(store)
    saved = repository.save_trace(_fallback_trace())
    tampered = {**saved, "fallback": "none"}
    store.write(
        repository.collection,
        str(saved["resolution_id"]),
        tampered,
        expected_revision=1,
    )

    with pytest.raises(ApplicationSkillConsumerRuntimeError, match="fallback"):
        repository.get_trace(str(saved["resolution_id"]))


def _verified_package(
    catalog: ApplicationSkillCatalog,
    tmp_path: Path,
    skill_id: str,
    *,
    marker: str,
):
    package_root = tmp_path / "staged" / skill_id
    package_root.mkdir(parents=True)
    skill = (
        "---\n"
        f"name: {skill_id}\n"
        f"description: Use {skill_id} for architecture review work.\n"
        "---\n\n"
        f"{marker}\n"
    )
    (package_root / "SKILL.md").write_text(skill, encoding="utf-8")
    return catalog.package_from_verified_content(
        {"SKILL.md": skill.encode("utf-8")},
        source_id="external-managed",
        package_root=package_root,
    )


def _activate_answer_binding(
    registry: ApplicationSkillBindingRegistry,
    package: ApplicationSkillPackage,
) -> None:
    preview = registry.preview_bind(
        package,
        project_id="project-alpha",
        allowed_consumers=("answer.model-request",),
        priority=700,
        trigger_terms=("architecture",),
    )
    registry.activate(
        package,
        project_id="project-alpha",
        allowed_consumers=("answer.model-request",),
        priority=700,
        trigger_terms=("architecture",),
        expected_registry_revision=int(preview["registry_revision"]),
        preview_token=str(preview["preview_token"]),
        confirm=True,
        reason="Bind the reviewed external method.",
    )


def _runtime(
    tmp_path: Path,
    *,
    sources: tuple[ApplicationSkillSource, ...] = (),
    external_sources=None,
    external_packages=None,
) -> tuple[ApplicationSkillConsumerRuntime, ApplicationSkillBindingRegistry]:
    store = _store(tmp_path)
    registry = ApplicationSkillBindingRegistry(store, now="2026-08-30T12:00:00+00:00")
    runtime = ApplicationSkillConsumerRuntime(
        catalog=ApplicationSkillCatalog(),
        sources=sources,
        resolver=ApplicationSkillResolver(
            registry,
            trace_store=ObjectStoreApplicationSkillTraceRepository(store),
            now="2026-08-30T12:00:00+00:00",
        ),
        external_sources=external_sources,
        external_packages=external_packages,
    )
    return runtime, registry


def _resolve(runtime: ApplicationSkillConsumerRuntime) -> dict[str, object]:
    return dict(runtime.resolve_context(
        project_id="project-alpha",
        consumer="answer.model-request",
        task_kind="project-answer",
        task_text="Please perform an architecture review.",
        invocation_id="model-request-alpha",
    ))


def test_external_verified_package_is_loaded_after_its_staging_path_is_deleted(
    tmp_path: Path,
) -> None:
    catalog = ApplicationSkillCatalog()
    package = _verified_package(
        catalog,
        tmp_path,
        "external-review",
        marker="EXTERNAL-VERIFIED-METHOD",
    )
    runtime, registry = _runtime(
        tmp_path,
        external_sources=lambda _: (_ for _ in ()).throw(AssertionError("legacy source must not run")),
        external_packages=lambda _: (package,),
    )
    _activate_answer_binding(registry, package)
    rmtree(tmp_path / "staged")

    resolved = _resolve(runtime)

    assert "EXTERNAL-VERIFIED-METHOD" in str(resolved["context_markdown"])
    assert [item["skill_id"] for item in resolved["selected"]] == ["external-review"]


def test_external_package_duplicate_with_filesystem_source_fails_closed(
    tmp_path: Path,
) -> None:
    source = tmp_path / "skills"
    package = _verified_package(
        ApplicationSkillCatalog(),
        tmp_path,
        "external-review",
        marker="EXTERNAL-METHOD",
    )
    filesystem = source / "external-review"
    filesystem.mkdir(parents=True)
    (filesystem / "SKILL.md").write_text(
        "---\n"
        "name: external-review\n"
        "description: Use external-review for architecture review work.\n"
        "---\n\n"
        "FILESYSTEM-METHOD\n",
        encoding="utf-8",
    )
    runtime, registry = _runtime(
        tmp_path,
        sources=(ApplicationSkillSource("user", source, "user"),),
        external_packages=lambda _: (package,),
    )
    _activate_answer_binding(registry, package)

    resolved = _resolve(runtime)

    assert resolved["selected"] == []
    assert resolved["fallback"] == "default_consumer_flow"
    assert "EXTERNAL-METHOD" not in str(resolved["context_markdown"])
    assert "FILESYSTEM-METHOD" not in str(resolved["context_markdown"])


def test_external_package_callback_contract_drift_is_rejected_before_resolution(
    tmp_path: Path,
) -> None:
    package = _verified_package(
        ApplicationSkillCatalog(),
        tmp_path,
        "external-review",
        marker="EXTERNAL-VERIFIED-METHOD",
    )
    runtime, _ = _runtime(
        tmp_path,
        external_packages=lambda _: (replace(package, fingerprint="0" * 64),),
    )

    with pytest.raises(ApplicationSkillError, match="contract drifted"):
        _resolve(runtime)


@pytest.mark.parametrize("kind", ("filesystem", "non-external", "non-package"))
def test_external_package_callback_rejects_nonimmutable_values_before_path_access(
    tmp_path: Path,
    kind: str,
) -> None:
    catalog = ApplicationSkillCatalog()
    verified = _verified_package(
        catalog,
        tmp_path,
        "external-review",
        marker="EXTERNAL-VERIFIED-METHOD",
    )
    if kind == "filesystem":
        candidate: object = catalog.inspect_package(verified.package_root, source_id="external-path")
    elif kind == "non-external":
        candidate = replace(verified, source_kind="user")
    else:
        candidate = object()
    rmtree(tmp_path / "staged")
    runtime, _ = _runtime(
        tmp_path,
        external_packages=lambda _: (candidate,),  # type: ignore[return-value]
    )

    with pytest.raises(ApplicationSkillConsumerRuntimeError, match="external Application Skill package"):
        _resolve(runtime)


def test_external_package_composition_retains_base_and_duplicate_issues(
    tmp_path: Path,
) -> None:
    source = tmp_path / "skills"
    source.mkdir()
    (source / "not-a-directory").write_text("invalid", encoding="utf-8")
    package = _verified_package(
        ApplicationSkillCatalog(),
        tmp_path,
        "external-review",
        marker="EXTERNAL-VERIFIED-METHOD",
    )
    filesystem = source / "external-review"
    filesystem.mkdir()
    (filesystem / "SKILL.md").write_text(
        "---\n"
        "name: external-review\n"
        "description: Use external-review for architecture review work.\n"
        "---\n\n"
        "FILESYSTEM-METHOD\n",
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    class _CapturingResolver:
        def resolve(self, snapshot, **_: object):
            captured["snapshot"] = snapshot
            return SimpleNamespace(
                resolution_id="resolution",
                project_id="project-alpha",
                consumer="answer.model-request",
                context_markdown="",
                selected=(),
                trace={"fallback": "default_consumer_flow"},
            )

    runtime = ApplicationSkillConsumerRuntime(
        catalog=ApplicationSkillCatalog(),
        sources=(ApplicationSkillSource("user", source, "user"),),
        resolver=_CapturingResolver(),  # type: ignore[arg-type]
        external_packages=lambda _: (package,),
    )

    _resolve(runtime)

    issues = captured["snapshot"].issues
    assert {(item.package_name, item.code) for item in issues} == {
        ("not-a-directory", "invalid_source_entry"),
        ("external-review", "duplicate_identity"),
    }


def test_legacy_external_source_fallback_remains_available_without_packages(
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy-external"
    package_root = source / "external-review"
    package_root.mkdir(parents=True)
    (package_root / "SKILL.md").write_text(
        "---\n"
        "name: external-review\n"
        "description: Use external-review for architecture review work.\n"
        "---\n\n"
        "LEGACY-EXTERNAL-SOURCE-METHOD\n",
        encoding="utf-8",
    )
    external_source = ApplicationSkillSource("external-managed", source, "external")
    catalog = ApplicationSkillCatalog()
    package = catalog.discover((external_source,)).get("external-review")
    assert package is not None
    runtime, registry = _runtime(
        tmp_path,
        external_sources=lambda _: (external_source,),
    )
    _activate_answer_binding(registry, package)

    resolved = _resolve(runtime)

    assert "LEGACY-EXTERNAL-SOURCE-METHOD" in str(resolved["context_markdown"])
