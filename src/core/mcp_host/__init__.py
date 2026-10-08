"""Controlled MCP host boundary.

Remote MCP descriptors are discovery hints only.  This package turns a local
allowlist into native AI capabilities and keeps remote tool output out of the
normal planner payload path.
"""

from .host import (
    MCPFatalTransportError,
    MCPCredentialGenerationChanged,
    MCPInputRequiredError,
    MCPHostConnection,
    MCPHostConnectionConfig,
    MCPHostError,
    MCPHostLimits,
    MCPReceiptStorePort,
    MCPToolPolicy,
    MCPTransportPort,
    TurnPayloadMCPReceiptStore,
)
from .stdio_config import (
    MCPStdioConfigError,
    MCPStdioApprovedLaunchManifestStorePort,
    MCPStdioConnectionConfig,
    MCPStdioLaunchAuthority,
    MCPStdioLaunchManifest,
)
from .stdio_transport import MCPStdioTransport, MCPStdioTransportError
from .streamable_http_config import (
    MCPStreamableHTTPAuthority,
    MCPStreamableHTTPConfigError,
    MCPStreamableHTTPConnectionConfig,
    MCPStreamableHTTPManifest,
)
from .streamable_http_transport import MCPHttpResponse, MCPStreamableHTTPTransport

__all__ = [
    "MCPHostConnection",
    "MCPFatalTransportError",
    "MCPCredentialGenerationChanged",
    "MCPInputRequiredError",
    "MCPHostConnectionConfig",
    "MCPHostError",
    "MCPHostLimits",
    "MCPReceiptStorePort",
    "MCPStdioConfigError",
    "MCPStdioApprovedLaunchManifestStorePort",
    "MCPStdioConnectionConfig",
    "MCPStdioLaunchAuthority",
    "MCPStdioLaunchManifest",
    "MCPStdioTransport",
    "MCPStdioTransportError",
    "MCPStreamableHTTPAuthority",
    "MCPStreamableHTTPConfigError",
    "MCPStreamableHTTPConnectionConfig",
    "MCPStreamableHTTPManifest",
    "MCPHttpResponse",
    "MCPStreamableHTTPTransport",
    "MCPToolPolicy",
    "MCPTransportPort",
    "TurnPayloadMCPReceiptStore",
]
