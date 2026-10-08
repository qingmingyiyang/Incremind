"""Read-only Companion Chat context preparation for the AI Turn migration."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from backend.companion_memory_runtime import build_companion_memory_bridge
from backend.companion_prompt_runtime import load_active_character_prompt
from backend.companion_state_runtime import resolve_economy_rules_path
from backend.companion_runtime_layout import build_companion_object_store
from core.aggregate_repository_factory import AggregateRepositoryFactory, AggregateRepositoryFactoryError
from core.ai_kernel import CapabilityDefinition, TurnPayloadStorePort, validate_turn_presentation_artifact
from core.companion_core import (
    CompanionRepository,
    CompanionStateReducer,
    ConversationRecallService,
    build_companion_clock,
    companion_chat_state_context,
    compose_companion_prompt,
)
from core.model_gateway import ModelExecutionControlPort, ModelGatewayPort, ModelRequest

from backend.api.ai_execution_control import begin_nested_model_call, execution_control_from
from backend.api.turn_model_routing_binding import load_turn_model_routing_binding


COMPANION_CHAT_OUTCOME = "companion.chat.respond"
COMPANION_CHAT_CONTEXT_CAPABILITY = "companion.chat.context.read"
COMPANION_CHAT_MESSAGE_WRITE_CAPABILITY = "companion.chat.message.write"

CharacterPromptLoader = Callable[[], tuple[str, int]]
StateProjectionLoader = Callable[[], Mapping[str, object]]
MemoryRecallLoader = Callable[[str], object]
ProjectSkillLoader = Callable[[str], Mapping[str, object] | None]


# A direct memory anchor is deliberately only available through the same
# controlled topic prompt as the legacy Companion endpoint.  An atom id is an
# authority selector, not general-purpose prompt context, so it must never be
# accepted beside an arbitrary user prompt.
_MEMORY_TOPIC_PROMPT = re.compile(
    r"^请根据我已经确认发布的长期记忆，围绕《(.{1,120})》说明这条记忆的意义、与当前工作的联系，并给出两个可继续追问的问题。请区分有证据的事实与推断。$"
)
_MEMORY_REVIEW_PROMPTS = {
    "请根据我已经确认发布的长期记忆，回顾今天值得注意的变化。请区分有证据的事实与暂无依据的推断。": "today_memory_review",
    "请根据我已经确认发布的长期记忆，梳理最近七天的重要变化、仍未解决的问题和下一步。请区分有证据的事实与暂无依据的推断。": "weekly_memory_review",
}


@dataclass(frozen=True, slots=True)
class CompanionChatScopeDependencies:
    """Fresh domain adapters for one Turn project scope.

    The shared AI runtime is process-wide, while Companion chat authorities are
    project-scoped.  Keeping this composition at invocation time prevents the
    first submitted Turn from pinning later Turns to its project.
    """

    repository: CompanionRepository
    character_prompt_loader: CharacterPromptLoader
    state_projection_loader: StateProjectionLoader
    memory_recall_loader: MemoryRecallLoader
    project_skill_loader: ProjectSkillLoader


class ScopedCompanionChatContextCapability:
    """Production context provider that resolves Companion dependencies by scope."""

    def __init__(self, *, container: object, namespace_id: str) -> None:
        self._container = container
        self._runtime_root = Path(getattr(container, "root_dir")).resolve()
        self._namespace_id = namespace_id

    def invoke(self, request: Mapping[str, object]) -> Mapping[str, object]:
        project_id = _turn_project_scope(request)
        dependencies = _scope_dependencies(self._container, self._runtime_root, project_id)
        return CompanionChatContextCapability(
            repository=dependencies.repository,
            character_prompt_loader=dependencies.character_prompt_loader,
            state_projection_loader=dependencies.state_projection_loader,
            memory_recall_loader=dependencies.memory_recall_loader,
            project_skill_loader=dependencies.project_skill_loader,
            project_id=project_id,
            namespace_id=self._namespace_id,
        ).invoke(request)


class ScopedCompanionChatMessageWriteCapability:
    """Production approved-write provider with per-Turn project dependencies."""

    def __init__(self, *, container: object, namespace_id: str, gateway: ModelGatewayPort | None, receipt_store: TurnPayloadStorePort) -> None:
        self._container = container
        self._runtime_root = Path(getattr(container, "root_dir")).resolve()
        self._namespace_id = namespace_id
        self._gateway = gateway
        self._receipt_store = receipt_store

    def invoke(self, request: Mapping[str, object]) -> Mapping[str, object]:
        project_id = _turn_project_scope(request)
        dependencies = _scope_dependencies(self._container, self._runtime_root, project_id)
        return CompanionChatMessageWriteCapability(
            repository=dependencies.repository,
            character_prompt_loader=dependencies.character_prompt_loader,
            gateway=self._gateway,
            receipt_store=self._receipt_store,
            project_id=project_id,
            namespace_id=self._namespace_id,
        ).invoke(request)


class CompanionChatContextCapability:
    """Resolve only the context a future approved chat write may consume.

    A new session is represented by a deterministic prospective identifier.  It
    is deliberately not created here: merely preparing an AI Turn must not add
    a session, message, interaction, memory candidate, or state mutation.
    """

    def __init__(
        self,
        *,
        repository: CompanionRepository,
        character_prompt_loader: CharacterPromptLoader,
        state_projection_loader: StateProjectionLoader | None = None,
        memory_recall_loader: MemoryRecallLoader | None = None,
        project_skill_loader: ProjectSkillLoader | None = None,
        project_id: str = "default",
        namespace_id: str = "default",
    ) -> None:
        self._repository = repository
        self._character_prompt_loader = character_prompt_loader
        self._state_projection_loader = state_projection_loader or (lambda: {})
        self._memory_recall_loader = memory_recall_loader
        self._project_skill_loader = project_skill_loader
        self._project_id = _project_id(project_id)
        self._namespace_id = namespace_id

    def invoke(self, request: Mapping[str, object]) -> Mapping[str, object]:
        arguments = _arguments(request)
        request_id = _required(arguments.get("request_id"), "request_id")
        text = _required(arguments.get("text"), "text")
        requested_session_id = arguments.get("session_id")
        if requested_session_id is not None and not isinstance(requested_session_id, str):
            raise ValueError("Companion Chat session id is invalid")
        self._repository.initialize()
        prompt_text, prompt_revision = self._character_prompt_loader()
        if not prompt_text or not isinstance(prompt_revision, int) or prompt_revision < 1:
            raise ValueError("Companion Chat prompt authority is unavailable")
        profile = self._repository.get_master_profile()
        profile_revision = profile.revision if profile is not None else 1
        session = self._repository.get_session(requested_session_id) if requested_session_id else None
        if requested_session_id and session is None:
            raise ValueError("Companion Chat session is unavailable")
        if session is not None:
            if session.closed_at is not None or session.project_id != self._project_id:
                raise ValueError("Companion Chat session is unavailable")
            # synchronize_session_authorities would mutate the session, so stale
            # authority is reported instead of being repaired in a read tool.
            if session.prompt_revision != prompt_revision or session.profile_revision != profile_revision:
                raise ValueError("Companion Chat session authority baseline is stale")
            session_id, context_epoch, revision = session.session_id, session.context_epoch, session.revision
            history = self._repository.list_context_messages(
                session_id=session_id, context_epoch=context_epoch, project_id=self._project_id,
            )
        else:
            session_id, context_epoch, revision, history = _prospective_session_id(request_id), 1, 0, ()
        if self._repository.get_message_by_request(request_id=request_id, role="user") is not None or self._repository.get_message_by_request(request_id=request_id, role="assistant") is not None:
            raise ValueError("Companion Chat request baseline is stale")
        target_memory_id = arguments.get("target_memory_id")
        if target_memory_id is not None and not isinstance(target_memory_id, str):
            raise ValueError("Companion Chat memory anchor is invalid")
        review_scope = arguments.get("review_scope")
        if review_scope is not None and not isinstance(review_scope, str):
            raise ValueError("Companion Chat memory review scope is invalid")
        memory_context, memory_trace = self._authorized_memory(
            text,
            target_memory_id=target_memory_id,
            review_scope=review_scope,
        )
        conversation_recall = ConversationRecallService(
            self._repository, project_id=self._project_id,
        ).recall(current_session_id=session_id, query=text)
        episodic_context = tuple(
            {
                "episode_id": episode.episode_id,
                "occurred_at": episode.occurred_at,
                "summary": episode.summary,
                "source_turns": [episode.user_message_id, episode.assistant_message_id],
            }
            for episode in conversation_recall.episodes
        )
        conversation_trace = {
            "status": "used" if episodic_context else "empty",
            "episode_count": len(episodic_context),
            "byte_count": conversation_recall.byte_count,
            "episodes": [
                {
                    "episode_id": episode.episode_id,
                    "session_id": episode.session_id,
                    "source_turns": [episode.user_message_id, episode.assistant_message_id],
                }
                for episode in conversation_recall.episodes
            ],
        }
        skill = self._project_skill_loader(self._project_id) if self._project_skill_loader else None
        project_context = {"project_id": self._project_id, "kind": "project_skill"}
        if isinstance(skill, Mapping):
            project_context["skill"] = dict(skill)
        prompt = compose_companion_prompt(
            route_key="companion.chat",
            master_profile=_profile_data(profile),
            character_prompt=prompt_text,
            modifiers=self._state_projection_loader(),
            published_context=(*memory_context, project_context),
            episodic_context=episodic_context,
            short_term_messages=tuple({"role": item.role, "text": item.content, "context_epoch": context_epoch} for item in history),
            user_payload={"text": text}, context_epoch=context_epoch,
        )
        return {
            "summary": "Companion Chat context is ready",
            "receipt_ref": None,
            "payload_ref": None,
            "evidence_refs": [f"crp://{self._namespace_id}/companion/sessions/{session_id}"],
            "result": {
                "schema_version": "1.0.0", "kind": "companion.chat.context", "request_id": request_id,
                "project_id": self._project_id, "text": text, "messages": list(prompt.messages),
                "baseline": {
                    "session_id": session_id, "session_exists": session is not None,
                    "session_revision": revision, "context_epoch": context_epoch,
                    "prompt_revision": prompt_revision, "profile_revision": profile_revision,
                    "history": [{"message_id": item.message_id, "revision": item.revision} for item in history],
                },
                "memory_trace": memory_trace,
                "conversation_recall_trace": conversation_trace,
                "prompt_trace": dict(prompt.trace),
            },
        }

    def _authorized_memory(
        self,
        text: str,
        *,
        target_memory_id: str | None,
        review_scope: str | None,
    ) -> tuple[tuple[Mapping[str, object], ...], Mapping[str, object]]:
        if self._memory_recall_loader is None:
            return (), {"status": "disabled"}
        topic = _MEMORY_TOPIC_PROMPT.fullmatch(text)
        expected_scope = _MEMORY_REVIEW_PROMPTS.get(text)
        query = text
        if topic is not None:
            expected_scope = "memory_topic_discussion"
            query = topic.group(1).strip()
        if target_memory_id is not None:
            if expected_scope != "memory_topic_discussion" or review_scope != expected_scope:
                raise ValueError("Companion Chat memory anchor requires the controlled topic prompt")
        elif review_scope != expected_scope:
            raise ValueError("Companion Chat memory review scope is invalid")
        try:
            if review_scope is None:
                result = self._memory_recall_loader(query)
            else:
                result = self._memory_recall_loader(
                    query,
                    review_scope=review_scope,
                    target_memory_id=target_memory_id,
                )
            # Recall selection metadata (ids, source locators, confidence and
            # audit fields) remains inside the memory authority.  The model
            # receives only the approved body; events, receipts and the
            # presentation therefore cannot become a second recall index.
            context = tuple(
                {"text": body}
                for item in getattr(result, "context", ())
                if isinstance(item, Mapping)
                for body in (_memory_body(item),)
                if body
            )
            return context, {"status": str(getattr(result, "status", "degraded")), "selected_count": len(getattr(result, "selected", ()))}
        except Exception:
            return (), {"status": "degraded"}


class CompanionChatMessageWriteCapability:
    """Persist exactly the reviewed chat exchange after kernel approval.

    The context capability intentionally performs no mutation.  This write
    capability is its paired commit point: it rechecks every authority that
    was visible to the model before creating the user and assistant messages.
    Repository request identities provide a second idempotency boundary below
    the AI Turn action log.
    """

    def __init__(
        self,
        *,
        repository: CompanionRepository,
        character_prompt_loader: CharacterPromptLoader,
        gateway: ModelGatewayPort | None,
        receipt_store: TurnPayloadStorePort,
        project_id: str = "default",
        namespace_id: str = "default",
    ) -> None:
        self._repository = repository
        self._character_prompt_loader = character_prompt_loader
        self._gateway = gateway
        self._receipt_store = receipt_store
        self._project_id = _project_id(project_id)
        self._namespace_id = namespace_id

    def invoke(self, request: Mapping[str, object]) -> Mapping[str, object]:
        turn_id = _required(request.get("turn_id"), "turn_id")
        arguments = _arguments(request)
        context = arguments.get("context")
        if not isinstance(context, Mapping):
            raise ValueError("Companion Chat write requires context")
        request_id, text, baseline = _context_identity(context)
        conversation_recall_trace = context.get("conversation_recall_trace")
        if not isinstance(conversation_recall_trace, Mapping):
            conversation_recall_trace = {"status": "empty", "episode_count": 0, "episodes": []}
        if len(text) > 4_000:
            raise ValueError("Companion Chat message is invalid")
        scope = request.get("scope")
        if not isinstance(scope, Mapping) or scope.get("project_id") != self._project_id:
            raise ValueError("Companion Chat write is outside the Turn project scope")

        self._repository.initialize()
        prior_user = self._repository.get_message_by_request(request_id=request_id, role="user")
        prior_assistant = self._repository.get_message_by_request(request_id=request_id, role="assistant")
        if prior_user is not None or prior_assistant is not None:
            return self._replayed_result(
                turn_id=turn_id,
                request_id=request_id,
                text=text,
                baseline=baseline,
                user_message=prior_user,
                assistant_message=prior_assistant,
            )
        prompt_text, prompt_revision = self._character_prompt_loader()
        if not prompt_text or not isinstance(prompt_revision, int) or prompt_revision < 1:
            raise ValueError("Companion Chat prompt authority is unavailable")
        profile = self._repository.get_master_profile()
        profile_revision = profile.revision if profile is not None else 1
        _revalidate_authorities(
            repository=self._repository,
            project_id=self._project_id,
            request_id=request_id,
            baseline=baseline,
            prompt_revision=prompt_revision,
            profile_revision=profile_revision,
        )
        messages = context.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ValueError("Companion Chat prompt messages are invalid")
        remote_allowed = arguments.get("allow_remote") is True
        routing = None
        if self._gateway is not None and remote_allowed:
            routing = load_turn_model_routing_binding(
                self._receipt_store, request, required_capability="text",
            )
        # A configured adapter is not egress authority.  The frozen routing
        # snapshot must still select an eligible route for this Turn.
        provider_call_performed = routing is not None and routing.snapshot.get("selected") is not None
        model_evidence_refs: tuple[str, ...] = ()
        if provider_call_performed:
            execution_control = execution_control_from(request)
            assert routing is not None
            nested_model = begin_nested_model_call(request)
            nested_error_code: str | None = None
            try:
                model_result = self._gateway.invoke(
                    ModelRequest(
                        capability="text",
                        input=json.dumps(messages, ensure_ascii=False, separators=(",", ":")),
                        parameters={
                            "temperature": 0,
                            "messages": messages,
                            **routing.parameters(),
                        },
                        privacy_scope="remote_allowed",
                        execution_control=execution_control,
                        metadata_sink=nested_model,
                    )
                )
            except Exception:
                nested_error_code = "ai.nested_model_failed"
                raise
            finally:
                model_evidence_refs = nested_model.finalize(
                    error_code=nested_error_code,
                )
            response_text = _model_text(model_result.output)
            provider_id = model_result.provider or "model-gateway"
            model_name = model_result.model
        else:
            response_text = "我现在不能连接模型，不过我还在这里陪着你。"
            provider_id = "local-fallback"
            model_name = ""
        if len(response_text) > 32_000:
            raise ValueError("Companion Chat message is invalid")
        # A provider call may outlive the approval window.  Check again just
        # before the first domain mutation so a changed history never receives
        # a reply generated against an obsolete prompt.
        _revalidate_authorities(
            repository=self._repository,
            project_id=self._project_id,
            request_id=request_id,
            baseline=baseline,
            prompt_revision=prompt_revision,
            profile_revision=profile_revision,
        )

        session = self._repository.get_session(str(baseline["session_id"]))
        if session is None:
            session = self._repository.create_session(
                session_id=str(baseline["session_id"]),
                context_epoch=int(baseline["context_epoch"]),
                prompt_revision=prompt_revision,
                profile_revision=profile_revision,
                project_id=self._project_id,
                started_at=_stable_timestamp(request_id),
            )
        user_message = self._repository.append_message(
            message_id=_message_id("user", request_id),
            request_id=request_id,
            session_id=session.session_id,
            context_epoch=session.context_epoch,
            role="user",
            status="completed",
            content=text,
            created_at=_stable_timestamp(request_id),
            provider_mode="none",
            project_id=self._project_id,
        )
        assistant_message = self._repository.append_message(
            message_id=_message_id("assistant", request_id),
            request_id=request_id,
            session_id=session.session_id,
            context_epoch=session.context_epoch,
            role="assistant",
            status="completed",
            content=response_text,
            created_at=_stable_timestamp(request_id),
            provider_mode="remote" if provider_call_performed else "local",
            project_id=self._project_id,
        )
        return self._result(
            turn_id=turn_id,
            request_id=request_id,
            session_id=session.session_id,
            context_epoch=session.context_epoch,
            user_message_id=user_message.message_id,
            assistant_message_id=assistant_message.message_id,
            response_text=assistant_message.content,
            provider_id=provider_id,
            model_name=model_name,
            provider_call_performed=provider_call_performed,
            replayed=False,
            model_evidence_refs=model_evidence_refs,
            conversation_recall_trace=conversation_recall_trace,
        )

    def _replayed_result(
        self,
        *,
        turn_id: str,
        request_id: str,
        text: str,
        baseline: Mapping[str, object],
        user_message: object,
        assistant_message: object,
    ) -> Mapping[str, object]:
        if user_message is None or assistant_message is None:
            raise ValueError("Companion Chat request baseline is stale")
        session = self._repository.get_session(str(baseline["session_id"]))
        if session is None or session.project_id != self._project_id:
            raise ValueError("Companion Chat request baseline is stale")
        if (
            getattr(user_message, "content", None) != text
            or getattr(user_message, "session_id", None) != session.session_id
            or getattr(assistant_message, "session_id", None) != session.session_id
            or getattr(user_message, "project_id", None) != self._project_id
            or getattr(assistant_message, "project_id", None) != self._project_id
        ):
            raise ValueError("Companion Chat request baseline is stale")
        return self._result(
            turn_id=turn_id,
            request_id=request_id,
            session_id=session.session_id,
            context_epoch=session.context_epoch,
            user_message_id=str(getattr(user_message, "message_id")),
            assistant_message_id=str(getattr(assistant_message, "message_id")),
            response_text=str(getattr(assistant_message, "content")),
            provider_id="replayed",
            model_name="",
            provider_call_performed=False,
            replayed=True,
            model_evidence_refs=(),
            conversation_recall_trace={"status": "replayed", "episode_count": 0, "episodes": []},
        )

    def _result(
        self,
        *,
        turn_id: str,
        request_id: str,
        session_id: str,
        context_epoch: int,
        user_message_id: str,
        assistant_message_id: str,
        response_text: str,
        provider_id: str,
        model_name: str,
        provider_call_performed: bool,
        replayed: bool,
        model_evidence_refs: tuple[str, ...],
        conversation_recall_trace: Mapping[str, object],
    ) -> Mapping[str, object]:
        presentation = {
            "status": "completed",
            "request_id": request_id,
            "session_id": session_id,
            "context_epoch": context_epoch,
            "user_message_id": user_message_id,
            "assistant_message_id": assistant_message_id,
            "text": response_text,
            "provider_id": provider_id,
            "model_name": model_name,
            "replayed": replayed,
            "provider_call_performed": provider_call_performed,
            "conversation_recall": dict(conversation_recall_trace),
        }
        artifact = validate_turn_presentation_artifact(
            {"schema_version": "1.0.0", "kind": COMPANION_CHAT_OUTCOME, "content": presentation}
        )
        receipt_ref = self._receipt_store.put(turn_id, "companion-chat-receipt", artifact)
        return {
            "summary": "Companion Chat reply saved",
            "receipt_ref": receipt_ref,
            "payload_ref": None,
            "evidence_refs": [
                f"crp://{self._namespace_id}/companion/sessions/{session_id}",
                *model_evidence_refs,
            ],
            "result": artifact,
        }


class CompanionChatTurnPlanner:
    """Collect Companion context then request approved response persistence."""

    def plan(
        self,
        request: Mapping[str, object],
        events: Sequence[Mapping[str, object]],
        capabilities: Sequence[CapabilityDefinition],
        payloads: TurnPayloadStorePort,
        execution_control: ModelExecutionControlPort | None = None,
    ) -> Mapping[str, object]:
        completed = next((event for event in reversed(events) if _tool_completed(event, COMPANION_CHAT_MESSAGE_WRITE_CAPABILITY)), None)
        if completed is not None:
            data = completed.get("data")
            assert isinstance(data, Mapping)
            return {
                "type": "complete",
                "summary": "Companion Chat reply saved",
                "payload_ref": data.get("payload_ref"),
                "evidence_refs": list(data.get("evidence_refs") or ()),
            }
        context_event = next((event for event in reversed(events) if _tool_completed(event, COMPANION_CHAT_CONTEXT_CAPABILITY)), None)
        if context_event is None:
            text, session_id, target_memory_id, review_scope = _chat_input(request)
            arguments: dict[str, object] = {"request_id": str(request["operation_id"]), "text": text}
            if session_id is not None:
                arguments["session_id"] = session_id
            if target_memory_id is not None:
                arguments["target_memory_id"] = target_memory_id
            if review_scope is not None:
                arguments["review_scope"] = review_scope
            return {"type": "tool", "capability_id": COMPANION_CHAT_CONTEXT_CAPABILITY, "arguments": arguments}
        data = context_event.get("data")
        payload_ref = data.get("payload_ref") if isinstance(data, Mapping) else None
        if not isinstance(payload_ref, str):
            raise ValueError("Companion Chat context payload is unavailable")
        context = payloads.get(payload_ref)
        if not isinstance(context, Mapping) or context.get("kind") != "companion.chat.context":
            raise ValueError("Companion Chat context payload is invalid")
        return {
            "type": "tool",
            "capability_id": COMPANION_CHAT_MESSAGE_WRITE_CAPABILITY,
            "arguments": {
                "context": dict(context),
                "allow_remote": isinstance(request.get("privacy"), Mapping)
                and request["privacy"].get("allow_remote") is True,
            },
        }


def _scope_dependencies(
    container: object,
    runtime_root: Path,
    project_id: str,
) -> CompanionChatScopeDependencies:
    """Compose only the domain readers needed by one scoped chat Turn."""

    repository = CompanionRepository.at_data_root(runtime_root)
    clock = build_companion_clock()
    reducer = CompanionStateReducer(
        repository,
        rules_path=resolve_economy_rules_path(container),
        now=clock.now_utc,
        local_day=clock.local_day,
    )
    store = build_companion_object_store(runtime_root)

    def load_project_skill(target_project_id: str) -> Mapping[str, object] | None:
        # The context capability supplies its already validated Turn project
        # identity.  Rejecting a substituted identifier keeps this loader from
        # becoming an ambient cross-project reader.
        if target_project_id != project_id:
            raise ValueError("Companion Chat Project Skill is outside the Turn project scope")
        try:
            skill = AggregateRepositoryFactory(
                runtime_root=runtime_root,
                namespace_id=store.namespace_id,
                json_store=store,
            ).project_skill_repository().load(project_id)
        except AggregateRepositoryFactoryError:
            return None
        return dict(skill) if isinstance(skill, Mapping) else None

    memory_bridge = build_companion_memory_bridge(
        container,
        repository=repository,
        project_id=project_id,
    )
    return CompanionChatScopeDependencies(
        repository=repository,
        character_prompt_loader=lambda: load_active_character_prompt(runtime_root),
        state_projection_loader=lambda: companion_chat_state_context(reducer.project()),
        memory_recall_loader=memory_bridge.recall,
        project_skill_loader=load_project_skill,
    )


def _arguments(request: Mapping[str, object]) -> Mapping[str, object]:
    value = request.get("arguments")
    if not isinstance(value, Mapping):
        raise ValueError("capability arguments are required")
    return value


def _required(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    return value.strip()


def _project_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Companion Chat project id is invalid")
    return value.strip()


def _turn_project_scope(request: Mapping[str, object]) -> str:
    scope = request.get("scope")
    if not isinstance(scope, Mapping):
        raise ValueError("Companion Chat capability requires project scope")
    return _project_id(scope.get("project_id"))


def _prospective_session_id(request_id: str) -> str:
    return "session:" + hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:32]


def _profile_data(profile: object) -> Mapping[str, object]:
    if profile is None:
        return {}
    return {key: getattr(profile, key) for key in ("nickname", "birthday", "oc_address", "relationship", "custom_notes", "revision")}


def _chat_input(request: Mapping[str, object]) -> tuple[str, str | None, str | None, str | None]:
    input_payload = request.get("input")
    if not isinstance(input_payload, Mapping):
        raise ValueError("Companion Chat Turn input is required")
    text = _required(input_payload.get("text"), "text")
    session_id: str | None = None
    target_memory_id: str | None = None
    refs = input_payload.get("refs")
    if isinstance(refs, list):
        for item in refs:
            if not isinstance(item, Mapping):
                raise ValueError("Companion Chat Turn ref is invalid")
            kind = item.get("kind")
            if kind == "companion_session":
                if session_id is not None:
                    raise ValueError("Companion Chat Turn has duplicate session refs")
                session_id = _required(item.get("object_id"), "session_id")
            elif kind == "atom":
                if target_memory_id is not None:
                    raise ValueError("Companion Chat Turn has duplicate memory refs")
                target_memory_id = _required(item.get("object_id"), "memory_id")
            else:
                raise ValueError("Companion Chat Turn ref is unsupported")
    elif refs is not None:
        raise ValueError("Companion Chat Turn refs are invalid")
    review_scope: str | None = _MEMORY_REVIEW_PROMPTS.get(text)
    topic = _MEMORY_TOPIC_PROMPT.fullmatch(text)
    if topic is not None:
        review_scope = "memory_topic_discussion"
    if target_memory_id is not None and review_scope != "memory_topic_discussion":
        raise ValueError("Companion Chat memory anchor requires the controlled topic prompt")
    return text, session_id, target_memory_id, review_scope


def _context_identity(context: Mapping[str, object]) -> tuple[str, str, Mapping[str, object]]:
    request_id = _required(context.get("request_id"), "request_id")
    text = _required(context.get("text"), "text")
    baseline = context.get("baseline")
    if not isinstance(baseline, Mapping):
        raise ValueError("Companion Chat context baseline is invalid")
    required = ("session_id", "session_exists", "session_revision", "context_epoch", "prompt_revision", "profile_revision", "history")
    if any(key not in baseline for key in required):
        raise ValueError("Companion Chat context baseline is invalid")
    if not isinstance(baseline["session_exists"], bool):
        raise ValueError("Companion Chat context baseline is invalid")
    for key in ("session_id",):
        _required(baseline[key], key)
    for key in ("session_revision", "context_epoch", "prompt_revision", "profile_revision"):
        value = baseline[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("Companion Chat context baseline is invalid")
    if not isinstance(baseline["history"], list):
        raise ValueError("Companion Chat context baseline is invalid")
    return request_id, text, baseline


def _revalidate_authorities(
    *,
    repository: CompanionRepository,
    project_id: str,
    request_id: str,
    baseline: Mapping[str, object],
    prompt_revision: int,
    profile_revision: int,
) -> None:
    if baseline["prompt_revision"] != prompt_revision or baseline["profile_revision"] != profile_revision:
        raise ValueError("Companion Chat authority baseline is stale")
    if repository.get_message_by_request(request_id=request_id, role="user") is not None:
        raise ValueError("Companion Chat request baseline is stale")
    if repository.get_message_by_request(request_id=request_id, role="assistant") is not None:
        raise ValueError("Companion Chat request baseline is stale")
    session_id = str(baseline["session_id"])
    session = repository.get_session(session_id)
    if baseline["session_exists"] is False:
        if session is not None:
            raise ValueError("Companion Chat session baseline is stale")
        if baseline["session_revision"] != 0 or baseline["context_epoch"] != 1:
            raise ValueError("Companion Chat session baseline is stale")
        if baseline["history"] != []:
            raise ValueError("Companion Chat history baseline is stale")
        return
    if session is None or session.closed_at is not None or session.project_id != project_id:
        raise ValueError("Companion Chat session baseline is stale")
    if (
        session.revision != baseline["session_revision"]
        or session.context_epoch != baseline["context_epoch"]
        or session.prompt_revision != baseline["prompt_revision"]
        or session.profile_revision != baseline["profile_revision"]
    ):
        raise ValueError("Companion Chat session baseline is stale")
    actual_history = [
        {"message_id": item.message_id, "revision": item.revision}
        for item in repository.list_context_messages(
            session_id=session.session_id,
            context_epoch=session.context_epoch,
            project_id=project_id,
        )
    ]
    if actual_history != baseline["history"]:
        raise ValueError("Companion Chat history baseline is stale")


def _model_text(output: object) -> str:
    if isinstance(output, Mapping):
        value = output.get("text", output.get("answer"))
    else:
        value = output
    return _required(value, "Companion Chat model response")


def _tool_completed(event: Mapping[str, object], capability_id: str) -> bool:
    data = event.get("data")
    return event.get("type") == "tool.completed" and isinstance(data, Mapping) and data.get("capability_id") == capability_id


def _optional_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _memory_body(item: Mapping[str, object]) -> str:
    """Return the sole recall field that may enter a model prompt."""

    value = item.get("text", item.get("content"))
    return value.strip() if isinstance(value, str) else ""


def _message_id(role: str, request_id: str) -> str:
    return f"message:{role}:{_digest(request_id)}"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _stable_timestamp(request_id: str) -> str:
    # Stable timestamps make repository-level replay safe if a process stops
    # after a domain write but before the AI Turn event is persisted.
    offset_seconds = int(hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:8], 16)
    return datetime.fromtimestamp(1_700_000_000 + offset_seconds, tz=timezone.utc).isoformat()
