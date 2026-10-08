"""The shared detector keeps commit-guard rules and redacts their matches."""
from importlib import import_module
from pathlib import Path
import subprocess
import sys

import pytest

from tools import task_guard


FAKE_KEY = "sk-" + "a1" * 12
HEADER = "-----" + "BEGIN PRIVATE KEY-----"
END = "-----" + "END PRIVATE KEY-----"


def shared():
    return import_module("backend.shared.secret_detection")


@pytest.mark.parametrize("text", [
    FAKE_KEY,
    "x " + "sk-" + "a" * 20,
    HEADER,
    "-----" + "BEGIN RSA PRIVATE KEY-----",
    "-----" + "BEGIN EC PRIVATE KEY-----",
    "-----" + "BEGIN OPENSSH PRIVATE KEY-----",
    "api_key = '" + "a" * 24 + "'",
    'SECRET: "' + "b" * 24 + '"',
    "access-token = '" + "c" * 24 + "'",
])
def test_all_original_secret_shapes_are_detected(text):
    assert shared().contains_secret(text) is True
    result = task_guard.scan_diff("+++ b/src/example.py\n+" + text)
    assert len(result.errors) == 1
    assert result.errors == ["src/example.py: possible secret added (value hidden)"]
    assert text not in result.errors[0]


@pytest.mark.parametrize("text", [
    "sk-test-DO-NOT-LEAK", "sk-" + "a" * 19, "xsk-" + "a" * 24,
    "api_key = '" + "a" * 23 + "'", "api_key = unquoted", "中文🙂\r\n普通正文",
])
def test_original_non_matches_remain_unchanged(text):
    assert shared().contains_secret(text) is False
    assert shared().redact_secrets(text) == text
    assert task_guard.scan_diff("+++ b/src/example.py\n+" + text).errors == []


def test_guard_imports_the_same_patterns_with_exact_original_flags():
    patterns = shared().SECRET_PATTERNS
    assert task_guard.SECRET_PATTERNS is patterns
    assert len(patterns) == 3
    assert [(p.pattern, p.flags) for p in patterns] == [
        (r"\bsk-(?!test-)[A-Za-z0-9_\-]{20,}", 32),
        (r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", 32),
        (r"(?i)\b(?:api[_-]?key|secret|access[_-]?token)\b\s*[:=]\s*[\"'][A-Za-z0-9_\-]{24,}[\"']", 34),
    ]


def test_redacts_multiple_secret_matches_without_rewriting_surrounding_bytes():
    value = "前🙂\r\n" + FAKE_KEY + "，后；api_key='" + "z" * 24 + "'\r尾"
    redacted = shared().redact_secrets(value)
    assert redacted == "前🙂\r\n[REDACTED_SECRET]，后；[REDACTED_SECRET]\r尾"
    assert shared().contains_secret(redacted) is False
    assert shared().redact_secrets(redacted) == redacted


@pytest.mark.parametrize("kind", ["", "RSA ", "EC ", "OPENSSH "])
def test_private_key_redaction_removes_body_and_matching_end_only(kind):
    begin = "-----" + "BEGIN " + kind + "PRIVATE KEY-----"
    end = "-----" + "END " + kind + "PRIVATE KEY-----"
    value = "先\r\n" + begin + "\r\nSYNTHETIC_PRIVATE_BODY\r\n" + end + "\r\n后"
    assert shared().redact_secrets(value) == "先\r\n[REDACTED_SECRET]\r\n后"


def test_unterminated_private_key_redacts_to_end_without_finding_new_shapes():
    assert shared().redact_secrets("先\n" + HEADER + "\nSYNTHETIC_BODY\n后") == "先\n[REDACTED_SECRET]"
    public = "-----" + "BEGIN PUBLIC KEY-----\nSYNTHETIC_PUBLIC_BODY"
    assert shared().redact_secrets(public) == public


def test_two_pem_blocks_and_overlapping_token_match_leave_surroundings_intact():
    value = "前" + HEADER + "\n" + FAKE_KEY + "\n" + END + "中" + HEADER + "\nBODY\n" + END + "后"
    assert shared().redact_secrets(value) == "前[REDACTED_SECRET]中[REDACTED_SECRET]后"


@pytest.mark.parametrize("value", [None, 4, [], {}])
def test_non_text_input_is_rejected(value):
    with pytest.raises(TypeError):
        shared().contains_secret(value)
    with pytest.raises(TypeError):
        shared().redact_secrets(value)


def test_real_standalone_guard_runs_without_pythonpath_or_hook_installation(tmp_path):
    shared()  # The shared capability must exist before checking standalone delivery.
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    source = tmp_path / "example.py"
    source.write_text("KEY = '" + FAKE_KEY + "'\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "example.py"], check=True, capture_output=True)
    script = Path(__file__).resolve().parents[3] / "tools" / "task_guard.py"
    blocked = subprocess.run([sys.executable, "-I", str(script), "--pre-commit"],
        cwd=tmp_path, capture_output=True, text=True)
    assert blocked.returncode == 1
    assert "possible secret added (value hidden)" in blocked.stdout
    assert FAKE_KEY not in blocked.stdout + blocked.stderr
    message = tmp_path / "message.txt"
    message.write_text("Keep the original guard behavior\n", encoding="utf-8")
    blocked_message = subprocess.run([sys.executable, "-I", str(script), "--commit-msg", str(message)],
        cwd=tmp_path, capture_output=True, text=True)
    assert blocked_message.returncode == 1
    assert "possible secret added (value hidden)" in blocked_message.stdout
    assert FAKE_KEY not in blocked_message.stdout + blocked_message.stderr
    source.write_text("KEY = 'sk-test-DO-NOT-LEAK'\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "example.py"], check=True, capture_output=True)
    allowed = subprocess.run([sys.executable, "-I", str(script), "--pre-commit"],
        cwd=tmp_path, capture_output=True, text=True)
    assert allowed.returncode == 0
    assert allowed.stdout == allowed.stderr == ""
    allowed_message = subprocess.run([sys.executable, "-I", str(script), "--commit-msg", str(message)],
        cwd=tmp_path, capture_output=True, text=True)
    assert allowed_message.returncode == 0
    assert allowed_message.stdout == allowed_message.stderr == ""
