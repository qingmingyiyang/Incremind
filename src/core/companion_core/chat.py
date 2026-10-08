from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import hashlib
import re
from typing import Protocol
from uuid import uuid4

from .errors import CompanionConflict, CompanionRepositoryError
from .model_routes import CompanionModelRouter, compose_companion_prompt
from .models import CompanionChatResult, CompanionMasterProfile, CompanionMessage, CompanionSession
from .repository import CompanionRepository


_REQUEST_ID = re.compile(r"^[a-z0-9][a-z0-9:_-]{0,127}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MEMORY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_-]{0,191}$")
_PROJECT_ID_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class CompanionChatError(ValueError):
    pass


class CompanionChatCancelled(CompanionChatError):
    pass


class CharacterPromptLoader(Protocol):
    def __call__(self) -> tuple[str, int]: ...


class StateProjectionLoader(Protocol):
    def __call__(self) -> Mapping[str, object]: ...


class AffectSink(Protocol):
    def __call__(self, signal: str, request_id: str) -> object: ...


class MemoryRecallLoader(Protocol):
    def __call__(
        self, query: str, *, review_scope: str | None = None, target_memory_id: str | None = None,
    ) -> object: ...


_MEMORY_REVIEW_PROMPTS = {
    "请根据我已经确认发布的长期记忆，回顾今天值得注意的变化。请区分有证据的事实与暂无依据的推断。": "today_memory_review",
    "请根据我已经确认发布的长期记忆，梳理最近七天的重要变化、仍未解决的问题和下一步。请区分有证据的事实与暂无依据的推断。": "weekly_memory_review",
}
_MEMORY_TOPIC_PROMPT_PREFIX = "请根据我已经确认发布的长期记忆，围绕《"
_MEMORY_TOPIC_PROMPT_SUFFIX = "》说明这条记忆的意义、与当前工作的联系，并给出两个可继续追问的问题。请区分有证据的事实与推断。"
_MEMORY_TOPIC_PROMPT = re.compile(
    rf"^{re.escape(_MEMORY_TOPIC_PROMPT_PREFIX)}(.{{1,120}}){re.escape(_MEMORY_TOPIC_PROMPT_SUFFIX)}$"
)
_PERSISTED_MEMORY_SCOPES = {
    "conversation_context",
    "today_memory_review",
    "weekly_memory_review",
    "memory_topic_discussion",
}


class CompanionChatService:
    def __init__(
        self,
        repository: CompanionRepository,
        *,
        model_router: CompanionModelRouter,
        character_prompt_loader: CharacterPromptLoader,
        state_projection_loader: StateProjectionLoader | None = None,
        affect_sink: AffectSink | None = None,
        memory_recall_loader: MemoryRecallLoader | None = None,
        project_id: str = "default",
        now: Callable[[], datetime] | None = None,
        session_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.repository = repository
        self.model_router = model_router
        self.character_prompt_loader = character_prompt_loader
        self.state_projection_loader = state_projection_loader or (lambda: {})
        self.affect_sink = affect_sink or (lambda _signal, _request_id: None)
        self.memory_recall_loader = memory_recall_loader
        self.project_id = _require_project_id(project_id)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.session_id_factory = session_id_factory or (lambda: f"session:{uuid4().hex}")

    def send(
        self,
        *,
        request_id: str,
        text: str,
        session_id: str | None = None,
        target_memory_id: str | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> CompanionChatResult:
        request_id = _require_request_id(request_id)
        text = _require_text(text)
        target_memory_id = _optional_memory_id(target_memory_id)
        if target_memory_id and _MEMORY_TOPIC_PROMPT.fullmatch(text) is None:
            raise CompanionChatError("chat memory anchor requires the controlled topic prompt")
        self.repository.initialize()
        existing_user = self.repository.get_message_by_request(request_id=request_id, role="user")
        existing_assistant = self.repository.get_message_by_request(request_id=request_id, role="assistant")
        if existing_user is None and existing_assistant is not None:
            raise CompanionConflict("chat request history was forgotten and cannot be replayed")
        if existing_user is not None:
            if (
                existing_user.content != text
                or existing_user.project_id != self.project_id
                or (session_id is not None and existing_user.session_id != session_id)
            ):
                raise CompanionConflict("chat request was replayed with different input")
            session_id = existing_user.session_id
        session = self._session(session_id)
        if existing_user is not None and (
            existing_user.session_id != session.session_id or existing_user.context_epoch != session.context_epoch
        ):
            raise CompanionConflict("chat replay no longer belongs to the active context epoch")
        if existing_assistant is not None and existing_assistant.project_id != self.project_id:
            raise CompanionConflict("chat request was replayed with different input")
        if existing_assistant is not None and existing_assistant.status == "completed":
            self._record_chat_interaction(request_id)
            trace = {"route_key": "companion.chat", "replayed": True}
            if existing_assistant.memory_review:
                trace["memory_recall"] = dict(existing_assistant.memory_review)
            return CompanionChatResult(
                session=session,
                user_message=existing_user,
                assistant_message=existing_assistant,
                source="provider" if existing_assistant.provider_mode == "remote" else "local",
                reason="replayed",
                trace=trace,
                replayed=True,
            )

        prior = tuple(
            message for message in self.repository.list_context_messages(
                session_id=session.session_id,
                context_epoch=session.context_epoch,
                project_id=self.project_id,
            ) if message.request_id != request_id
        )
        created_at = _utc(self.now())
        user_message = existing_user or self.repository.append_message(
            message_id=_message_id("user", request_id),
            request_id=request_id,
            session_id=session.session_id,
            context_epoch=session.context_epoch,
            role="user",
            status="completed",
            content=text,
            created_at=created_at,
            provider_mode="none",
            project_id=self.project_id,
        )
        prompt_text, prompt_revision = self.character_prompt_loader()
        if prompt_revision != session.prompt_revision:
            raise CompanionConflict("chat prompt revision drifted from the session")
        recall = self._recall(text, target_memory_id=target_memory_id)
        prompt = compose_companion_prompt(
            route_key="companion.chat",
            master_profile=_profile_data(self.repository.get_master_profile()),
            character_prompt=prompt_text,
            modifiers=self.state_projection_loader(),
            published_context=recall[0],
            short_term_messages=tuple(
                {"role": message.role, "text": message.content, "context_epoch": message.context_epoch}
                for message in prior
            ),
            user_payload={"text": text},
            context_epoch=session.context_epoch,
        )
        try:
            outcome = self.model_router.execute(
                route_key="companion.chat",
                prompt=prompt,
                request_id=request_id,
                cancelled=cancelled,
            )
            if outcome.get("reason") == "cancelled":
                self._store_noncompleted(existing_assistant, request_id, session, "cancelled")
                raise CompanionChatCancelled("chat request was cancelled")
            source = str(outcome.get("source") or "local")
            provider_mode = "remote" if source == "provider" else "local"
            assistant = self._store_completed(
                existing_assistant,
                request_id=request_id,
                session=session,
                text=str(outcome["text"]),
                provider_mode=provider_mode,
                memory_review=_memory_review_record(recall[1], source=source),
            )
            try:
                self.affect_sink(str(outcome.get("affect", "neutral")), request_id)
            except (CompanionConflict, CompanionRepositoryError):
                # Affect is advisory state feedback. A limit or storage conflict must
                # never turn a successfully generated and persisted reply into failure.
                pass
        except CompanionChatCancelled:
            raise
        except Exception as error:
            self._store_noncompleted(existing_assistant, request_id, session, "failed")
            raise CompanionChatError("companion chat generation failed") from error
        self._record_chat_interaction(request_id)
        trace = dict(outcome.get("trace") or {})
        trace["memory_recall"] = recall[1]
        return CompanionChatResult(
            session=session,
            user_message=user_message,
            assistant_message=assistant,
            source=source,
            reason=str(outcome.get("reason")) if outcome.get("reason") else None,
            trace=trace,
            replayed=False,
        )

    def _recall(
        self, text: str, *, target_memory_id: str | None = None,
    ) -> tuple[tuple[Mapping[str, object], ...], Mapping[str, object]]:
        if self.memory_recall_loader is None:
            return (), {"status": "disabled", "backend": "none", "selected": [], "dropped_reasons": []}
        review_scope = _MEMORY_REVIEW_PROMPTS.get(text)
        query = text
        topic_match = _MEMORY_TOPIC_PROMPT.fullmatch(text)
        if topic_match is not None:
            review_scope = "memory_topic_discussion"
            query = topic_match.group(1).strip()
        try:
            if review_scope and target_memory_id:
                result = self.memory_recall_loader(
                    query, review_scope=review_scope, target_memory_id=target_memory_id,
                )
            elif review_scope:
                result = self.memory_recall_loader(query, review_scope=review_scope)
            else:
                result = self.memory_recall_loader(query)
            context = tuple(getattr(result, "context", ()))
            selected = [dict(item) for item in getattr(result, "selected", ())]
            trace = {
                "status": str(getattr(result, "status", "degraded")),
                "backend": str(getattr(result, "backend", "unavailable")),
                "selected": selected,
                "dropped_reasons": list(getattr(result, "dropped_reasons", ())),
            }
            resolved_scope = getattr(result, "review_scope", None)
            if resolved_scope in {"today_memory_review", "weekly_memory_review", "memory_topic_discussion"}:
                trace["review_scope"] = resolved_scope
            return context, trace
        except Exception:
            trace = {
                "status": "degraded", "backend": "unavailable", "selected": [],
                "dropped_reasons": ["index_unavailable"],
            }
            if review_scope in {"today_memory_review", "weekly_memory_review", "memory_topic_discussion"}:
                trace["review_scope"] = review_scope
            return (), trace

    def _session(self, session_id: str | None) -> CompanionSession:
        prompt_text, prompt_revision = self.character_prompt_loader()
        if not prompt_text or prompt_revision < 1:
            raise CompanionChatError("character prompt authority is unavailable")
        profile = self.repository.get_master_profile()
        profile_revision = profile.revision if profile is not None else 1
        if session_id is None:
            return self.repository.create_session(
                session_id=self.session_id_factory(),
                context_epoch=1,
                prompt_revision=prompt_revision,
                profile_revision=profile_revision,
                started_at=_utc(self.now()),
                project_id=self.project_id,
            )
        session = self.repository.get_session(session_id)
        if session is None or session.closed_at is not None:
            raise CompanionChatError("chat session is unavailable")
        if session.project_id != self.project_id:
            raise CompanionConflict("chat session belongs to a different project")
        return self.repository.synchronize_session_authorities(
            session_id=session.session_id,
            prompt_revision=prompt_revision,
            profile_revision=profile_revision,
        )

    def _store_completed(
        self,
        existing: CompanionMessage | None,
        *,
        request_id: str,
        session: CompanionSession,
        text: str,
        provider_mode: str,
        memory_review: Mapping[str, object],
    ) -> CompanionMessage:
        if existing is not None:
            return self.repository.resolve_message(
                message_id=existing.message_id,
                expected_revision=existing.revision,
                status="completed",
                content=text,
                provider_mode=provider_mode,
                memory_review=memory_review,
            )
        return self.repository.append_message(
            message_id=_message_id("assistant", request_id),
            request_id=request_id,
            session_id=session.session_id,
            context_epoch=session.context_epoch,
            role="assistant",
            status="completed",
            content=text,
            created_at=_utc(self.now()),
            provider_mode=provider_mode,
            project_id=self.project_id,
            memory_review=memory_review,
        )

    def _store_noncompleted(
        self,
        existing: CompanionMessage | None,
        request_id: str,
        session: CompanionSession,
        status: str,
    ) -> None:
        if existing is None:
            self.repository.append_message(
                message_id=_message_id("assistant", request_id),
                request_id=request_id,
                session_id=session.session_id,
                context_epoch=session.context_epoch,
                role="assistant",
                status=status,
                content="",
                created_at=_utc(self.now()),
                provider_mode="none",
                project_id=self.project_id,
            )

    def _record_chat_interaction(self, request_id: str) -> None:
        self.repository.record_interaction(event_id=f"chat:{_digest(request_id)}", kind="chat")


def _profile_data(profile: CompanionMasterProfile | None) -> Mapping[str, object]:
    if profile is None:
        return {}
    return {
        "nickname": profile.nickname,
        "birthday": profile.birthday,
        "oc_address": profile.oc_address,
        "relationship": profile.relationship,
        "custom_notes": profile.custom_notes,
        "revision": profile.revision,
    }


def _memory_review_record(recall: Mapping[str, object], *, source: str) -> dict[str, object]:
    scope = recall.get("review_scope")
    memory_ids: list[str] = []
    selected = recall.get("selected")
    if isinstance(selected, list):
        for item in selected:
            value = str(item.get("memory_id") or "").strip() if isinstance(item, Mapping) else ""
            if _MEMORY_ID.fullmatch(value) is None or value in memory_ids:
                continue
            memory_ids.append(value)
            if len(memory_ids) == 4:
                break
    if scope is None and memory_ids:
        scope = "conversation_context"
    if scope not in _PERSISTED_MEMORY_SCOPES:
        return {}
    generated = source == "provider"
    status = str(recall.get("status") or "degraded")
    if status not in {"recalled", "empty", "degraded", "disabled"}:
        status = "degraded"
    return {
        "review_scope": scope,
        "status": status,
        "matched_count": len(memory_ids),
        "memory_ids": memory_ids if generated else [],
        "generated": generated,
    }


def _message_id(role: str, request_id: str) -> str:
    return f"message:{role}:{_digest(request_id)}"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _require_request_id(value: object) -> str:
    if not isinstance(value, str) or _REQUEST_ID.fullmatch(value) is None:
        raise CompanionChatError("chat request id is invalid")
    return value


def _require_text(value: object) -> str:
    if not isinstance(value, str):
        raise CompanionChatError("chat text is invalid")
    text = value.strip()
    if not text or len(text) > 4_000 or _CONTROL.search(text):
        raise CompanionChatError("chat text is invalid")
    return text


def _optional_memory_id(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _MEMORY_ID.fullmatch(value) is None:
        raise CompanionChatError("chat memory id is invalid")
    return value


def _require_project_id(value: object) -> str:
    if not isinstance(value, str):
        raise CompanionChatError("chat project id is invalid")
    project_id = value.strip()
    if not project_id or len(project_id) > 191 or _PROJECT_ID_CONTROL.search(project_id):
        raise CompanionChatError("chat project id is invalid")
    return project_id


def _utc(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CompanionChatError("chat clock is invalid")
    return value.astimezone(timezone.utc).isoformat()
