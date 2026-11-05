from contextlib import contextmanager
from copy import deepcopy

from core.ai_kernel.product_admission import seal_admission
from core.ai_kernel.turn_kinds import freeze_turn_request

from .budget import text_tokens
from .external_agent_guard import _BINDINGS, _RESERVATIONS
from .external_context import ARCHIVE, CAPABILITY, SHARED_ARCHIVE_VERSION, ExternalContextError, _encoded, _safe
from .external_executor_plugins import effective_external_agent_settings
from .policies import get
from .policies.pipelines import versions_for_turn
from .privacy import privacy_revision
from .shared_external_catalog import SharedExternalCatalog
from .shared_mcp_authority import SharedMcpAuthority
from .shared_mcp_intake import SharedMcpIntake
from .shared_mcp_materials import MATERIAL, SharedMcpMaterials
from .shared_mcp_usage import SharedMcpUsage


_NATIVE_REQUEST = {'client', 'tool', 'query', 'budget', 'scope'}
_REQUEST = _NATIVE_REQUEST | {'scope_kind'}


class SharedMcpContext:
    def __init__(self, application, factory, context):
        if context.records is not application.state.recognition_records:
            raise ExternalContextError('external_context_owner_changed')
        self.context, self.records = context, context.records
        self.authority = SharedMcpAuthority(application, factory)
        self.catalog = SharedExternalCatalog(self.authority, self.records, owner_id=context.owner_id)
        self.materials = SharedMcpMaterials(self)
        self.intake = SharedMcpIntake(self)
        self.usage = SharedMcpUsage(self)

    def _binding(self, tx, turn_id, request, materials, snapshots):
        binding = {'schema_version': 2, 'owner_id': self.context.owner_id, 'turn_id': turn_id,
            'request_json': _encoded(request), 'settings_revision': self._settings_revision(tx, request),
            'privacy_revision': privacy_revision(tx), 'material_refs': materials, 'source_snapshots': snapshots}
        if tx.read(_BINDINGS, turn_id) is not None:
            raise ExternalContextError('external_context_binding_invalid')
        tx.put(_BINDINGS, turn_id, binding, expected_revision=0)
        return binding

    @staticmethod
    def _settings_revision(tx, request):
        return effective_external_agent_settings(tx, client=request['client'])['revision']

    @staticmethod
    def request(archive):
        args = archive['request']['capability_request']['arguments']
        shared = archive['shared']
        request = {**args, 'scope_kind': 'shared'}
        if shared['kind'] != 'catalog':
            request['source_namespace'] = shared['source_namespace']
        return request

    @staticmethod
    def _privacy(binding, client):
        # Core 保留四字段隐私投影，完整来源身份保存在同一原生轮次的共享归档。
        return {'mode': 'remote_allowed', 'allow_remote': True, 'pii': 'possible',
            'consent_refs': ['crp://default/external-agent-settings/' + client], 'retention': 'session',
            'privacy_revision': binding['privacy_revision'], 'excluded_refs': [],
            'material_refs': [{key: row[key] for key in MATERIAL - {'namespace'}} for row in binding['material_refs']],
            'source_snapshots': binding['source_snapshots']}

    def _save(self, turn_id, request, binding, shared, handoff, *, selections=(), mapping=None, refs=(),
              admission=None, session_id, operation_id, idempotency_key, created_at):
        api = self.context
        if api.turns.get_request(turn_id) is not None:
            raise ExternalContextError('external_context_binding_invalid')
        frozen = freeze_turn_request('external.context', turn_id=turn_id, session_id=session_id,
            operation_id=operation_id, idempotency_key=idempotency_key, project_id=request['scope']['project_id'],
            created_at=created_at, text=request['query'], privacy=self._privacy(binding, request['client']), refs=refs,
            capability_request={'mode': 'execute_exact_v1', 'capability_id': CAPABILITY,
                'arguments': {key: request[key] for key in _NATIVE_REQUEST}})
        frozen['policy_versions'] = {**versions_for_turn('external.context'), 'handoff': handoff['version'].removeprefix('handoff')}
        bundle = None if admission is None else seal_admission(frozen, admission.prepare(frozen), api.turns)
        accepted = api.runtime.accept_turn(frozen, admission_bundle=bundle)
        actual = api.turns.get_request(turn_id)
        if (accepted.turn_id != turn_id or accepted.replayed or accepted.status in {'failed', 'cancelled'}
                or _encoded(actual) != _encoded(frozen)):
            raise ExternalContextError('external_context_not_accepted')
        archive = {'schema_version': SHARED_ARCHIVE_VERSION, 'owner_id': api.owner_id, 'turn_id': turn_id,
            'request': actual, 'binding': binding, 'selections': list(selections), 'handoff': handoff,
            'mapping': {} if mapping is None else mapping, 'shared': shared}
        api.turns.get_or_create_immutable_payload(turn_id, ARCHIVE, archive)
        api._archive(turn_id)
        return deepcopy(actual)

    @_safe
    def prepare_catalog(self, turn_id, request, **identity):
        request = {**request, 'scope_kind': 'shared'}
        native, actor = self.authority.current(request['client'], 'projects')
        queries = self.catalog.resolve(actor)
        with self.authority.registry.records.begin() as central, self.records.begin() as tx:
            catalog = self.catalog.snapshot(request['client'], native, central=central, actor_reader=tx, queries=queries)
            binding = self._binding(tx, turn_id, request, [], [])
            tx.commit()
        handoff = get('handoff', version='@3')(catalog['projects'], count_tokens=text_tokens, budget=request['budget'])
        shared = {'schema_version': 1, 'kind': 'catalog', 'native': native, 'catalog': catalog}
        return self._save(turn_id, request, binding, shared, handoff, **identity)

    @_safe
    def prepare_recall(self, turn_id, request, **identity):
        return self.materials.prepare_recall(turn_id, request, **identity)

    @_safe
    def prepare_read(self, turn_id, request, selections, **identity):
        return self.materials.prepare_read(turn_id, request, selections, **identity)

    def validate_archive(self, archive):
        native = archive['request']
        request, shared, binding = self.request(archive), archive['shared'], archive['binding']
        if (not isinstance(shared, dict) or shared.get('schema_version') != 1
                or not isinstance(request, dict) or request.get('scope_kind') != 'shared'
                or not isinstance(binding, dict) or set(binding) != {'schema_version', 'owner_id', 'turn_id',
                    'request_json', 'settings_revision', 'privacy_revision', 'material_refs', 'source_snapshots'}
                or binding['schema_version'] != 2 or binding['owner_id'] != self.context.owner_id
                or binding['turn_id'] != archive['turn_id'] or binding['request_json'] != _encoded(request)
                or _encoded(native['privacy']) != _encoded(self._privacy(binding, request['client']))
                or _encoded(self.context.turns.get_request(archive['turn_id'])) != _encoded(native)):
            raise ExternalContextError('external_context_binding_invalid')
        if shared.get('kind') == 'catalog':
            if (set(shared) != {'schema_version', 'kind', 'native', 'catalog'} or set(request) != _REQUEST
                    or request['tool'] != 'projects' or request['scope'] != {'user_id': self.context.owner_id, 'project_id': 'default'}
                    or archive['selections'] != [] or archive['mapping'] != {}
                    or binding['material_refs'] != [] or binding['source_snapshots'] != []
                    or native['policy_versions']['handoff'] != '@3'
                    or _encoded(archive['handoff']) != _encoded(get('handoff', version='@3')(
                        shared['catalog']['projects'], count_tokens=text_tokens, budget=request['budget']))):
                raise ExternalContextError('external_context_binding_invalid')
        else:
            self.materials.validate_archive(archive)

    def _validate_binding(self, tx, archive):
        request = self.request(archive)
        binding = archive['binding']
        row = tx.read(_BINDINGS, archive['turn_id'])
        if (row is None or row.revision != 1 or _encoded(row.payload) != _encoded(binding)
                or binding['settings_revision'] != self._settings_revision(tx, request)):
            raise ExternalContextError('external_context_binding_invalid')
        self.authority.require_external(tx, request['client'])
        return request

    @contextmanager
    def delivery_scope(self, archive):
        shared = archive['shared']
        request = self.request(archive)
        if shared['kind'] == 'catalog':
            with self.authority.registry.records.begin_read() as reader:
                actor = self.authority.validate(reader, shared['native'], request['client'], 'projects')
            queries = self.catalog.resolve(actor)
        else:
            query = self.materials.resolve(archive)
        with self.authority.registry.records.begin() as central, self.records.begin() as tx:
            self._validate_binding(tx, archive)
            if shared['kind'] == 'catalog':
                self.catalog.validate(shared['catalog'], shared['native'], central=central, actor_reader=tx, queries=queries)
                yield tx
            else:
                with self.materials.current(archive, query, central=central, actor_reader=tx):
                    yield tx

    def verify(self, archive):
        with self.delivery_scope(archive):
            return None

    def reserve(self, archive):
        request = self.request(archive)
        with self.delivery_scope(archive) as tx:
            self.context.guard._reserve_once(tx, _RESERVATIONS, 'turn_id', archive['turn_id'], request)
            tx.commit()

    def require_current_delivery(self, archive, client, tool):
        native, _actor = self.authority.current(client, tool)
        if native != archive['shared']['native']:
            raise ExternalContextError('external_context_owner_changed')
        self.verify(archive)

    @_safe
    def report_use(self, ref, archive, outcome, ids, client):
        return self.usage.report(ref, archive, outcome, ids, client)


def install_shared_mcp(application, factory):
    api = application.state.external_context
    if api.shared is None:
        api.bind_shared(SharedMcpContext(application, factory, api))
    elif api.shared.authority.factory is not factory:
        raise ExternalContextError('external_context_owner_changed')
