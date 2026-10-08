from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from .errors import CompanionIntegrityError, CompanionRepositoryError
from .models import (
    CompanionMasterProfile,
    CompanionProfileMutation,
    CompanionPromptBindingMutation,
)
from .repository import CompanionRepository


_BIRTHDAY = re.compile(r"^(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$")
_PROMPT_MAX_LENGTH = 12_000


@dataclass(frozen=True, slots=True)
class PromptSnapshot:
    prompt_id: str
    content: str
    activation_revision: int
    unit_revision: int


@dataclass(frozen=True, slots=True)
class PromptActivationChange:
    before: PromptSnapshot | None
    after: PromptSnapshot


class CompanionPromptAuthority(Protocol):
    def current(self) -> PromptSnapshot | None: ...

    def activate(self, *, content: str, expected_activation_revision: int) -> PromptActivationChange: ...

    def restore_previous(self, *, expected_activation_revision: int) -> PromptActivationChange: ...

    def compensate(self, change: PromptActivationChange) -> None: ...


@dataclass(frozen=True, slots=True)
class CharacterPromptMutation:
    snapshot: PromptSnapshot
    binding: CompanionPromptBindingMutation


class CompanionProfileService:
    def __init__(
        self,
        repository: CompanionRepository,
        *,
        prompt_authority: CompanionPromptAuthority | None = None,
    ) -> None:
        self.repository = repository
        self.prompt_authority = prompt_authority

    def get_profile(self) -> CompanionMasterProfile | None:
        return self.repository.get_master_profile()

    def save_profile(
        self,
        *,
        expected_revision: int,
        nickname: str,
        birthday: str | None,
        oc_address: str,
        relationship: str,
        custom_notes: str,
        updated_at: str,
    ) -> CompanionProfileMutation:
        normalized = validate_profile_input(
            nickname=nickname,
            birthday=birthday,
            oc_address=oc_address,
            relationship=relationship,
            custom_notes=custom_notes,
        )
        return self.repository.save_master_profile(
            expected_revision=expected_revision,
            updated_at=updated_at,
            **normalized,
        )

    def get_character_prompt(self) -> PromptSnapshot | None:
        return self._authority().current()

    def save_character_prompt(
        self,
        *,
        content: str,
        expected_binding_revision: int,
        expected_activation_revision: int,
        updated_at: str,
    ) -> CharacterPromptMutation:
        safe_content = validate_prompt_content(content)
        authority = self._authority()
        change = authority.activate(
            content=safe_content,
            expected_activation_revision=expected_activation_revision,
        )
        return self._commit_prompt_change(
            authority=authority,
            change=change,
            expected_binding_revision=expected_binding_revision,
            updated_at=updated_at,
        )

    def restore_previous_prompt(
        self,
        *,
        expected_binding_revision: int,
        expected_activation_revision: int,
        updated_at: str,
    ) -> CharacterPromptMutation:
        authority = self._authority()
        change = authority.restore_previous(
            expected_activation_revision=expected_activation_revision,
        )
        validate_prompt_content(change.after.content)
        return self._commit_prompt_change(
            authority=authority,
            change=change,
            expected_binding_revision=expected_binding_revision,
            updated_at=updated_at,
        )

    def _commit_prompt_change(
        self,
        *,
        authority: CompanionPromptAuthority,
        change: PromptActivationChange,
        expected_binding_revision: int,
        updated_at: str,
    ) -> CharacterPromptMutation:
        _validate_snapshot(change.after)
        try:
            binding = self.repository.apply_prompt_binding(
                expected_revision=expected_binding_revision,
                active_prompt_id=change.after.prompt_id,
                activation_revision=change.after.activation_revision,
                unit_revision=change.after.unit_revision,
                updated_at=updated_at,
            )
        except Exception:
            try:
                authority.compensate(change)
            except Exception as compensation_error:
                raise CompanionIntegrityError("prompt activation compensation failed") from compensation_error
            raise
        return CharacterPromptMutation(snapshot=change.after, binding=binding)

    def _authority(self) -> CompanionPromptAuthority:
        if self.prompt_authority is None:
            raise CompanionRepositoryError("companion prompt authority is not configured")
        return self.prompt_authority


def validate_profile_input(
    *,
    nickname: str,
    birthday: str | None,
    oc_address: str,
    relationship: str,
    custom_notes: str,
) -> dict[str, str | None]:
    normalized_birthday = birthday.strip() if isinstance(birthday, str) else birthday
    values = {
        "nickname": _normalize_text("nickname", nickname, maximum=120, required=True, multiline=False),
        "oc_address": _normalize_text("oc_address", oc_address, maximum=120, required=True, multiline=False),
        "relationship": _normalize_text("relationship", relationship, maximum=1_000, required=True, multiline=True),
        "custom_notes": _normalize_text("custom_notes", custom_notes, maximum=2_000, required=False, multiline=True),
    }
    if normalized_birthday == "":
        normalized_birthday = None
    if normalized_birthday is not None:
        if not isinstance(normalized_birthday, str) or _BIRTHDAY.fullmatch(normalized_birthday) is None:
            raise CompanionRepositoryError("profile birthday must use MM-DD")
        month, day = (int(part) for part in normalized_birthday.split("-"))
        try:
            date(2000, month, day)
        except ValueError as exc:
            raise CompanionRepositoryError("profile birthday is not a calendar date") from exc
    return {**values, "birthday": normalized_birthday}


def validate_prompt_content(content: str) -> str:
    return _normalize_text(
        "character_prompt",
        content,
        maximum=_PROMPT_MAX_LENGTH,
        required=True,
        multiline=True,
    )


def _normalize_text(
    label: str,
    value: object,
    *,
    maximum: int,
    required: bool,
    multiline: bool,
) -> str:
    if not isinstance(value, str):
        raise CompanionRepositoryError(f"profile {label} must be text")
    normalized = unicodedata.normalize("NFC", value).strip()
    if required and not normalized:
        raise CompanionRepositoryError(f"profile {label} is required")
    if len(normalized) > maximum:
        raise CompanionRepositoryError(f"profile {label} is too long")
    for character in normalized:
        if not multiline and character in {"\n", "\r", "\t"}:
            raise CompanionRepositoryError(f"profile {label} must be a single line")
        if unicodedata.category(character) == "Cc" and character not in ({"\n", "\t"} if multiline else set()):
            raise CompanionRepositoryError(f"profile {label} contains a control character")
    return normalized


def _validate_snapshot(snapshot: PromptSnapshot) -> None:
    if not isinstance(snapshot.prompt_id, str) or not snapshot.prompt_id:
        raise CompanionIntegrityError("prompt authority returned an invalid prompt id")
    if not isinstance(snapshot.activation_revision, int) or snapshot.activation_revision < 1:
        raise CompanionIntegrityError("prompt authority returned an invalid activation revision")
    if not isinstance(snapshot.unit_revision, int) or snapshot.unit_revision < 1:
        raise CompanionIntegrityError("prompt authority returned an invalid unit revision")
