"""外部任务资料定位指引；只接受受控的相对任务子目录。"""
import re


def v1(task, material_directory):
    if (not isinstance(task, str) or not task.strip() or not isinstance(material_directory, str)
            or not re.fullmatch(r'agent_workspaces/[A-Za-z0-9][A-Za-z0-9._-]{0,127}', material_directory)):
        raise ValueError('external_task_input_invalid')
    return (task + '\n\n本次任务的交接资料与附件位于 ' + material_directory
        + '/。先读取其中的 TASK.md、CONTEXT.md 和附件；memory-mcp.json 只记录本次记忆连接配置。')
