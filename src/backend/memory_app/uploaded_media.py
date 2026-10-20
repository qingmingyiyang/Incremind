"""Read uploaded local media through existing domain adapters."""

from collections.abc import Mapping
from pathlib import Path, PureWindowsPath

from backend.recognition import RecognitionConflict


VIDEO_SUFFIXES = frozenset({".mp4", ".mkv", ".mov", ".avi", ".m4v"})
MAX_VIDEO_BYTES = 2 * 1024 * 1024 * 1024
UPLOAD_REQUEST_BYTES = MAX_VIDEO_BYTES + 1024 * 1024


def group_entries(records, item):
    """保留上传主记录与图片组的原始条目，不改写冻结路径。"""
    group = records.read('v2_image_groups', item['id'])
    if group is None:
        return [{'path': item['original_path'], 'name': item.get('original_name') or item.get('title')
                 or PureWindowsPath(item['original_path']).name}]
    payload = group.payload
    entries = payload.get('images')
    if (payload.get('project_id') != item['project_id'] or payload.get('item_id') != item['id']
            or not isinstance(entries, list) or not entries
            or any(not isinstance(entry, Mapping) or set(entry) != {'path', 'name'}
                or not all(isinstance(entry[key], str) and entry[key] for key in ('path', 'name'))
                or PureWindowsPath(entry['name']).name != entry['name'] for entry in entries)
            or entries[0]['path'] != item['original_path']):
        raise RecognitionConflict('image_material_binding_invalid')
    return entries


def original_path(runtime_root, item, *, recorded_path=None, ordinal=None):
    """用上传时的受管文件名定位当前运行根，旧绝对路径只作为文件名凭据。"""
    recorded = recorded_path if recorded_path is not None else item.get('original_path')
    identity = item.get('id')
    if not isinstance(recorded, str) or not isinstance(identity, str) or not identity:
        raise ValueError('original_not_found')
    filename = PureWindowsPath(recorded).name
    suffix = PureWindowsPath(filename).suffix
    if (not suffix or suffix != suffix.lower() or PureWindowsPath(identity).name != identity
            or ordinal is not None and (type(ordinal) is not int or ordinal < 1)):
        raise ValueError('original_not_found')
    allowed = {identity + suffix} if ordinal in {None, 1} else set()
    if item.get('input_kind') == 'image':
        allowed.add(f'{identity}-{ordinal or 1}{suffix}')
    if filename not in allowed:
        raise ValueError('original_not_found')
    root = (Path(runtime_root) / 'workspace').resolve(strict=True)
    path = (root / filename).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError('original_not_found')
    return path


class UploadedImageReference:
    """A read-only authorization projection of this one actual upload."""
    def __init__(self, item, path):
        self.reference = 'workspace://' + item['id']
        self.record = {'id': 'image-upload-' + item['id'], 'source_id': item['id'],
            'image_reference': self.reference, 'path': str(path), 'status': 'authorized'}

    def read(self, collection, identity):
        return dict(self.record) if collection == 'authorized_file_refs' and identity == self.record['id'] else None

    def list(self, collection):
        return [dict(self.record)] if collection == 'authorized_file_refs' else []


def read_uploaded_image(owner, item, project_id, item_id, run_id):
    try:
        return owner.read_images(project_id, item_id, run_id, UploadedImageReference)
    except Exception:
        # Provider stderr and local paths must never become a public error or log.
        raise ValueError('image_processing_failed') from None
