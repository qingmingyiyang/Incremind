"""Governed local Plugin package intake contracts."""

from .package_intake import (
    PluginPackageIntake,
    PluginPackageIntakeConflict,
    PluginPackageIntakeError,
)
from .skill_activation import PluginSkillActivation
from .tool_activation import LocalLookupToolProvider, PluginToolActivation, PluginToolBinding
from .mcp_reference_activation import (
    PluginMCPReferenceActivation,
    PluginMCPReferenceBinding,
    PluginMCPReferenceStatus,
)
from .hands_activation import (
    PluginHandsActivation,
    PluginHandsActivationAuthority,
    PluginHandsActivationConflict,
    PluginHandsActivationError,
)
from .hook_activation import (
    PluginHookActivation,
    PluginHookActivationAuthority,
    PluginHookActivationConflict,
    PluginHookActivationError,
)

__all__ = [
    "PluginPackageIntake",
    "PluginPackageIntakeConflict",
    "PluginPackageIntakeError",
    "PluginSkillActivation",
    "LocalLookupToolProvider",
    "PluginToolActivation",
    "PluginToolBinding",
    "PluginMCPReferenceActivation",
    "PluginMCPReferenceBinding",
    "PluginMCPReferenceStatus",
    "PluginHandsActivation",
    "PluginHandsActivationAuthority",
    "PluginHandsActivationConflict",
    "PluginHandsActivationError",
    "PluginHookActivation",
    "PluginHookActivationAuthority",
    "PluginHookActivationConflict",
    "PluginHookActivationError",
]
