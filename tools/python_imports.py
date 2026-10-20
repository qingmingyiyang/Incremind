"""供结构体检与依赖门禁共用的静态 Python 导入收集。"""

from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path


def collect_package_imports(
    path: Path, source_root: Path, package: str
) -> list[tuple[int, str]]:
    """返回目标包内的导入行与模块名，不导入或执行被扫描的文件。

    from 子树模块保留其基模块名；from 父包 import 子包补全到目标包。
    相对导入以文件所在目录为包，包含 __init__.py；越过顶层即报错。
    函数和条件中的静态导入也计入，动态导入与运行时属性不在此范围。
    """
    relative = path.relative_to(source_root)
    current_package = ".".join(relative.parent.parts)
    tree = ast.parse(path.read_bytes(), filename=str(path))
    rows: list[tuple[int, str]] = []

    def within_package(module: str) -> bool:
        return module == package or module.startswith(package + ".")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            rows.extend(
                (node.lineno, alias.name)
                for alias in node.names
                if within_package(alias.name)
            )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                try:
                    module = resolve_name("." * node.level + module, current_package)
                except ImportError as error:
                    raise ValueError(
                        f"{path}:{node.lineno}: invalid relative import: {error}"
                    ) from error
            if within_package(module):
                rows.append((node.lineno, module))
            else:
                rows.extend(
                    (node.lineno, f"{module}.{alias.name}")
                    for alias in node.names
                    if within_package(f"{module}.{alias.name}")
                )

    return sorted(rows)
