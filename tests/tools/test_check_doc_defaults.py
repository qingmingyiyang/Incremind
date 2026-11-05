from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from tools import check_doc_defaults as defaults


ROOT = Path(__file__).resolve().parents[2]


def _copy_inputs(target: Path) -> dict[str, str]:
    for name in (*defaults.DOCUMENTS, defaults.POLICY_PATH):
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, destination)
    return defaults.read_inputs(target)


def _set_current_block(path: Path, block: str) -> None:
    text = path.read_text(encoding="utf-8")
    before, _, rest = text.partition(defaults.START_MARKER)
    _, _, after = rest.partition(defaults.END_MARKER)
    path.write_text(before + defaults.START_MARKER + "\n" + block + "\n"
                    + defaults.END_MARKER + after, encoding="utf-8")


def test_repository_current_defaults_match_active() -> None:
    assert defaults.check_defaults(defaults.read_inputs(ROOT)) == []


@pytest.mark.parametrize("document", defaults.DOCUMENTS)
def test_inconsistent_current_document_is_rejected(tmp_path: Path, document: str) -> None:
    _copy_inputs(tmp_path)
    _set_current_block(tmp_path / document,
                       "Current defaults: compose@999 / ladder@15 / retrieve@9 / rerank@2")
    errors = defaults.check_defaults(defaults.read_inputs(tmp_path))
    assert any(document in error and "compose@999" in error for error in errors)


def test_active_is_read_from_policy_file(tmp_path: Path) -> None:
    inputs = _copy_inputs(tmp_path)
    selected = defaults.read_active(inputs[defaults.POLICY_PATH])
    policy = tmp_path / defaults.POLICY_PATH
    source = policy.read_text(encoding="utf-8")
    old = f"'compose': '{selected['compose']}'"
    assert source.count(old) == 1
    policy.write_text(source.replace(old, "'compose': '@999'"), encoding="utf-8")
    errors = defaults.check_defaults(defaults.read_inputs(tmp_path))
    assert len(errors) == 3
    assert all("compose@999" in error for error in errors)


def test_historical_results_outside_current_block_are_preserved(tmp_path: Path) -> None:
    _copy_inputs(tmp_path)
    for document in defaults.DOCUMENTS:
        path = tmp_path / document
        path.write_text(path.read_text(encoding="utf-8")
                        + "\nHistorical evaluation: compose@1 / ladder@2 / retrieve@3 / rerank@4.\n",
                        encoding="utf-8")
    assert defaults.check_defaults(defaults.read_inputs(tmp_path)) == []


@pytest.mark.parametrize("block", [
    "compose@34 / ladder@15 / retrieve@9",
    "compose@34 / compose@34 / ladder@15 / retrieve@9 / rerank@2",
    "compose@34 / ladder@15 / retrieve@9 / rerank@2 / extract@5",
    "compose@0 / ladder@15 / retrieve@9 / rerank@2",
])
def test_incomplete_or_ambiguous_current_block_is_rejected(tmp_path: Path, block: str) -> None:
    _copy_inputs(tmp_path)
    _set_current_block(tmp_path / defaults.DOCUMENTS[0], block)
    assert defaults.check_defaults(defaults.read_inputs(tmp_path))


@pytest.mark.parametrize("extra", [False, True])
def test_malformed_version_cannot_match_a_valid_prefix(tmp_path: Path, extra: bool) -> None:
    inputs = _copy_inputs(tmp_path)
    active = defaults.read_active(inputs[defaults.POLICY_PATH])
    block = " / ".join(name + active[name] for name in defaults.INTERFACES)
    invalid = "compose" + active["compose"] + ".1"
    block = block + " / " + invalid if extra else block.replace("compose" + active["compose"], invalid)
    _set_current_block(tmp_path / "README.md", block)
    assert defaults.check_defaults(defaults.read_inputs(tmp_path))


@pytest.mark.parametrize("markers", [
    "",
    defaults.START_MARKER,
    defaults.END_MARKER + defaults.START_MARKER,
    defaults.START_MARKER + defaults.START_MARKER + defaults.END_MARKER,
])
def test_missing_duplicate_or_reversed_markers_are_rejected(tmp_path: Path, markers: str) -> None:
    _copy_inputs(tmp_path)
    (tmp_path / defaults.DOCUMENTS[0]).write_text(markers, encoding="utf-8")
    assert defaults.check_defaults(defaults.read_inputs(tmp_path))


def test_cli_reports_real_document_mismatch_and_correction(tmp_path: Path) -> None:
    _copy_inputs(tmp_path)
    command = [sys.executable, str(ROOT / "tools/check_doc_defaults.py"), "--root", str(tmp_path)]
    passed = subprocess.run(command, capture_output=True, text=True, check=False)
    assert passed.returncode == 0
    assert "match ACTIVE" in passed.stdout
    _set_current_block(tmp_path / "README.md", "compose@999 / ladder@15 / retrieve@9 / rerank@2")
    rejected = subprocess.run(command, capture_output=True, text=True, check=False)
    assert rejected.returncode == 1
    assert "README.md" in rejected.stdout
    shutil.copyfile(ROOT / "README.md", tmp_path / "README.md")
    corrected = subprocess.run(command, capture_output=True, text=True, check=False)
    assert corrected.returncode == 0


@pytest.mark.parametrize("hook", ["--pre-commit", "--commit-msg"])
@pytest.mark.parametrize("directory", ["", "src"])
def test_guard_uses_staged_documents_and_policy(tmp_path: Path, hook: str, directory: str) -> None:
    _copy_inputs(tmp_path)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    message = tmp_path / "message.txt"
    message.write_text("Check real staged defaults\n", encoding="utf-8")
    command = [sys.executable, str(ROOT / "tools/task_guard.py"), hook]
    if hook == "--commit-msg":
        command.append(str(message))
    working_directory = tmp_path / directory
    passed = subprocess.run(command, cwd=working_directory, capture_output=True, text=True, check=False)
    assert passed.returncode == 0
    _set_current_block(tmp_path / "README.md", "compose@999 / ladder@15 / retrieve@9 / rerank@2")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    shutil.copyfile(ROOT / "README.md", tmp_path / "README.md")
    rejected = subprocess.run(command, cwd=working_directory, capture_output=True, text=True, check=False)
    assert rejected.returncode == 1
    assert "README.md" in rejected.stdout and "compose@999" in rejected.stdout
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    corrected = subprocess.run(command, cwd=working_directory, capture_output=True, text=True, check=False)
    assert corrected.returncode == 0


@pytest.mark.parametrize("directory", ["", "src"])
def test_guard_rejects_staged_policy_change_hidden_by_working_file(tmp_path: Path, directory: str) -> None:
    inputs = _copy_inputs(tmp_path)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    policy = tmp_path / defaults.POLICY_PATH
    active = defaults.read_active(inputs[defaults.POLICY_PATH])
    source = policy.read_text(encoding="utf-8")
    policy.write_text(source.replace(f"'compose': '{active['compose']}'", "'compose': '@999'"),
                      encoding="utf-8")
    subprocess.run(["git", "add", defaults.POLICY_PATH], cwd=tmp_path, check=True)
    shutil.copyfile(ROOT / defaults.POLICY_PATH, policy)
    rejected = subprocess.run([sys.executable, str(ROOT / "tools/task_guard.py"), "--pre-commit"],
                              cwd=tmp_path / directory, capture_output=True, text=True, check=False)
    assert rejected.returncode == 1
    assert "compose@999" in rejected.stdout


def test_guard_rejects_missing_staged_document(tmp_path: Path) -> None:
    _copy_inputs(tmp_path)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "add", defaults.POLICY_PATH], cwd=tmp_path, check=True)
    rejected = subprocess.run([sys.executable, str(ROOT / "tools/task_guard.py"), "--pre-commit"],
                              cwd=tmp_path, capture_output=True, text=True, check=False)
    assert rejected.returncode == 1
    assert "README.md" in rejected.stdout and "missing from the index" in rejected.stdout
