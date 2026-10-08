"""本机目录首次确认的唯一 CAS owner；界面确认由后续入口负责。"""
from pathlib import Path
import re
from uuid import uuid4

from backend.shared.deployment import DeploymentLayout
from backend.shared.secret_detection import contains_secret
from core.storage_provider import SQLiteUnitOfWorkConflict

from .external_workspace import task_path_identity, validate_task_path


PERMISSIONS = 'v2_external_host_permissions'
_IDENTITY = re.compile(r'[A-Za-z0-9][A-Za-z0-9._~-]{0,127}\Z')


def _invalid():
    raise ValueError('external_host_permission_unavailable')


class HostPermissions:
    def __init__(self, records, *, owner_id, deployment):
        if (not isinstance(deployment, DeploymentLayout) or deployment.mode != 'desktop'
                or owner_id != 'local-user'):
            _invalid()
        self.records, self.owner_id, self.deployment = records, owner_id, deployment

    def _folder(self, path):
        if (not isinstance(path, Path) or not path.is_absolute() or contains_secret(str(path))):
            _invalid()
        try:
            validate_task_path(path)
            return task_path_identity(path, directory=True)
        except (OSError, ValueError):
            _invalid()

    def _row(self, reader, path):
        rows = [row for row in reader.list(PERMISSIONS)
            if row.payload.get('owner_id') == self.owner_id and row.payload.get('scope') == 'folder'
            and row.payload.get('path') == str(path)]
        if len(rows) > 1:
            _invalid()
        return rows[0] if rows else None

    def folder_reference(self, path):
        identity = self._folder(path)
        row = self._row(self.records, path)
        if row is None:
            return None
        expected = {'owner_id':self.owner_id, 'scope':'folder', 'path':str(path),
            'turn_id':None, 'directory_identity':identity}
        if (row.payload != expected or not _IDENTITY.fullmatch(row.object_id)
                or contains_secret(row.object_id)):
            _invalid()
        return {'id':row.object_id, 'revision':row.revision}

    def confirm_folder(self, path, *, confirmed, expected_revision):
        if type(confirmed) is not bool or confirmed is not True:
            raise ValueError('external_host_confirmation_required')
        if type(expected_revision) is not int or expected_revision < 0:
            _invalid()
        identity = self._folder(path)
        wanted = {'owner_id':self.owner_id, 'scope':'folder', 'path':str(path),
            'turn_id':None, 'directory_identity':identity}
        with self.records.begin() as tx:
            row = self._row(tx, path)
            if (row.revision if row else 0) != expected_revision:
                raise SQLiteUnitOfWorkConflict('external_host_permission_revision_conflict')
            if self._folder(path) != identity:
                _invalid()
            if row is not None:
                if row.payload != wanted:
                    _invalid()
                return {'id':row.object_id, 'revision':row.revision}
            row = tx.put(PERMISSIONS, 'folder-' + uuid4().hex, wanted, expected_revision=0)
            tx.commit()
        return {'id':row.object_id, 'revision':row.revision}
