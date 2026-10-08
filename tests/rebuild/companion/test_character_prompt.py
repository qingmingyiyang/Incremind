from __future__ import annotations

from core.companion_core import (
    DEFAULT_CHARACTER_PROMPT,
    DEFAULT_CHARACTER_PROMPT_REVISION,
    default_character_prompt,
)


def test_default_character_prompt_is_detailed_revisioned_domain_policy() -> None:
    content, revision = default_character_prompt()
    assert content == DEFAULT_CHARACTER_PROMPT
    assert revision == DEFAULT_CHARACTER_PROMPT_REVISION == 2
    assert all(term in content for term in (
        "关系与称呼",
        "表达风格",
        "事实边界",
        "相处原则",
        "不假装看见屏幕",
    ))
    assert "MASTER_PROFILE_DATA" in content


def test_default_character_prompt_contract_is_stable_and_non_empty() -> None:
    first = default_character_prompt()
    second = default_character_prompt()
    assert first == second
    assert first[0].strip() == first[0]
    assert len(first[0]) > 300
