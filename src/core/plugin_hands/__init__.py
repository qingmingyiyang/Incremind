"""Disabled-by-default Plugin Hands process-host foundation."""

from .contracts import PLUGIN_HANDS_PROTOCOL, PluginHandsControl, PluginHandsInvocation, PluginHandsLaunch, PluginHandsLease, PluginHandsOutcome
from .contained_host import PluginHandsContainedExecution, WindowsContainedPluginHandsHost
from .stdio_runner import PluginHandsStdioRunner
from .workspace import PluginHandsWorkspace, PluginHandsWorkspaceManager

__all__ = ["PLUGIN_HANDS_PROTOCOL", "PluginHandsContainedExecution", "PluginHandsControl", "PluginHandsInvocation", "PluginHandsLaunch", "PluginHandsLease", "PluginHandsOutcome", "PluginHandsStdioRunner", "PluginHandsWorkspace", "PluginHandsWorkspaceManager", "WindowsContainedPluginHandsHost"]
