"""新增执行能力必须经原唯一准入登记。"""
import pytest

from backend.api.capability_admission import (CORE_CAPABILITY_INVENTORY,
    RuntimeCapabilityAdmission, CapabilityAdmissionError)
from core.ai_kernel import ScopedCapabilityRegistry
from tests.architecture.test_capability_admission_gate import _definition, _Provider


def test_external_task_inventory_and_original_admission():
    items = [item for item in CORE_CAPABILITY_INVENTORY
        if item.capability_id == 'external.task.execute']
    assert len(items) == 1
    assert (items[0].version, items[0].review_revision, items[0].registration_kind) == (
        1,'2026.10.06','core')
    admission = RuntimeCapabilityAdmission(ScopedCapabilityRegistry())
    assert admission.register_core(_definition('external.task.execute'), _Provider()) is not None
    with pytest.raises(CapabilityAdmissionError, match='version_drift'):
        admission.register_core(_definition('external.task.execute',2), _Provider())
