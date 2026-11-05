from types import MappingProxyType


def native_mcp_auth():
    return MappingProxyType({'pending_seconds': 600, 'code_seconds': 120,
        'access_seconds': 3600, 'refresh_seconds': 2592000, 'human_seconds': 300,
        'body_bytes': 16384, 'tool_body_bytes': 1048576, 'pending_per_client': 32, 'pending_global': 256,
        'refresh_tombstones_per_family': 128, 'clients_global': 32})
