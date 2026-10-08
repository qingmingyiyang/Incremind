from __future__ import annotations

from pathlib import Path

import pytest

from core.product_core import (
    ObjectStorePersonaRepository,
    PersonaConflictError,
    PersonaError,
    PersonaExtractor,
    PersonaTemplateContext,
    RollbackPersonaRevision,
    UpdatePersonaConfirmation,
)
from core.storage_provider import JsonObjectStore


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _confirmed_atom(
    *,
    atom_id: str = "atom-alpha",
    project_id: str = "project-alpha",
    language_style: str | None = "克制、温柔、避免命令式",
    format_preferences: tuple[str, ...] = ("Markdown", "短段落"),
    avoidances: tuple[str, ...] = ("不要使用 emoji",),
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1.0.0",
        "id": atom_id,
        "layer": "atom",
        "project_id": project_id,
        "content": "已确认的 Atom 内容。",
        "source_refs": [{"source_id": "source-alpha", "locator": "char:0-80"}],
        "trust_status": "user_confirmed",
        "revision": 1,
        "created_at": "2026-07-04T09:00:00+08:00",
        "updated_at": "2026-07-04T09:00:00+08:00",
    }
    if language_style:
        payload["language_style"] = language_style
    if format_preferences:
        payload["format_preferences"] = list(format_preferences)
    if avoidances:
        payload["avoidances"] = list(avoidances)
    return payload


def _confirmed_scenario(
    *,
    scenario_id: str = "scenario-alpha",
    project_id: str = "project-beta",
) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "id": scenario_id,
        "layer": "scenario",
        "project_id": project_id,
        "content": "已确认的场景记忆。",
        "source_refs": [{"source_id": "source-beta", "locator": "char:0-100"}],
        "trust_status": "user_confirmed",
        "revision": 1,
        "created_at": "2026-07-04T09:30:00+08:00",
        "updated_at": "2026-07-04T09:30:00+08:00",
    }


def _publish_persona(
    repository: ObjectStorePersonaRepository,
    *,
    scope: str = "global",
    entries: list[dict[str, object]] | None = None,
) -> None:
    repository.save(
        PersonaExtractor().extract(
            scope=scope,
            confirmed_entries=entries or [_confirmed_atom()],
        )
    )
    repository.update_confirmation(
        scope,
        status="confirmed",
        actor="user",
        reason="test publication",
    )


# ---------------------------------------------------------------------------
# Repository tests
# ---------------------------------------------------------------------------


def test_repository_digest_returns_empty_when_no_persona(tmp_path: Path) -> None:
    repo = ObjectStorePersonaRepository(_store(tmp_path))
    digest = repo.digest("global")
    assert digest.ready is False
    assert digest.scope == "global"
    assert digest.revision == 0
    assert digest.language_style == ()
    assert digest.evidence_refs == ()


def test_repository_save_persists_persona_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    extractor = PersonaExtractor()
    record = extractor.extract(
        scope="global",
        confirmed_entries=[_confirmed_atom()],
    )
    repo = ObjectStorePersonaRepository(store)
    repo.save(record)
    assert repo.digest("global").ready is False
    assert repo.review_digest("global").ready is True
    repo.update_confirmation("global", status="confirmed", reason="用户确认")
    digest = repo.digest("global")
    assert digest.ready is True
    assert digest.scope == "global"
    assert digest.revision == 1
    assert "克制、温柔、避免命令式" in digest.language_style
    assert "Markdown" in digest.format_preferences
    assert "project-alpha" in digest.common_projects
    assert "不要使用 emoji" in digest.avoidances
    assert len(digest.evidence_refs) == 1
    assert digest.evidence_refs[0].object_id == "atom-alpha"


def test_repository_save_rejects_invalid_payload(tmp_path: Path) -> None:
    repo = ObjectStorePersonaRepository(_store(tmp_path))
    bad_record = {
        "schema_version": "1.0.0",
        "id": "persona-global",
        "scope": "global",
        "statements": [],  # empty
        "evidence_refs": [],
        "confirmation": {"required": True, "status": "pending", "actor": None, "reason": None},
        "revision": 1,
        "trust_status": "system_generated",
        "created_at": "2026-07-04T09:00:00+08:00",
        "updated_at": "2026-07-04T09:00:00+08:00",
    }
    fake_record = type(
        "FakeRecord",
        (),
        {
            "to_payload": lambda self: bad_record,
            "scope": "global",
            "revision": 1,
        },
    )()
    with pytest.raises(PersonaError, match="statement"):
        repo.save(fake_record)


def test_repository_get_returns_none_when_missing(tmp_path: Path) -> None:
    repo = ObjectStorePersonaRepository(_store(tmp_path))
    assert repo.get("global") is None
    assert repo.get("series") is None


def test_repository_list_scopes_returns_published_scopes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    extractor = PersonaExtractor()
    global_record = extractor.extract(
        scope="global",
        confirmed_entries=[_confirmed_atom()],
    )
    project_record = extractor.extract(
        scope="project",
        confirmed_entries=[_confirmed_scenario(project_id="project-gamma")],
    )
    repo = ObjectStorePersonaRepository(store)
    repo.save(global_record)
    repo.update_confirmation("global", status="confirmed", reason="用户确认")
    repo.save(project_record)
    repo.update_confirmation("project", status="confirmed", reason="用户确认")
    assert repo.list_scopes() == ("global", "project")


# ---------------------------------------------------------------------------
# Extractor tests
# ---------------------------------------------------------------------------


def test_extractor_rejects_unconfirmed_entries(tmp_path: Path) -> None:
    extractor = PersonaExtractor()
    unconfirmed = _confirmed_atom()
    unconfirmed["trust_status"] = "system_generated"
    with pytest.raises(PersonaError, match="user_confirmed"):
        extractor.extract(scope="global", confirmed_entries=[unconfirmed])


def test_extractor_rejects_empty_entries(tmp_path: Path) -> None:
    extractor = PersonaExtractor()
    with pytest.raises(PersonaError, match="confirmed entries"):
        extractor.extract(scope="global", confirmed_entries=[])


def test_extractor_rejects_invalid_scope(tmp_path: Path) -> None:
    extractor = PersonaExtractor()
    with pytest.raises(PersonaError, match="scope"):
        extractor.extract(scope="invalid", confirmed_entries=[_confirmed_atom()])


def test_extractor_derives_statements_from_multiple_entries(tmp_path: Path) -> None:
    extractor = PersonaExtractor()
    record = extractor.extract(
        scope="global",
        confirmed_entries=[
            _confirmed_atom(project_id="project-alpha"),
            _confirmed_scenario(project_id="project-beta"),
        ],
    )
    assert record.scope == "global"
    assert record.id == "persona-global"
    project_statements = [s for s in record.statements if s.category == "workflow"]
    assert len(project_statements) == 2
    assert any("project-alpha" in s.content for s in project_statements)
    assert any("project-beta" in s.content for s in project_statements)
    assert len(record.evidence_refs) == 2


def test_extractor_skips_entries_without_source_refs(tmp_path: Path) -> None:
    extractor = PersonaExtractor()
    entry_no_refs = _confirmed_atom()
    entry_no_refs["source_refs"] = []
    record = extractor.extract(
        scope="global",
        confirmed_entries=[entry_no_refs, _confirmed_scenario()],
    )
    # Only the scenario with source_refs contributes evidence.
    assert len(record.evidence_refs) == 1
    assert record.evidence_refs[0].object_id == "scenario-alpha"


def test_extractor_fails_when_no_statements_can_be_derived(tmp_path: Path) -> None:
    extractor = PersonaExtractor()
    # Entry with source_refs but no project_id, language_style, format_preferences, avoidances.
    bare_entry = {
        "schema_version": "1.0.0",
        "id": "atom-bare",
        "layer": "atom",
        "content": "已确认的 Atom。",
        "source_refs": [{"source_id": "source-alpha", "locator": "char:0-50"}],
        "trust_status": "user_confirmed",
        "revision": 1,
        "created_at": "2026-07-04T09:00:00+08:00",
        "updated_at": "2026-07-04T09:00:00+08:00",
    }
    with pytest.raises(PersonaError, match="could not derive"):
        extractor.extract(scope="global", confirmed_entries=[bare_entry])


# ---------------------------------------------------------------------------
# Template context tests
# ---------------------------------------------------------------------------


def test_template_context_returns_empty_prefix_when_no_persona(tmp_path: Path) -> None:
    repo = ObjectStorePersonaRepository(_store(tmp_path))
    context = PersonaTemplateContext(repo)
    assert context.render_template_prefix("global") == ""
    digest = context.persona_context_for("global")
    assert digest.ready is False


def test_template_context_renders_prefix_when_persona_published(tmp_path: Path) -> None:
    store = _store(tmp_path)
    extractor = PersonaExtractor()
    repo = ObjectStorePersonaRepository(store)
    _publish_persona(repo)
    context = PersonaTemplateContext(repo)
    prefix = context.render_template_prefix("global")
    assert "Persona 提示" in prefix
    assert "克制、温柔、避免命令式" in prefix
    assert "Markdown" in prefix
    assert "不要使用 emoji" in prefix


def test_template_context_digest_round_trips_through_payload(tmp_path: Path) -> None:
    store = _store(tmp_path)
    extractor = PersonaExtractor()
    repo = ObjectStorePersonaRepository(store)
    _publish_persona(repo)
    digest = repo.digest("global")
    payload = digest.to_payload()
    assert payload["ready"] is True
    assert payload["scope"] == "global"
    assert isinstance(payload["language_style"], list)
    assert isinstance(payload["evidence_refs"], list)
    assert payload["evidence_refs"][0]["object_id"] == "atom-alpha"


# ---------------------------------------------------------------------------
# Revision history tests (L4 Persona 数据闭环)
# ---------------------------------------------------------------------------


def test_save_pushes_old_record_to_revisions_and_bumps_revision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    extractor = PersonaExtractor()
    repo = ObjectStorePersonaRepository(store)
    _publish_persona(repo)
    # A second reviewed draft becomes current and archives the old confirmed current.
    _publish_persona(repo)
    digest = repo.digest("global")
    assert digest.revision == 2
    revisions = repo.list_revisions("global")
    assert len(revisions) == 1
    assert revisions[0]["revision"] == 1


def test_list_revisions_returns_newest_first(tmp_path: Path) -> None:
    store = _store(tmp_path)
    extractor = PersonaExtractor()
    repo = ObjectStorePersonaRepository(store)
    for _ in range(3):
        _publish_persona(repo)
    revisions = repo.list_revisions("global")
    assert [r["revision"] for r in revisions] == [2, 1]


def test_get_revision_returns_specific_historical_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    extractor = PersonaExtractor()
    repo = ObjectStorePersonaRepository(store)
    _publish_persona(repo)
    _publish_persona(repo)
    historical = repo.get_revision("global", 1)
    assert historical is not None
    assert historical["revision"] == 1
    assert repo.get_revision("global", 99) is None


def test_save_writes_transition_audit_log(tmp_path: Path) -> None:
    store = _store(tmp_path)
    extractor = PersonaExtractor()
    repo = ObjectStorePersonaRepository(store)
    repo.save(
        extractor.extract(scope="global", confirmed_entries=[_confirmed_atom()]),
        actor="user",
        reason="初始蒸馏",
        now="2026-07-04T09:00:00+08:00",
    )
    transitions = repo.list_transitions("global")
    assert len(transitions) == 1
    assert transitions[0]["transition_type"] == "draft_saved"
    assert transitions[0]["from_revision"] is None
    assert transitions[0]["to_revision"] == 1
    assert transitions[0]["actor"] == "user"
    assert transitions[0]["reason"] == "初始蒸馏"


# ---------------------------------------------------------------------------
# Confirmation tests (confirm / reject)
# ---------------------------------------------------------------------------


def test_update_confirmation_confirms_pending_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    extractor = PersonaExtractor()
    repo = ObjectStorePersonaRepository(store)
    repo.save(extractor.extract(scope="global", confirmed_entries=[_confirmed_atom()]))
    # 草稿初始为 pending
    assert repo.digest("global").ready is False
    assert repo.review_digest("global").confirmation["status"] == "pending"
    assert repo.review_digest("global").trust_status == "system_generated"

    use_case = UpdatePersonaConfirmation(
        repository=repo,
        now="2026-07-04T10:00:00+08:00",
    )
    digest = use_case.execute(
        scope="global",
        status="confirmed",
        actor="user",
        reason="用户确认 Persona 草稿",
    )
    assert digest.confirmation["status"] == "confirmed"
    assert digest.trust_status == "user_confirmed"
    assert digest.revision == 1
    # 首次确认没有旧的已确认 current，历史保持为空。
    revisions = repo.list_revisions("global")
    assert revisions == ()
    # transition 日志
    transitions = repo.list_transitions("global")
    confirm_transitions = [t for t in transitions if t["transition_type"] == "confirm"]
    assert len(confirm_transitions) == 1
    assert confirm_transitions[0]["from_revision"] is None
    assert confirm_transitions[0]["to_revision"] == 1


def test_update_confirmation_rejects_pending_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    extractor = PersonaExtractor()
    repo = ObjectStorePersonaRepository(store)
    repo.save(extractor.extract(scope="global", confirmed_entries=[_confirmed_atom()]))

    use_case = UpdatePersonaConfirmation(repository=repo)
    digest = use_case.execute(
        scope="global",
        status="rejected",
        reason="用户拒绝 Persona 草稿",
    )
    assert digest.ready is False
    assert repo.get_draft("global") is None
    assert repo.get("global") is None


def test_rejecting_new_draft_keeps_confirmed_current(tmp_path: Path) -> None:
    repo = ObjectStorePersonaRepository(_store(tmp_path))
    _publish_persona(repo)
    original = repo.digest("global")
    repo.save(
        PersonaExtractor().extract(
            scope="global",
            confirmed_entries=[_confirmed_atom(language_style="尚未确认的新风格")],
        )
    )

    rejected = UpdatePersonaConfirmation(repository=repo).execute(
        scope="global",
        status="rejected",
        reason="不采用这次变化",
    )

    assert rejected == original
    assert repo.get_draft("global") is None


def test_update_confirmation_rejects_non_pending_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    extractor = PersonaExtractor()
    repo = ObjectStorePersonaRepository(store)
    repo.save(extractor.extract(scope="global", confirmed_entries=[_confirmed_atom()]))
    use_case = UpdatePersonaConfirmation(repository=repo)
    use_case.execute(scope="global", status="confirmed", reason="首次确认")
    # 已确认 current 不是草稿，再次确认应失败。
    with pytest.raises(PersonaError, match="has not been distilled"):
        use_case.execute(scope="global", status="confirmed", reason="重复确认")


def test_update_confirmation_rejects_invalid_status(tmp_path: Path) -> None:
    store = _store(tmp_path)
    extractor = PersonaExtractor()
    repo = ObjectStorePersonaRepository(store)
    repo.save(extractor.extract(scope="global", confirmed_entries=[_confirmed_atom()]))
    use_case = UpdatePersonaConfirmation(repository=repo)
    with pytest.raises(PersonaError, match="confirmed or rejected"):
        use_case.execute(scope="global", status="pending", reason="无效状态")


def test_update_confirmation_rejects_missing_reason(tmp_path: Path) -> None:
    store = _store(tmp_path)
    extractor = PersonaExtractor()
    repo = ObjectStorePersonaRepository(store)
    repo.save(extractor.extract(scope="global", confirmed_entries=[_confirmed_atom()]))
    use_case = UpdatePersonaConfirmation(repository=repo)
    with pytest.raises(PersonaError, match="reason is required"):
        use_case.execute(scope="global", status="confirmed", reason="")


def test_update_confirmation_fails_when_no_persona_published(tmp_path: Path) -> None:
    repo = ObjectStorePersonaRepository(_store(tmp_path))
    use_case = UpdatePersonaConfirmation(repository=repo)
    with pytest.raises(PersonaError, match="has not been distilled"):
        use_case.execute(scope="global", status="confirmed", reason="无 Persona")


# ---------------------------------------------------------------------------
# Rollback tests
# ---------------------------------------------------------------------------


def test_rollback_restores_previous_revision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    extractor = PersonaExtractor()
    repo = ObjectStorePersonaRepository(store)
    # 第一次确认：revision 1
    _publish_persona(repo)
    first_revision = repo.get("global")["revision"]
    assert first_revision == 1
    # 第二次确认：revision 2
    _publish_persona(repo)
    assert repo.get("global")["revision"] == 2

    use_case = RollbackPersonaRevision(
        repository=repo,
        now="2026-07-04T11:00:00+08:00",
    )
    digest = use_case.execute(
        scope="global",
        reason="回滚到上一版本",
    )
    # 回滚后 revision 升为 3，但内容来自 revision 1
    assert digest.revision == 3
    # 历史 revision 中应同时存在 1（被回滚的目标已被推入）和 2（被回滚的当前）
    revisions = repo.list_revisions("global")
    revision_numbers = [r["revision"] for r in revisions]
    assert 2 in revision_numbers
    assert 1 in revision_numbers


def test_rollback_to_specific_revision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    extractor = PersonaExtractor()
    repo = ObjectStorePersonaRepository(store)
    _publish_persona(repo)
    _publish_persona(repo)
    _publish_persona(repo)
    assert repo.get("global")["revision"] == 3

    use_case = RollbackPersonaRevision(repository=repo)
    digest = use_case.execute(
        scope="global",
        to_revision=1,
        reason="回滚到 revision 1",
    )
    assert digest.revision == 4


def test_rollback_fails_when_no_history(tmp_path: Path) -> None:
    store = _store(tmp_path)
    extractor = PersonaExtractor()
    repo = ObjectStorePersonaRepository(store)
    _publish_persona(repo)
    use_case = RollbackPersonaRevision(repository=repo)
    with pytest.raises(PersonaError, match="no historical revisions"):
        use_case.execute(scope="global", reason="无历史可回滚")


def test_rollback_fails_when_target_revision_missing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    extractor = PersonaExtractor()
    repo = ObjectStorePersonaRepository(store)
    _publish_persona(repo)
    _publish_persona(repo)
    use_case = RollbackPersonaRevision(repository=repo)
    with pytest.raises(PersonaError, match="revision 99 not found"):
        use_case.execute(scope="global", to_revision=99, reason="无效目标")


def test_rollback_fails_when_no_persona_published(tmp_path: Path) -> None:
    repo = ObjectStorePersonaRepository(_store(tmp_path))
    use_case = RollbackPersonaRevision(repository=repo)
    with pytest.raises(PersonaError, match="has not been distilled"):
        use_case.execute(scope="global", reason="无 Persona")


def test_rollback_writes_transition_audit_log(tmp_path: Path) -> None:
    store = _store(tmp_path)
    extractor = PersonaExtractor()
    repo = ObjectStorePersonaRepository(store)
    _publish_persona(repo)
    _publish_persona(repo)
    use_case = RollbackPersonaRevision(repository=repo)
    use_case.execute(scope="global", reason="回滚测试")
    transitions = repo.list_transitions("global")
    rollback_transitions = [t for t in transitions if t["transition_type"] == "rollback"]
    assert len(rollback_transitions) == 1
    assert rollback_transitions[0]["from_revision"] == 2
    assert rollback_transitions[0]["to_revision"] == 3


def test_rollback_rejects_missing_reason(tmp_path: Path) -> None:
    store = _store(tmp_path)
    extractor = PersonaExtractor()
    repo = ObjectStorePersonaRepository(store)
    _publish_persona(repo)
    _publish_persona(repo)
    use_case = RollbackPersonaRevision(repository=repo)
    with pytest.raises(PersonaError, match="reason is required"):
        use_case.execute(scope="global", reason="")


def test_rollback_rejects_stale_current_cas_without_mutation(tmp_path: Path) -> None:
    repo = ObjectStorePersonaRepository(_store(tmp_path))
    _publish_persona(repo)
    _publish_persona(repo)
    before = repo.digest("global")

    with pytest.raises(PersonaConflictError, match="current revision conflicted"):
        RollbackPersonaRevision(repository=repo).execute(
            scope="global",
            reason="过期页面回滚",
            expected_draft_revision=0,
            expected_current_revision=999,
        )

    assert repo.digest("global") == before


# ---------------------------------------------------------------------------
# Confirm + Rollback interaction test
# ---------------------------------------------------------------------------


def test_confirm_then_rollback_never_restores_pending_state(tmp_path: Path) -> None:
    """回滚只使用已确认历史，不允许 pending 草稿重新进入 current。"""
    store = _store(tmp_path)
    extractor = PersonaExtractor()
    repo = ObjectStorePersonaRepository(store)
    _publish_persona(repo)
    _publish_persona(
        repo,
        entries=[_confirmed_atom(language_style="简洁、直接")],
    )
    rollback_use_case = RollbackPersonaRevision(repository=repo)
    rolled_back = rollback_use_case.execute(
        scope="global",
        reason="回滚确认操作",
    )
    assert rolled_back.confirmation["status"] == "confirmed"
    assert rolled_back.trust_status == "user_confirmed"
