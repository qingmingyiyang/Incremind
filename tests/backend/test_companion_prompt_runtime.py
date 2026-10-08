from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend import companion_prompt_runtime
from core.companion_core import CHARACTER_PROMPT_ID, default_character_prompt


def test_active_character_prompt_keeps_user_content_and_revision_priority(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = SimpleNamespace(prompt_activation={"revision": 17})

    class ActiveConfig:
        def __init__(self, store):
            assert store == "store"

        def execute(self):
            return config

    monkeypatch.setattr(companion_prompt_runtime, "build_companion_object_store", lambda root: "store" if root == tmp_path else None)
    monkeypatch.setattr(companion_prompt_runtime, "GetDeveloperStudioConfig", ActiveConfig)
    monkeypatch.setattr(
        companion_prompt_runtime,
        "resolve_active_prompt",
        lambda value, prompt_id: {"content": "  请叫我自定义角色，并使用粤语。  "}
        if value is config and prompt_id == CHARACTER_PROMPT_ID else None,
    )

    assert companion_prompt_runtime.load_active_character_prompt(tmp_path) == ("请叫我自定义角色，并使用粤语。", 17)


def test_active_character_prompt_revision_has_a_minimum_of_one(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = SimpleNamespace(prompt_activation={"revision": 0})
    monkeypatch.setattr(companion_prompt_runtime, "build_companion_object_store", lambda _root: object())
    monkeypatch.setattr(companion_prompt_runtime, "GetDeveloperStudioConfig", lambda _store: SimpleNamespace(execute=lambda: config))
    monkeypatch.setattr(companion_prompt_runtime, "resolve_active_prompt", lambda *_args: {"content": "有效角色"})
    assert companion_prompt_runtime.load_active_character_prompt(tmp_path) == ("有效角色", 1)


@pytest.mark.parametrize("failure", ["store", "config", "resolve", "revision", "empty"])
def test_prompt_runtime_fails_safe_to_domain_policy(
    tmp_path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    config = SimpleNamespace(prompt_activation={"revision": "invalid" if failure == "revision" else 9})

    def build_store(_root):
        if failure == "store":
            raise OSError("store unavailable")
        return object()

    class ConfigReader:
        def __init__(self, _store):
            pass

        def execute(self):
            if failure == "config":
                raise RuntimeError("config unavailable")
            return config

    def resolve(*_args):
        if failure == "resolve":
            raise ValueError("activation invalid")
        return {"content": "   " if failure == "empty" else "有效角色"}

    monkeypatch.setattr(companion_prompt_runtime, "build_companion_object_store", build_store)
    monkeypatch.setattr(companion_prompt_runtime, "GetDeveloperStudioConfig", ConfigReader)
    monkeypatch.setattr(companion_prompt_runtime, "resolve_active_prompt", resolve)

    assert companion_prompt_runtime.load_active_character_prompt(tmp_path) == default_character_prompt()
