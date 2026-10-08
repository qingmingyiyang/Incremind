from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Mapping, Protocol
from dataclasses import dataclass
from uuid import uuid4

from backend.shared.filesystem import atomic_write_text, KeyedLockManager
from backend.shared.interprocess_lock import interprocess_file_lock
from backend.security.file_attribution import file_attribution


class SecretStore(Protocol):
    def set(self, key: str, value: str) -> None:
        ...

    def delete(self, key: str) -> None:
        ...

    def get_snapshot(self, key: str) -> "SecretSnapshot":
        ...

    def get_generation(self, key: str) -> int:
        ...

    def has_secret(self, key: str) -> bool:
        ...

    def replace_many(self, values: Mapping[str, str | None]) -> None:
        ...


@dataclass(frozen=True, slots=True)
class SecretSnapshot:
    """One locally atomic secret value and its monotonically increasing epoch."""

    value: str
    generation: int

    def __post_init__(self) -> None:
        value = self.value
        generation = self.generation
        if not isinstance(value, str) or not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            raise ValueError("secret snapshot is invalid")


class InMemorySecretStore:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._lock = RLock()
        self._values = {
            key: value.strip()
            for key, value in dict(values or {}).items()
            if isinstance(key, str) and isinstance(value, str) and value.strip()
        }
        self._generations = {key: 1 for key in self._values}

    def get_snapshot(self, key: str) -> SecretSnapshot:
        with self._lock:
            return SecretSnapshot(self._values.get(key, ""), self._generations.get(key, 0))

    def get_generation(self, key: str) -> int:
        with self._lock:
            return self._generations.get(key, 0)

    def has_secret(self, key: str) -> bool:
        with self._lock:
            return key in self._values

    def set(self, key: str, value: str) -> None:
        normalized = value.strip()
        if not normalized:
            self.delete(key)
            return
        with self._lock:
            self._values[key] = normalized
            self._generations[key] = self._generations.get(key, 0) + 1

    def delete(self, key: str) -> None:
        with self._lock:
            if key not in self._values:
                return
            self._values.pop(key)
            self._generations[key] = self._generations.get(key, 0) + 1

    def replace_many(self, values: Mapping[str, str | None]) -> None:
        with self._lock:
            for key, value in values.items():
                normalized = value.strip() if isinstance(value, str) else ""
                if normalized:
                    self._values[key] = normalized
                else:
                    self._values.pop(key, None)
                self._generations[key] = self._generations.get(key, 0) + 1


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


class DPAPIFileSecretStore:
    def __init__(self, path: Path, *, mutation_attribution=None) -> None:
        if os.name != "nt":
            raise RuntimeError("DPAPI 安全存储仅支持 Windows。")
        self._path = path
        self._lock = RLock()
        self._mutation_attribution = mutation_attribution

    def _locked(self):
        return self._lock

    def _encrypt(self, value: bytes) -> bytes:
        return _protect(value)

    def _decrypt(self, value: bytes) -> bytes:
        return _unprotect(value)

    def get_snapshot(self, key: str) -> SecretSnapshot:
        with self._locked():
            payload = self._read()
            record = payload.get(key)
            if record is None:
                return SecretSnapshot("", 0)
            encoded = record.get("ciphertext", "")
            generation = record["generation"]
            if not encoded:
                return SecretSnapshot("", generation)
            try:
                return SecretSnapshot(
                    self._decrypt(base64.b64decode(encoded)).decode("utf-8"), generation,
                )
            except (ValueError, OSError, UnicodeDecodeError) as error:
                raise RuntimeError("本机安全存储中的密钥无法解密。") from error

    def get_generation(self, key: str) -> int:
        with self._locked():
            record = self._read().get(key)
            return 0 if record is None else record["generation"]

    def has_secret(self, key: str) -> bool:
        return bool(self.get_snapshot(key).value)

    def set(self, key: str, value: str) -> None:
        normalized = value.strip()
        if not normalized:
            self.delete(key)
            return
        with self._locked():
            payload = self._read()
            previous = payload.get(key, {"generation": 0})
            payload[key] = {
                "ciphertext": base64.b64encode(self._encrypt(normalized.encode("utf-8"))).decode("ascii"),
                "generation": previous["generation"] + 1,
            }
            self._write(payload)

    def delete(self, key: str) -> None:
        with self._locked():
            payload = self._read()
            previous = payload.get(key)
            if previous is None or not previous.get("ciphertext"):
                return
            payload[key] = {"generation": previous["generation"] + 1}
            self._write(payload)

    def replace_many(self, values: Mapping[str, str | None]) -> None:
        with self._locked():
            payload = self._read()
            for key, value in values.items():
                previous = payload.get(key, {"generation": 0})
                normalized = value.strip() if isinstance(value, str) else ""
                record: dict[str, object] = {"generation": int(previous["generation"]) + 1}
                if normalized:
                    record["ciphertext"] = base64.b64encode(
                        self._encrypt(normalized.encode("utf-8")),
                    ).decode("ascii")
                payload[key] = record
            self._write(payload)

    def _read(self) -> dict[str, dict[str, object]]:
        if not self._path.is_file():
            return {}
        payload = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("本机安全存储文件损坏。")
        if payload.get("schema_version") == 2:
            records = payload.get("records")
            if not isinstance(records, dict):
                raise RuntimeError("本机安全存储文件损坏。")
            normalized: dict[str, dict[str, object]] = {}
            for key, value in records.items():
                if not isinstance(key, str) or not isinstance(value, dict):
                    raise RuntimeError("本机安全存储文件损坏。")
                generation = value.get("generation")
                ciphertext = value.get("ciphertext")
                if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
                    raise RuntimeError("本机安全存储文件损坏。")
                if ciphertext is not None and (not isinstance(ciphertext, str) or not ciphertext):
                    raise RuntimeError("本机安全存储文件损坏。")
                normalized[key] = {"generation": generation}
                if ciphertext is not None:
                    normalized[key]["ciphertext"] = ciphertext
            return normalized
        # The original ciphertext-only shape was already durable.  Treat every
        # extant value as generation one until the next atomic write upgrades it.
        return {
            str(key): {"ciphertext": str(value), "generation": 1}
            for key, value in payload.items()
        }

    def _write(self, payload: dict[str, dict[str, object]]) -> None:
        value={"schema_version":2,"records":payload}
        if self._mutation_attribution is not None:
            previous=json.loads(self._path.read_text(encoding='utf8')) if self._path.exists() else {}
            value=self._mutation_attribution.json(value,previous)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self._path,
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        )


_SERVER_SECRET_LOCKS = KeyedLockManager()


class ServerSecretStoreError(RuntimeError):
    """An allowlisted server credential error, carrying no secret material."""

    def __init__(self, code: str) -> None:
        if code not in {
            'server_master_key_required', 'server_master_key_invalid',
            'server_secret_decryption_failed', 'server_secret_file_invalid',
            'server_secret_store_busy', 'server_secret_store_unavailable',
        }:
            raise ValueError('server_secret_error_code_invalid')
        self.code = code
        super().__init__(code)


def _server_cipher():
    from cryptography.fernet import Fernet
    key = os.environ.get('CHRIPTMAS_SERVER_MASTER_KEY')
    if key is None:
        directory = os.environ.get('CREDENTIALS_DIRECTORY', '').strip()
        if not directory:
            raise ServerSecretStoreError('server_master_key_required')
        try:
            with (Path(directory) / 'chriptmas-master-key').open('rb') as source:
                key = source.read(256).decode('ascii').strip()
        except (OSError, UnicodeError):
            raise ServerSecretStoreError('server_master_key_required') from None
    try:
        return Fernet(key.strip().encode('ascii'))
    except (ValueError, UnicodeError):
        raise ServerSecretStoreError('server_master_key_invalid') from None


def _restrict_secret_file(descriptor: int, path: Path) -> None:
    if hasattr(os, 'fchmod'):
        os.fchmod(descriptor, 0o600)
    else:
        # Windows test hosts exercise this adapter without claiming POSIX ACLs.
        os.chmod(path, 0o600)


def restrict_server_secret_file(path: Path) -> None:
    """Tighten one existing credential file without requiring its master key."""
    path = Path(path)
    descriptor = None
    try:
        if path.is_symlink():
            raise ServerSecretStoreError('server_secret_file_invalid')
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        opened = os.fstat(descriptor)
        named = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(named.st_mode) or (
                opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise ServerSecretStoreError('server_secret_file_invalid')
        _restrict_secret_file(descriptor, path)
        current = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(current.st_mode) or (
                opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise ServerSecretStoreError('server_secret_file_invalid')
    except OSError:
        raise ServerSecretStoreError('server_secret_store_unavailable') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


class ServerFileSecretStore(DPAPIFileSecretStore):
    """The existing credential epochs, encrypted with a server-supplied master."""

    def __init__(self, path: Path, *, mutation_attribution=None) -> None:
        self._path = Path(path)
        self._mutation_attribution = mutation_attribution

    @contextmanager
    def _locked(self):
        try:
            with _SERVER_SECRET_LOCKS.hold(str(self._path.resolve())):
                with interprocess_file_lock(self._path):
                    yield
        except TimeoutError:
            raise ServerSecretStoreError('server_secret_store_busy') from None
        except OSError:
            raise ServerSecretStoreError('server_secret_store_unavailable') from None

    def get_snapshot(self, key: str) -> SecretSnapshot:
        if not self._path.exists() and not self._path.is_symlink():
            return SecretSnapshot('', 0)
        try:
            return super().get_snapshot(key)
        except ServerSecretStoreError:
            raise
        except RuntimeError:
            raise ServerSecretStoreError('server_secret_decryption_failed') from None

    def get_generation(self, key: str) -> int:
        if not self._path.exists() and not self._path.is_symlink():
            return 0
        return super().get_generation(key)

    def _encrypt(self, value: bytes) -> bytes:
        return _server_cipher().encrypt(value)

    def _decrypt(self, value: bytes) -> bytes:
        from cryptography.fernet import InvalidToken
        try:
            return _server_cipher().decrypt(value)
        except InvalidToken:
            raise ServerSecretStoreError('server_secret_decryption_failed') from None

    def _read(self) -> dict[str, dict[str, object]]:
        if self._path.is_symlink():
            raise ServerSecretStoreError('server_secret_file_invalid')
        if not self._path.exists():
            return {}
        restrict_server_secret_file(self._path)
        try:
            payload = json.loads(self._path.read_text(encoding='utf-8'))
            if not isinstance(payload, dict) or payload.get('encryption') != 'fernet-v1' or payload.get('schema_version') != 2:
                raise ServerSecretStoreError('server_secret_file_invalid')
            try:
                records = super()._read()
            except RuntimeError:
                raise ServerSecretStoreError('server_secret_file_invalid') from None
            # A wrong master must not overwrite one value and leave the other
            # credentials encrypted with an incompatible master.
            for record in records.values():
                if record.get('ciphertext'):
                    self._decrypt(base64.b64decode(record['ciphertext'], validate=True))
            return records
        except (ValueError, OSError, UnicodeError):
            raise ServerSecretStoreError('server_secret_decryption_failed') from None

    def _write(self, payload: dict[str, dict[str, object]]) -> None:
        _server_cipher()  # Even deletion/batch saves require the master.
        value={'schema_version':2,'encryption':'fernet-v1','records':payload}
        if self._mutation_attribution is not None:
            previous=json.loads(self._path.read_text(encoding='utf8')) if self._path.exists() else {}
            value=self._mutation_attribution.json(value,previous)
        if self._path.is_symlink():
            raise ServerSecretStoreError('server_secret_file_invalid')
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(f'.{self._path.name}.{uuid4().hex}.tmp')
        try:
            descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
                _restrict_secret_file(output.fileno(), temporary)
                output.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self._path)
        finally:
            if temporary.exists():
                temporary.unlink()


def _is_windows() -> bool:
    return os.name == 'nt'


def build_model_secret_store(root_dir: Path) -> SecretStore:
    attribution=file_attribution(root_dir,'secrets')
    if _is_windows():
        return DPAPIFileSecretStore(root_dir / 'secrets.json',mutation_attribution=attribution)
    return ServerFileSecretStore(root_dir / 'secrets.json',mutation_attribution=attribution)


def build_secret_store(root_dir: Path) -> SecretStore:
    if file_attribution(root_dir,'secrets') is not None:
        return build_model_secret_store(root_dir)
    if not _is_windows():
        return ServerFileSecretStore(root_dir / 'secrets.json')
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    base = Path(local_app_data) if local_app_data else root_dir / "data"
    workspace_key = hashlib.sha256(str(root_dir.resolve()).encode("utf-8")).hexdigest()[:16]
    return DPAPIFileSecretStore(base / "Chriptmas_Replay" / "secrets" / f"{workspace_key}.json")


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _protect(data: bytes) -> bytes:
    source, source_buffer = _blob(data)
    output = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    result = crypt32.CryptProtectData(
        ctypes.byref(source),
        "Chriptmas_Replay",
        None,
        None,
        None,
        0x1,
        ctypes.byref(output),
    )
    del source_buffer
    if not result:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)


def _unprotect(data: bytes) -> bytes:
    source, source_buffer = _blob(data)
    output = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    result = crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        None,
        None,
        None,
        0x1,
        ctypes.byref(output),
    )
    del source_buffer
    if not result:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)
