"""Explicit, proposal-only consolidation of one private Companion episode.

This router is intentionally not self-registering: the shared API router list
must opt in to it explicitly.  No scheduler, automatic publication, or
background reaper is introduced by this module.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.container import ApiContainerDep
from backend.companion_runtime_layout import build_companion_object_store
from core.aggregate_repository_factory import AggregateRepositoryFactory, AggregateRepositoryFactoryError
from core.companion_core import CompanionRepository, CompanionRepositoryError
from core.memory_core import ObjectStoreMemoryCandidateRepository, ObjectStoreMemoryStore, SQLiteMemoryReader
from core.product_core.memory_distillation import (
    DistillConversationEpisodeToMemoryProposal,
    MemoryDistillationError,
    ObjectStoreMemoryDistillationDiaryRepository,
    serialize_memory_distillation_result,
)


router = APIRouter(tags=["memory-distillation"])
_PATH = "/api/rebuild/companion/memory-distillations"
_FIELDS = {"project_id", "current_session_id", "episode_id", "command_id"}
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_-]{0,127}$")
_COMMAND_ID = re.compile(r"^distill-[A-Za-z0-9][A-Za-z0-9:_-]{0,119}$")
_NO_STORE = {"Cache-Control": "no-store"}


@router.get(_PATH + "/episodes")
async def list_distillable_companion_episodes(
    project_id: str, current_session_id: str, container: ApiContainerDep,
) -> JSONResponse:
    """Return bounded projection metadata, never historical message bodies."""
    try:
        _require_id(project_id, "project_id")
        _require_id(current_session_id, "current_session_id")
        repository = CompanionRepository.at_data_root(container.root_dir)
        current = repository.get_session(current_session_id)
        if current is None or current.project_id != project_id:
            raise MemoryDistillationError("current session is unavailable for this project")
        items = repository.list_conversation_episodes(
            project_id=project_id, current_session_id=current_session_id, limit=12,
        )
        return JSONResponse(content={"items": [{
            "episode_id": item.episode_id, "session_id": item.session_id,
            "occurred_at": item.occurred_at, "summary": item.summary[:320],
            "source_turns": [item.user_message_id, item.assistant_message_id],
        } for item in items]}, headers=_NO_STORE)
    except (CompanionRepositoryError, MemoryDistillationError, ValueError):
        return _error(409, "memory_distillation_rejected")


class CompanionMemoryDistillationCommand:
    """Resolve one same-project Episode and make a pending-review proposal only."""

    def __init__(
        self,
        *,
        repository: CompanionRepository,
        candidates: ObjectStoreMemoryCandidateRepository,
        diary: ObjectStoreMemoryDistillationDiaryRepository,
        published_memory: Iterable[Mapping[str, object]] = (),
        now: str | None = None,
    ) -> None:
        self._repository = repository
        self._candidates = candidates
        self._diary = diary
        self._published_memory = tuple(dict(item) for item in published_memory)
        self._now = now or datetime.now(timezone.utc).isoformat(timespec="seconds")

    def execute(
        self,
        *,
        project_id: str,
        current_session_id: str,
        episode_id: str,
        command_id: str,
    ) -> Mapping[str, object]:
        _require_id(project_id, "project_id")
        _require_id(current_session_id, "current_session_id")
        _require_id(episode_id, "episode_id")
        if not _COMMAND_ID.fullmatch(command_id):
            raise MemoryDistillationError("memory distillation command_id is invalid")
        current_session = self._repository.get_session(current_session_id)
        if current_session is None or current_session.project_id != project_id:
            raise MemoryDistillationError("current session is unavailable for this project")
        episode = next(
            (
                item
                for item in self._repository.list_conversation_episodes(
                    project_id=project_id,
                    current_session_id=current_session_id,
                    limit=12,
                )
                if item.episode_id == episode_id
            ),
            None,
        )
        if episode is None:
            raise MemoryDistillationError("conversation episode is unavailable for this project")
        existing = self._diary.get(command_id)
        if existing is not None:
            if (
                existing.get("project_id") != project_id
                or existing.get("episode_id") != episode_id
                or existing.get("requested_from_session_id") != current_session_id
            ):
                raise MemoryDistillationError("memory distillation command identity conflicts")
            candidate_id = existing.get("candidate_id")
            if candidate_id is not None and (
                not isinstance(candidate_id, str) or self._candidates.get(candidate_id) is None
            ):
                raise MemoryDistillationError("memory distillation candidate is unavailable")
            return _replayed_result(existing)
        result = DistillConversationEpisodeToMemoryProposal(
            messages=self._repository,
            candidates=self._candidates,
            now=self._now,
        ).execute(episode, published_memory=self._published_memory)
        diary = self._diary.record(
            command_id=command_id,
            current_session_id=current_session_id,
            episode=episode,
            result=result,
            created_at=self._now,
        )
        return {
            **serialize_memory_distillation_result(result),
            "diary": {**serialize_memory_distillation_result(result)["diary"], "id": diary["id"]},
            "proposal_only": True,
            "auto_publication": "disabled",
        }


@router.post(_PATH)
async def distill_companion_episode(request: Request, container: ApiContainerDep) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        return _error(400, "memory_distillation_invalid")
    if not isinstance(payload, Mapping) or set(payload) != _FIELDS:
        return _error(400, "memory_distillation_invalid")
    values = {field: payload.get(field) for field in _FIELDS}
    if any(not isinstance(value, str) for value in values.values()):
        return _error(400, "memory_distillation_invalid")
    try:
        store = build_companion_object_store(container.root_dir)
        factory = AggregateRepositoryFactory(
            runtime_root=container.root_dir,
            namespace_id=store.namespace_id,
            json_store=store,
        )
        resolution = factory.memory_publication_authority_resolution()
        memory = SQLiteMemoryReader(resolution.records) if resolution.records is not None else ObjectStoreMemoryStore(store)
        command = CompanionMemoryDistillationCommand(
            repository=CompanionRepository.at_data_root(container.root_dir),
            candidates=ObjectStoreMemoryCandidateRepository(store),
            diary=ObjectStoreMemoryDistillationDiaryRepository(store),
            published_memory=memory.list_by_project(str(values["project_id"])),
        )
        result = command.execute(
            project_id=str(values["project_id"]),
            current_session_id=str(values["current_session_id"]),
            episode_id=str(values["episode_id"]),
            command_id=str(values["command_id"]),
        )
    except (AggregateRepositoryFactoryError, CompanionRepositoryError, MemoryDistillationError, ValueError):
        return _error(409, "memory_distillation_rejected")
    return JSONResponse(status_code=201, content=result, headers=_NO_STORE)


def _require_id(value: object, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise MemoryDistillationError(f"memory distillation {label} is invalid")
    return value


def _replayed_result(entry: Mapping[str, object]) -> Mapping[str, object]:
    return {
        "episode_id": entry["episode_id"],
        "candidate_id": entry.get("candidate_id"),
        "status": entry["disposition"],
        "diary": {
            "id": entry["id"],
            "source_refs": entry["source_refs"],
            "disposition": entry["disposition"],
            "reason_codes": entry["reason_codes"],
            "confidence": entry["confidence"],
            "conflicts": entry["conflicts"],
            "estimated_cost_tokens": entry["estimated_cost_tokens"],
        },
        "memory_publication_state": (
            "candidate_created_not_published" if entry.get("candidate_id") else "not_published"
        ),
        "proposal_only": True,
        "auto_publication": "disabled",
        "replayed": True,
    }


def _error(status_code: int, code: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": code}, headers=_NO_STORE)
