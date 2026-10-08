"""Legacy import compatibility for the moved kernel composition."""
import sys
from backend.memory_app.kernel import agent_organization_runtime as _implementation

sys.modules[__name__] = _implementation
