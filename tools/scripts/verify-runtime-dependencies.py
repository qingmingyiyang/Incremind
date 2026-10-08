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

from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, Version


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REQUIREMENTS = PROJECT_ROOT / "requirements.txt"

IMPORT_TARGETS = {
    "fastapi": "fastapi",
    "anyio": "anyio",
    "uvicorn": "uvicorn",
    "python-multipart": "multipart",
    "faster-whisper": "faster_whisper",
    "huggingface-hub": "huggingface_hub",
    "httpx": "httpx",
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
}


@dataclass(frozen=True)
class DependencyFailure:
    package: str
    reason: str


def _canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


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
    args = parser.parse_args()

    try:
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
