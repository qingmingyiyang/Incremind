from copy import deepcopy
from uuid import NAMESPACE_URL, uuid5

from ..transaction_records import TransactionRecords
from .external_context import USES, ExternalContextError, _encoded
from .shared_auxiliary import _qualified_commit
from .shared_mcp_materials import _delivery
from .shared_runtime import SharedWriteRecords
from .usage import UsageService


COMMANDS = 'v2_shared_mcp_usage'


class SharedMcpUsage:
    def __init__(self, shared):
        self.shared, self.api, self.authority = shared, shared.context, shared.authority

    @staticmethod
    def _targets(archive, ids):
        wanted, result = set(ids), []
        for entry in [*archive['handoff']['entries'], *archive['handoff']['profile']]:
            identity = entry['id']
            if identity in wanted:
                proof = archive['mapping'][identity]
                target = {**proof['material'], 'kind': proof['usage_kind']}
                if target not in result:
                    result.append(target)
        return result

    def _state(self, tx, ref, archive, outcome, ids):
        turn = archive['turn_id']
        _delivery(tx, self.api, turn, ref, outcome)
        order = [entry['id'] for entry in [*archive['handoff']['entries'], *archive['handoff']['profile']]]
        row = tx.read(USES, turn)
        old = {'owner_id': self.api.owner_id, 'immutable_ref': ref, 'ids': [], 'objects': []}
        if row is not None:
            old = deepcopy(dict(row.payload))
            if (set(old) != {'owner_id', 'immutable_ref', 'ids', 'objects'}
                    or old['owner_id'] != self.api.owner_id or old['immutable_ref'] != ref
                    or not isinstance(old['ids'], list) or old['ids'] != [value for value in order if value in old['ids']]
                    or old['objects'] != self._targets(archive, old['ids'])
                    or not 1 <= row.revision <= len(old['ids'])):
                raise ExternalContextError('external_context_binding_invalid')
        used = [identity for identity in order if identity in set(old['ids']) | set(ids)]
        result = {**old, 'ids': used, 'objects': self._targets(archive, used)}
        new = [target for target in result['objects'] if target not in old['objects'] and target['kind'] is not None]
        return row, old, result, new

    def _actor_receipt(self, tx, archive, state, profile):
        row, old, payload, _new = state
        if payload == old:
            return
        for target in profile:
            UsageService(TransactionRecords(tx), source_owner=self.api.records).record_usage(
                target['kind'], target['id'], target['project_id'], 1.0, event_kind='citation')
        tx.put(USES, archive['turn_id'], payload, expected_revision=row.revision if row else 0)
        tx.commit()

    def report(self, ref, archive, outcome, ids, client):
        request = self.shared.request(archive)
        native, actor = self.authority.current(client, 'report_use')
        if (request['client'] != client or native != archive['shared']['native']
                or any(identity not in archive['mapping'] for identity in ids)):
            raise ExternalContextError('external_context_citations_invalid')
        result = {'turn_id': archive['turn_id'], 'ids': ids}
        if not ids:
            with self.shared.delivery_scope(archive) as tx:
                _delivery(tx, self.api, archive['turn_id'], ref, outcome)
            return result
        query = self.shared.materials.resolve(archive)
        admission, lease = query.origin_admission, query.origin_admission.lease
        if admission.actor != actor:
            raise ExternalContextError('external_context_owner_changed')
        scope = {'project_revision': lease.project_revision, 'member_revision': lease.member_revision,
            'account_revision': lease.account_revision}
        with self.authority.registry.records.begin_read() as central:
            role = self.authority.registry.authorize(central, actor, lease.project_id, authority=scope).role
        with self.shared.delivery_scope(archive) as tx:
            state = self._state(tx, ref, archive, outcome, ids)
            source = [target for target in state[3] if target['namespace'] == lease.namespace]
            if role == 'viewer' or not source:
                profile = [target for target in state[3] if target['namespace'] == admission.personal['namespace']]
                self._actor_receipt(tx, archive, state, profile)
                return result
        writer = SharedWriteRecords(admission.registry, actor, lease.project_id, authority=scope,
            action='use', object_kind='memory', object_id=lease.project_id, object_title='外部记忆使用')
        with writer.begin() as source_tx, _qualified_commit(admission), self.api.records.begin() as actor_tx:
            self.authority.validate(source_tx._central, native, client, 'report_use')
            self.authority.require_external(actor_tx, client, project=lease.project_id, source_reader=source_tx)
            self.shared.materials._validate_current_in_source(archive, query, source_tx, actor_tx)
            state = self._state(actor_tx, ref, archive, outcome, ids)
            profile = []
            usage = UsageService(TransactionRecords(source_tx), source_owner=writer)
            for target in state[3]:
                if target['namespace'] == admission.personal['namespace']:
                    profile.append(target)
                    continue
                if target['namespace'] != lease.namespace or target['project_id'] != lease.project_id:
                    raise ExternalContextError('external_context_material_changed')
                value = {'project_id': lease.project_id, 'namespace': lease.namespace,
                    'actor_user_id': actor.user_id, 'target': target}
                command = 'mcp-use-' + uuid5(NAMESPACE_URL,
                    _encoded([admission.personal['namespace'], archive['turn_id'], target])).hex
                saved = source_tx.read(COMMANDS, command)
                if saved is not None:
                    if saved.revision != 1 or _encoded(saved.payload) != _encoded(value):
                        raise ExternalContextError('external_context_binding_invalid')
                    continue
                recorded = usage.record_usage(target['kind'], target['id'], lease.project_id, 1.0, event_kind='citation')
                if recorded is None:
                    raise ExternalContextError('external_context_material_changed')
                source_tx.put(COMMANDS, command, value, expected_revision=0)
            source_tx.commit()
            self._actor_receipt(actor_tx, archive, state, profile)
        return result
