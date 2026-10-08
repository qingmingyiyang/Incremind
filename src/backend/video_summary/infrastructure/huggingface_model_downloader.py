from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
import hashlib
import json
import os
from pathlib import Path
import shutil

from backend.video_summary.generation.ports import ProgressReporter


@dataclass(frozen=True)
class HuggingFaceDownloadSpec:
    repo_id: str
    revision: str
    target_dir: Path
    required_files: tuple[str, ...]
    required_file_patterns: tuple[str, ...]
    allow_patterns: tuple[str, ...] = ()
    endpoint: str = "https://huggingface.co"
    max_workers: int = 4


MODEL_ARTIFACT_MANIFEST = ".chriptmas-model-manifest.json"


class HuggingFaceModelDownloader:
    def download(self, spec: HuggingFaceDownloadSpec, reporter: ProgressReporter) -> Path:
        _validate_spec(spec)
        temp_dir = spec.target_dir.with_name(f".{spec.target_dir.name}.download")
        reporter.update("download", 0.0, f"正在连接模型仓库：{spec.repo_id}")
        reporter.raise_if_cancelled()

        try:
            _remove_path(temp_dir)
            temp_dir.mkdir(parents=True, exist_ok=True)
            reporter.update("download", 5.0, f"正在下载模型文件：{spec.repo_id}")
            self._snapshot_download(spec=spec, temp_dir=temp_dir)
            reporter.raise_if_cancelled()
            reporter.update("validate", 95.0, f"正在校验模型文件：{spec.repo_id}")
            _remove_path(temp_dir / ".cache")
            _validate_downloaded_model(temp_dir, spec)
            write_downloaded_model_manifest(temp_dir, spec)
            verify_downloaded_model(temp_dir, spec)
            _remove_path(spec.target_dir)
            temp_dir.replace(spec.target_dir)
        except Exception:
            _remove_path(temp_dir)
            raise
        return spec.target_dir

    def _snapshot_download(self, *, spec: HuggingFaceDownloadSpec, temp_dir: Path) -> None:
        from huggingface_hub import snapshot_download

        kwargs: dict[str, object] = {
            "repo_id": spec.repo_id,
            "revision": spec.revision,
            "local_dir": temp_dir,
            "max_workers": spec.max_workers,
            "token": False,
        }
        if spec.endpoint:
            kwargs["endpoint"] = spec.endpoint
        if spec.allow_patterns:
            kwargs["allow_patterns"] = spec.allow_patterns
        snapshot_download(**kwargs)


def verify_downloaded_model(model_dir: Path, spec: HuggingFaceDownloadSpec) -> None:
    _validate_spec(spec)
    if not model_dir.is_dir() or _is_linklike(model_dir):
        raise RuntimeError("本地模型目录必须是普通目录")
    manifest_path = model_dir / MODEL_ARTIFACT_MANIFEST
    if not manifest_path.is_file() or _is_linklike(manifest_path):
        raise RuntimeError("本地模型制品清单必须是普通文件")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("本地模型缺少有效的受治理制品清单") from error
    expected_identity = {
        "schema_version": "1.0.0",
        "source": "huggingface_snapshot",
        "repo_id": spec.repo_id,
        "revision": spec.revision,
        "endpoint": spec.endpoint,
    }
    if not isinstance(payload, dict) or any(payload.get(key) != value for key, value in expected_identity.items()):
        raise RuntimeError("本地模型制品清单身份不匹配")
    recorded = payload.get("files")
    if not isinstance(recorded, list) or not recorded:
        raise RuntimeError("本地模型制品清单文件列表无效")
    actual = _artifact_records(model_dir, spec)
    if recorded != actual:
        raise RuntimeError("本地模型制品与受治理清单不匹配")


def _validate_downloaded_model(model_dir: Path, spec: HuggingFaceDownloadSpec) -> None:
    _validate_spec(spec)
    missing_files = [file_name for file_name in spec.required_files if not (model_dir / file_name).is_file()]
    if missing_files:
        raise RuntimeError(f"模型下载完成但缺少必要文件：{', '.join(missing_files)}")
    if spec.required_file_patterns and not any(
        fnmatch(path.name, pattern)
        for path in model_dir.rglob("*")
        if path.is_file()
        for pattern in spec.required_file_patterns
    ):
        patterns = ", ".join(spec.required_file_patterns)
        raise RuntimeError(f"模型下载完成但缺少匹配文件：{patterns}")


def _validate_spec(spec: HuggingFaceDownloadSpec) -> None:
    if (
        not isinstance(spec.repo_id, str)
        or spec.repo_id.count("/") != 1
        or not all(part and all(character.isalnum() or character in "._-" for character in part) for part in spec.repo_id.split("/"))
        or not isinstance(spec.revision, str)
        or len(spec.revision) != 40
        or any(character not in "0123456789abcdef" for character in spec.revision)
        or spec.endpoint != "https://huggingface.co"
    ):
        raise RuntimeError("模型下载来源必须绑定有效的不可变revision")


def write_downloaded_model_manifest(model_dir: Path, spec: HuggingFaceDownloadSpec) -> None:
    """Seal files already validated inside a trusted acquisition transaction."""
    _validate_downloaded_model(model_dir, spec)
    payload = {
        "schema_version": "1.0.0",
        "source": "huggingface_snapshot",
        "repo_id": spec.repo_id,
        "revision": spec.revision,
        "endpoint": spec.endpoint,
        "files": _artifact_records(model_dir, spec),
    }
    target = model_dir / MODEL_ARTIFACT_MANIFEST
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    temporary.replace(target)


def _artifact_records(model_dir: Path, spec: HuggingFaceDownloadSpec) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    if not model_dir.is_dir() or _is_linklike(model_dir):
        raise RuntimeError("本地模型目录必须是普通目录")
    for path in sorted(model_dir.iterdir(), key=lambda item: item.name):
        if path.name == MODEL_ARTIFACT_MANIFEST:
            if not path.is_file() or _is_linklike(path):
                raise RuntimeError("本地模型制品清单必须是普通文件")
            continue
        if _is_linklike(path) or not path.is_file():
            raise RuntimeError("本地模型制品只能包含普通文件")
        relative = path.name
        if not any(fnmatch(relative, pattern) for pattern in spec.allow_patterns):
            raise RuntimeError("本地模型包含未审核的额外文件")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        records.append({"path": relative, "size": path.stat().st_size, "sha256": digest.hexdigest()})
    if len(records) > 16:
        raise RuntimeError("本地模型制品文件数量超出治理上限")
    return records


def _is_linklike(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(os.path, "isjunction", lambda _value: False)(path))


def _remove_path(path: Path) -> None:
    if _is_linklike(path):
        raise RuntimeError("模型获取临时路径不能是符号链接或junction")
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()
