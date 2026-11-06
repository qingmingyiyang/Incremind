import logging
from datetime import datetime, timezone
from uuid import uuid4

from backend.recognition import RecognitionConflict
from core.storage_provider import SQLiteUnitOfWorkConflict

from ..model_config import ModelConfigurationError
from ..structured_generation import InvalidStructuredOutput
from ..transaction_records import TransactionRecords
from .intent import parse_scope_tag
from .memory_turn import MemoryTurn
from .policies import get, version
from .policies.project_angle_learning import (
    AngleProposal, DISMISS_WINDOW, INACTIVE_WINDOW, MAX_EXAMPLES, MIN_DAYS, MIN_QUESTIONS, QUESTION_WINDOW,
)
from .privacy import egress_allowed, is_private_project
from .signals import SignalService, _fact_time
from .turn_logical_scope import TurnLogicalScope


ANGLES = 'v2_project_angles'
PROPOSALS = 'v2_project_angle_proposals'
_LOGGER = logging.getLogger(__name__)
_ANGLE_FIELDS = ('id', 'label', 'focus', 'kind', 'refines', 'triggers')


def _angles(row):
    return row.payload['items'] if row is not None else ()


def active_angles(reader, project_id, *, created_at=None):
    bound = created_at if isinstance(created_at, datetime) else _fact_time(created_at)
    if created_at is not None and (bound is None or bound.tzinfo is None):
        raise ValueError('project_angle_material_time_invalid')
    return tuple({**{field: item[field] for field in _ANGLE_FIELDS}, 'triggers': list(item['triggers'])}
        for item in _angles(reader.read(ANGLES, project_id)) if item['state'] == 'active'
        and (bound is None or datetime.fromisoformat(item['confirmed_at']) <= bound))


class ProjectAngles:
    def __init__(self, application, records, models, *, local_timezone=timezone.utc,
                 now=lambda: datetime.now(timezone.utc)):
        self.records, self.models, self.now = records, models, now
        self.local_timezone = local_timezone
        self.scopes = TurnLogicalScope(application)

    def _source(self, row, project, *, reader=None):
        if row is None:
            return None
        value = row.payload
        answer = value.get('receipt', {}).get('ask')
        question = value.get('user_text')
        at = _fact_time(value.get('created_at'))
        if (value.get('project_id') != project or value.get('intent') != 'ask' or value.get('by') == 'admin'
                or at is None or not isinstance(question, str) or not question.strip()
                or not isinstance(answer, dict) or not isinstance(answer.get('answer'), str)
                or not isinstance(answer.get('citations'), list) or answer.get('interruption')):
            return None
        try:
            if self.scopes.read(row, reader=reader) != 'personal':
                return None
        except RecognitionConflict:
            return None
        _, _, cleaned = parse_scope_tag(question)
        return (row, cleaned, at, not answer['citations']) if cleaned else None

    def _questions(self, project, *, now, cutoff):
        after = max(now - INACTIVE_WINDOW, cutoff or datetime.min.replace(tzinfo=timezone.utc))
        rows = self.records.list_matching('v2_turns', project_id=project, intent='ask')
        questions = []
        for row in rows:
            at = _fact_time(row.payload.get('created_at'))
            if at is None or not after < at <= now:
                continue
            fact = self._source(row, project)
            if fact is not None:
                questions.append(fact)
        return sorted(questions, key=lambda fact: (fact[2], fact[0].object_id))

    def _maintain(self, project, questions, now):
        prior = self.records.read(ANGLES, project)
        if prior is None:
            return
        learned = active_angles(self.records, project)
        route = get('angle_route')
        used = {}
        for _, question, at, _ in questions:
            for identity in route(question, learned=learned):
                used.setdefault(identity, []).append(at)
        items = []
        for item in _angles(prior):
            if item['state'] == 'active':
                since = datetime.fromisoformat(item['activated_at'])
                mapped = used.get(item['id'], ())
                matches = [at for at in mapped if at >= since]
                last = max(matches, default=since)
                observed = _fact_time(item.get('last_used_at'))
                if observed is not None:
                    last = max(last, observed)
                item = {**item, 'last_used_at': last.isoformat(),
                        'count': sum(at >= now - QUESTION_WINDOW for at in mapped)}
                if last + INACTIVE_WINDOW <= now:
                    item.update(state='paused', paused_at=now.isoformat(), paused_by='auto')
            items.append(item)
        if items != prior.payload['items']:
            with self.records.begin() as tx:
                tx.put(ANGLES, project, {**prior.payload, 'items': items}, expected_revision=prior.revision)
                tx.commit()

    def _groups(self, project, questions, now):
        gaps = self.records.read('v2_gaps', project)
        dismissed = {row.payload['gap_id'] for row in self.records.list_matching('v2_gap_dismissals', project_id=project)}
        missing = {identity for group in gaps.payload['items'] if group['unresolved'] and group['id'] not in dismissed
            for identity in group['turn_ids']} if gaps else set()
        route, related = get('angle_route'), get('gap', version='@1').related
        learned = active_angles(self.records, project)
        groups = []
        for fact in questions:
            row, question, at, uncited = fact
            if at < now - QUESTION_WINDOW or not (uncited or row.object_id in missing or not route(question, learned=learned)):
                continue
            group = next((group for group in groups if related(group[0][1], question)), None)
            if group is None:
                groups.append([fact])
            else:
                group.append(fact)
        return [group for group in groups if len(group) >= MIN_QUESTIONS
                and len({fact[2].astimezone(self.local_timezone).date() for fact in group}) >= MIN_DAYS]

    def _known(self, reader, project, question, now, settings_revision):
        related = get('gap', version='@1').related
        for row in reader.list(PROPOSALS):
            value = row.payload
            if value['project_id'] != project:
                continue
            if value['state'] in {'learning', 'proposed'} and value['settings_revision'] != settings_revision:
                continue
            if value['state'] == 'dismissed' and datetime.fromisoformat(value['decided_at']) + DISMISS_WINDOW <= now:
                continue
            if value['state'] == 'learning' and datetime.fromisoformat(value['created_at']) + QUESTION_WINDOW <= now:
                continue
            anchor = reader.read('v2_turns', value['anchor'])
            if (anchor is not None and anchor.payload.get('project_id') == project
                    and isinstance(anchor.payload.get('user_text'), str)
                    and related(question, parse_scope_tag(anchor.payload['user_text'])[2])):
                return True
        return False

    def _claim(self, project, group, now, settings):
        anchor, question, _, _ = group[0]
        # 每天至少留一个例子，余量按最新提问补齐，记录只保存编号与修订。
        by_day = {}
        for row, _, at, _ in group:
            by_day[at.astimezone(self.local_timezone).date()] = row
        examples = list(by_day.values())[-MAX_EXAMPLES:]
        for row, *_ in reversed(group):
            if len(examples) >= MAX_EXAMPLES:
                break
            if row not in examples:
                examples.append(row)
        with self.records.begin() as tx:
            if (SignalService._settings(tx) != settings or not self._allowed(tx, project)
                    or self._known(tx, project, question, now, settings[1])):
                return None
            if any(tx.read('v2_turns', row.object_id) != row for row in (anchor, *examples)):
                raise RecognitionConflict('project_angle_questions_changed')
            identity = 'angle-' + uuid4().hex
            row = tx.put(PROPOSALS, identity, {'project_id': project, 'state': 'learning',
                'anchor': anchor.object_id, 'question_refs': [[item.object_id, item.revision] for item in examples],
                'count': len(group), 'created_at': now.isoformat(), 'settings_revision': settings[1],
                'policy': version('project_angle_learning')}, expected_revision=0)
            tx.commit()
            return row

    def _allowed(self, reader, project):
        return (reader.read('v2_projects', project) is not None and not is_private_project(reader, project)
                and egress_allowed(reader, self.models, project, 'generation'))

    def _generate(self, row, now, configuration):
        value = row.payload
        project, references = value['project_id'], value['question_refs']

        def validate(reader):
            settings, revision = SignalService._settings(reader)
            if (not settings['enabled'] or revision != value['settings_revision'] or not self._allowed(reader, project)
                    or self.models.public()['generation'] != configuration or reader.read(PROPOSALS, row.object_id) != row):
                raise RecognitionConflict('project_angle_inputs_changed')
            questions = []
            for identity, expected_revision in references:
                source = reader.read('v2_turns', identity)
                fact = self._source(source, project, reader=None if reader is self.records else reader)
                if fact is None or source.revision != expected_revision or not now - QUESTION_WINDOW <= fact[2] <= now:
                    raise RecognitionConflict('project_angle_questions_changed')
                questions.append(fact[1])
            return questions

        questions = validate(self.records)
        turn = MemoryTurn(self.records, self.models, kind='memory.consolidate', project=project,
            key=['project-angle-v1', row.object_id], materials=[], validate=lambda: validate(self.records),
            automatic={'producer': 'consolidation', 'mode': 'project', 'scene': None, 'anchors': []})
        messages = get('project_angle_learning', version=value['policy'])(questions)
        output, _ = turn.generate(messages, response_model=AngleProposal, max_tokens=500)

        def existing():
            saved = self.records.read(PROPOSALS, row.object_id)
            return saved.object_id if saved is not None and saved.payload.get('turn_id') == turn.turn_id else None

        def commit():
            with self.records.begin() as tx:
                turn.validate_commit(tx)
                validate(TransactionRecords(tx))
                tx.put(PROPOSALS, row.object_id, {**value, 'state': 'proposed', 'proposal': output.model_dump(mode='json'),
                    'turn_id': turn.turn_id}, expected_revision=row.revision)
                tx.commit()
                return row.object_id

        return turn.propose(key='project-angle', write=commit, existing=existing)

    def run(self):
        now = self.now().astimezone(timezone.utc)
        settings = SignalService._settings(self.records)
        if not settings[0]['enabled']:
            return
        configuration = self.models.public()['generation']
        cutoff = _fact_time(settings[0]['cleared_at'])
        for project_row in self.records.list('v2_projects'):
            project = project_row.object_id
            try:
                questions = self._questions(project, now=now, cutoff=cutoff)
                self._maintain(project, questions, now)
                if (not configuration.get('configured') or configuration.get('enabled') is False
                        or not self._allowed(self.records, project)):
                    continue
                pending = [row for row in self.records.list_matching(PROPOSALS, project_id=project, state='learning')
                    if row.payload['settings_revision'] == settings[1]
                    and datetime.fromisoformat(row.payload['created_at']) + QUESTION_WINDOW > now]
                for group in self._groups(project, questions, now):
                    row = self._claim(project, group, now, settings)
                    if row is not None:
                        pending.append(row)
                for row in pending:
                    try:
                        self._generate(row, now, configuration)
                    except (RecognitionConflict, InvalidStructuredOutput, ModelConfigurationError, SQLiteUnitOfWorkConflict) as error:
                        _LOGGER.warning('project_angle_generation_failed exception_type=%s', type(error).__name__)
            except (RecognitionConflict, SQLiteUnitOfWorkConflict) as error:
                _LOGGER.warning('project_angle_maintenance_failed exception_type=%s', type(error).__name__)

    def current(self, project):
        if self.records.read('v2_projects', project) is None:
            return []
        prior = self.records.read(ANGLES, project)
        active = [{'id': item['id'], 'label': item['label'], 'count': item['count'],
                   'state': item['state'], 'revision': prior.revision} for item in _angles(prior)]
        settings, revision = SignalService._settings(self.records)
        if is_private_project(self.records, project) or not settings['enabled']:
            return active
        proposed = [{'id': row.object_id, 'label': row.payload['proposal']['label'], 'count': row.payload['count'],
                     'state': 'proposed', 'revision': row.revision} for row in self.records.list_matching(
                         PROPOSALS, project_id=project, state='proposed') if row.payload['settings_revision'] == revision]
        return proposed + active

    def decide(self, project, identity, action, revision, *, by):
        if (not isinstance(action, str) or action not in {'confirm', 'dismiss', 'pause', 'resume'}
                or type(revision) is not int or revision < 1 or by not in {'user', 'admin'}):
            raise ValueError('project_angle_decision_invalid')
        now = self.now().astimezone(timezone.utc).isoformat()
        with self.records.begin() as tx:
            if tx.read('v2_projects', project) is None:
                raise RecognitionConflict('project_angle_unavailable')
            prior = tx.read(ANGLES, project)
            items = list(_angles(prior))
            if action in {'confirm', 'dismiss'}:
                row = tx.read(PROPOSALS, identity)
                settings, settings_revision = SignalService._settings(tx)
                if (row is None or row.revision != revision or row.payload['project_id'] != project
                        or row.payload['state'] != 'proposed' or is_private_project(tx, project)
                        or not settings['enabled'] or row.payload['settings_revision'] != settings_revision):
                    raise RecognitionConflict('project_angle_changed')
                if action == 'confirm':
                    proposal = AngleProposal.model_validate(row.payload['proposal']).model_dump(mode='json')
                    if any(item['label'].casefold() == proposal['label'].casefold() for item in items):
                        raise RecognitionConflict('project_angle_label_exists')
                state = 'confirmed' if action == 'confirm' else 'dismissed'
                tx.put(PROPOSALS, identity, {**row.payload, 'state': state, 'decided_at': now, 'by': by},
                       expected_revision=row.revision)
                if action == 'confirm':
                    items.append({**proposal, 'id': 'p' + uuid4().hex,
                        'proposal_id': identity, 'state': 'active', 'confirmed_at': now, 'activated_at': now,
                        'by': by, 'count': row.payload['count']})
            else:
                state = 'paused' if action == 'pause' else 'active'
                expected_state = 'active' if action == 'pause' else 'paused'
                index = next((index for index, item in enumerate(items) if item['id'] == identity), None)
                if (index is None or prior.revision != revision or items[index]['state'] != expected_state):
                    raise RecognitionConflict('project_angle_changed')
                item = {**items[index], 'state': state}
                if action == 'pause':
                    item.update(paused_by=by, paused_at=now)
                else:
                    item.update(activated_at=now, resumed_by=by, count=0, last_used_at=now)
                items[index] = item
            if items != list(_angles(prior)):
                tx.put(ANGLES, project, {'project_id': project, 'items': items}, expected_revision=prior.revision if prior else 0)
            tx.commit()
        return {'id': identity, 'state': state}
