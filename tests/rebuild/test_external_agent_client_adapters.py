from __future__ import annotations

import pytest

from backend.api.external_agent_context_runtime import (
    EXTERNAL_AGENT_ADAPTERS,
    generate_external_agent_client_templates,
)
from core.ai_kernel import AgentAdapterProfile
from core.ai_kernel.external_agent_client_adapters import (
    ClientAdapterTemplateRegistry,
    ClientAdapterTemplate,
    ExternalAgentClientAdapterConflict,
    ExternalAgentClientAdapterError,
    create_client_adapter_template_registry,
    generate_client_templates,
    install,
    preview_install,
    preview_rollback,
    preview_upgrade,
    rollback,
    preview_uninstall,
    uninstall,
    upgrade,
)


def test_three_profiles_are_generated_from_the_runtime_profiles_with_one_contract():
    templates = _templates()

    assert set(templates) == {"codex", "claude", "workbuddy"}
    for adapter_id, template in templates.items():
        assert f"adapter_id: {adapter_id}" in template.content
        assert f"adapter_revision: {template.adapter_revision}" in template.content
        assert f"template_revision: {template.template_revision}" in template.content
        assert "start_session, resolve_context, get_changes, acknowledge_changes, submit_memory_proposal" in template.content
        assert "cookie" in template.content.lower()
        assert "C:\\" not in template.content
        assert "secret=" not in template.content.lower()


def test_production_generator_uses_the_single_runtime_profile_authority():
    templates = generate_external_agent_client_templates(target_ids=_target_ids())

    assert {
        key: (value.adapter_revision, value.template_revision)
        for key, value in templates.items()
    } == {
        profile.adapter_id: (profile.revision, profile.template_revision)
        for profile in EXTERNAL_AGENT_ADAPTERS
    }


def test_generation_fails_closed_when_runtime_profiles_or_opaque_targets_are_not_exact():
    with pytest.raises(ExternalAgentClientAdapterError, match="incomplete"):
        generate_client_templates(_profiles()[:2], target_ids={"codex": "codex-instructions", "claude": "claude-instructions"})
    with pytest.raises(ExternalAgentClientAdapterError, match="target"):
        generate_client_templates(_profiles(), target_ids={
            "codex": "C:/Users/private/AGENTS.md",
            "claude": "claude-instructions",
            "workbuddy": "workbuddy-instructions",
        })
    injected = list(_profiles())
    injected[0] = AgentAdapterProfile("codex", 1, "bad\nmarker", 65536, ("project_assistance",))
    with pytest.raises(ExternalAgentClientAdapterError, match="template revision"):
        generate_client_templates(injected, target_ids=_target_ids())


def test_preview_never_mutates_and_confirmed_install_only_owns_its_exact_marker_block():
    template = _templates()["codex"]
    user_text = "# my instructions\nKeep this text.\n"

    preview = preview_install(template, user_text)
    assert preview.action == "install_preview"
    assert preview.next_text.startswith(user_text)
    assert user_text == "# my instructions\nKeep this text.\n"

    with pytest.raises(ExternalAgentClientAdapterError, match="confirmation"):
        install(template, current_text=user_text, expected_current=user_text, confirm=False)
    installed = install(template, current_text=user_text, expected_current=user_text, confirm=True)
    assert installed.action == "installed"
    assert installed.next_text.startswith(user_text)
    assert installed.receipt == {
        "schema_version": "1.0.0", "action": "installed", "target_id": "codex-instructions",
        "adapter_id": "codex", "adapter_revision": 1, "template_revision": "openai-skill-map-v1",
        "managed_block": "chriptmas-os-external-agent-bridge",
        "placement": {
            "mode": "append_tail", "slot_id": "codex-project-instructions",
            "anchor_found": False, "degraded": True, "fact": "client_drift",
        },
    }
    assert user_text not in str(installed.receipt)


@pytest.mark.parametrize(
    ("adapter_id", "anchor"),
    (
        ("codex", "<!-- codex:project-instructions -->"),
        ("claude", "<!-- claude:project-context -->"),
        ("workbuddy", "<!-- workbuddy:memory-context -->"),
    ),
)
def test_each_client_uses_only_its_declared_instruction_slot(adapter_id, anchor):
    template = _templates()[adapter_id]
    original = f"# before\n{anchor}\n# after\n"

    preview = preview_install(template, original)

    assert preview.next_text == f"# before\n{anchor}\n{template.content}\n\n# after\n"
    assert preview.receipt["placement"] == {
        "mode": "declared_slot", "slot_id": template.slot_id,
        "anchor_found": True, "degraded": False,
    }
    installed = install(
        template, current_text=original, expected_current=original, confirm=True,
    )
    assert installed.next_text == preview.next_text
    assert uninstall(
        template, current_text=installed.next_text, expected_current=installed.next_text, confirm=True,
    ).next_text == original


def test_missing_anchor_degrades_to_append_tail_without_exposing_configuration_text():
    template = _templates()["workbuddy"]
    original = "# private user instructions\n"

    preview = preview_install(template, original)

    assert preview.next_text.startswith(original)
    assert preview.receipt["placement"] == {
        "mode": "append_tail", "slot_id": "workbuddy-memory-context",
        "anchor_found": False, "degraded": True, "fact": "client_drift",
    }
    assert original not in str(preview.receipt)


def test_unknown_or_duplicated_client_anchor_profiles_fail_closed():
    base = _templates()["codex"]
    unknown = ClientAdapterTemplate(
        adapter_id="unknown", adapter_revision=base.adapter_revision,
        template_revision=base.template_revision, target_id=base.target_id,
        content=base.content, slot_id="unknown-slot",
    )
    with pytest.raises(ExternalAgentClientAdapterError, match="unknown"):
        preview_install(unknown, "# user\n")
    duplicate_anchor = (
        "<!-- codex:project-instructions -->\n"
        "<!-- codex:project-instructions -->\n"
    )
    with pytest.raises(ExternalAgentClientAdapterConflict, match="anchor drifted"):
        preview_install(base, duplicate_anchor)


def test_install_and_uninstall_are_idempotent_but_drift_and_unconfirmed_mutations_fail_closed():
    template = _templates()["claude"]
    original = "# user-owned\n"
    installed = install(template, current_text=original, expected_current=original, confirm=True)

    replay = install(template, current_text=installed.next_text, expected_current=installed.next_text, confirm=True)
    assert replay.action == "install_replay"
    assert replay.next_text == installed.next_text
    with pytest.raises(ExternalAgentClientAdapterConflict, match="revision drifted"):
        install(template, current_text=installed.next_text + "changed\n", expected_current=installed.next_text, confirm=True)
    with pytest.raises(ExternalAgentClientAdapterConflict, match="managed block drifted"):
        preview_install(template, installed.next_text.replace("template_revision:", "tampered_revision:", 1))
    duplicate_marker = installed.next_text + "<!-- chriptmas-os-external-agent-bridge:claude:begin -->\n"
    with pytest.raises(ExternalAgentClientAdapterConflict, match="managed block drifted"):
        install(template, current_text=duplicate_marker, expected_current=duplicate_marker, confirm=True)
    with pytest.raises(ExternalAgentClientAdapterError, match="confirmation"):
        uninstall(template, current_text=installed.next_text, expected_current=installed.next_text, confirm=False)

    preview = preview_uninstall(template, installed.next_text)
    assert preview.action == "uninstall_preview"
    assert preview.next_text == original
    removed = uninstall(template, current_text=installed.next_text, expected_current=installed.next_text, confirm=True)
    assert removed.action == "uninstalled"
    assert removed.next_text == original
    with_user_text_after = installed.next_text + "# later user text\n"
    preserved = uninstall(
        template, current_text=with_user_text_after, expected_current=with_user_text_after, confirm=True,
    )
    assert preserved.next_text == original + "# later user text\n"
    replay_removed = uninstall(template, current_text=original, expected_current=original, confirm=True)
    assert replay_removed.action == "uninstall_replay"


def test_confirmed_v1_to_v2_upgrade_uses_registry_current_and_preserves_user_text():
    v1 = _templates()["codex"]
    v2 = _templates_v2()["codex"]
    original = "# user-owned instructions\n"
    v1_registry = _registry(current=(v1,))
    installed = install(
        v1, current_text=original, expected_current=original, confirm=True, registry=v1_registry,
    )
    assert installed.registry is not None
    v2_registry = _registry(
        current=(v2,), historical=(v1,), installed_history=installed.registry.installed_history,
    )

    preview = preview_upgrade(v2_registry, adapter_id="codex", current_text=installed.next_text)
    assert preview.action == "upgrade_preview"
    assert preview.next_text.startswith(original)
    assert v1.content not in preview.next_text
    assert v2.content in preview.next_text
    assert preview.receipt["previous_template_revision"] == v1.template_revision

    upgraded = upgrade(
        v2_registry, adapter_id="codex", current_text=installed.next_text,
        expected_current=installed.next_text, confirm=True,
    )
    assert upgraded.action == "upgraded"
    assert upgraded.next_text == preview.next_text
    assert upgraded.registry is not None
    assert upgraded.registry.installed_current_for("codex") == v2
    assert upgraded.receipt["template_revision"] == v2.template_revision
    assert original not in str(upgraded.receipt)


def test_upgrade_is_preview_only_and_any_confirmation_or_cas_or_block_failure_keeps_input_unchanged():
    v1 = _templates()["claude"]
    v2 = _templates_v2()["claude"]
    installed = install(v1, current_text="# user\n", expected_current="# user\n", confirm=True, registry=_registry(current=(v1,)))
    assert installed.registry is not None
    registry = _registry(current=(v2,), historical=(v1,), installed_history=installed.registry.installed_history)
    current = installed.next_text

    with pytest.raises(ExternalAgentClientAdapterError, match="confirmation"):
        upgrade(registry, adapter_id="claude", current_text=current, expected_current=current, confirm=False)
    with pytest.raises(ExternalAgentClientAdapterConflict, match="revision drifted"):
        upgrade(registry, adapter_id="claude", current_text=current, expected_current="# stale\n", confirm=True)
    tampered = current.replace("template_revision:", "tampered_revision:", 1)
    with pytest.raises(ExternalAgentClientAdapterConflict, match="managed block drifted"):
        upgrade(registry, adapter_id="claude", current_text=tampered, expected_current=tampered, confirm=True)
    assert current == installed.next_text


def test_rollback_requires_an_explicitly_installed_revision_and_is_cas_protected():
    v3 = _templates_v3()["workbuddy"]
    v1 = _templates()["workbuddy"]
    v2 = _templates_v2()["workbuddy"]
    original = "# user-owned\n"
    installed = install(v1, current_text=original, expected_current=original, confirm=True, registry=_registry(current=(v1,), historical=(v3,)))
    assert installed.registry is not None
    upgrade_registry = _registry(
        current=(v2,), historical=(v3, v1), installed_history=installed.registry.installed_history,
    )
    upgraded = upgrade(
        upgrade_registry, adapter_id="workbuddy", current_text=installed.next_text,
        expected_current=installed.next_text, confirm=True,
    )
    assert upgraded.registry is not None

    with pytest.raises(ExternalAgentClientAdapterConflict, match="was not installed"):
        preview_rollback(
            upgraded.registry, adapter_id="workbuddy", to_adapter_revision=v3.adapter_revision,
            to_template_revision=v3.template_revision, current_text=upgraded.next_text,
        )
    preview = preview_rollback(
        upgraded.registry, adapter_id="workbuddy", to_adapter_revision=v1.adapter_revision,
        to_template_revision=v1.template_revision, current_text=upgraded.next_text,
    )
    assert preview.action == "rollback_preview"
    assert v2.content not in preview.next_text
    assert v1.content in preview.next_text
    with pytest.raises(ExternalAgentClientAdapterError, match="confirmation"):
        rollback(
            upgraded.registry, adapter_id="workbuddy", to_adapter_revision=v1.adapter_revision,
            to_template_revision=v1.template_revision, current_text=upgraded.next_text,
            expected_current=upgraded.next_text, confirm=False,
        )
    rolled_back = rollback(
        upgraded.registry, adapter_id="workbuddy", to_adapter_revision=v1.adapter_revision,
        to_template_revision=v1.template_revision, current_text=upgraded.next_text,
        expected_current=upgraded.next_text, confirm=True,
    )
    assert rolled_back.action == "rolled_back"
    assert rolled_back.next_text == installed.next_text
    assert rolled_back.registry is not None
    assert rolled_back.registry.installed_current_for("workbuddy") == v1


def _templates():
    return generate_client_templates(_profiles(), target_ids=_target_ids())


def _templates_v3():
    return generate_client_templates(_profiles(version=3), target_ids=_target_ids())


def _templates_v2():
    return generate_client_templates(_profiles(version=2), target_ids=_target_ids())


def _registry(*, current, historical=(), installed_history=()) -> ClientAdapterTemplateRegistry:
    return create_client_adapter_template_registry(
        current_templates=current, historical_templates=historical, installed_history=installed_history,
    )


def _target_ids():
    return {
        "codex": "codex-instructions",
        "claude": "claude-instructions",
        "workbuddy": "workbuddy-instructions",
    }


def _profiles(*, version: int = 1):
    suffix = f"v{version}"
    return (
        AgentAdapterProfile("codex", version, f"openai-skill-map-{suffix}", 65536, ("project_assistance",)),
        AgentAdapterProfile("claude", version, f"claude-project-map-{suffix}", 65536, ("project_assistance",)),
        AgentAdapterProfile("workbuddy", version, f"workbuddy-memory-map-{suffix}", 65536, ("project_assistance",)),
    )
