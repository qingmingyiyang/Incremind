"""Explicit, reversible instruction-block templates for external Agent clients.

The module deliberately does not know client configuration paths and never
writes a configuration file.  A caller supplies configuration text, previews
the exact managed block, and can apply or remove that block only after an
explicit confirmation and an optimistic-concurrency check.  The Bridge remains
the only project-context and memory authority.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import re

from .external_agent_context import AgentAdapterProfile, ExternalAgentContextError


_TARGET_ID = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_TEMPLATE_REVISION = re.compile(r"^[a-z][a-z0-9._-]{0,127}$")
_CLIENTS = frozenset({"codex", "claude", "workbuddy"})
_BEGIN = "<!-- chriptmas-os-external-agent-bridge:{adapter_id}:begin -->"
_END = "<!-- chriptmas-os-external-agent-bridge:{adapter_id}:end -->"


@dataclass(frozen=True, slots=True)
class ClientAdapterSlotStrategy:
    """A fixed, client-declared instruction slot.

    This is deliberately a small allowlist, rather than a parser for arbitrary
    client configuration.  The adapter layer does not inspect requests or
    extract user text; it only knows where an already-authorized managed block
    may be placed in a caller-provided instruction document.
    """

    adapter_id: str
    slot_id: str
    anchor: str


_CLIENT_ADAPTER_SLOT_STRATEGIES: Mapping[str, ClientAdapterSlotStrategy] = {
    "codex": ClientAdapterSlotStrategy(
        "codex", "codex-project-instructions", "<!-- codex:project-instructions -->",
    ),
    "claude": ClientAdapterSlotStrategy(
        "claude", "claude-project-context", "<!-- claude:project-context -->",
    ),
    "workbuddy": ClientAdapterSlotStrategy(
        "workbuddy", "workbuddy-memory-context", "<!-- workbuddy:memory-context -->",
    ),
}


class ExternalAgentClientAdapterError(ExternalAgentContextError):
    """A client-template operation was invalid or cannot be safely applied."""


class ExternalAgentClientAdapterConflict(ExternalAgentClientAdapterError):
    """The user-owned configuration changed or the managed block drifted."""


@dataclass(frozen=True, slots=True)
class ClientAdapterTemplate:
    """A generated, client-specific view of one runtime adapter profile."""

    adapter_id: str
    adapter_revision: int
    template_revision: str
    target_id: str
    content: str
    slot_id: str = ""


@dataclass(frozen=True, slots=True)
class ManagedClientAdapterRevision:
    """An opaque revision identity retained after a confirmed mutation.

    The registry deliberately records identities, rather than configuration
    text or a filesystem location.  Its owner may persist this small state
    beside a client integration, but this module never does so itself.
    """

    target_id: str
    adapter_id: str
    adapter_revision: int
    template_revision: str


@dataclass(frozen=True, slots=True)
class ClientAdapterTemplateRegistry:
    """Version inventory plus the confirmed installed history for one caller.

    ``current_templates`` is the revision that an upgrade may select.  Older
    generated blocks remain in ``template_history`` so an explicit rollback
    can replace only a block that this registry proves was installed before.
    No operation infers a revision from user-owned text.
    """

    current_templates: tuple[ClientAdapterTemplate, ...]
    template_history: tuple[ClientAdapterTemplate, ...]
    installed_history: tuple[ManagedClientAdapterRevision, ...] = ()

    def __post_init__(self) -> None:
        current = tuple(self.current_templates)
        history = tuple(self.template_history)
        if not current or not history:
            raise ExternalAgentClientAdapterError("client adapter registry is incomplete")
        if len({_template_identity(template) for template in history}) != len(history):
            raise ExternalAgentClientAdapterError("client adapter registry history is ambiguous")
        if len({template.adapter_id for template in current}) != len(current):
            raise ExternalAgentClientAdapterError("client adapter registry current revisions are ambiguous")
        known = {_template_identity(template) for template in history}
        if any(_template_identity(template) not in known for template in current):
            raise ExternalAgentClientAdapterError("client adapter registry current revision is unknown")
        if any(_revision_identity(revision) not in known for revision in self.installed_history):
            raise ExternalAgentClientAdapterError("client adapter registry installed revision is unknown")

    def current_for(self, adapter_id: str) -> ClientAdapterTemplate:
        matches = [template for template in self.current_templates if template.adapter_id == adapter_id]
        if len(matches) != 1:
            raise ExternalAgentClientAdapterError("client adapter registry current revision is unavailable")
        return matches[0]

    def template_for(self, revision: ManagedClientAdapterRevision) -> ClientAdapterTemplate:
        matches = [template for template in self.template_history if _template_identity(template) == _revision_identity(revision)]
        if len(matches) != 1:
            raise ExternalAgentClientAdapterError("client adapter registry revision is unavailable")
        return matches[0]

    def installed_current_for(self, adapter_id: str) -> ClientAdapterTemplate:
        matches = [revision for revision in self.installed_history if revision.adapter_id == adapter_id]
        if not matches:
            raise ExternalAgentClientAdapterConflict("client adapter has no confirmed installed revision")
        return self.template_for(matches[-1])

    def records_installation(self, template: ClientAdapterTemplate) -> "ClientAdapterTemplateRegistry":
        _assert_template_is_known(self, template)
        revision = _revision_from_template(template)
        return ClientAdapterTemplateRegistry(
            current_templates=self.current_templates,
            template_history=self.template_history,
            installed_history=(*self.installed_history, revision),
        )


@dataclass(frozen=True, slots=True)
class ClientAdapterMutation:
    """A previewable mutation.  ``receipt`` intentionally excludes config text."""

    action: str
    target_id: str
    adapter_id: str
    adapter_revision: int
    template_revision: str
    next_text: str
    receipt: Mapping[str, object]
    registry: ClientAdapterTemplateRegistry | None = None


def generate_client_templates(
    adapter_profiles: Iterable[AgentAdapterProfile], *, target_ids: Mapping[str, str],
) -> dict[str, ClientAdapterTemplate]:
    """Generate templates from the Bridge runtime profiles, never a copy of them.

    All three supported clients must be provided once.  This makes a stale
    template revision fail closed rather than becoming a second revision source.
    ``target_ids`` are opaque caller-owned labels, not filesystem paths.
    """
    profiles = tuple(adapter_profiles)
    by_id = {profile.adapter_id: profile for profile in profiles}
    if len(by_id) != len(profiles) or set(by_id) != _CLIENTS:
        raise ExternalAgentClientAdapterError("client adapter profiles are incomplete")
    if set(target_ids) != _CLIENTS:
        raise ExternalAgentClientAdapterError("client adapter targets are incomplete")
    result: dict[str, ClientAdapterTemplate] = {}
    for adapter_id in sorted(_CLIENTS):
        target_id = target_ids[adapter_id]
        if not isinstance(target_id, str) or not _TARGET_ID.fullmatch(target_id):
            raise ExternalAgentClientAdapterError("client adapter target is invalid")
        profile = by_id[adapter_id]
        if not _TEMPLATE_REVISION.fullmatch(profile.template_revision):
            raise ExternalAgentClientAdapterError("client adapter template revision is invalid")
        result[adapter_id] = ClientAdapterTemplate(
            adapter_id=adapter_id,
            adapter_revision=profile.revision,
            template_revision=profile.template_revision,
            target_id=target_id,
            content=_render_block(profile),
            slot_id=_slot_strategy_for_adapter(adapter_id).slot_id,
        )
    return result


def create_client_adapter_template_registry(
    *, current_templates: Iterable[ClientAdapterTemplate], historical_templates: Iterable[ClientAdapterTemplate] = (),
    installed_history: Iterable[ManagedClientAdapterRevision] = (),
) -> ClientAdapterTemplateRegistry:
    """Create an in-memory governed revision registry without touching a client.

    A caller carries forward ``installed_history`` when it publishes a newer
    runtime profile.  This makes the v1 -> v2 transition explicit and gives a
    later rollback a concrete, confirmed target rather than a guessed old
    instruction block.
    """
    current = tuple(current_templates)
    history = (*tuple(historical_templates), *current)
    return ClientAdapterTemplateRegistry(
        current_templates=current,
        template_history=history,
        installed_history=tuple(installed_history),
    )


def preview_install(template: ClientAdapterTemplate, current_text: str) -> ClientAdapterMutation:
    """Return a preview only; callers retain responsibility for displaying it."""
    _slot_strategy_for_template(template)
    _validate_text(current_text)
    _assert_block_state(template, current_text, expected="absent")
    next_text, placement = _place_block(template, current_text)
    return _mutation("install_preview", template, next_text, placement=placement)


def install(
    template: ClientAdapterTemplate, *, current_text: str, expected_current: str,
    confirm: bool, registry: ClientAdapterTemplateRegistry | None = None,
) -> ClientAdapterMutation:
    """Return a confirmed install mutation after exact-text CAS validation."""
    _slot_strategy_for_template(template)
    _require_confirmation(confirm)
    _assert_expected_current(current_text, expected_current)
    updated_registry = _record_current_installation(registry, template)
    if _has_exact_block(template, current_text):
        _assert_block_state(template, current_text, expected="exact")
        return _mutation("install_replay", template, current_text, registry=updated_registry)
    _assert_block_state(template, current_text, expected="absent")
    next_text, placement = _place_block(template, current_text)
    return _mutation("installed", template, next_text, registry=updated_registry, placement=placement)


def preview_uninstall(template: ClientAdapterTemplate, current_text: str) -> ClientAdapterMutation:
    """Return an uninstall preview only if the exact generated block is present."""
    _slot_strategy_for_template(template)
    _validate_text(current_text)
    _assert_block_state(template, current_text, expected="exact")
    return _mutation("uninstall_preview", template, _remove_block(current_text, template.content))


def uninstall(
    template: ClientAdapterTemplate, *, current_text: str, expected_current: str,
    confirm: bool,
) -> ClientAdapterMutation:
    """Return a confirmed uninstall mutation while preserving all user-owned text."""
    _slot_strategy_for_template(template)
    _require_confirmation(confirm)
    _assert_expected_current(current_text, expected_current)
    if not _contains_markers(template, current_text):
        return _mutation("uninstall_replay", template, current_text)
    _assert_block_state(template, current_text, expected="exact")
    return _mutation("uninstalled", template, _remove_block(current_text, template.content))


def preview_upgrade(
    registry: ClientAdapterTemplateRegistry, *, adapter_id: str, current_text: str,
) -> ClientAdapterMutation:
    """Preview replacing the last confirmed revision with the registry current one."""
    _validate_text(current_text)
    previous = registry.installed_current_for(adapter_id)
    replacement = registry.current_for(adapter_id)
    _slot_strategy_for_template(previous)
    _slot_strategy_for_template(replacement)
    _assert_upgrade_pair(previous, replacement)
    _assert_block_state(previous, current_text, expected="exact")
    return _mutation(
        "upgrade_preview", replacement, _replace_block(current_text, previous, replacement),
        previous=previous,
    )


def upgrade(
    registry: ClientAdapterTemplateRegistry, *, adapter_id: str, current_text: str,
    expected_current: str, confirm: bool,
) -> ClientAdapterMutation:
    """Confirm a CAS-protected upgrade to the registry current revision."""
    _require_confirmation(confirm)
    _assert_expected_current(current_text, expected_current)
    previous = registry.installed_current_for(adapter_id)
    replacement = registry.current_for(adapter_id)
    _slot_strategy_for_template(previous)
    _slot_strategy_for_template(replacement)
    _assert_upgrade_pair(previous, replacement)
    _assert_block_state(previous, current_text, expected="exact")
    updated_registry = registry.records_installation(replacement)
    return _mutation(
        "upgraded", replacement, _replace_block(current_text, previous, replacement),
        previous=previous, registry=updated_registry,
    )


def preview_rollback(
    registry: ClientAdapterTemplateRegistry, *, adapter_id: str, to_adapter_revision: int,
    to_template_revision: str, current_text: str,
) -> ClientAdapterMutation:
    """Preview rollback only to a concrete revision recorded as installed."""
    _validate_text(current_text)
    current = registry.installed_current_for(adapter_id)
    target = _installed_rollback_target(
        registry, adapter_id=adapter_id, adapter_revision=to_adapter_revision,
        template_revision=to_template_revision,
    )
    _slot_strategy_for_template(current)
    _slot_strategy_for_template(target)
    _assert_upgrade_pair(current, target)
    _assert_block_state(current, current_text, expected="exact")
    return _mutation(
        "rollback_preview", target, _replace_block(current_text, current, target), previous=current,
    )


def rollback(
    registry: ClientAdapterTemplateRegistry, *, adapter_id: str, to_adapter_revision: int,
    to_template_revision: str, current_text: str, expected_current: str, confirm: bool,
) -> ClientAdapterMutation:
    """Confirm a CAS-protected rollback to a previously installed revision."""
    _require_confirmation(confirm)
    _assert_expected_current(current_text, expected_current)
    current = registry.installed_current_for(adapter_id)
    target = _installed_rollback_target(
        registry, adapter_id=adapter_id, adapter_revision=to_adapter_revision,
        template_revision=to_template_revision,
    )
    _slot_strategy_for_template(current)
    _slot_strategy_for_template(target)
    _assert_upgrade_pair(current, target)
    _assert_block_state(current, current_text, expected="exact")
    updated_registry = registry.records_installation(target)
    return _mutation(
        "rolled_back", target, _replace_block(current_text, current, target),
        previous=current, registry=updated_registry,
    )


def _render_block(profile: AgentAdapterProfile) -> str:
    begin = _BEGIN.format(adapter_id=profile.adapter_id)
    end = _END.format(adapter_id=profile.adapter_id)
    # These are instructions, not credentials or client-specific hook config.
    return "\n".join((
        begin,
        "# Chriptmas OS external-agent context bridge",
        f"adapter_id: {profile.adapter_id}",
        f"adapter_revision: {profile.revision}",
        f"template_revision: {profile.template_revision}",
        "Use only the local authenticated External Agent Context Bridge.",
        "Start a project_assistance session, consume its bounded startup map, then request only map-scoped context refs.",
        "Acknowledge the durable project change cursor after processing changes.",
        "Memory writes are proposal-only and require explicit user confirmation; never write project memory directly.",
        "Do not supply, request, persist, or copy secrets, cookies, tokens, absolute paths, raw configuration, or arbitrary URLs.",
        "Bridge contract: start_session, resolve_context, get_changes, acknowledge_changes, submit_memory_proposal.",
        end,
    ))


def _mutation(
    action: str, template: ClientAdapterTemplate, next_text: str, *,
    previous: ClientAdapterTemplate | None = None,
    registry: ClientAdapterTemplateRegistry | None = None,
    placement: Mapping[str, object] | None = None,
) -> ClientAdapterMutation:
    receipt: dict[str, object] = {
        "schema_version": "1.0.0",
        "action": action,
        "target_id": template.target_id,
        "adapter_id": template.adapter_id,
        "adapter_revision": template.adapter_revision,
        "template_revision": template.template_revision,
        "managed_block": "chriptmas-os-external-agent-bridge",
    }
    if previous is not None:
        receipt.update({
            "previous_adapter_revision": previous.adapter_revision,
            "previous_template_revision": previous.template_revision,
        })
    if placement is not None:
        receipt["placement"] = dict(placement)
    return ClientAdapterMutation(
        action=action,
        target_id=template.target_id,
        adapter_id=template.adapter_id,
        adapter_revision=template.adapter_revision,
        template_revision=template.template_revision,
        next_text=next_text,
        receipt=receipt,
        registry=registry,
    )


def _place_block(
    template: ClientAdapterTemplate, current_text: str,
) -> tuple[str, Mapping[str, object]]:
    """Insert at the declared client slot, or append with a visible downgrade.

    An absent client anchor is not a reason to overwrite or reject a user's
    configuration.  The caller receives a non-sensitive receipt fact so its UI
    can offer a later repair when the client profile changes.
    """
    strategy = _slot_strategy_for_template(template)
    anchor_count = current_text.count(strategy.anchor)
    if anchor_count == 1:
        anchor_end = current_text.index(strategy.anchor) + len(strategy.anchor)
        next_text = (
            f"{current_text[:anchor_end]}\n{template.content}\n{current_text[anchor_end:]}"
        )
        return next_text, {
            "mode": "declared_slot",
            "slot_id": strategy.slot_id,
            "anchor_found": True,
            "degraded": False,
        }
    if anchor_count == 0:
        return _append_block(current_text, template.content), {
            "mode": "append_tail",
            "slot_id": strategy.slot_id,
            "anchor_found": False,
            "degraded": True,
            "fact": "client_drift",
        }
    raise ExternalAgentClientAdapterConflict("client adapter instruction anchor drifted")


def _append_block(current_text: str, block: str) -> str:
    if not current_text:
        return f"{block}\n"
    separator = "" if current_text.endswith("\n") else "\n"
    return f"{current_text}{separator}\n{block}\n"


def _remove_block(current_text: str, block: str) -> str:
    if current_text.count(block) != 1:
        raise ExternalAgentClientAdapterConflict("client adapter managed block drifted")
    start = current_text.index(block)
    end = start + len(block)
    # Installation puts one separating newline before the block.  Remove only
    # that separator when it was added by us; text after the block remains
    # user-owned and byte-for-byte intact.
    if start > 0 and current_text[start - 1] == "\n":
        start -= 1
    if end < len(current_text) and current_text[end] == "\n":
        end += 1
    return current_text[:start] + current_text[end:]


def _replace_block(current_text: str, previous: ClientAdapterTemplate, replacement: ClientAdapterTemplate) -> str:
    if current_text.count(previous.content) != 1:
        raise ExternalAgentClientAdapterConflict("client adapter managed block drifted")
    return current_text.replace(previous.content, replacement.content, 1)


def _template_identity(template: ClientAdapterTemplate) -> tuple[str, str, int, str]:
    return (template.target_id, template.adapter_id, template.adapter_revision, template.template_revision)


def _revision_identity(revision: ManagedClientAdapterRevision) -> tuple[str, str, int, str]:
    return (revision.target_id, revision.adapter_id, revision.adapter_revision, revision.template_revision)


def _revision_from_template(template: ClientAdapterTemplate) -> ManagedClientAdapterRevision:
    return ManagedClientAdapterRevision(
        target_id=template.target_id,
        adapter_id=template.adapter_id,
        adapter_revision=template.adapter_revision,
        template_revision=template.template_revision,
    )


def _assert_template_is_known(registry: ClientAdapterTemplateRegistry, template: ClientAdapterTemplate) -> None:
    if _template_identity(template) not in {_template_identity(item) for item in registry.template_history}:
        raise ExternalAgentClientAdapterError("client adapter registry revision is unknown")


def _record_current_installation(
    registry: ClientAdapterTemplateRegistry | None, template: ClientAdapterTemplate,
) -> ClientAdapterTemplateRegistry | None:
    if registry is None:
        return None
    current = registry.current_for(template.adapter_id)
    if _template_identity(current) != _template_identity(template):
        raise ExternalAgentClientAdapterConflict("client adapter install is not the registry current revision")
    return registry.records_installation(template)


def _assert_upgrade_pair(previous: ClientAdapterTemplate, replacement: ClientAdapterTemplate) -> None:
    if previous.adapter_id != replacement.adapter_id or previous.target_id != replacement.target_id:
        raise ExternalAgentClientAdapterConflict("client adapter revision target drifted")
    if _template_identity(previous) == _template_identity(replacement):
        raise ExternalAgentClientAdapterConflict("client adapter revision is already current")


def _installed_rollback_target(
    registry: ClientAdapterTemplateRegistry, *, adapter_id: str, adapter_revision: int,
    template_revision: str,
) -> ClientAdapterTemplate:
    matches = [
        revision for revision in registry.installed_history
        if revision.adapter_id == adapter_id
        and revision.adapter_revision == adapter_revision
        and revision.template_revision == template_revision
    ]
    if not matches:
        raise ExternalAgentClientAdapterConflict("client adapter rollback revision was not installed")
    return registry.template_for(matches[-1])


def _has_exact_block(template: ClientAdapterTemplate, text: str) -> bool:
    return text.count(template.content) == 1 and _contains_markers(template, text)


def _contains_markers(template: ClientAdapterTemplate, text: str) -> bool:
    begin = _BEGIN.format(adapter_id=template.adapter_id)
    end = _END.format(adapter_id=template.adapter_id)
    return begin in text or end in text


def _assert_block_state(template: ClientAdapterTemplate, text: str, *, expected: str) -> None:
    begin = _BEGIN.format(adapter_id=template.adapter_id)
    end = _END.format(adapter_id=template.adapter_id)
    begin_count, end_count = text.count(begin), text.count(end)
    exact = _has_exact_block(template, text)
    if expected == "absent" and begin_count == end_count == 0:
        return
    if expected == "exact" and begin_count == end_count == 1 and exact:
        return
    raise ExternalAgentClientAdapterConflict("client adapter managed block drifted")


def _assert_expected_current(current_text: str, expected_current: str) -> None:
    _validate_text(current_text)
    _validate_text(expected_current)
    if current_text != expected_current:
        raise ExternalAgentClientAdapterConflict("client adapter configuration revision drifted")


def _require_confirmation(confirm: bool) -> None:
    if confirm is not True:
        raise ExternalAgentClientAdapterError("client adapter mutation requires explicit confirmation")


def _validate_text(value: str) -> None:
    if not isinstance(value, str):
        raise ExternalAgentClientAdapterError("client adapter configuration text is invalid")


def _slot_strategy_for_template(template: ClientAdapterTemplate) -> ClientAdapterSlotStrategy:
    strategy = _slot_strategy_for_adapter(template.adapter_id)
    if template.slot_id != strategy.slot_id:
        raise ExternalAgentClientAdapterError("client adapter slot strategy is invalid")
    return strategy


def _slot_strategy_for_adapter(adapter_id: str) -> ClientAdapterSlotStrategy:
    try:
        return _CLIENT_ADAPTER_SLOT_STRATEGIES[adapter_id]
    except KeyError as error:
        raise ExternalAgentClientAdapterError("client adapter profile is unknown") from error
