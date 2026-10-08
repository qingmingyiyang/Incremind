from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

import core.external_extension_runtime.artifact_evidence as artifact_evidence
from core.external_extension_runtime.artifact_evidence import (
    ArtifactEvidenceError,
    ImmutableQuarantineArtifactStore,
    _new_evidence,
    artifact_receipt_ref,
)
from core.external_extension_runtime.windows_handle_io import (
    WindowsHandleIoError,
    WindowsHandleTreeIo,
)
from core.external_extension_runtime.fact_store import (
    ExternalExtensionFactConflict,
    ExternalExtensionFactStore,
    artifact_ref,
    resolution_ref,
)
from core.external_extensions import ArtifactInventory, ResolvedSource, parse_install_intent
from core.storage_provider import SQLiteStructuredRecordStore


REVISION = "a" * 40
OPERATION = "acquire-operation-1001"


def _source(*, revision: str = REVISION, artifact_ref: str = "crp://extension-artifacts/fixture/1001") -> ResolvedSource:
    return ResolvedSource(
        source_kind="github_repository",
        canonical_locator="https://github.com/example/fixture",
        immutable_revision=revision,
        artifact_ref=artifact_ref,
    )


def _inventory() -> ArtifactInventory:
    return ArtifactInventory.capture(
        {
            "SKILL.md": b"---\nname: fixture\ndescription: Fixture.\n---\n",
            "resources/example.txt": b"immutable evidence\n",
        }
    )


def _operation_directory(root: Path, container: str, operation: str = OPERATION) -> Path:
    return root / container / artifact_evidence._operation_component(operation)


def _facts(tmp_path: Path) -> ExternalExtensionFactStore:
    tmp_path.mkdir(parents=True, exist_ok=True)
    database = tmp_path / "facts.sqlite3"
    return ExternalExtensionFactStore(
        SQLiteStructuredRecordStore(database),
        ImmutableQuarantineArtifactStore(tmp_path / "quarantine"),
    )


def _record_intake(facts: ExternalExtensionFactStore) -> tuple[str, ResolvedSource]:
    intent = parse_install_intent(
        "install https://github.com/example/fixture",
        intent_id="install-intent-1001",
        requested_ref=REVISION,
    )
    source = ResolvedSource(
        source_kind="github_repository",
        canonical_locator="https://github.com/example/fixture",
        immutable_revision=REVISION,
        artifact_ref=artifact_ref(intent.intent_id, OPERATION),
    )
    facts.record_intent(intent, command_id="record-intent-1001")
    facts.record_resolution_observation(
        operation_id=OPERATION,
        intent_reference="crp://external-extension-install-intents/install-intent-1001",
        source=source,
    )
    facts.record_resolution(
        operation_id=OPERATION,
        intent_reference="crp://external-extension-install-intents/install-intent-1001",
        source=source,
    )
    facts.commit_artifact(operation_id=OPERATION, source=source, inventory=_inventory())
    return (
        facts.record_intake(
            operation_id=OPERATION,
            resolution_reference=resolution_ref(OPERATION),
            manifest=None,
            review_plan=None,
            quarantine_code="rejected",
        ),
        source,
    )


def test_commit_is_idempotent_and_receipt_has_no_absolute_machine_path(tmp_path: Path) -> None:
    root = tmp_path / "quarantine"
    store = ImmutableQuarantineArtifactStore(root)

    first = store.commit(OPERATION, _source(), _inventory())
    second = store.commit(OPERATION, _source(), _inventory())

    assert first == second
    assert first.operation_id == OPERATION
    assert first.artifact_ref == "crp://extension-artifacts/fixture/1001"
    assert first.source_revision == REVISION
    assert first.artifact_receipt_ref == artifact_receipt_ref(OPERATION)
    assert first.inventory == _inventory()
    assert len(first.content_sha256) == 64
    assert first.file_count == 2
    assert first.total_bytes == _inventory().total_bytes

    receipt = _operation_directory(root, "operations") / "receipt.json"
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert payload["artifact_receipt_ref"] == artifact_receipt_ref(OPERATION)
    assert str(root) not in receipt.read_text(encoding="utf-8")
    assert "canonical_locator" not in payload
    assert "https://" not in receipt.read_text(encoding="utf-8")

    artifact_file = _operation_directory(root, "operations") / "artifact" / "SKILL.md"
    if os.name != "nt":
        assert stat.S_IMODE(artifact_file.stat().st_mode) & 0o111 == 0


def test_existing_evidence_or_source_drift_fails_closed(tmp_path: Path) -> None:
    store = ImmutableQuarantineArtifactStore(tmp_path / "quarantine")
    store.commit(OPERATION, _source(), _inventory())

    with pytest.raises(ArtifactEvidenceError, match="drifted"):
        store.commit(OPERATION, _source(revision="b" * 40), _inventory())
    with pytest.raises(ArtifactEvidenceError, match="drifted"):
        store.commit(OPERATION, _source(), ArtifactInventory.capture({"SKILL.md": b"other"}))


def test_probe_rehashes_bytes_and_rejects_tampered_tree(tmp_path: Path) -> None:
    root = tmp_path / "quarantine"
    store = ImmutableQuarantineArtifactStore(root)
    store.commit(OPERATION, _source(), _inventory())
    target = _operation_directory(root, "operations") / "artifact" / "SKILL.md"
    os.chmod(target, 0o600)
    target.write_bytes(b"x" * len(_inventory().read_bytes("SKILL.md")))
    os.chmod(target, 0o400)

    with pytest.raises(ArtifactEvidenceError, match="digest drifted"):
        store.probe(OPERATION, _source())


def test_receipt_reader_rejects_bytes_over_64_kib(tmp_path: Path) -> None:
    root = tmp_path / "quarantine"
    store = ImmutableQuarantineArtifactStore(root)
    store.commit(OPERATION, _source(), _inventory())
    receipt = _operation_directory(root, "operations") / "receipt.json"
    os.chmod(receipt, 0o600)
    receipt.write_bytes(b"x" * (artifact_evidence._MAX_RECEIPT_BYTES + 1))
    os.chmod(receipt, 0o400)

    with pytest.raises(ArtifactEvidenceError, match="receipt"):
        store.load_final(OPERATION, _source())


def test_windows_handle_read_returns_frozen_inventory_and_next_read_detects_drift(tmp_path: Path) -> None:
    """The returned evidence owns bytes; later pathname changes cannot alter it."""
    if os.name != "nt":
        pytest.skip("Windows HANDLE quarantine path is Windows-only")
    root = tmp_path / "quarantine"
    store = ImmutableQuarantineArtifactStore(root)
    store.commit(OPERATION, _source(), _inventory())

    first = store.load_final(OPERATION, _source())
    assert first is not None
    target = _operation_directory(root, "operations") / "artifact" / "SKILL.md"
    os.chmod(target, 0o600)
    target.write_bytes(b"x" * len(first.inventory.read_bytes("SKILL.md")))
    os.chmod(target, 0o400)

    assert first.inventory.read_bytes("SKILL.md") == _inventory().read_bytes("SKILL.md")
    with pytest.raises(ArtifactEvidenceError, match="digest drifted"):
        store.load_final(OPERATION, _source())


@pytest.mark.parametrize(
    "operation",
    (
        "effect:artifact-1001",
        "CON.12345",
        "trailing-operation.",
        OPERATION,
    ),
)
def test_operation_component_encodes_every_windows_operation_identity(
    operation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(artifact_evidence.os, "name", "nt")

    component = artifact_evidence._operation_component(operation)

    assert component.startswith("operation-")
    assert component == artifact_evidence._operation_component(operation)
    assert component != operation
    assert "/" not in component
    assert "\\" not in component
    assert component.rstrip(". ") == component


def test_windows_handle_store_encodes_windows_operation_identity(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("Windows HANDLE quarantine path is Windows-only")
    operation = "effect:artifact-1001"
    root = tmp_path / "quarantine"
    store = ImmutableQuarantineArtifactStore(root)

    evidence = store.commit(operation, _source(), _inventory())

    assert evidence.operation_id == operation
    assert store.load_final(operation, _source()) == evidence
    final_names = [path.name for path in (root / "operations").iterdir()]
    assert len(final_names) == 1
    assert final_names == [artifact_evidence._operation_component(operation)]


def test_operation_component_keeps_posix_colon_operation_path_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operation = "effect:artifact-1001"
    monkeypatch.setattr(artifact_evidence.os, "name", "posix")

    assert artifact_evidence._operation_component(operation) == operation
    store = object.__new__(ImmutableQuarantineArtifactStore)
    store._root = tmp_path  # type: ignore[attr-defined]
    assert store._final_dir(operation) == tmp_path / "operations" / operation
    assert store._pending_dir(operation) == tmp_path / ".pending" / operation


def _make_junction(link: Path, target: Path) -> None:
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("real Windows junction creation is unavailable on this host")


class _JunctionSwapHandleIo(WindowsHandleTreeIo):
    """Swap a managed name immediately before its HANDLE-relative open."""

    def __init__(self, link: Path, target: Path, *, name: str, occurrence: int) -> None:
        super().__init__()
        self._link = link
        self._target = target
        self._name = name
        self._occurrence = occurrence
        self._seen = 0

    def _before_child_open(self, _parent_handle: int, name: str) -> None:
        if name != self._name:
            return
        self._seen += 1
        if self._seen != self._occurrence:
            return
        shutil.rmtree(self._link)
        _make_junction(self._link, self._target)


def test_windows_handle_load_final_rejects_real_artifact_junction_swap(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("Windows HANDLE quarantine path is Windows-only")
    root = tmp_path / "quarantine"
    writer = ImmutableQuarantineArtifactStore(root)
    writer.commit(OPERATION, _source(), _inventory())
    outside = tmp_path / "outside-artifact"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("must not be read", encoding="utf-8")
    artifact = _operation_directory(root, "operations") / "artifact"
    io = _JunctionSwapHandleIo(artifact, outside, name="artifact", occurrence=1)
    raced = ImmutableQuarantineArtifactStore(root, handle_io=io)

    with pytest.raises(ArtifactEvidenceError, match="unsafe"):
        raced.load_final(OPERATION, _source())

    assert sentinel.read_text(encoding="utf-8") == "must not be read"
    artifact.rmdir()  # Remove the junction itself; never the outside target.


def test_windows_handle_promotion_rejects_real_target_parent_junction_swap(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("Windows HANDLE quarantine path is Windows-only")
    root = tmp_path / "quarantine"
    writer = ImmutableQuarantineArtifactStore(root)
    evidence = _new_evidence(OPERATION, _source(), _inventory())
    writer._write_pending(writer._pending_dir(OPERATION), evidence)
    outside = tmp_path / "outside-operations"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("must not be written", encoding="utf-8")
    operations = root / "operations"
    # First open is probe(final); second is promotion's target parent open.
    io = _JunctionSwapHandleIo(operations, outside, name="operations", occurrence=2)
    raced = ImmutableQuarantineArtifactStore(root, handle_io=io)

    with pytest.raises(ArtifactEvidenceError):
        raced.probe(OPERATION, _source())

    assert sentinel.read_text(encoding="utf-8") == "must not be written"
    assert not (outside / OPERATION).exists()
    assert writer._pending_dir(OPERATION).is_dir()
    operations.rmdir()  # Remove the junction itself; never the outside target.


class _ReparseRaceHandleIo:
    """Narrow store seam: a HANDLE walk saw a reparse swap before any read."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def ensure_directory_chain(self, _root: Path, parts: tuple[str, ...]) -> None:
        self.calls.append(("ensure", parts))

    def read_bounded_tree(self, _root: Path, parts: tuple[str, ...], **_limits: object) -> dict[str, bytes]:
        self.calls.append(("read", parts))
        raise WindowsHandleIoError("reparse point encountered")

    def write_new_tree(self, _root: Path, parts: tuple[str, ...], _files: object) -> bool:
        self.calls.append(("write", parts))
        raise AssertionError("a reparse failure must not fall through to writing")

    def move_dir_no_replace(self, _root: Path, source: tuple[str, ...], _parent: tuple[str, ...], _name: str) -> None:
        self.calls.append(("move", source))
        raise AssertionError("a reparse failure must not fall through to promotion")


def test_windows_handle_reparse_swap_fails_closed_without_external_sentinel_read(tmp_path: Path) -> None:
    """A root/artifact junction race is rejected by the HANDLE reader seam.

    Creating a real junction needs host-specific privileges, so this checks the
    store's only permitted reaction to the lower-level reparse detection.  The
    lower-level class has its own real HANDLE swap tests.
    """
    if os.name != "nt":
        pytest.skip("Windows HANDLE quarantine path is Windows-only")
    sentinel = tmp_path / "outside-sentinel.txt"
    sentinel.write_text("must never be read", encoding="utf-8")
    io = _ReparseRaceHandleIo()
    store = ImmutableQuarantineArtifactStore(tmp_path / "quarantine", handle_io=io)  # type: ignore[arg-type]

    with pytest.raises(ArtifactEvidenceError, match="unsafe"):
        store.probe(OPERATION, _source())

    assert sentinel.read_text(encoding="utf-8") == "must never be read"
    assert ("read", ("operations", artifact_evidence._operation_component(OPERATION))) in io.calls


def test_probe_promotes_complete_pending_receipt_after_pre_promote_crash(tmp_path: Path) -> None:
    root = tmp_path / "quarantine"
    store = ImmutableQuarantineArtifactStore(root)
    evidence = _new_evidence(OPERATION, _source(), _inventory())
    pending = store._pending_dir(OPERATION)
    store._write_pending(pending, evidence)

    recovered = store.probe(OPERATION, _source())

    assert recovered == evidence
    assert (_operation_directory(root, "operations") / "receipt.json").is_file()
    assert not pending.exists()


def test_load_final_never_reads_or_promotes_pending_evidence(tmp_path: Path) -> None:
    root = tmp_path / "quarantine"
    store = ImmutableQuarantineArtifactStore(root)
    evidence = _new_evidence(OPERATION, _source(), _inventory())
    pending = store._pending_dir(OPERATION)
    store._write_pending(pending, evidence)

    assert store.load_final(OPERATION, _source()) is None
    assert pending.is_dir()
    assert not _operation_directory(root, "operations").exists()


def test_verified_intake_artifact_reads_final_after_store_restart(tmp_path: Path) -> None:
    facts = _facts(tmp_path)
    intake_ref, _source_value = _record_intake(facts)

    assert facts.load_verified_intake_artifact(intake_ref).inventory == _inventory()

    restarted = _facts(tmp_path)
    evidence = restarted.load_verified_intake_artifact(intake_ref)

    assert evidence.inventory == _inventory()
    assert evidence.file_count == 2


def test_verified_intake_artifact_fails_closed_for_pending_tamper_and_ref_drift(tmp_path: Path) -> None:
    facts = _facts(tmp_path)
    intake_ref, source = _record_intake(facts)
    root = tmp_path / "quarantine"
    final = _operation_directory(root, "operations")
    pending = _operation_directory(root, ".pending")
    final.replace(pending)

    with pytest.raises(ExternalExtensionFactConflict, match="missing"):
        facts.load_verified_intake_artifact(intake_ref)
    assert pending.is_dir()

    pending.replace(final)
    target = final / "artifact" / "SKILL.md"
    os.chmod(target, 0o600)
    target.write_bytes(b"x" * len(_inventory().read_bytes("SKILL.md")))
    os.chmod(target, 0o400)
    with pytest.raises(ExternalExtensionFactConflict, match="invalid"):
        facts.load_verified_intake_artifact(intake_ref)

    facts = _facts(tmp_path / "ref-drift")
    intake_ref, source = _record_intake(facts)
    records = facts._records
    record = records.read("external_extension_intake_receipts", OPERATION)
    assert record is not None
    payload = dict(record.payload)
    payload["artifact_ref"] = source.artifact_ref + "-drift"
    with records.begin() as uow:
        uow.put(record.collection, record.object_id, payload, expected_revision=record.revision)
        uow.commit()
    with pytest.raises(ExternalExtensionFactConflict, match="drifted"):
        facts.load_verified_intake_artifact(intake_ref)


def test_incomplete_pending_tree_is_not_recovered(tmp_path: Path) -> None:
    root = tmp_path / "quarantine"
    store = ImmutableQuarantineArtifactStore(root)
    pending = store._pending_dir(OPERATION)
    pending.mkdir(parents=True)
    (pending / "artifact").mkdir()

    with pytest.raises(ArtifactEvidenceError, match="receipt"):
        store.probe(OPERATION, _source())


def test_unsafe_root_operation_and_reparse_paths_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ArtifactEvidenceError, match="root"):
        ImmutableQuarantineArtifactStore("relative-quarantine")

    store = ImmutableQuarantineArtifactStore(tmp_path / "quarantine")
    with pytest.raises(ArtifactEvidenceError, match="operation"):
        store.commit("../escape", _source(), _inventory())

    linked = tmp_path / "linked-root"
    target = tmp_path / "target"
    target.mkdir()
    try:
        linked.symlink_to(target, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("symlink privilege is unavailable on this host")
    with pytest.raises(ArtifactEvidenceError, match="directory"):
        ImmutableQuarantineArtifactStore(linked)
