"""Commit guard for plan execution (AGENTS.md sections 2.1 and 6).

Run automatically by the git hooks it installs:
    python tools/task_guard.py --install          # write .git/hooks/pre-commit and commit-msg
    python tools/task_guard.py --pre-commit       # scan the staged diff
    python tools/task_guard.py --commit-msg FILE  # also require "Assertion changes:" when asserts were removed

It blocks newly added test-skip markers, likely secrets, staged runtime/work/.env files,
and removed test assertions that the commit message does not explain.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Hooks execute this file directly, including from a different working
# directory. The shared leaf has no application or optional dependency import.
_SRC_ROOT = str(Path(__file__).resolve().parents[1] / "src")
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)
from backend.shared.secret_detection import SECRET_PATTERNS

CODE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
SELF_PATHS = {"tools/task_guard.py", "tests/tools/test_task_guard.py"}

SKIP_PATTERNS = (
    re.compile(r"@pytest\.mark\.(skip|skipif|xfail)\b"),
    re.compile(r"\bpytest\.(skip|xfail|importorskip)\("),
    re.compile(r"@unittest\.(skip|skipIf|skipUnless|expectedFailure)\b"),
    re.compile(r"\b(it|test|describe|suite)\.(skip|only|todo)\b"),
    re.compile(r"\b(xit|xdescribe|xtest|fit|fdescribe)\("),
)
ASSERT_PATTERN = re.compile(r"^\s*(assert\b|self\.assert\w*\(|expect\(|await expect\()")
MESSAGE_MARKER = "Assertion changes:"


@dataclass
class ScanResult:
    errors: list[str] = field(default_factory=list)
    removed_asserts: list[str] = field(default_factory=list)


def _is_test_path(path: str) -> bool:
    name = Path(path).name
    return path.startswith("tests/") or ".test." in name or name.startswith("test_")


def _forbidden_path(path: str) -> bool:
    name = Path(path).name
    return (path.startswith("runtime/") or path.startswith("work/")
            or name == ".env" or (name.endswith(".env") and not name.endswith(".env.example")))


def scan_diff(diff_text: str, staged_paths: list[str] | None = None) -> ScanResult:
    """Scan a `git diff --cached -U0` text. Pure function so it can be unit tested."""
    result = ScanResult()
    for path in staged_paths or ():
        if _forbidden_path(path):
            result.errors.append(f"{path}: runtime/, work/ and .env files must never be committed")
    current = ""
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            current = target[2:] if target.startswith("b/") else target
            continue
        if line.startswith("--- ") or not current:
            continue
        code_file = Path(current).suffix in CODE_SUFFIXES and current not in SELF_PATHS
        if line.startswith("+"):
            added = line[1:]
            if code_file and any(p.search(added) for p in SKIP_PATTERNS):
                result.errors.append(f"{current}: new skip/only/xfail marker: {added.strip()[:120]}")
            if current not in SELF_PATHS and any(p.search(added) for p in SECRET_PATTERNS):
                result.errors.append(f"{current}: possible secret added (value hidden)")
        elif line.startswith("-") and code_file and _is_test_path(current):
            removed = line[1:]
            if ASSERT_PATTERN.search(removed):
                result.removed_asserts.append(f"{current}: {removed.strip()[:120]}")
    return result


def _git(*args: str) -> str:
    completed = subprocess.run(["git", *args], capture_output=True, check=True)
    return completed.stdout.decode("utf-8", errors="replace")


def _staged() -> tuple[str, list[str]]:
    diff = _git("diff", "--cached", "-U0", "--no-color", "--no-ext-diff")
    paths = [p for p in _git("diff", "--cached", "--name-only", "--diff-filter=ACMR").splitlines() if p]
    return diff, paths


def _report(result: ScanResult, message: str | None) -> int:
    errors = list(result.errors)
    if message is not None and result.removed_asserts and MESSAGE_MARKER not in message:
        errors.append(
            f"{len(result.removed_asserts)} test assertion line(s) removed or changed; add an "
            f"'{MESSAGE_MARKER}' section to the commit message explaining each (AGENTS.md 2.1)."
        )
    if errors:
        print("task_guard: commit blocked (AGENTS.md 2.1 / 6). Fix the issues; do not use --no-verify.")
        for error in errors:
            print("  - " + error)
        for removed in result.removed_asserts[:20]:
            print("    removed assertion: " + removed)
        return 1
    if message is None and result.removed_asserts:
        print(f"task_guard: {len(result.removed_asserts)} assertion line(s) removed; "
              f"the commit message must include '{MESSAGE_MARKER}'.")
    return 0


_HOOK = """#!/bin/sh
root="$(git rev-parse --show-toplevel)"
if [ -x "$root/.venv/Scripts/python.exe" ]; then py="$root/.venv/Scripts/python.exe";
elif [ -x "$root/.venv/bin/python" ]; then py="$root/.venv/bin/python";
else py=python3; fi
exec "$py" "$root/tools/task_guard.py" {args}
"""


def install() -> int:
    hooks = Path(_git("rev-parse", "--git-path", "hooks").strip())
    hooks.mkdir(parents=True, exist_ok=True)
    for name, args in (("pre-commit", "--pre-commit"), ("commit-msg", '--commit-msg "$1"')):
        path = hooks / name
        path.write_text(_HOOK.replace("{args}", args), encoding="utf-8", newline="\n")
        path.chmod(0o755)
        print(f"installed {path}")
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["--install"]:
        return install()
    if argv[:1] == ["--commit-msg"] and len(argv) == 2:
        diff, paths = _staged()
        message = Path(argv[1]).read_text(encoding="utf-8", errors="replace")
        return _report(scan_diff(diff, paths), message)
    if argv[:1] in (["--pre-commit"], []):
        diff, paths = _staged()
        return _report(scan_diff(diff, paths), None)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
