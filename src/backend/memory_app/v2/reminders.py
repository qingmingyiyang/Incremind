"""你定的提醒保存在独立事实集合中，时间决策由已登记的纯策略负责。"""
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Request

from ..workspace_contracts import _json, _project, _text
from .policies import get, version
from .policies.insight_time import instant
from .privacy import is_private_project
from core.storage_provider import SQLiteUnitOfWorkConflict


COLLECTION = 'v2_reminders'
_UNSET = object()


@dataclass(frozen=True)
class PreparedReminder:
    """解析的参考时刻和候选版本只在本次处理内保留，不进入事实记录。"""
    at: str
    text: str
    reference: str
    policy_version: str


def _view(row):
    return {'id': row.object_id, **row.payload, 'revision': row.revision}


def _scene(scene):
    if scene is not None and (not isinstance(scene, str) or not scene.strip()):
        raise HTTPException(400, 'invalid_reminder_scene')
    return scene


def _replay(row, *, project_id, scene, turn_id, text):
    expected = {'project_id': project_id, 'scene': scene, 'turn_id': turn_id, 'text': text}
    if any(row.payload[key] != value for key, value in expected.items()):
        raise HTTPException(409, 'reminder_turn_conflict')
    return _view(row)


class ReminderService:
    """同一 Turn 的提醒只建一次，所有写入遵循调用方事务和事实修订。"""
    def __init__(self, records, *, now, local_timezone, policy_version=None):
        self.records, self.now, self.local_timezone = records, now, local_timezone
        self.policy_version = policy_version

    def _policy(self):
        return get('remind', version=self.policy_version)

    def parse(self, text, *, eligible_text=None):
        selected = version('remind') if self.policy_version is None else self.policy_version
        policy = get('remind', version=selected)
        reference = self.now().isoformat()
        parsed = policy(text if eligible_text is None else eligible_text, reference, self.local_timezone)
        if parsed is None:
            return None
        # 入口去范围标签只用于识别，事实正文始终采用调用方传入的完整原话。
        return PreparedReminder(parsed['at'], text, reference,
                                '@' + selected.lstrip('@'))

    def stage(self, tx, *, project_id, scene, turn_id, parsed):
        project_id, turn_id = _project(project_id), _project(turn_id)
        scene = _scene(scene)
        if not isinstance(parsed, PreparedReminder):
            raise ValueError('invalid_prepared_reminder')
        _text(parsed.text, 'reminder_text')
        payload = {'project_id': project_id, 'scene': scene, 'at': parsed.at,
                   'text': parsed.text, 'turn_id': turn_id, 'state': 'active'}
        existing = tx.read(COLLECTION, turn_id)
        if existing is not None:
            return _replay(existing, project_id=project_id, scene=scene, turn_id=turn_id,
                           text=parsed.text)
        # 保存不能因处理跨过到点时刻而推翻本轮已接受的未来时间。
        normalized = get('remind', version=parsed.policy_version)(
            parsed.at, parsed.reference, self.local_timezone, operation='at')
        if normalized is None:
            raise HTTPException(400, 'invalid_reminder_at')
        payload['at'] = normalized
        return _view(tx.put(COLLECTION, turn_id, payload, expected_revision=0))

    def create(self, *, project_id, scene, turn_id, text):
        project_id, turn_id, scene = _project(project_id), _project(turn_id), _scene(scene)
        _text(text, 'reminder_text')
        with self.records.begin() as tx:
            old = tx.read(COLLECTION, turn_id)
            if old is not None:
                # 重放取已保存的事实，不按更晚的时钟重新解释相对日期。
                return _replay(old, project_id=project_id, scene=scene, turn_id=turn_id,
                               text=text)
            parsed = self.parse(text)
            if parsed is None:
                return None
            saved = self.stage(tx, project_id=project_id, scene=scene, turn_id=turn_id,
                               parsed=parsed)
            tx.commit()
            return saved

    def read(self, identity, *, project_id=None, reader=None):
        row = (reader or self.records).read(COLLECTION, _project(identity))
        if row is None or project_id is not None and row.payload['project_id'] != project_id:
            raise HTTPException(404, 'reminder_not_found')
        return _view(row)

    def update(self, identity, *, project_id, expected_revision, at=_UNSET, state=_UNSET):
        if type(expected_revision) is not int or expected_revision < 1:
            raise HTTPException(400, 'invalid_expected_revision')
        if at is _UNSET and state is _UNSET:
            raise HTTPException(400, 'invalid_reminder_fields')
        if state is not _UNSET and (not isinstance(state, str)
                                    or state not in {'active', 'done', 'deleted'}):
            raise HTTPException(400, 'invalid_reminder_state')
        if at is not _UNSET:
            at = self._policy()(at, self.now().isoformat(), self.local_timezone, operation='at')
            if at is None:
                raise HTTPException(400, 'invalid_reminder_at')
        try:
            with self.records.begin() as tx:
                current = self.read(identity, project_id=_project(project_id), reader=tx)
                if current['revision'] != expected_revision:
                    raise HTTPException(409, 'reminder_revision_conflict')
                payload = {key: current[key] for key in
                           ('project_id', 'scene', 'at', 'text', 'turn_id', 'state')}
                if at is not _UNSET:
                    payload['at'] = at
                if state is not _UNSET:
                    payload['state'] = state
                saved = tx.put(COLLECTION, identity, payload, expected_revision=expected_revision)
                tx.commit()
                return _view(saved)
        except SQLiteUnitOfWorkConflict:
            raise HTTPException(409, 'reminder_revision_conflict') from None

    def due(self, project_id, *, at=None):
        _project(project_id)
        clock = instant((self.now() if at is None else at).isoformat())
        if clock is None:
            raise ValueError('invalid_reminder_reference')
        with self.records.begin() as tx:
            if is_private_project(tx, project_id):
                return []
            result = []
            for row in tx.list(COLLECTION):
                payload = row.payload
                if payload['project_id'] != project_id or payload['state'] != 'active':
                    continue
                due_at = instant(payload['at'])
                if due_at is None:
                    raise ValueError('invalid_stored_reminder_at')
                if due_at <= clock:
                    result.append(_view(row))
            return sorted(result, key=lambda value: (value['at'], value['id']))


def install_reminder_routes(application, *, service):
    """只接新 v2 读取与修改接口，应用装配由产品入口统一负责。"""
    router = APIRouter(prefix='/api/v2/reminders')

    @router.get('/{id}')
    async def get_reminder(id: str, project_id: str):
        return service.read(id, project_id=_project(project_id))

    @router.patch('/{id}')
    async def patch_reminder(id: str, request: Request):
        body = await _json(request)
        if (set(body).difference({'at', 'state', 'expected_revision'})
                or 'expected_revision' not in body):
            raise HTTPException(400, 'invalid_reminder_fields')
        current = service.read(id)
        return service.update(id, project_id=current['project_id'], **body)

    application.include_router(router)
