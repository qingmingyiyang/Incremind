"""Pure deployment paths; desktop contracts remain owned by runtime_root_config."""
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class DeploymentLayout:
    mode: str
    user_root: Path
    server_root: Path | None = None


def runtime_backup_roots(runtime_root):
    """The Settings vault and its existing external recovery management root."""
    active = (Path(runtime_root) / '.rebuild-data').expanduser().absolute().resolve(strict=False)
    management = active.parent / f'.{active.name}-recovery'
    return active, management / 'snapshots', management / 'operations'


def resolve_deployment(default_root: Path, *, environment: Mapping[str, str] | None = None) -> DeploymentLayout:
    environment = os.environ if environment is None else environment
    mode = environment.get('CHRIPTMAS_DEPLOY', 'desktop')
    if mode not in {'desktop', 'server'}:
        raise ValueError('deployment_invalid')
    configured = environment.get('CHRIPTMAS_APP_ROOT', '').strip()
    if mode == 'server':
        if not configured:
            raise ValueError('server_root_required')
        if any(key.startswith('CHRIPTMAS_RUNTIME_') for key in environment):
            raise ValueError('server_runtime_contract_conflict')
    root = Path(configured).expanduser().resolve() if configured else Path(default_root).resolve()
    if mode == 'desktop':
        return DeploymentLayout(mode, root)
    user = (root / 'users' / 'local-user').resolve()
    if not user.is_relative_to(root) or not (root / 'server').resolve().is_relative_to(root):
        raise ValueError('server_layout_invalid')
    return DeploymentLayout(mode, user, root)
