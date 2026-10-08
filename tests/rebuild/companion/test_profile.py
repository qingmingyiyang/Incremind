from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.companion_core import (
    CompanionConflict,
    CompanionIntegrityError,
    CompanionProfileService,
    CompanionRepository,
    CompanionRepositoryError,
    PromptActivationChange,
    PromptSnapshot,
    validate_profile_input,
    validate_prompt_content,
)


NOW = "2026-07-19T12:00:00+00:00"


class FakePromptAuthority:
    def __init__(self, *, compensate_fails: bool = False) -> None:
        self.snapshots = [PromptSnapshot("pt-companion-character", "初始人设", 1, 1)]
        self.compensate_fails = compensate_fails
        self.compensations = 0

    def current(self) -> PromptSnapshot:
        return self.snapshots[-1]

    def activate(self, *, content: str, expected_activation_revision: int) -> PromptActivationChange:
        before = self.current()
        if before.activation_revision != expected_activation_revision:
            raise CompanionConflict("prompt activation revision conflict")
        after = PromptSnapshot(
            "pt-companion-character",
            content,
            before.activation_revision + 1,
            before.unit_revision + 1,
        )
        self.snapshots.append(after)
        return PromptActivationChange(before, after)

    def restore_previous(self, *, expected_activation_revision: int) -> PromptActivationChange:
        before = self.current()
        if before.activation_revision != expected_activation_revision:
            raise CompanionConflict("prompt activation revision conflict")
        if len(self.snapshots) < 2:
            raise CompanionRepositoryError("no previous prompt")
        prior = self.snapshots[-2]
        after = PromptSnapshot(
            "pt-companion-character",
            prior.content,
            before.activation_revision + 1,
            before.unit_revision + 1,
        )
        self.snapshots.append(after)
        return PromptActivationChange(before, after)

    def compensate(self, change: PromptActivationChange) -> None:
        self.compensations += 1
        if self.compensate_fails:
            raise RuntimeError("compensation unavailable")
        if self.current() != change.after:
            raise RuntimeError("activation drift")
        self.snapshots.pop()


def repository(tmp_path: Path) -> CompanionRepository:
    repo = CompanionRepository(
        tmp_path / "companion.sqlite3",
        now=lambda: datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc),
    )
    repo.initialize()
    return repo


def create_open_session(repo: CompanionRepository, session_id: str = "session-one") -> None:
    repo.create_session(
        session_id=session_id,
        context_epoch=1,
        prompt_revision=1,
        profile_revision=1,
        started_at=NOW,
    )


def session_facts(repo: CompanionRepository, session_id: str = "session-one") -> tuple[int, int, int, int]:
    connection = sqlite3.connect(repo.database_path)
    try:
        row = connection.execute(
            "SELECT context_epoch, prompt_revision, profile_revision, revision FROM companion_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return tuple(int(value) for value in row)


def test_unicode_profile_saves_with_cas_and_rebases_open_sessions(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    create_open_session(repo)
    service = CompanionProfileService(repo)

    mutation = service.save_profile(
        expected_revision=0,
        nickname="  帝權  ",
        birthday="02-29",
        oc_address="御主",
        relationship="重要的长期协作者",
        custom_notes="喜欢繁體字與 emoji 🐻",
        updated_at=NOW,
    )

    assert mutation.sessions_rebased == 1
    assert mutation.profile.nickname == "帝權"
    assert mutation.profile.birthday == "02-29"
    assert service.get_profile() == mutation.profile
    assert session_facts(repo) == (2, 1, 1, 2)

    with pytest.raises(CompanionConflict, match="expected revision 0, found 1"):
        service.save_profile(
            expected_revision=0,
            nickname="不会覆盖",
            birthday=None,
            oc_address="御主",
            relationship="关系",
            custom_notes="",
            updated_at=NOW,
        )
    assert service.get_profile() == mutation.profile


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("nickname", " ", "required"),
        ("nickname", "a\x00b", "control"),
        ("nickname", "a\nb", "single line"),
        ("birthday", "2-03", "MM-DD"),
        ("birthday", "02-30", "calendar"),
        ("oc_address", "x" * 121, "too long"),
        ("relationship", "x" * 1001, "too long"),
        ("custom_notes", "x" * 2001, "too long"),
    ],
)
def test_profile_validation_rejects_invalid_input(field: str, value: str, message: str) -> None:
    payload = {
        "nickname": "帝权",
        "birthday": "07-19",
        "oc_address": "御主",
        "relationship": "搭档",
        "custom_notes": "",
    }
    payload[field] = value
    with pytest.raises(CompanionRepositoryError, match=message):
        validate_profile_input(**payload)


def test_profile_export_shape_is_separate_from_persona_and_prompt(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    profile = CompanionProfileService(repo).save_profile(
        expected_revision=0,
        nickname="帝权",
        birthday=None,
        oc_address="御主",
        relationship="搭档",
        custom_notes="只属于御主档案",
        updated_at=NOW,
    ).profile
    assert set(profile.__dataclass_fields__) == {
        "profile_id", "nickname", "birthday", "oc_address", "relationship", "custom_notes", "revision", "updated_at"
    }
    assert "persona" not in profile.__dataclass_fields__
    assert "prompt" not in profile.__dataclass_fields__


def test_prompt_activation_updates_binding_and_context_epoch_without_deleting_history(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    create_open_session(repo)
    repo.append_message(
        message_id="message-old",
        request_id="request-old",
        session_id="session-one",
        context_epoch=1,
        role="user",
        status="completed",
        content="旧设定下的消息",
        created_at=NOW,
        provider_mode="local",
    )
    authority = FakePromptAuthority()
    service = CompanionProfileService(repo, prompt_authority=authority)

    result = service.save_character_prompt(
        content="  新的人设\n请称呼我为御主。  ",
        expected_binding_revision=0,
        expected_activation_revision=1,
        updated_at=NOW,
    )

    assert result.snapshot.content == "新的人设\n请称呼我为御主。"
    assert result.binding.binding.activation_revision == 2
    assert result.binding.sessions_rebased == 1
    assert repo.get_prompt_binding() == result.binding.binding
    assert session_facts(repo) == (2, 2, 1, 2)
    page = repo.list_messages(session_id="session-one")
    assert [message.content for message in page.items] == ["旧设定下的消息"]
    assert page.items[0].context_epoch == 1


def test_binding_conflict_compensates_prompt_activation_without_leaking_content(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    authority = FakePromptAuthority()
    service = CompanionProfileService(repo, prompt_authority=authority)
    first = service.save_character_prompt(
        content="第一版",
        expected_binding_revision=0,
        expected_activation_revision=1,
        updated_at=NOW,
    )
    canary = "PRIVATE-PROMPT-CANARY"

    with pytest.raises(CompanionConflict) as captured:
        service.save_character_prompt(
            content=canary,
            expected_binding_revision=0,
            expected_activation_revision=2,
            updated_at=NOW,
        )

    assert canary not in str(captured.value)
    assert authority.compensations == 1
    assert authority.current() == first.snapshot
    assert repo.get_prompt_binding() == first.binding.binding


def test_compensation_failure_is_explicit_and_does_not_echo_prompt(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    authority = FakePromptAuthority(compensate_fails=True)
    service = CompanionProfileService(repo, prompt_authority=authority)
    service.save_character_prompt(
        content="第一版",
        expected_binding_revision=0,
        expected_activation_revision=1,
        updated_at=NOW,
    )
    canary = "SECRET-ROLLBACK-CANARY"
    with pytest.raises(CompanionIntegrityError, match="compensation failed") as captured:
        service.save_character_prompt(
            content=canary,
            expected_binding_revision=0,
            expected_activation_revision=2,
            updated_at=NOW,
        )
    assert canary not in str(captured.value)


def test_restore_previous_creates_new_activation_and_rebases_again(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    create_open_session(repo)
    authority = FakePromptAuthority()
    service = CompanionProfileService(repo, prompt_authority=authority)
    first = service.save_character_prompt(
        content="温柔版本",
        expected_binding_revision=0,
        expected_activation_revision=1,
        updated_at=NOW,
    )
    second = service.save_character_prompt(
        content="严肃版本",
        expected_binding_revision=1,
        expected_activation_revision=2,
        updated_at=NOW,
    )
    restored = service.restore_previous_prompt(
        expected_binding_revision=2,
        expected_activation_revision=3,
        updated_at=NOW,
    )
    assert first.snapshot.content == "温柔版本"
    assert second.snapshot.content == "严肃版本"
    assert restored.snapshot.content == "温柔版本"
    assert restored.snapshot.activation_revision == 4
    assert restored.binding.binding.revision == 3
    assert session_facts(repo) == (4, 4, 1, 4)


@pytest.mark.parametrize("content", ["", " \n ", "x\x00y", "x" * 12_001])
def test_prompt_validation_rejects_blank_control_and_overlong_content(content: str) -> None:
    with pytest.raises(CompanionRepositoryError):
        validate_prompt_content(content)


def test_prompt_service_fails_closed_without_authority(tmp_path: Path) -> None:
    service = CompanionProfileService(repository(tmp_path))
    with pytest.raises(CompanionRepositoryError, match="not configured"):
        service.save_character_prompt(
            content="人设",
            expected_binding_revision=0,
            expected_activation_revision=1,
            updated_at=NOW,
        )
