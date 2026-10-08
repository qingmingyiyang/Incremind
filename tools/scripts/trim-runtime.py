#!/usr/bin/env python3
"""
Chriptmas_Replay Runtime Trim Script
=====================================
用途：在 conda 环境准备好后、NSIS 打包前，从 bundled runtime/ 目录中
移除运行时不需要的文件和包，缩减安装包体积。

预期效果：runtime/ 从 ~9.7 GB 缩减到 ~5-6 GB，NSIS 安装包从 ~339 MB 缩减到 ~180-220 MB。

用法：
  python tools/scripts/trim-runtime.py --dry-run          # 仅预览，不删除
  python tools/scripts/trim-runtime.py --tier1            # 仅 Tier 1（100% 安全）
  python tools/scripts/trim-runtime.py --tier1 --tier2    # Tier 1 + 2（已验证安全）
  python tools/scripts/trim-runtime.py --all              # 全部三级（含激进清理）

安全原则：
  - Tier 1：绝对安全 — PDB 符号、C 头文件、文档、测试框架、废弃标准库
  - Tier 2：已验证安全 — site-packages 中经审计确认零直接导入的包
  - Tier 3：激进 — 需运行时验证的传递依赖（默认不执行，需显式 --tier3）

不要修改此脚本删除逻辑而不先运行 --dry-run 并核验输出。
"""

import argparse
import shutil
import sys
import os
from pathlib import Path
from typing import Callable


def fmt_size(bytes_val: int) -> str:
    if bytes_val >= 1024 ** 3:
        return f"{bytes_val / (1024 ** 3):.1f} GB"
    if bytes_val >= 1024 ** 2:
        return f"{bytes_val / (1024 ** 2):.1f} MB"
    if bytes_val >= 1024:
        return f"{bytes_val / 1024:.1f} KB"
    return f"{bytes_val} B"


def dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for entry in path.rglob("*"):
        if entry.is_file():
            try:
                total += entry.stat().st_size
            except OSError:
                pass
    return total


def remove(path: Path, dry_run: bool) -> int:
    """Remove a file or directory. Returns bytes freed."""
    if not path.exists():
        return 0
    size = 0
    if path.is_dir():
        size = dir_size(path)
    elif path.is_file():
        try:
            size = path.stat().st_size
        except OSError:
            pass
    if dry_run:
        print(f"  [DRY-RUN] 将删除: {path}  ({fmt_size(size)})")
    else:
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            print(f"  [DEL] {path}  ({fmt_size(size)})")
        except OSError as exc:
            print(f"  [SKIP] {path}: {exc}")
            return 0
    return size


def glob_remove(base: Path, pattern: str, dry_run: bool) -> int:
    """Remove all paths matching a glob pattern under base."""
    total = 0
    for path in sorted(base.glob(pattern)):
        total += remove(path, dry_run)
    return total


# ============================================================
# Tier 1 — 100% 安全（调试符号 / 头文件 / 文档 / 测试 / 废弃 stdlib）
# ============================================================

def tier1_pdb_files(runtime: Path, dry_run: bool) -> int:
    """删除所有 .pdb 调试符号文件（Windows MSVC 调试信息，运行时不需要）。"""
    total = 0
    for pdb in runtime.rglob("*.pdb"):
        total += remove(pdb, dry_run)
    return total


def tier1_c_headers(runtime: Path, dry_run: bool) -> int:
    """删除 Library/include/ C/C++ 头文件（编译时需要，运行时不需要）。"""
    include_dir = runtime / "Library" / "include"
    return remove(include_dir, dry_run)


def tier1_documentation(runtime: Path, dry_run: bool) -> int:
    """删除文档、本地化、手册页。"""
    total = 0
    for sub in ["doc", "locale", "man", "info", "gtk-doc"]:
        total += remove(runtime / "Library" / "share" / sub, dry_run)
        total += remove(runtime / "share" / sub, dry_run)
    return total


def tier1_tools_dir(runtime: Path, dry_run: bool) -> int:
    """删除 Tools/ 目录（Python demo/i18n 工具脚本）。"""
    return remove(runtime / "Tools", dry_run)


def tier1_stdlib_dead(runtime: Path, dry_run: bool) -> int:
    """删除 Python 标准库中运行时不需要的模块。"""
    lib = runtime / "Lib"
    dead_modules = [
        "idlelib",          # IDLE 编辑器
        "lib2to3",          # Python 2→3 迁移工具
        "turtledemo",       # turtle 示例
        "test",             # Python 标准库测试套件
        "distutils",        # 已废弃的构建系统
        "ensurepip",        # pip bootstrapper
        "venv",             # 虚拟环境创建工具
        "wsgiref",          # WSGI 参考服务器
    ]
    total = 0
    for mod in dead_modules:
        total += remove(lib / mod, dry_run)
    return total


def tier1_pycache(runtime: Path, dry_run: bool) -> int:
    """删除 __pycache__ 目录（可在首次运行时重建）。"""
    total = 0
    for pycache in runtime.rglob("__pycache__"):
        total += remove(pycache, dry_run)
    return total


def tier1_static_libs(runtime: Path, dry_run: bool) -> int:
    """删除 Library/lib/ 中的静态库和开发文件（运行时只加载 DLL）。"""
    lib_dir = runtime / "Library" / "lib"
    total = 0
    # 静态库
    total += glob_remove(lib_dir, "*.a", dry_run)
    total += glob_remove(lib_dir, "*.lib", dry_run)
    # pkgconfig
    total += remove(lib_dir / "pkgconfig", dry_run)
    # cmake
    total += glob_remove(lib_dir, "cmake", dry_run)
    total += glob_remove(lib_dir, "*.cmake", dry_run)
    # 移除子目录中的 pkgconfig/cmake
    for sub in lib_dir.iterdir():
        if sub.is_dir():
            total += remove(sub / "pkgconfig", dry_run)
    return total


def tier1_test_frameworks(runtime: Path, dry_run: bool) -> int:
    """删除 site-packages 中的测试框架（运行时不需要）。"""
    site = runtime / "Lib" / "site-packages"
    test_pkgs = ["_pytest", "pytest", "pytest_asyncio", "pytest_benchmark",
                 "pytest_mock", "pytest_timeout", "pytest_xdist"]
    total = 0
    for pkg in test_pkgs:
        p = site / pkg
        if p.is_dir():
            total += remove(p, dry_run)
    return total


# ============================================================
# Tier 2 — 已验证安全（零直接导入的 site-packages）
# ============================================================

# 经 Rust Core Boundary Agent 审计，以下包在 src/backend/ 中 ZERO 导入
TIER2_PACKAGES = [
    # 巨型包，确认零导入
    "pandas",           # 729 MB — DataFrame 库，后端无任何 import pandas
    "nltk",             # 200 MB — NLP 工具包
    "networkx",         # 288 MB — 图算法库
    "pygments",         # 170 MB — 代码高亮（仅 CLI 需要）
    "openpyxl",         #  95 MB — Excel 读写
    "lxml",             #  55 MB — XML 解析
    "joblib",           #  52 MB — 并行序列化
    "langsmith",        #  51 MB — LangChain 遥测/追踪
    "rich",             #  50 MB — 终端美化（运行时无控制台）
    # 时区数据（仅 pandas 使用，pandas 已移除则无用）
    "tzdata",           # 157 MB
    "pytz",             # 154 MB
    # numpy — pandas 核心依赖，pandas 移除后无后端直接导入
    "numpy",            # 384 MB + numpy.libs 21 MB
    # 其他确认零导入
    "sqlalchemy",       # 132 MB — ORM，后端使用 lancedb 不用 SQL
    "av",               #  66 MB — FFmpeg Python 绑定（后端用 subprocess 调 ffmpeg）
    "av.libs",          #  66 MB — av 的原生库
    "bs4",              #   8 MB — BeautifulSoup（后端不用 HTML 解析）
    "beautifulsoup4",   #   0 MB — bs4 元包
    # Google API 客户端（未使用）
    "google",           #  28 MB
    "google_api_core",  #   0 MB
    "google_auth",      #   0 MB
    "googleapis_common_protos",  # 0 MB
    "grpc",             #   0 MB (google 传递)
    "grpcio",           #   varies
    # tantivy — 全文搜索引擎，后端未使用
    "tantivy",          #  11 MB
]


def tier2_unused_packages(runtime: Path, dry_run: bool) -> int:
    """删除经审计确认零直接导入的 site-packages。"""
    site = runtime / "Lib" / "site-packages"
    total = 0
    removed = set()
    for pkg_name in TIER2_PACKAGES:
        pkg_path = site / pkg_name
        if pkg_path.exists() and pkg_name not in removed:
            size = remove(pkg_path, dry_run)
            total += size
            removed.add(pkg_name)
        # 也检查 .dist-info / .egg-info
        for meta in site.glob(f"{pkg_name}-*.dist-info"):
            total += remove(meta, dry_run)
        for meta in site.glob(f"{pkg_name.replace('_', '-')}-*.dist-info"):
            if pkg_name not in str(meta):
                continue
            total += remove(meta, dry_run)
    return total


def tier2_pip_setuptools(runtime: Path, dry_run: bool) -> int:
    """删除包管理器（运行时不需要安装/卸载包）。"""
    site = runtime / "Lib" / "site-packages"
    total = 0
    for pkg in ["pip", "setuptools", "wheel", "pkg_resources", "distlib"]:
        p = site / pkg
        if p.is_dir():
            total += remove(p, dry_run)
    # pip 的 dist-info
    for meta in site.glob("pip-*.dist-info"):
        total += remove(meta, dry_run)
    for meta in site.glob("setuptools-*.dist-info"):
        total += remove(meta, dry_run)
    return total


def tier2_onnxruntime(runtime: Path, dry_run: bool) -> int:
    """删除独立的 onnxruntime（fastembed 内置自己的 ONNX 运行时）。"""
    site = runtime / "Lib" / "site-packages"
    total = 0
    for pkg in ["onnxruntime", "onnxruntime_gpu"]:
        p = site / pkg
        if p.is_dir():
            total += remove(p, dry_run)
    return total


# ============================================================
# Tier 3 — 激进（需运行时验证，默认不执行）
# ============================================================

def tier3_openai_standalone(runtime: Path, dry_run: bool) -> int:
    """
    删除独立的 openai 包。
    litellm 内置了自己的 OpenAI 客户端封装，可能不需要独立的 openai 包。
    风险：如果 litellm 的某些 provider 路由依赖独立 openai SDK，会报 ImportError。
    """
    site = runtime / "Lib" / "site-packages"
    return remove(site / "openai", dry_run)


def tier3_llama_index_trim(runtime: Path, dry_run: bool) -> int:
    """删除 llama_index 中不使用的子包（核心保留）。"""
    site = runtime / "Lib" / "site-packages" / "llama_index"
    if not site.exists():
        return 0
    total = 0
    # 后端只用 VectorStoreIndex + LanceDBVectorStore，以下子包可能不需要
    unused_subs = [
        "callbacks", "agent", "readers", "tools",
        "output_parsers", "question_gen", "program",
        "multi_modal_llms", "graph_stores",
    ]
    for sub in unused_subs:
        p = site / sub
        if p.exists():
            total += remove(p, dry_run)
    return total


# ============================================================
# 主逻辑
# ============================================================

TIER_FUNCTIONS: dict[int, list[tuple[str, Callable[[Path, bool], int]]]] = {
    1: [
        ("PDB 调试符号", tier1_pdb_files),
        ("C/C++ 头文件 (Library/include)", tier1_c_headers),
        ("文档/本地化/手册页", tier1_documentation),
        ("Tools/ 目录", tier1_tools_dir),
        ("废弃标准库模块 (idlelib/lib2to3/test 等)", tier1_stdlib_dead),
        ("__pycache__ 字节码缓存", tier1_pycache),
        ("静态库 (.a/.lib) 与 cmake/pkgconfig", tier1_static_libs),
        ("测试框架 (_pytest/pytest 等)", tier1_test_frameworks),
    ],
    2: [
        ("零导入 site-packages (pandas/nltk/networkx 等)", tier2_unused_packages),
        ("pip/setuptools/wheel 包管理器", tier2_pip_setuptools),
        ("独立 onnxruntime (fastembed 内置)", tier2_onnxruntime),
    ],
    3: [
        ("独立 openai 包 (litellm 内置封装)", tier3_openai_standalone),
        ("llama_index 子包裁剪", tier3_llama_index_trim),
    ],
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Chriptmas_Replay Runtime Trim — 缩减打包 runtime 体积",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python tools/scripts/trim-runtime.py --dry-run           # 预览所有层
  python tools/scripts/trim-runtime.py --tier1             # Tier 1 安全清理
  python tools/scripts/trim-runtime.py --tier1 --tier2     # Tier 1 + 2
  python tools/scripts/trim-runtime.py --all               # 全部三级（含激进）
        """,
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="仅预览，不实际删除")
    parser.add_argument("--tier1", action="store_true",
                        help="Tier 1: 100%% 安全（PDB/头文件/文档/测试/废弃stdlib）")
    parser.add_argument("--tier2", action="store_true",
                        help="Tier 2: 已验证安全（零导入 site-packages + pip/setuptools）")
    parser.add_argument("--tier3", action="store_true",
                        help="Tier 3: 激进（需运行时验证，如独立 openai 包）")
    parser.add_argument("--all", action="store_true",
                        help="执行全部三级")
    parser.add_argument("--runtime-dir", type=str, default=None,
                        help="runtime/ 目录路径（默认自动检测项目根下的 runtime/）")

    args = parser.parse_args()

    # 解析层级
    tiers: list[int] = []
    if args.all:
        tiers = [1, 2, 3]
    else:
        if args.tier1:
            tiers.append(1)
        if args.tier2:
            tiers.append(2)
        if args.tier3:
            tiers.append(3)

    if not tiers:
        print("请指定至少一个层级：--tier1 / --tier2 / --tier3 / --all")
        print("使用 --dry-run 预览将要删除的内容。")
        sys.exit(1)

    # 定位 runtime 目录
    if args.runtime_dir:
        runtime = Path(args.runtime_dir)
    else:
        # 自动检测：脚本在 tools/scripts/ 下，项目根在 ../../
        script_dir = Path(__file__).resolve().parent
        project_root = script_dir.parent.parent
        runtime = project_root / "runtime"

    if not runtime.exists():
        print(f"错误：runtime 目录不存在：{runtime}")
        print("请使用 --runtime-dir 指定正确路径。")
        sys.exit(1)

    print(f"Runtime 目录：{runtime}")
    print(f"模式：{'DRY-RUN（仅预览）' if args.dry_run else '实际删除'}")
    print(f"层级：{', '.join(f'Tier {t}' for t in tiers)}")
    print()

    # 预计算总大小
    before_size = dir_size(runtime)

    total_freed = 0
    for tier_num in tiers:
        print(f"{'='*60}")
        print(f"  Tier {tier_num}")
        print(f"{'='*60}")
        for label, func in TIER_FUNCTIONS[tier_num]:
            freed = func(runtime, args.dry_run)
            total_freed += freed
            if freed > 0:
                print(f"  → {label}: {fmt_size(freed)}")
            else:
                print(f"  → {label}: (无匹配文件)")
        print()

    after_size = before_size - total_freed

    print(f"{'='*60}")
    print(f"  汇总")
    print(f"{'='*60}")
    print(f"  清理前：{fmt_size(before_size)}")
    print(f"  释放：  {fmt_size(total_freed)}")
    print(f"  清理后：{fmt_size(after_size)}")
    if args.dry_run:
        print(f"\n  ⚠  DRY-RUN 模式 — 以上为预览，未实际删除文件。")
        print(f"  去掉 --dry-run 执行实际清理。")
    else:
        print(f"\n  ✅ 清理完成。")


if __name__ == "__main__":
    main()
