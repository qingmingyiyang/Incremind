import base64
import asyncio
from copy import deepcopy
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
from threading import RLock
import time

import pytest

from backend.memory_app.v2.external_output_contract import delivery_policy_versions, frozen_output_policy, output_policy
from backend.memory_app.v2.external_task_files import clean_output_files, freeze_output_tree, generated_attachments
from backend.memory_app.v2.external_workspace import task_path_identity, verify_task_material, ExternalWorkspaceError
from backend.memory_app.workspace_intake import WorkspaceIntake, GeneratedTaskFiles
from backend.memory_app.workspace_items import WorkspaceItems
from backend.memory_app.processing_lease import ProcessingLease
from backend.shared.secret_detection import redact_secrets
from core.external_extension_runtime.windows_handle_io import WindowsHandleIoError, WindowsHandleTreeIo
from core.storage_provider import SQLiteStructuredRecordStore
from core.storage_provider.record_lineage import capture_lineage, verify_lineage, RecordLineageError
from backend.recognition import RecognitionService, RecognitionConflict, WorkScope
from backend.memory_app.source_egress import SourceEgressService


def test_output_text_cannot_issue_a_policy_version():
    spec = output_policy('@1')
    value = {'policy_versions': {'handoff': '@1'}, 'input': {'text': json.dumps({'output': spec})}}
    assert frozen_output_policy(value) is None
    assert delivery_policy_versions(value) == {'handoff': '@1'}
    issued = deepcopy(value)
    issued['policy_versions']['external_task_output'] = '@1'
    assert frozen_output_policy(issued) == spec
    assert delivery_policy_versions(issued) == {'handoff': '@1'}
    issued['input']['text'] = '{}'
    with pytest.raises(ValueError, match='external_output_policy_invalid'):
        frozen_output_policy(issued)


def test_real_output_tree_never_reads_control_or_authentication_directories(tmp_path):
    (tmp_path / 'outputs').mkdir()
    (tmp_path / 'executor-home').mkdir()
    (tmp_path / 'executor-home' / 'auth.json').write_bytes(b'not-an-output')
    (tmp_path / 'TASK.md').write_bytes(b'not-an-output')
    (tmp_path / 'outputs' / 'result.md').write_bytes(b'actual output')
    assert freeze_output_tree(tmp_path, task_path_identity(tmp_path, directory=True), output_policy('@1')) == {
        'result.md': b'actual output'}


def test_real_hard_link_is_rejected_without_changing_default_reader_contract(tmp_path):
    (tmp_path / 'outputs').mkdir()
    outside = tmp_path / 'outside.txt'
    outside.write_bytes(b'outside')
    os.link(outside, tmp_path / 'outputs' / 'linked.txt')
    if os.name == 'nt':
        assert WindowsHandleTreeIo().read_bounded_tree(tmp_path, ('outputs',),
            max_files=2, max_file_bytes=32, max_total_bytes=64) == {'linked.txt': b'outside'}
    with pytest.raises((ValueError, WindowsHandleIoError)):
        freeze_output_tree(tmp_path, task_path_identity(tmp_path, directory=True), output_policy('@1'))


def test_opened_root_identity_must_match_the_issued_directory(tmp_path):
    (tmp_path / 'outputs').mkdir()
    identity = task_path_identity(tmp_path, directory=True)
    identity[1] += 1
    with pytest.raises((ValueError, WindowsHandleIoError)):
        freeze_output_tree(tmp_path, identity, output_policy('@1'))


@pytest.mark.parametrize('limit', ['max_files', 'max_file_bytes', 'max_total_bytes'])
def test_real_files_obey_each_hard_capture_limit(tmp_path, limit):
    (tmp_path / 'outputs').mkdir()
    (tmp_path / 'outputs' / 'one.txt').write_bytes(b'1234')
    (tmp_path / 'outputs' / 'two.txt').write_bytes(b'5678')
    spec = output_policy('@1')
    spec[limit] = 1
    with pytest.raises((ValueError, WindowsHandleIoError)):
        freeze_output_tree(tmp_path, task_path_identity(tmp_path, directory=True), spec)


def test_text_code_is_frozen_after_real_redaction_and_binary_is_explicitly_rejected():
    secret = 'sk-' + 'a' * 40
    accepted, rejected = clean_output_files({'result.py': ('print("' + secret + '")').encode(),
        'result.pdf': b'%PDF-test', 'broken.txt': b'\xff', 'nul.txt': b'\x00'}, output_policy('@1'), redact_secrets)
    assert len(accepted) == 1 and accepted[0]['name'] == 'result.py'
    assert secret.encode() not in base64.b64decode(accepted[0]['content'])
    assert rejected == ['external_output_encoding_invalid', 'external_output_encoding_invalid',
        'external_output_type_not_supported']


@pytest.mark.parametrize('name', ['../secret.txt', '/absolute.txt', 'bad:name.txt', 'folder/../name.txt'])
def test_output_names_cannot_supply_parent_or_absolute_paths(name):
    accepted, rejected = clean_output_files({name: b'body'}, output_policy('@1'), redact_secrets)
    assert not accepted and rejected == ['external_output_name_unsafe']


def test_real_directory_reparse_cannot_read_a_tree_outside_outputs(tmp_path):
    (tmp_path / 'outputs').mkdir()
    (tmp_path / 'outside').mkdir()
    (tmp_path / 'outside' / 'private.txt').write_bytes(b'not an output')
    link = tmp_path / 'outputs' / 'linked'
    if os.name == 'nt':
        result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(link), str(tmp_path / 'outside')],
            capture_output=True, check=False)
        assert result.returncode == 0
    else:
        os.symlink(tmp_path / 'outside', link, target_is_directory=True)
    with pytest.raises((ValueError, OSError, WindowsHandleIoError)):
        freeze_output_tree(tmp_path, task_path_identity(tmp_path, directory=True), output_policy('@1'))


def test_existing_single_task_reference_needs_no_new_attachment_store(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    assert generated_attachments(records, 'default', {'id': 'doc'}, tmp_path) == []
    assert records.read('v2_external_task_attachments', 'doc') is None


def test_generated_intake_rejects_an_unqualified_descriptor_without_writing(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    items = WorkspaceItems(records, ProcessingLease(records, 'workspace_items', 'test-owner'), RLock())
    intake = WorkspaceIntake(tmp_path, items, None)
    with pytest.raises(ValueError, match='generated_file_authority_invalid'):
        asyncio.run(intake.prepare_generated_task_files({'files': []}))
    assert not intake.root.exists()


@pytest.mark.parametrize('revision', [1, 2])
def test_incomplete_or_modified_attachment_marker_never_creates_kernel_authority(tmp_path, revision):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    with records.begin() as tx:
        row = tx.put('v2_external_task_attachments', 'doc', {'project_id': 'default'}, expected_revision=0)
        if revision == 2:
            tx.put('v2_external_task_attachments', 'doc', row.payload, expected_revision=1)
        tx.commit()
    with pytest.raises(ValueError):
        generated_attachments(records, 'default', {'id': 'doc'}, tmp_path)
    assert not (tmp_path / '.rebuild-data' / 'ai-turns.sqlite3').exists()


def test_real_source_lineage_keeps_content_edits_and_rejects_delete_recreation(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    with records.begin() as tx:
        tx.put('workspace_items', 'file-source', {'source_text': 'initial'}, expected_revision=0)
        birth = capture_lineage(tx, 'workspace_items', 'file-source')
        tx.commit()
    with records.begin() as tx:
        tx.put('workspace_items', 'file-source', {'source_text': 'edited'}, expected_revision=1)
        tx.commit()
    verify_lineage(records, birth)
    with records.begin() as tx:
        tx.delete('workspace_items', 'file-source', expected_revision=2)
        tx.put('workspace_items', 'file-source', {'source_text': 'initial'}, expected_revision=0)
        tx.commit()
    with pytest.raises(RecordLineageError):
        verify_lineage(records, birth)


def test_managed_file_replay_identity_also_rejects_a_real_hard_link(tmp_path):
    file = tmp_path / 'managed.txt'
    file.write_bytes(b'captured safe bytes')
    assert GeneratedTaskFiles._managed_identity(file, b'captured safe bytes')[3] == len(b'captured safe bytes')
    os.link(file, tmp_path / 'other.txt')
    with pytest.raises(ExternalWorkspaceError):
        GeneratedTaskFiles._managed_identity(file, b'captured safe bytes')


def test_real_original_inherits_its_actual_parent_policy_without_changing_child_policy(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    scope = WorkScope('local-user', 'default')
    parent_id = RecognitionService(records).stage_experience(scope=scope, content='合成的用户依据',
        provenance={'kind': 'user_statement', 'actor': 'local-user', 'source_refs': []})
    items = WorkspaceItems(records, ProcessingLease(records, 'workspace_items', 'test-owner'), RLock())
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    original = workspace / 'file-source.txt'
    original.write_text('合成文件内容', encoding='utf-8')
    items.create_upload({'id': 'file-source', 'project_id': 'default', 'input_kind': 'file',
        'title': '合成文件', 'source_text': '合成文件内容', 'status': 'confirmed', 'document_id': None,
        'original_path': str(original), 'original_name': 'file-source.txt',
        'provenance': {'source_refs': [{'type': 'experience', 'id': parent_id, 'revision': 1}]}})
    authority = SourceEgressService(records)
    authority.set_policy(scope, 'experience', parent_id, 1, 0, ['generation', 'embedding', 'rerank'])
    ref = {'type': 'original_item', 'id': 'file-source', 'revision': 1}
    authority.require(authority.snapshot(scope, [ref]), 'generation')
    authority.set_policy(scope, 'experience', parent_id, 1, 1, [])
    with pytest.raises(RecognitionConflict):
        authority.require(authority.snapshot(scope, [ref]), 'generation')
    assert records.read('source_egress_original_item_policies', 'file-source') is None


def test_material_single_link_is_optional_and_identity_is_checked_on_the_actual_descriptor(tmp_path):
    path = tmp_path / 'material.txt'
    path.write_bytes(b'actual bytes')
    identity = task_path_identity(path)
    assert verify_task_material(path, b'actual bytes', expected_identity=identity, require_single_link=True) == identity
    os.link(path, tmp_path / 'linked.txt')
    assert verify_task_material(path, b'actual bytes') == identity
    with pytest.raises(ExternalWorkspaceError):
        verify_task_material(path, b'actual bytes', expected_identity=identity, require_single_link=True)
    wrong = list(identity)
    wrong[1] += 1
    with pytest.raises(ExternalWorkspaceError):
        verify_task_material(path, b'actual bytes', expected_identity=wrong)


def _rotate_real_materials(candidate, parked, temporary, stop, ready, rotations):
    paths = [Path(candidate), Path(parked), Path(temporary)]
    root = paths[0].parent.resolve()
    assert all(path.parent.resolve() == root for path in paths)
    moves = [(paths[0], paths[2]), (paths[1], paths[0]), (paths[2], paths[1])]
    phase = 0
    ready.set()
    while not stop.is_set():
        try:
            os.rename(*moves[phase])
        except OSError:
            continue
        phase = (phase + 1) % len(moves)
        if phase == 0:
            with rotations.get_lock():
                rotations.value += 1


def test_real_rename_race_cannot_qualify_a_hardlinked_descriptor_using_another_path_stat(tmp_path):
    candidate, parked, temporary = (tmp_path / name for name in ('candidate.txt', 'parked.txt', 'rotating.txt'))
    body = b'actual equal bytes' * 256
    candidate.write_bytes(body)
    parked.write_bytes(body)
    identity = task_path_identity(candidate)
    single = task_path_identity(parked)
    assert verify_task_material(parked, body, expected_identity=single, require_single_link=True) == single
    os.link(candidate, tmp_path / 'outside-link.txt')
    context = multiprocessing.get_context('spawn')
    stop, ready, rotations = context.Event(), context.Event(), context.Value('i', 0)
    worker = context.Process(target=_rotate_real_materials,
        args=(str(candidate), str(parked), str(temporary), stop, ready, rotations))
    worker.start()
    observations, accepted, attempts = {1: 0, 2: 0}, 0, 0
    try:
        assert ready.wait(15)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            attempts += 1
            try:
                count = candidate.stat().st_nlink
                if count in observations:
                    observations[count] += 1
                verify_task_material(candidate, body, expected_identity=identity, require_single_link=True)
            except (OSError, ExternalWorkspaceError):
                continue
            accepted += 1
    finally:
        stop.set()
        worker.join(10)
        if worker.is_alive():
            worker.terminate()
            worker.join(5)
    assert worker.exitcode == 0 and rotations.value > 0 and attempts > 0
    assert observations[1] > 0 and observations[2] > 0 and accepted == 0
    print(json.dumps({'real_process_rotations': rotations.value, 'attempts': attempts,
        'path_single_link_observed': observations[1], 'path_hard_link_observed': observations[2],
        'qualified_hardlinked_descriptors': accepted, 'single_link_control': 'passed'}))
