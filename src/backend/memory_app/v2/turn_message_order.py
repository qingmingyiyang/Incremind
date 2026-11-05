from copy import deepcopy
from datetime import datetime

from backend.recognition import RecognitionConflict


COLLECTION = 'v2_turn_message_order'
HEADS = 'v2_thread_message_heads'
ACCEPTANCES = 'v2_turn_message_acceptances'
_ACCEPTANCE_FIELDS = {'version', 'owner', 'actor', 'public_id', 'project_id', 'requested_project_id',
    'thread_id', 'body', 'accepted_at', 'origin', 'new_thread', 'request_key', 'sequence', 'previous_id'}


def _instant(value):
    if not isinstance(value, str):
        raise RecognitionConflict('turn_message_time_invalid')
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise RecognitionConflict('turn_message_time_invalid')
    return result


class TurnMessageOrder:
    def __init__(self, freshness):
        self.freshness = freshness

    def reserve(self, tx, *, public_id, project_id, thread_id, body, accepted_at,
                request_key=None, origin='user', new_thread=False):
        service, view = self.freshness, self.freshness._reader(tx)
        service._validate()
        if (any(not isinstance(value, str) or not value for value in (public_id, project_id, thread_id, origin))
                or not isinstance(body, dict) or type(new_thread) is not bool):
            raise RecognitionConflict('turn_message_acceptance_invalid')
        _instant(accepted_at)
        thread = view.read('v2_threads', thread_id)
        if (new_thread and thread is not None or not new_thread
                and (thread is None or thread.payload.get('project_id') != project_id)):
            raise RecognitionConflict('turn_message_thread_unavailable')
        payload = {'version': 1, 'owner': deepcopy(service.owner), 'actor': service._actor(),
            'public_id': public_id, 'project_id': None if new_thread else project_id,
            'requested_project_id': project_id, 'thread_id': thread_id, 'body': deepcopy(body),
            'accepted_at': accepted_at, 'origin': origin, 'new_thread': new_thread, 'request_key': request_key}
        old = view.read(ACCEPTANCES, public_id)
        if old is not None:
            if (old.revision != 1 or {key: value for key, value in old.payload.items()
                    if key not in {'sequence', 'previous_id'}} != payload):
                raise RecognitionConflict('turn_message_acceptance_replay_changed')
            return deepcopy(old.payload)
        head = view.read(HEADS, thread_id)
        if head is not None:
            prior = view.read(ACCEPTANCES, head.payload.get('public_id', '')) or view.read(
                COLLECTION, head.payload.get('public_id', ''))
            if (new_thread or head.payload.get('owner') != service.owner
                    or head.payload.get('project_id') != project_id or prior is None
                    or prior.payload.get('owner') != service.owner or prior.payload.get('thread_id') != thread_id
                    or prior.payload.get('sequence') != head.payload.get('sequence')):
                raise RecognitionConflict('turn_message_order_owner_changed')
        payload.update(sequence=head.payload['sequence'] + 1 if head else 1,
            previous_id=head.payload['public_id'] if head else None)
        view.put(ACCEPTANCES, public_id, payload, expected_revision=0)
        view.put(HEADS, thread_id, {'owner': deepcopy(service.owner), 'project_id': payload['project_id'],
            'public_id': public_id, 'sequence': payload['sequence']}, expected_revision=head.revision if head else 0)
        return deepcopy(payload)

    def _acceptance(self, view, public_id):
        row = view.read(ACCEPTANCES, public_id)
        if row is None:
            return None
        value = row.payload
        actor = value.get('actor')
        if (row.revision != 1 or set(value) != _ACCEPTANCE_FIELDS or type(value.get('version')) is not int
                or value['version'] != 1 or value.get('owner') != self.freshness.owner
                or not isinstance(actor, dict) or set(actor) != {'user_id', 'by'}
                or not isinstance(actor['user_id'], str) or not actor['user_id']
                or actor['by'] is not None and actor['by'] != 'admin'
                or value.get('public_id') != public_id
                or type(value.get('sequence')) is not int or value['sequence'] < 1
                or type(value.get('new_thread')) is not bool or not isinstance(value.get('body'), dict)):
            raise RecognitionConflict('turn_message_acceptance_changed')
        if (any(not isinstance(value[field], str) or not value[field]
                for field in ('requested_project_id', 'thread_id', 'origin'))
                or value['new_thread'] and value['project_id'] is not None
                or not value['new_thread'] and value['project_id'] != value['requested_project_id']
                or (value['sequence'] == 1) != (value['previous_id'] is None)
                or value['previous_id'] is not None and (not isinstance(value['previous_id'], str) or not value['previous_id'])
                or value['request_key'] is not None and (not isinstance(value['request_key'], str) or not value['request_key'])):
            raise RecognitionConflict('turn_message_acceptance_changed')
        _instant(value['accepted_at'])
        if value['request_key'] is not None:
            request = view.read('v2_turn_requests', value['request_key'])
            if (request is None or request.payload.get('body') != value['body']
                    or request.payload.get('created_at') != value['accepted_at']):
                raise RecognitionConflict('turn_message_acceptance_request_changed')
            if request.payload.get('state') == 'completed':
                result = request.payload.get('result', {})
                if result.get('turn', {}).get('id') != public_id or result.get('thread_id') != value['thread_id']:
                    raise RecognitionConflict('turn_message_acceptance_result_changed')
        return row

    def record_message(self, tx, *, public_id, project_id, thread_id, text, accepted_at,
                       origin='user', native_request=None, ordering_only=False):
        service, view = self.freshness, self.freshness._reader(tx)
        service._validate()
        if any(not isinstance(value, str) or not value for value in
               (public_id, project_id, thread_id, origin)) or not isinstance(text, str) or type(ordering_only) is not bool:
            raise RecognitionConflict('turn_message_identity_invalid')
        _instant(accepted_at)
        thread = view.read('v2_threads', thread_id)
        if thread is None or thread.payload.get('project_id') != project_id:
            raise RecognitionConflict('turn_message_thread_unavailable')
        public = view.read('v2_turns', public_id)
        if public is not None and (public.payload.get('project_id') != project_id
                or public.payload.get('thread_id') != thread_id or public.payload.get('user_text') != text
                or public.payload.get('parent_turn_id') is not None):
            raise RecognitionConflict('turn_message_public_changed')
        native_identity = None
        if native_request is not None:
            from core.ai_kernel.product_admission import admission_identity
            actual = service._native().get_request(native_request['turn_id'])
            if actual != native_request or actual['scope']['project_id'] != project_id:
                raise RecognitionConflict('turn_message_native_not_accepted')
            native_identity = admission_identity(actual)
        payload = {'version': 1, 'owner': deepcopy(service.owner), 'actor': service._actor(),
            'public_id': public_id, 'project_id': project_id, 'thread_id': thread_id, 'text': text,
            'accepted_at': accepted_at, 'origin': origin, 'native_identity': native_identity}
        accepted = self._acceptance(view, public_id)
        if ordering_only and (accepted is None or public is None or public.payload.get('intent') != 'remember'
                or native_request is not None or accepted.payload['requested_project_id'] != project_id
                or accepted.payload['body'].get('scope_kind') != 'shared'):
            raise RecognitionConflict('turn_message_order_only_acceptance_required')
        if accepted is not None:
            value = accepted.payload
            if (value['thread_id'] != thread_id or value['actor'] != payload['actor']
                    or value['accepted_at'] != accepted_at or value['origin'] != origin
                    or value['project_id'] is not None and value['project_id'] != project_id):
                raise RecognitionConflict('turn_message_acceptance_binding_changed')
            payload.update(sequence=value['sequence'], previous_id=value['previous_id'],
                acceptance_ref={'collection': ACCEPTANCES, 'object_id': public_id, 'revision': accepted.revision})
        previous = view.read(COLLECTION, public_id)
        if previous is not None:
            expected = (previous.payload if accepted is not None else
                {key: value for key, value in previous.payload.items() if key not in {'sequence', 'previous_id'}})
            if ordering_only or previous.revision != 1 or expected != payload:
                raise RecognitionConflict('turn_message_replay_changed')
            return deepcopy(previous.payload)
        if accepted is not None:
            head = view.read(HEADS, thread_id)
            if head is None or head.payload.get('owner') != service.owner:
                raise RecognitionConflict('turn_message_order_owner_changed')
            if head.payload.get('project_id') is None:
                if (not accepted.payload['new_thread'] or head.payload.get('public_id') != public_id
                        or head.payload.get('sequence') != 1):
                    raise RecognitionConflict('turn_message_order_owner_changed')
                view.put(HEADS, thread_id, {**head.payload, 'project_id': project_id}, expected_revision=head.revision)
            elif head.payload['project_id'] != project_id:
                raise RecognitionConflict('turn_message_order_owner_changed')
            if ordering_only:
                # 共享原件由另一资料根处理；顺序仍来自原受理，个人学习不扫描该回合。
                return None
            view.put(COLLECTION, public_id, payload, expected_revision=0)
            return deepcopy(payload)
        head = view.read(HEADS, thread_id)
        if head is not None and (head.payload.get('owner') != service.owner
                or head.payload.get('project_id') != project_id):
            raise RecognitionConflict('turn_message_order_owner_changed')
        payload.update(sequence=head.payload['sequence'] + 1 if head else 1,
            previous_id=head.payload['public_id'] if head else None)
        view.put(COLLECTION, public_id, payload, expected_revision=0)
        view.put(HEADS, thread_id, {'owner': deepcopy(service.owner), 'project_id': project_id,
            'public_id': public_id, 'sequence': payload['sequence']}, expected_revision=head.revision if head else 0)
        return deepcopy(payload)

    def read(self, reader, public_id):
        view = self.freshness._reader(reader)
        self.freshness._validate()
        row = view.read(COLLECTION, public_id)
        if row is None:
            return None
        if row.revision != 1 or row.payload.get('owner') != self.freshness.owner:
            raise RecognitionConflict('turn_message_order_owner_changed')
        payload = row.payload
        accepted = self._acceptance(view, public_id)
        if accepted is not None or payload.get('acceptance_ref') is not None:
            if (accepted is None or payload['acceptance_ref'] != {'collection': ACCEPTANCES,
                    'object_id': public_id, 'revision': accepted.revision}
                    or any(payload.get(field) != accepted.payload.get(field)
                        for field in ('actor', 'thread_id', 'accepted_at', 'origin', 'sequence', 'previous_id'))
                    or accepted.payload['project_id'] is not None and payload['project_id'] != accepted.payload['project_id']):
                raise RecognitionConflict('turn_message_acceptance_binding_changed')
        if type(payload.get('sequence')) is not int or payload['sequence'] < 1:
            raise RecognitionConflict('turn_message_sequence_invalid')
        if payload['previous_id'] is None:
            if payload['sequence'] != 1:
                raise RecognitionConflict('turn_message_sequence_invalid')
        else:
            prior = view.read(COLLECTION, payload['previous_id']) or self._acceptance(view, payload['previous_id'])
            if (prior is None or prior.revision != 1 or prior.payload.get('owner') != self.freshness.owner
                    or prior.payload.get('thread_id') != payload['thread_id']
                    or prior.payload.get('project_id') not in {None, payload['project_id']}
                    or prior.payload.get('sequence') != payload['sequence'] - 1):
                raise RecognitionConflict('turn_message_predecessor_changed')
        return row

    def pair(self, reader, public_id):
        current = self.read(reader, public_id)
        if current is None or current.payload['previous_id'] is None:
            return None
        previous = self.read(reader, current.payload['previous_id'])
        return (previous, current) if previous is not None else None
