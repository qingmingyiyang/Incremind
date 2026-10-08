"""原准入 registry 与真实 SQLite 装配，不启动外部任务。"""
import gc
import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.api.capability_admission import ReviewedCoreCapabilityRegistry, RuntimeCapabilityAdmission
from backend.memory_app.v2.external_context import ExternalContext
from backend.memory_app.v2.external_host import HostAdmission
from backend.memory_app.v2.external_runner import definition
from backend.shared.deployment import DeploymentLayout
from core.ai_kernel import SQLiteAITurnStore, ScopedCapabilityRegistry
from core.ai_kernel.registry import CapabilityRegistryError
from core.document_engine.sqlite_runtime import SQLiteDocumentRepository
from core.storage_provider import SQLiteStructuredRecordStore


@pytest.fixture
def composition(tmp_path):
    root=tmp_path/'user'; root.mkdir()
    records=SQLiteStructuredRecordStore(root/'records.sqlite3')
    turns=SQLiteAITurnStore(root/'turns.sqlite3')
    dispatch=ScopedCapabilityRegistry()
    reviewed=ReviewedCoreCapabilityRegistry(RuntimeCapabilityAdmission(dispatch))
    context=ExternalContext(records,owner_id='local-user',documents=SQLiteDocumentRepository(records))
    context.install(reviewed,turns)
    host=HostAdmission(deployment=DeploymentLayout('desktop',root,None),owner_id='local-user',records=records,registrations={})
    yield root,records,turns,dispatch,reviewed,SimpleNamespace(external_context=context,external_execution_host=host)
    gc.collect()


def install(values,root=None):
    runtime_root,_,turns,_,registry,state=values
    return importlib.import_module('backend.memory_app.v2.external_runtime').install_external_runner(state,registry,turns,runtime_root=runtime_root if root is None else root)


def test_real_reviewed_registration_and_duplicate_contract(composition):
    _,records,turns,dispatch,_,state=composition
    runner=install(composition)
    assert runner.records is records and runner.turns is turns
    assert runner.context is state.external_context and runner.host is state.external_execution_host
    assert dispatch.resolve('external.task.execute')==(definition(),runner)
    with pytest.raises(CapabilityRegistryError,match='already registered'): install(composition)


def test_absent_host_does_not_register(composition):
    *_,dispatch,registry,state=composition
    del state.external_execution_host
    before=dispatch.snapshot()
    assert install(composition) is None
    assert dispatch.snapshot()==before


@pytest.mark.parametrize('change',['bool','root','relative','traversal','records','owner','turns','context'])
def test_invalid_binding_fixed_rejection_without_registry_changes(composition,change,tmp_path):
    root,records,turns,dispatch,registry,state=composition
    supplied=root
    if change=='bool': state.external_execution_host=True
    elif change=='root': supplied=tmp_path/'another'
    elif change=='relative': supplied=Path('relative')
    elif change=='traversal': supplied=root/'..'/'user'
    elif change=='records': state.external_execution_host.records=SQLiteStructuredRecordStore(root/'other.sqlite3')
    elif change=='owner': state.external_execution_host.owner_id='other-user'
    elif change=='turns': state.external_context.turns=SQLiteAITurnStore(root/'other-turns.sqlite3')
    elif change=='context': state.external_context=True
    before=dispatch.snapshot()
    with pytest.raises(ValueError,match='^external_runtime_binding_invalid$'): install(composition,supplied)
    assert dispatch.snapshot()==before
