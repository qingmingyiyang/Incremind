from datetime import datetime, timezone

from core.storage_provider import SQLiteUnitOfWorkConflict
from .policies import get


SETTINGS = 'v2_backup_settings'
KEY = 'schedule'
JOBS = 'v2_backup_jobs'
COMMANDS = 'v2_backup_schedule_commands'
VERSION = '@1'


class BackupSchedule:
    def __init__(self, records, *, now=lambda: datetime.now(timezone.utc)):
        self.records, self.now = records, now

    @property
    def policy(self):
        return get('backup_schedule', version=VERSION)

    def settings(self, reader=None):
        row = (reader or self.records).read(SETTINGS, KEY)
        return {**self.policy({'operation': 'settings', 'settings': row.payload if row else None}),
                'revision': row.revision if row else 0}

    def slot(self, *, reader=None):
        return self.policy({'operation': 'slot', 'settings': self.settings(reader), 'now': self.now()})

    def due(self):
        slot = self.slot()
        if slot is None or not slot['due']:
            return False
        row = self.records.read(JOBS, slot['key'])
        return row is None or row.payload.get('status') not in {'done', 'failed'}

    def view(self):
        settings, now = self.settings(), self.now()
        slot = self.policy({'operation': 'slot', 'settings': settings, 'now': now})
        row = self.records.read(JOBS, slot['key']) if slot else None
        return {**settings, 'current_status': row.payload.get('status') if row else None,
            'next_at': self.policy({'operation': 'next', 'settings': settings, 'now': now,
                'completed': row is not None and row.payload.get('status') in {'done', 'failed'}}), 'policy_version': VERSION}

    def save(self, body, *, validate=lambda: None):
        fields = {'frequency', 'time', 'timezone', 'weekday', 'month_day'}
        if (not isinstance(body, dict) or set(body) != fields | {'expected_revision', 'command_id'}
                or type(body['expected_revision']) is not int or body['expected_revision'] < 0):
            raise ValueError('backup_schedule_invalid')
        value = self.policy({'operation': 'settings', 'settings': body})
        with self.records.begin() as tx:
            validate()
            prior = tx.read(COMMANDS, body['command_id'])
            if prior is not None:
                if prior.payload['settings'] != value or prior.payload['expected_revision'] != body['expected_revision']:
                    raise SQLiteUnitOfWorkConflict('backup_schedule_command_changed')
                return {**prior.payload, 'command_id': body['command_id']}
            row = tx.read(SETTINGS, KEY)
            if (row.revision if row else 0) != body['expected_revision']:
                raise SQLiteUnitOfWorkConflict('backup_schedule_changed')
            saved = tx.put(SETTINGS, KEY, {**value, 'policy_version': VERSION}, expected_revision=body['expected_revision'])
            receipt = {'settings': value, 'expected_revision': body['expected_revision'],
                       'revision': saved.revision, 'state': 'saved'}
            tx.put(COMMANDS, body['command_id'], receipt, expected_revision=0)
            validate()
            tx.commit()
        return {**receipt, 'command_id': body['command_id']}

    def command(self, command_id):
        row = self.records.read(COMMANDS, command_id)
        return {**row.payload, 'command_id': command_id} if row is not None else None
