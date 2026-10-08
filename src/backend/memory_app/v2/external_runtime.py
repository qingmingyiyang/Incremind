"""把已配置宿主接入原 Kernel composition，不发现 CLI 或授予权限。"""
from pathlib import Path

from .external_context import ExternalContext
from .external_host import HostAdmission
from .external_runner import ExternalRunner, definition
from .external_workspace import validate_task_path


def install_external_runner(state, registry, turns, *, runtime_root):
    """原交接 installer 已绑定 Turn store；没有可信宿主时保持未注册。"""
    host = getattr(state, 'external_execution_host', None)
    if host is None:
        return None
    context = getattr(state, 'external_context', None)
    try:
        if not isinstance(host, HostAdmission) or not isinstance(context, ExternalContext):
            raise ValueError
        validate_task_path(runtime_root)
        validate_task_path(host.deployment.user_root)
        validate_task_path(context.records.database_path)
        if (not isinstance(host, HostAdmission) or not isinstance(context, ExternalContext)
                or not isinstance(runtime_root, Path) or not runtime_root.is_absolute()
                or not isinstance(host.deployment.user_root, Path)
                or not host.deployment.user_root.is_absolute()
                or host.deployment.user_root.resolve() != runtime_root.resolve()
                or context.turns is not turns or turns is None
                or context.records is not host.records or context.owner_id != host.owner_id
                or not context.records.database_path.is_absolute()
                or not context.records.database_path.resolve().is_relative_to(runtime_root.resolve())):
            raise ValueError
        runner = ExternalRunner(context.records, owner_id=context.owner_id,
            context=context, host=host, turns=turns)
    except (AttributeError, TypeError, ValueError, OSError):
        raise ValueError('external_runtime_binding_invalid') from None
    # 登记错误沿原 registry 合同传播，不覆盖或吞掉已有 capability。
    registry.register(definition(), runner)
    return runner
