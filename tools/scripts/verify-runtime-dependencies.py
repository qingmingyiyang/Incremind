#!/usr/bin/env python3
"""Verify that a bundled Python runtime satisfies the declared requirements."""

from __future__ import annotations

import argparse
import importlib
import re
import sys
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.parse import urlsplit

from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, Version


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REQUIREMENTS = PROJECT_ROOT / "requirements.txt"

IMPORT_TARGETS = {
    "argon2-cffi": "argon2",
    "fastapi": "fastapi",
    "anyio": "anyio",
    "uvicorn": "uvicorn",
    "python-multipart": "multipart",
    "faster-whisper": "faster_whisper",
    "huggingface-hub": "huggingface_hub",
    "httpx": "httpx",
    "httpx-sse": "httpx_sse",
    "litellm": "litellm",
    "langgraph": "langgraph",
    "lancedb": "lancedb",
    "pandas": "pandas",
    "llama-index-core": "llama_index.core",
    "llama-index-vector-stores-lancedb": "llama_index.vector_stores.lancedb",
    "llama-index-embeddings-openai": "llama_index.embeddings.openai",
    "fastembed": "fastembed",
    "onnxruntime": "onnxruntime",
    "yt-dlp": "yt_dlp",
    "pillow": "PIL",
    "psutil": "psutil",
    "chaoxing-downloader": "chaoxing_downloader",
    "python-docx": "docx",
    "python-pptx": "pptx",
    "pypdf": "pypdf",
    "pydantic": "pydantic",
    "pyyaml": "yaml",
    "cryptography": "cryptography",
    "pyjwt": "jwt",
    "langgraph-checkpoint": "langgraph.checkpoint",
    "langchain-core": "langchain_core",
    "blake3": "blake3",
    "qrcode": "qrcode",
    "mcp": "mcp",
    "sqlite-vec": "sqlite_vec",
    "av": "av",
}


@dataclass(frozen=True)
class DependencyFailure:
    package: str
    reason: str


def _canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def compile_requirements(requirements_path: Path, output_path: Path) -> tuple[Requirement, ...]:
    # pip 只用于构建期词法解析，最终安装包无需保留 pip。
    from pip._internal.network.session import PipSession
    from pip._internal.req.req_file import RequirementsFileParser, get_line_parser
    from pip._internal.exceptions import RequirementsFileParseError

    root = requirements_path.absolute().parent
    line_parser = get_line_parser(None)

    def option_values(options):
        values = vars(options).copy()
        control = values.pop("format_control")
        values["format_control"] = (control.no_binary, control.only_binary)
        return values

    defaults = option_values(line_parser("")[1])
    requirements: list[Requirement] = []

    def visit(candidate: Path, active: tuple[Path, ...]) -> None:
        resolved = candidate.resolve()
        if not resolved.is_relative_to(root.resolve()):
            raise ValueError(f"requirements include escapes its root: {candidate}")
        if resolved == output_path.resolve():
            raise ValueError(f"compiled output would overwrite a requirements source: {candidate}")
        for entry in (candidate, *candidate.parents):
            if entry.is_symlink() or entry.is_junction():
                raise ValueError(f"linked requirements path is not allowed: {entry}")
            if entry == root:
                break
        if resolved in active:
            raise ValueError(f"requirements include cycle: {candidate}")
        if not resolved.is_file():
            raise ValueError(f"requirements file does not exist: {candidate}")
        # 只解析经过边界检查的单个本地文件，再由这里控制包含图。
        for line in parser._parse_file(str(resolved), False):
            values = option_values(line.opts)
            includes = values.pop("requirements")
            unsupported = {key for key, value in values.items() if value != defaults[key]}
            if unsupported:
                raise ValueError(f"unsupported requirements options at {candidate}:{line.lineno}: {sorted(unsupported)}")
            if includes:
                if line.requirement is not None or len(includes) != 1:
                    raise ValueError(f"invalid requirements include at {candidate}:{line.lineno}")
                include = includes[0]
                if urlsplit(include).scheme or Path(include).is_absolute() or "\\" in include:
                    raise ValueError(f"unsafe requirements include: {include}")
                visit(resolved.parent / include, (*active, resolved))
            elif line.requirement is not None:
                requirement = Requirement(line.requirement)
                if requirement.url:
                    raise ValueError(f"direct URL requirements are not supported: {requirement.name}")
                requirements.append(requirement)
            else:
                raise ValueError(f"unsupported empty requirements directive at {candidate}:{line.lineno}")

    with PipSession() as session:
        parser = RequirementsFileParser(session, line_parser)
        try:
            visit(requirements_path.absolute(), ())
        except RequirementsFileParseError as error:
            raise ValueError(str(error)) from error
    return tuple(requirements)


def load_requirements(requirements_path: Path) -> tuple[Requirement, ...]:
    if not requirements_path.is_file():
        raise ValueError(f"requirements file does not exist: {requirements_path}")

    requirements: list[Requirement] = []
    for line_number, raw_line in enumerate(
        requirements_path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("-r", "--requirement", "-c", "--constraint")):
            raise ValueError(
                f"nested requirements are not supported by this verifier: "
                f"{requirements_path}:{line_number}"
            )
        try:
            requirements.append(Requirement(line))
        except InvalidRequirement as error:
            raise ValueError(
                f"invalid requirement at {requirements_path}:{line_number}: {line}"
            ) from error
    return tuple(requirements)


def verify_requirements(requirements: tuple[Requirement, ...]) -> tuple[DependencyFailure, ...]:
    failures: list[DependencyFailure] = []
    for requirement in requirements:
        if requirement.marker is not None and not requirement.marker.evaluate({"extra": ""}):
            continue
        package_name = _canonical_name(requirement.name)
        import_target = IMPORT_TARGETS.get(package_name)
        if import_target is None:
            failures.append(
                DependencyFailure(package_name, "no explicit import target is configured")
            )
            continue

        try:
            installed_version = Version(version(requirement.name))
        except PackageNotFoundError:
            failures.append(DependencyFailure(package_name, "distribution is not installed"))
            continue
        except InvalidVersion as error:
            failures.append(
                DependencyFailure(package_name, f"installed version is invalid: {error}")
            )
            continue

        if requirement.specifier and installed_version not in requirement.specifier:
            failures.append(
                DependencyFailure(
                    package_name,
                    f"installed version {installed_version} does not satisfy {requirement.specifier}",
                )
            )
            continue

        try:
            importlib.import_module(import_target)
        except Exception as error:
            failures.append(
                DependencyFailure(
                    package_name,
                    f"cannot import {import_target}: {type(error).__name__}: {error}",
                )
            )
    return tuple(failures)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify installed distributions and imports against a requirements file."
    )
    parser.add_argument(
        "--requirements",
        type=Path,
        default=DEFAULT_REQUIREMENTS,
        help="requirements file to verify (default: project requirements.txt)",
    )
    parser.add_argument("--compile-output", type=Path, help="build-only: flatten local includes using pip; does not verify installed dependencies")
    args = parser.parse_args()

    try:
        if args.compile_output is not None:
            requirements = compile_requirements(args.requirements, args.compile_output)
            args.compile_output.write_text("".join(f"{item}\n" for item in requirements), encoding="utf-8")
            print(f"compiled runtime requirements: {len(requirements)} requirements")
            return 0
        requirements = load_requirements(args.requirements)
    except ValueError as error:
        print(f"runtime dependency verifier setup error: {error}", file=sys.stderr)
        return 2

    failures = verify_requirements(requirements)
    if failures:
        print(
            f"runtime dependency verifier failed for {len(failures)} of {len(requirements)} requirements:",
            file=sys.stderr,
        )
        for failure in failures:
            print(f"- {failure.package}: {failure.reason}", file=sys.stderr)
        return 1

    print(f"runtime dependency verifier passed: {len(requirements)} requirements")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
