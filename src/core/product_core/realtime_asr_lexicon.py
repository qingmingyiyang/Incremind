"""Local authority for real-time ASR vocabulary.

The lexicon is deliberately small and boring: a term is usable by a speech
session only after a local manual write or an explicit acceptance of a pending
candidate.  Discovery is proposal-only and never changes the active set.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from .ports import ObjectStorePort


MAX_SESSION_TERMS = 2_000
MAX_SUPER_TERMS = 50
NORMAL_WEIGHT_MIN = 1
NORMAL_WEIGHT_MAX = 5
SUPER_WEIGHT = 50
MAX_NON_ASCII_TERM_LENGTH = 15
MAX_ASCII_TERM_SEGMENTS = 7

_STATE_COLLECTION = "realtime_asr_lexicon_state"
_TERM_COLLECTION = "realtime_asr_lexicon_terms"
_CANDIDATE_COLLECTION = "realtime_asr_lexicon_candidates"
_STATE_ID = "default"


class RealtimeAsrLexiconError(ValueError):
    """Raised when a local ASR lexicon transition is unsafe or invalid."""


@dataclass(frozen=True, slots=True)
class RealtimeAsrLexiconTerm:
    term_id: str
    term: str
    weight: int
    status: Literal["active", "retired"]
    origin: Literal["manual", "candidate"]
    revision: int

    @property
    def is_super(self) -> bool:
        return self.weight == SUPER_WEIGHT


@dataclass(frozen=True, slots=True)
class RealtimeAsrLexiconCandidate:
    candidate_id: str
    term: str
    suggested_weight: int
    status: Literal["pending_review", "accepted", "rejected"]
    revision: int


@dataclass(frozen=True, slots=True)
class RealtimeAsrLexiconSelection:
    revision: int
    terms: tuple[RealtimeAsrLexiconTerm, ...]


class RealtimeAsrLexicon:
    """Persist and select the user-controlled vocabulary for live ASR.

    Collection records are intentionally public metadata only; no audio,
    transcript or provider credentials belong here.  ``revision`` advances on
    every semantic mutation so a real-time session can pin a stable selection.
    """

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        now: Callable[[], str] | None = None,
    ) -> None:
        self._store = object_store
        self._now = now or _utc_now

    def current_revision(self) -> int:
        state = self._state()
        return _positive_int(state.get("revision"), "lexicon revision", allow_zero=True)

    def manual_upsert(
        self,
        term: str,
        *,
        weight: int = NORMAL_WEIGHT_MIN,
        is_super: bool = False,
    ) -> RealtimeAsrLexiconTerm:
        """Create or reactivate a manually controlled active term."""

        clean_term, normalized = _term(term)
        clean_weight = _weight(weight, is_super=is_super)
        existing = self._find_term(normalized)
        self._assert_super_capacity(
            new_is_super=clean_weight == SUPER_WEIGHT,
            replacing=existing,
        )
        revision = self._advance_revision()
        payload = {
            "schema_version": "1.0.0",
            "id": existing["id"] if existing else _term_id(),
            "term": clean_term,
            "normalized_term": normalized,
            "weight": clean_weight,
            "status": "active",
            "origin": "manual",
            "revision": revision,
            "created_at": existing.get("created_at") if existing else self._now(),
            "updated_at": self._now(),
        }
        self._store.write(_TERM_COLLECTION, str(payload["id"]), payload, expected_revision=None)
        return _term_record(payload)

    def retire(self, term: str) -> RealtimeAsrLexiconTerm:
        """Retire an active term; it will no longer appear in sessions."""

        _clean, normalized = _term(term)
        existing = self._find_term(normalized)
        if existing is None:
            raise RealtimeAsrLexiconError("lexicon term not found")
        if existing.get("status") == "retired":
            return _term_record(existing)
        revision = self._advance_revision()
        payload = dict(existing) | {"status": "retired", "revision": revision, "updated_at": self._now()}
        self._store.write(_TERM_COLLECTION, str(payload["id"]), payload, expected_revision=None)
        return _term_record(payload)

    def propose_candidate(
        self,
        term: str,
        *,
        suggested_weight: int = NORMAL_WEIGHT_MIN,
        source_ref: str | None = None,
    ) -> RealtimeAsrLexiconCandidate:
        """Record an automatic proposal without changing the active lexicon."""

        clean_term, normalized = _term(term)
        clean_weight = _weight(suggested_weight, is_super=False)
        ref = _optional_ref(source_ref)
        revision = self._advance_revision()
        payload = {
            "schema_version": "1.0.0",
            "id": f"rt-asr-candidate-{uuid4().hex}",
            "term": clean_term,
            "normalized_term": normalized,
            "suggested_weight": clean_weight,
            "status": "pending_review",
            "source_ref": ref,
            "revision": revision,
            "created_at": self._now(),
            "updated_at": self._now(),
        }
        self._store.write(_CANDIDATE_COLLECTION, str(payload["id"]), payload, expected_revision=None)
        return _candidate_record(payload)

    def accept_candidate(
        self,
        candidate_id: str,
        *,
        weight: int | None = None,
        is_super: bool = False,
    ) -> RealtimeAsrLexiconTerm:
        """Accept a pending proposal and make its term active exactly once."""

        candidate = self._pending_candidate(candidate_id)
        clean_weight = _weight(
            candidate["suggested_weight"] if weight is None else weight,
            is_super=is_super,
        )
        existing = self._find_term(str(candidate["normalized_term"]))
        self._assert_super_capacity(
            new_is_super=clean_weight == SUPER_WEIGHT,
            replacing=existing,
        )
        revision = self._advance_revision()
        term_payload = {
            "schema_version": "1.0.0",
            "id": existing["id"] if existing else _term_id(),
            "term": candidate["term"],
            "normalized_term": candidate["normalized_term"],
            "weight": clean_weight,
            "status": "active",
            "origin": "candidate",
            "revision": revision,
            "created_at": existing.get("created_at") if existing else self._now(),
            "updated_at": self._now(),
        }
        candidate_payload = dict(candidate) | {
            "status": "accepted",
            "accepted_term_id": term_payload["id"],
            "revision": revision,
            "updated_at": self._now(),
        }
        self._store.write(_TERM_COLLECTION, str(term_payload["id"]), term_payload, expected_revision=None)
        self._store.write(_CANDIDATE_COLLECTION, str(candidate_payload["id"]), candidate_payload, expected_revision=None)
        return _term_record(term_payload)

    def reject_candidate(self, candidate_id: str) -> RealtimeAsrLexiconCandidate:
        """Reject a pending proposal without changing active vocabulary."""

        candidate = self._pending_candidate(candidate_id)
        revision = self._advance_revision()
        payload = dict(candidate) | {"status": "rejected", "revision": revision, "updated_at": self._now()}
        self._store.write(_CANDIDATE_COLLECTION, str(payload["id"]), payload, expected_revision=None)
        return _candidate_record(payload)

    def select_for_session(self, *, limit: int = MAX_SESSION_TERMS) -> RealtimeAsrLexiconSelection:
        """Return a deterministic, bounded snapshot for one ASR session."""

        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise RealtimeAsrLexiconError("session term limit must be positive")
        bounded_limit = min(limit, MAX_SESSION_TERMS)
        active = [
            _term_record(item)
            for item in self._store.list(_TERM_COLLECTION)
            if item.get("status") == "active"
        ]
        active.sort(key=lambda item: (-item.weight, item.term.casefold(), item.term))
        return RealtimeAsrLexiconSelection(
            revision=self.current_revision(),
            terms=tuple(active[:bounded_limit]),
        )

    def list_candidates(self, *, status: str = "pending_review") -> tuple[RealtimeAsrLexiconCandidate, ...]:
        if status not in {"pending_review", "accepted", "rejected"}:
            raise RealtimeAsrLexiconError("candidate status is invalid")
        items = [_candidate_record(item) for item in self._store.list(_CANDIDATE_COLLECTION) if item.get("status") == status]
        return tuple(sorted(items, key=lambda item: (item.term.casefold(), item.candidate_id)))

    def list_terms(self) -> tuple[RealtimeAsrLexiconTerm, ...]:
        items = [_term_record(item) for item in self._store.list(_TERM_COLLECTION)]
        return tuple(sorted(items, key=lambda item: (item.term.casefold(), item.term_id)))

    def _pending_candidate(self, candidate_id: str) -> Mapping[str, object]:
        clean_id = _candidate_id(candidate_id)
        candidate = self._store.read(_CANDIDATE_COLLECTION, clean_id)
        if candidate is None:
            raise RealtimeAsrLexiconError("lexicon candidate not found")
        if candidate.get("status") != "pending_review":
            raise RealtimeAsrLexiconError("lexicon candidate must be pending_review")
        _candidate_record(candidate)
        return candidate

    def _find_term(self, normalized_term: str) -> Mapping[str, object] | None:
        found = [item for item in self._store.list(_TERM_COLLECTION) if item.get("normalized_term") == normalized_term]
        if len(found) > 1:
            raise RealtimeAsrLexiconError("lexicon term authority is duplicated")
        return found[0] if found else None

    def _assert_super_capacity(self, *, new_is_super: bool, replacing: Mapping[str, object] | None) -> None:
        if not new_is_super:
            return
        replacing_is_super = replacing is not None and replacing.get("status") == "active" and replacing.get("weight") == SUPER_WEIGHT
        if replacing_is_super:
            return
        active_super_count = sum(
            1
            for item in self._store.list(_TERM_COLLECTION)
            if item.get("status") == "active" and item.get("weight") == SUPER_WEIGHT
        )
        if active_super_count >= MAX_SUPER_TERMS:
            raise RealtimeAsrLexiconError("super lexicon term limit exceeded")

    def _state(self) -> Mapping[str, object]:
        state = self._store.read(_STATE_COLLECTION, _STATE_ID)
        if state is None:
            return {"id": _STATE_ID, "revision": 0}
        if state.get("id") != _STATE_ID:
            raise RealtimeAsrLexiconError("lexicon state identity is invalid")
        _positive_int(state.get("revision"), "lexicon revision", allow_zero=True)
        return state

    def _advance_revision(self) -> int:
        previous = self.current_revision()
        revision = previous + 1
        self._store.write(
            _STATE_COLLECTION,
            _STATE_ID,
            {
                "schema_version": "1.0.0",
                "id": _STATE_ID,
                "revision": revision,
                "updated_at": self._now(),
            },
            expected_revision=None,
        )
        return revision


def _term(value: object) -> tuple[str, str]:
    if not isinstance(value, str):
        raise RealtimeAsrLexiconError("lexicon term must be text")
    clean = value.strip()
    if not clean:
        raise RealtimeAsrLexiconError("lexicon term length is invalid")
    if any(character.isspace() and character not in {" ", "\t"} or ord(character) < 32 for character in clean):
        raise RealtimeAsrLexiconError("lexicon term contains unsupported control characters")
    if any(ord(character) > 127 for character in clean):
        if len(clean) > MAX_NON_ASCII_TERM_LENGTH:
            raise RealtimeAsrLexiconError("lexicon term length is invalid")
    elif len(clean.split()) > MAX_ASCII_TERM_SEGMENTS:
        raise RealtimeAsrLexiconError("lexicon term length is invalid")
    return clean, clean.casefold()


def _weight(value: object, *, is_super: bool) -> int:
    if is_super:
        return SUPER_WEIGHT
    if not isinstance(value, int) or isinstance(value, bool) or not NORMAL_WEIGHT_MIN <= value <= NORMAL_WEIGHT_MAX:
        raise RealtimeAsrLexiconError("normal lexicon weight must be between 1 and 5")
    return value


def _term_id() -> str:
    return f"rt-asr-term-{uuid4().hex}"


def _candidate_id(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("rt-asr-candidate-"):
        raise RealtimeAsrLexiconError("lexicon candidate id is invalid")
    return value


def _optional_ref(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise RealtimeAsrLexiconError("candidate source ref is invalid")
    return value.strip()


def _positive_int(value: object, field: str, *, allow_zero: bool = False) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < (0 if allow_zero else 1):
        raise RealtimeAsrLexiconError(f"{field} is invalid")
    return value


def _term_record(value: Mapping[str, object]) -> RealtimeAsrLexiconTerm:
    status = value.get("status")
    origin = value.get("origin")
    if status not in {"active", "retired"} or origin not in {"manual", "candidate"}:
        raise RealtimeAsrLexiconError("lexicon term record is invalid")
    term, normalized = _term(value.get("term"))
    if value.get("normalized_term") != normalized:
        raise RealtimeAsrLexiconError("lexicon term normalization drifted")
    weight = value.get("weight")
    if weight == SUPER_WEIGHT:
        clean_weight = SUPER_WEIGHT
    else:
        clean_weight = _weight(weight, is_super=False)
    identifier = value.get("id")
    if not isinstance(identifier, str) or not identifier.startswith("rt-asr-term-"):
        raise RealtimeAsrLexiconError("lexicon term id is invalid")
    return RealtimeAsrLexiconTerm(
        term_id=identifier,
        term=term,
        weight=clean_weight,
        status=status,
        origin=origin,
        revision=_positive_int(value.get("revision"), "term revision"),
    )


def _candidate_record(value: Mapping[str, object]) -> RealtimeAsrLexiconCandidate:
    status = value.get("status")
    if status not in {"pending_review", "accepted", "rejected"}:
        raise RealtimeAsrLexiconError("lexicon candidate record is invalid")
    term, normalized = _term(value.get("term"))
    if value.get("normalized_term") != normalized:
        raise RealtimeAsrLexiconError("lexicon candidate normalization drifted")
    return RealtimeAsrLexiconCandidate(
        candidate_id=_candidate_id(value.get("id")),
        term=term,
        suggested_weight=_weight(value.get("suggested_weight"), is_super=False),
        status=status,
        revision=_positive_int(value.get("revision"), "candidate revision"),
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = (
    "MAX_SESSION_TERMS",
    "MAX_SUPER_TERMS",
    "NORMAL_WEIGHT_MAX",
    "NORMAL_WEIGHT_MIN",
    "SUPER_WEIGHT",
    "MAX_ASCII_TERM_SEGMENTS",
    "MAX_NON_ASCII_TERM_LENGTH",
    "RealtimeAsrLexicon",
    "RealtimeAsrLexiconCandidate",
    "RealtimeAsrLexiconError",
    "RealtimeAsrLexiconSelection",
    "RealtimeAsrLexiconTerm",
)
