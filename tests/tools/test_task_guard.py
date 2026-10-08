from __future__ import annotations

from tools import task_guard as guard

SKIP = "@pytest.mark." + "skip"
ONLY = "it." + "only"
FAKE_KEY = "sk-" + "a1" * 12


def _diff(path: str, *lines: str) -> str:
    return "\n".join([f"diff --git a/{path} b/{path}", f"--- a/{path}", f"+++ b/{path}", "@@ -1 +1 @@", *lines])


def test_blocks_new_python_skip_marker() -> None:
    result = guard.scan_diff(_diff("tests/memory_app/test_x.py", "+" + SKIP + "(reason='later')"))
    assert any("skip" in error for error in result.errors)


def test_blocks_new_js_only_marker() -> None:
    result = guard.scan_diff(_diff("tests/frontend/x.test.jsx", "+" + ONLY + "('renders', () => {})"))
    assert result.errors


def test_skip_words_in_docs_are_allowed() -> None:
    result = guard.scan_diff(_diff("AGENTS.md", "+禁止新增 " + SKIP + " 标记"))
    assert result.errors == []


def test_blocks_secret_but_allows_test_placeholder() -> None:
    blocked = guard.scan_diff(_diff("src/backend/x.py", f"+KEY = '{FAKE_KEY}'"))
    allowed = guard.scan_diff(_diff("tests/x.py", "+KEY = 'sk-test-DO-NOT-LEAK'"))
    assert blocked.errors and "hidden" in blocked.errors[0]
    assert FAKE_KEY not in " ".join(blocked.errors)
    assert allowed.errors == []


def test_blocks_runtime_work_and_env_paths() -> None:
    result = guard.scan_diff("", ["runtime/secrets.json", "work/qa/a.png", ".env", ".env.example", "src/a.py"])
    assert len(result.errors) == 3


def test_removed_assertion_requires_commit_message_section(capsys) -> None:
    result = guard.scan_diff(_diff("tests/memory_app/test_x.py", "-    assert item['status'] == 'staged'"))
    assert result.removed_asserts
    assert guard._report(result, "Change policy\n") == 1
    assert guard._report(result, "Change policy\n\nAssertion changes:\n- staged -> confirmed per T2.4\n") == 0
    assert guard._report(result, None) == 0
    assert "Assertion changes:" in capsys.readouterr().out


def test_removed_assertion_outside_tests_is_ignored() -> None:
    result = guard.scan_diff(_diff("src/backend/x.py", "-    assert value"))
    assert result.removed_asserts == []
