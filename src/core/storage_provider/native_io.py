import os
from pathlib import Path


def native_io_path(path: Path, *, resolve=True) -> str:
    resolved = str(path.resolve(strict=False) if resolve else path.absolute())
    if os.name != 'nt' or resolved.startswith('\\\\?\\'):
        return resolved
    if resolved.startswith('\\\\'):
        return '\\\\?\\UNC\\' + resolved[2:]
    return '\\\\?\\' + resolved
