"""Bind product privacy to the kernel's auxiliary execution adapter."""
from ..kernel.memory_turn import MemoryTurn as KernelMemoryTurn, embedding_request as kernel_embedding_request
from .privacy import egress_allowed
from .turn_requests import freeze_product_turn, validate_frozen_inputs


class MemoryTurn(KernelMemoryTurn):
    def __init__(self, records, models, *, freeze_request=None, **kwargs):
        super().__init__(records, models, freeze_request=freeze_request or freeze_product_turn,
            validate_request=validate_frozen_inputs, remote_allowed=egress_allowed, **kwargs)


def embedding_request(records, models, project, materials, key, validate, transport, request):
    return kernel_embedding_request(records, models, project, materials, key, validate,
        transport, request, turn_factory=MemoryTurn)
