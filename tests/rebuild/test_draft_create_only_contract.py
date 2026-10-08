"""The T11.9 exception never admits general mutations or external effects."""
import pytest

from core.ai_kernel.contracts import AIKernelContractError, validate_capability_manifest


def manifest(**changes):
    return {
        'mode': 'write', 'operation_semantics': 'receipt_required',
        'requires_approval': False, 'tool_exposed': True,
        'write_scope': 'draft_create_only', **changes,
    }


def test_receipted_draft_creation_can_declare_no_approval():
    assert validate_capability_manifest(manifest())['requires_approval'] is False


@pytest.mark.parametrize('mode', ['external', 'platform'])
def test_external_and_platform_cannot_claim_draft_creation(mode):
    with pytest.raises(AIKernelContractError):
        validate_capability_manifest(manifest(mode=mode, requires_approval=True))


@pytest.mark.parametrize('write_scope', [None, 'update', 'delete'])
def test_existing_object_mutations_still_require_approval(write_scope):
    with pytest.raises(AIKernelContractError):
        validate_capability_manifest(manifest(write_scope=write_scope))


def test_draft_creation_cannot_omit_durable_receipt():
    with pytest.raises(AIKernelContractError):
        validate_capability_manifest(manifest(operation_semantics='none'))
