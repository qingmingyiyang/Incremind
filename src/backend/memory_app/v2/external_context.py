"""Qualified read handoffs through the existing Kernel, never a model adapter."""
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from functools import wraps
import json
from pathlib import Path
import sqlite3

from backend.recognition import WorkScope
from backend.recognition.external_input_dependencies import json_proof
from backend.recognition.external_turn_facts import IDENTITIES, completed, source_store
from backend.recognition.sql_source_identities import (
    KIND as SQL_IDENTITIES, CollectingReader, capture_identities, make_companion, validate_companion,
)
from backend.shared.secret_detection import contains_secret, redact_secrets
from core.ai_kernel import CapabilityDefinition, validate_turn_request
from core.ai_kernel.turn_kinds import freeze_turn_request
from core.ai_tooling import tool_from_capability
from core.document_engine import SQLiteDocumentRepository

from ..context_adapter import format_recognition_content
from ..source_egress import SourceEgressService, recognition_service
from ..transaction_records import TransactionRecords
from .budget import text_tokens
from .external_agent_guard import ExternalAgentGuard, ExternalAgentGuardError
from .layers import summary_of
from .policies import get, override
from .policies.pipelines import versions_for_turn
from .privacy import resolve_turn_material
from .usage import UsageService


CAPABILITY = 'external.context.execute'
ARCHIVE = 'external-context-handoff-v1'
DELIVERIES = 'v2_external_agent_deliveries'
USES = 'v2_external_agent_citations'
_FIELDS = {'type', 'id', 'revision', 'project_id', 'layer', 'windows'}
_LAYERS = {'original_item': {'L0'}, 'original_source': {'L0'},
           'document': {'L1', 'L2'}, 'recognition': {'L3'}}
_ARCHIVE_FIELDS = {'schema_version', 'owner_id', 'turn_id', 'request', 'binding',
                   'selections', 'handoff', 'mapping'}
_ORIGIN_FIELDS = {'turn_id', 'id', 'immutable_ref', 'outcome_ref'}


class ExternalContextError(ValueError):
    """A fixed code, with no body, private title, credentials or local path."""


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _entry_key(entry, project):
    # Public entries already retain typed roots. Equal SQL/JSON object IDs
    # cannot replace each other's proof without changing those exact sources.
    return entry['object_id'], entry['layer'], project, _encoded(entry['sources'])


def _safe(method):
    @wraps(method)
    def call(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except (ExternalContextError, ExternalAgentGuardError):
            raise
        except Exception:
            raise ExternalContextError('external_context_unavailable') from None
    return call


def _selections(values):
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise ExternalContextError('external_context_selection_invalid')
    result = []
    for row in values:
        if (not isinstance(row, Mapping) or set(row) != _FIELDS
                or not isinstance(row['type'], str) or row['type'] not in _LAYERS
                or not isinstance(row['layer'], str) or row['layer'] not in _LAYERS[row['type']]
                or not isinstance(row['windows'], list) or (row['layer'] == 'L3' and row['windows'])):
            raise ExternalContextError('external_context_selection_invalid')
        last = 0
        for window in row['windows']:
            if (not isinstance(window, dict) or set(window) != {'start', 'end'}
                    or any(type(window[key]) is not int for key in window)
                    or not last <= window['start'] < window['end']):
                raise ExternalContextError('external_context_selection_invalid')
            last = window['end']
        result.append(deepcopy(dict(row)))
    return result


def definition():
    capability = CapabilityDefinition(CAPABILITY, 1, 'read', False, 'read_only',
        'crp://default/contracts/external-context-request.schema.json',
        'crp://default/contracts/external-context-result.schema.json')
    tool = tool_from_capability(capability)
    return replace(capability, tool_definition=replace(tool, idempotency='never_retry',
        retry_policy=replace(tool.retry_policy, max_attempts=1, retryable_error_codes=())))


class ExternalContext:
    def __init__(self, records, *, owner_id, documents, now=None):
        self.records, self.owner_id, self.documents = records, owner_id, documents
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.guard = ExternalAgentGuard(records, owner_id=owner_id, now=self.now)
        self.turns = self.runtime = None
        self.catalog = None
        self.recall = None

    def bind_recall(self, recall):
        if (recall.records is not self.records or recall.owner_id != self.owner_id
                or (self.recall is not None and self.recall is not recall)):
            raise ExternalContextError('external_context_owner_changed')
        self.recall = recall

    def bind_catalog(self, catalog):
        if (catalog.records is not self.records or catalog.owner_id != self.owner_id
                or (self.catalog is not None and self.catalog is not catalog)):
            raise ExternalContextError('external_context_owner_changed')
        self.catalog = catalog

    def install(self, registry, turns):
        if self.turns is not None and self.turns is not turns:
            raise ExternalContextError('external_context_owner_changed')
        self.turns = turns
        registry.register(definition(), self)
        return self.bind_runtime

    def bind_runtime(self, runtime):
        if self.runtime is not None and self.runtime is not runtime:
            raise ExternalContextError('external_context_owner_changed')
        self.runtime = runtime

    def _entry(self, tx, row, selection, snapshot):
        body, kind, identity = row['payload'], row['type'], row['id']
        conditions = []
        if kind == 'recognition':
            # Qualified evidence reads use the domain's read-only snapshot,
            # not the write-only TransactionRecords adapter. The outer writer
            # transaction and exact revision check preserve the frozen owner.
            recognition = recognition_service(self.records).get_recognition(
                scope=WorkScope(self.owner_id, row['project_id']), recognition_id=identity)
            if recognition is None or not recognition.authorized or recognition.revision != row['revision']:
                raise ExternalContextError('external_context_material_changed')
            entry = recognition.retrieval_projection()
            text, conditions = format_recognition_content(entry), list(entry['conditions'])
        elif kind == 'document':
            text = SQLiteDocumentRepository(TransactionRecords(tx), namespace_id=self.documents.namespace_id).markdown(
                identity, revision=row['revision'])
            if selection['layer'] == 'L2' and isinstance(text, str):
                text = summary_of(text)[0]
        elif kind == 'original_item':
            text = body.get('source_text')
        else:
            metadata = body.get('metadata')
            text = (metadata.get('content_snapshot') or metadata.get('content')) if isinstance(metadata, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise ExternalContextError('external_context_material_changed')
        if selection['windows']:
            if selection['windows'][-1]['end'] > len(text):
                raise ExternalContextError('external_context_selection_invalid')
            text = '\n\n'.join(text[window['start']:window['end']] for window in selection['windows'])
        sources = [{**root, 'project_id': row['project_id']} for root in snapshot['roots']]
        for source in sources:
            node = next(node for node in snapshot['nodes'] if node['type'] == source['type'] and node['id'] == source['id'])
            if 'incarnation' in node:
                source['incarnation'] = node['incarnation']
        title = body.get('title') or identity
        if not isinstance(title, str):
            raise ExternalContextError('external_context_material_changed')
        return {'object_id': identity, 'layer': selection['layer'], 'title': title,
                'excerpt': text, 'sources': sources, 'revision': row['revision'], 'conditions': conditions}

    @_safe
    def prepare_projects(self, turn_id, request, **identity):
        if (self.catalog is None or request.get('tool') != 'projects'
                or request.get('scope', {}).get('project_id') != 'default'):
            raise ExternalContextError('external_context_binding_invalid')
        return self.prepare(turn_id, request, [], _catalog=True, **identity)

    @_safe
    def prepare_recall(self, turn_id, request, *, scene=None, **identity):
        if self.recall is None:
            raise ExternalContextError('external_context_owner_changed')
        plan = self.recall.plan(request, scene=scene)
        with override(**plan['policy_versions']):
            return self.prepare(turn_id, request, plan['selections'], _recall=plan, **identity)

    @_safe
    def prepare(self, turn_id, request, selections, *, session_id, operation_id, idempotency_key, created_at,
                origin=None, _catalog=False, _recall=None, validate_archive=None):
        # An accepted old Turn must already have its own frozen identities.
        # Retrying preparation cannot anchor today's rows into an old proof.
        if self.turns.get_request(turn_id) is not None:
            self._archive(turn_id)
        if validate_archive is not None and not callable(validate_archive):
            raise TypeError('external_context_archive_validator_invalid')
        versions = versions_for_turn('external.context')
        if _catalog:
            if (self.catalog is None or request.get('tool') != 'projects' or selections or origin is not None
                    or request.get('scope', {}).get('project_id') != 'default'):
                raise ExternalContextError('external_context_binding_invalid')
            versions['handoff'] = '@2'
        selections = _selections(selections)
        materials = [{key: row[key] for key in ('type', 'id', 'revision', 'project_id')} for row in selections]
        # A document may contribute both L1 and L2. Authority is frozen once.
        unique = list({(row['type'], row['id'], row['project_id']): row for row in materials}.values())
        if any(row not in unique for row in materials):
            raise ExternalContextError('external_context_selection_invalid')
        if _recall is not None:
            if (self.recall is None or _catalog or origin is not None
                    or _encoded(_recall['request']) != _encoded(request)
                    or _encoded(_recall['selections']) != _encoded(selections)
                    or {key: value for key, value in _recall['policy_versions'].items() if key != 'compose'} != versions):
                raise ExternalContextError('external_context_binding_invalid')
            unique = deepcopy(_recall['materials'])
            if any(row not in unique for row in materials):
                raise ExternalContextError('external_context_binding_invalid')
        with self.records.begin() as tx:
            guard = ExternalAgentGuard(TransactionRecords(tx), owner_id=self.owner_id, now=self.now)
            if _recall is not None:
                self.recall.validate(_recall, tx)
            parent = None
            if origin is not None:
                if (request['tool'] != 'read' or not isinstance(origin, Mapping)
                        or set(origin) != {'turn_id', 'id'}
                        or any(not isinstance(origin[key], str) for key in origin)
                        or origin['turn_id'] == turn_id):
                    raise ExternalContextError('external_context_binding_invalid')
                ref, old, outcome_ref = self._completed(origin['turn_id'])
                prior = old['request']['capability_request']['arguments']
                delivery = tx.read(DELIVERIES, origin['turn_id'])
                if (prior['client'] != request['client'] or origin['id'] not in old['mapping']
                        or delivery is None or delivery.revision != 1
                        or delivery.payload.get('owner_id') != self.owner_id
                        or delivery.payload.get('immutable_ref') != ref
                        or delivery.payload.get('outcome_ref') != outcome_ref):
                    raise ExternalContextError('external_context_not_delivered')
                # Revalidate the original graph inside the same writer transaction
                # that freezes and renders its child. Recreated source IDs cannot
                # replace the delivered incarnation in the gap after HTTP lookup.
                guard.validate(origin['turn_id'], prior)
                proof = old['mapping'][origin['id']]
                allowed = {(node['type'], node['id'], node['source_revision'], proof['material']['project_id'])
                    for node in proof['snapshot']['nodes']
                    if node['type'] in {'original_item', 'original_source'}}
                if any(selection['layer'] != 'L0' or (selection['type'], selection['id'],
                        selection['revision'], selection['project_id']) not in allowed for selection in selections):
                    raise ExternalContextError('external_context_selection_invalid')
                parent = {**dict(origin), 'immutable_ref': ref, 'outcome_ref': outcome_ref}
            binding = guard.freeze(turn_id, request, unique)
            if _recall is not None:
                self.recall.validate_binding(_recall, binding)
            if parent is not None:
                original_nodes = {(node['type'], node['id']): node for node in proof['snapshot']['nodes']}
                for snapshot in binding['source_snapshots']:
                    for node in snapshot['nodes']:
                        previous = original_nodes.get((node['type'], node['id']))
                        if previous is None or _encoded(previous) != _encoded(node):
                            raise ExternalContextError('external_context_material_changed')
            resolved = guard.validate(turn_id, request)
            entries, profile, proofs = [], [], {}
            for selection in selections:
                slot = next(n for n, material in enumerate(unique) if all(material[key] == selection[key] for key in material))
                row, snapshot = resolved[slot], binding['source_snapshots'][slot]
                entry = self._entry(tx, row, selection, snapshot)
                (profile if row['project_id'] == 'me' else entries).append(entry)
                proofs[_entry_key(entry, row['project_id'])] = {
                    'material': unique[slot], 'snapshot': snapshot,
                    'usage_kind': 'insight' if row['type'] == 'recognition' else 'document' if row['type'] == 'document' else None}
            catalog = self.catalog.snapshot(request['client'], tx) if _catalog else None
            if catalog is None:
                handoff = get('handoff', version=versions['handoff'])(entries, profile=profile, count_tokens=text_tokens, budget=request['budget'])
            else:
                handoff = get('handoff', version=versions['handoff'])(catalog['projects'], count_tokens=text_tokens, budget=request['budget'])
            sql_entries = {}
            for entry in [*handoff['entries'], *handoff['profile']]:
                project = 'me' if entry['id'].startswith('P') else request['scope']['project_id']
                proof = proofs[_entry_key(entry, project)]
                # Collect only this final number's actual selected evidence.
                # An independent reader and memo keep shared roots complete.
                collector = CollectingReader(tx)
                own = WorkScope(self.owner_id, project)
                material, _ = resolve_turn_material(collector, own, proof['material'])
                SourceEgressService(TransactionRecords(collector), _memo={}).validate_snapshot(own, proof['snapshot'])
                if material['type'] == 'document':
                    documents = SQLiteDocumentRepository(TransactionRecords(collector), namespace_id=self.documents.namespace_id)
                    if (documents.revision(material['id'], material['revision']) is None
                            or documents.markdown(material['id'], revision=material['revision']) is None):
                        raise ExternalContextError('external_context_material_changed')
                sql_entries[entry['id']] = {'proof': deepcopy(proof),
                    'sql_identities': capture_identities(tx, collector)}
            # No input can change between rendered bytes and their frozen proof.
            guard.validate(turn_id, request)
            if catalog is not None:
                self.catalog.validate(catalog, tx)
            if _recall is not None:
                self.recall.validate(_recall, tx)
                self.recall.validate_binding(_recall, binding)
            if validate_archive is not None:
                # 校验真实归档内容，拒绝时回滚同事务的绑定，不允许回调改写权威数据。
                validate_archive(deepcopy({'binding': binding, 'selections': selections, 'handoff': handoff}))
            tx.commit()
        privacy = {'mode': 'remote_allowed', 'allow_remote': True, 'pii': 'possible',
            'consent_refs': ['crp://default/external-agent-settings/' + request['client']],
            'retention': 'session', 'privacy_revision': binding['privacy_revision'], 'excluded_refs': [],
            'material_refs': binding['material_refs'], 'source_snapshots': binding['source_snapshots']}
        frozen = freeze_turn_request('external.context', turn_id=turn_id, session_id=session_id,
            operation_id=operation_id, idempotency_key=idempotency_key, project_id=request['scope']['project_id'],
            created_at=created_at, text=request['query'], privacy=privacy, refs=[row['ref'] for row in resolved],
            capability_request={'mode': 'execute_exact_v1', 'capability_id': CAPABILITY, 'arguments': request})
        frozen['policy_versions'] = versions
        validate_turn_request(frozen)
        mapping = {entry['id']: proofs[_entry_key(entry,
            'me' if entry['id'].startswith('P') else request['scope']['project_id'])]
            for entry in [*handoff['entries'], *handoff['profile']]}
        archive = {'schema_version': '1.0.0', 'owner_id': self.owner_id, 'turn_id': turn_id,
            'request': frozen, 'binding': binding, 'selections': selections, 'handoff': handoff, 'mapping': mapping}
        if parent is not None:
            archive.update(schema_version='2.0.0', origin=parent)
        if catalog is not None:
            archive.update(schema_version='3.0.0', catalog=catalog)
        if _recall is not None:
            archive.update(schema_version='4.0.0', recall=deepcopy(_recall))
        accepted = self.runtime.accept_turn(frozen)
        actual = self.turns.get_request(turn_id)
        if (accepted.turn_id != turn_id or accepted.status in {'failed', 'cancelled'}
                or _encoded(actual) != _encoded(frozen)
                or actual['desired_outcome'] != 'external.context'
                or actual['capability_request']['arguments']['scope']['user_id'] != self.owner_id):
            raise ExternalContextError('external_context_not_accepted')
        # The entry precheck may have raced with another creator. Only the
        # original atomic claim can produce identities for a new archive.
        if accepted.replayed:
            self._archive(turn_id)
        self.turns.get_or_create_immutable_payload(turn_id, ARCHIVE, archive)
        # Sole producer-only bypass: the new companion has not been persisted
        # yet. The original created receipt alone permits False; consumers and
        # replayed claims require the already persisted, bound companion.
        saved_ref, saved = self._archive(turn_id, _require_sql_identities=accepted.replayed)
        eligible = {}
        for identity, proof in saved['mapping'].items():
            if (proof['material']['type'] == 'original_source'
                    and all(node['type'] == 'original_source' and isinstance(node.get('incarnation'), str)
                        and node['incarnation'] != 'legacy' for node in proof['snapshot']['nodes'])):
                json_proof(proof, self.owner_id)
                eligible[identity] = deepcopy(proof)
        if Path(self.turns.effect_runner.log.database).resolve() != source_store(self.records).root / 'ai-turns.sqlite3':
            raise ExternalContextError('external_context_owner_changed')
        self.turns.get_or_create_immutable_payload(turn_id, IDENTITIES, {'schema_version': 1,
            'owner_id': self.owner_id, 'turn_id': turn_id, 'immutable_ref': saved_ref, 'entries': eligible})
        sql_companion = make_companion(owner_id=self.owner_id, turn_id=turn_id,
            immutable_ref=saved_ref, mapping=saved['mapping'], entries=sql_entries)
        self.turns.get_or_create_immutable_payload(turn_id, SQL_IDENTITIES, sql_companion)
        return deepcopy(frozen)

    def _archive(self, turn_id, *, _require_sql_identities=True):
        saved = self.turns.get_immutable_payload(turn_id, ARCHIVE)
        if (saved is None or not isinstance(saved[1], dict)
                or saved[1].get('schema_version') not in {'1.0.0', '2.0.0', '3.0.0', '4.0.0'}
                or set(saved[1]) != (_ARCHIVE_FIELDS | {'origin'} if saved[1]['schema_version'] == '2.0.0'
                    else _ARCHIVE_FIELDS | {'catalog'} if saved[1]['schema_version'] == '3.0.0'
                    else _ARCHIVE_FIELDS | {'recall'} if saved[1]['schema_version'] == '4.0.0' else _ARCHIVE_FIELDS)
                or saved[1]['owner_id'] != self.owner_id
                or saved[1]['turn_id'] != turn_id):
            raise ExternalContextError('external_context_binding_invalid')
        if saved[1]['schema_version'] == '2.0.0':
            origin = saved[1]['origin']
            if (not isinstance(origin, dict) or set(origin) != _ORIGIN_FIELDS
                    or any(not isinstance(origin[key], str) or not origin[key] for key in origin)
                    or origin['turn_id'] == turn_id
                    or saved[1]['request']['capability_request']['arguments']['tool'] != 'read'):
                raise ExternalContextError('external_context_binding_invalid')
        if saved[1]['schema_version'] == '3.0.0':
            if (self.catalog is None or saved[1]['mapping'] != {} or saved[1]['selections'] != []
                    or saved[1]['request']['capability_request']['arguments']['tool'] != 'projects'
                    or saved[1]['request']['policy_versions']['handoff'] != '@2'
                    or saved[1]['handoff']['version'] != 'handoff@2'):
                raise ExternalContextError('external_context_binding_invalid')
        if saved[1]['schema_version'] == '4.0.0':
            plan = saved[1]['recall']
            if (self.recall is None or _encoded(plan['request']) != _encoded(saved[1]['request']['capability_request']['arguments'])
                    or _encoded(plan['selections']) != _encoded(saved[1]['selections'])
                    or {key: value for key, value in plan['policy_versions'].items() if key != 'compose'} != saved[1]['request']['policy_versions']
                    or saved[1]['handoff']['version'] != 'handoff@1'):
                raise ExternalContextError('external_context_binding_invalid')
        if _require_sql_identities:
            try:
                identities = self.turns.get_immutable_payload(turn_id, SQL_IDENTITIES)
                if identities is None:
                    raise ExternalContextError('external_context_binding_invalid')
                validate_companion(identities[1], owner_id=self.owner_id, turn_id=turn_id,
                    immutable_ref=saved[0], mapping=saved[1]['mapping'])
            except (ValueError, TypeError, KeyError, IndexError):
                raise ExternalContextError('external_context_binding_invalid') from None
        return saved

    def _validate_recall(self, archive, reader=None):
        if archive['schema_version'] == '4.0.0':
            self.recall.validate(archive['recall'], reader)
            self.recall.validate_binding(archive['recall'], archive['binding'])

    def invoke(self, request):
        ref, archive = self._archive(request['turn_id'])
        frozen = self.turns.get_request(request['turn_id'])
        if (_encoded(frozen) != _encoded(archive['request'])
                or _encoded(request['arguments']) != _encoded(frozen['capability_request']['arguments'])
                or _encoded(request['privacy']) != _encoded(frozen['privacy'])):
            raise ExternalContextError('external_context_binding_invalid')
        if archive['schema_version'] == '3.0.0':
            self.catalog.validate(archive['catalog'])
        self._validate_recall(archive)
        self.guard.reserve(request['turn_id'], request['arguments'])
        return {'summary': 'External context prepared', 'payload_ref': ref, 'evidence_refs': []}

    def _completed(self, turn_id):
        ref, archive = self._archive(turn_id)
        def effect(identity):
            value = self.turns.effect_runner.log.get(identity)
            return None if value is None else {'state': value.state.value, 'result_ref': value.result_ref,
                'turn_id': value.turn_id, 'attempt': value.attempt, 'intent_ref': value.intent_ref}
        try:
            outcome_ref, _ = completed(ref, archive, self.turns.get_request(turn_id),
                self.turns.events_after(turn_id), self.turns.get, effect)
        except ValueError as error:
            raise ExternalContextError(str(error)) from None
        return ref, archive, outcome_ref

    @_safe
    def execute(self, turn_id, *, runtime, runner):
        if runtime is not self.runtime:
            raise ExternalContextError('external_context_owner_changed')
        _, archive = self._archive(turn_id)
        self.guard.validate(turn_id, archive['request']['capability_request']['arguments'])
        if archive['schema_version'] == '3.0.0':
            self.catalog.validate(archive['catalog'])
        self._validate_recall(archive)
        runner.accept_and_submit(archive['request'])
        receipt = runner.wait_for_terminal(turn_id, timeout_seconds=125)
        if receipt is None or receipt.status != 'completed':
            raise ExternalContextError('external_context_not_completed')
        ref, archive, outcome_ref = self._completed(turn_id)
        self.guard.validate(turn_id, archive['request']['capability_request']['arguments'])
        if archive['schema_version'] == '3.0.0':
            self.catalog.validate(archive['catalog'])
        self._validate_recall(archive)
        with self.records.begin() as tx:
            if archive['schema_version'] == '3.0.0':
                ExternalAgentGuard(TransactionRecords(tx), owner_id=self.owner_id, now=self.now).validate(
                    turn_id, archive['request']['capability_request']['arguments'])
                self.catalog.validate(archive['catalog'], tx)
            if archive['schema_version'] == '4.0.0':
                ExternalAgentGuard(TransactionRecords(tx), owner_id=self.owner_id, now=self.now).validate(
                    turn_id, archive['request']['capability_request']['arguments'])
                self._validate_recall(archive, tx)
            current = tx.read(DELIVERIES, turn_id)
            payload = {'owner_id': self.owner_id, 'turn_id': turn_id, 'immutable_ref': ref,
                'outcome_ref': outcome_ref, 'at': current.payload['at'] if current else self.now().isoformat()}
            if current is not None and (current.revision != 1 or _encoded(current.payload) != _encoded(payload)):
                raise ExternalContextError('external_context_binding_invalid')
            if current is None:
                tx.put(DELIVERIES, turn_id, payload, expected_revision=0)
                tx.commit()
        return deepcopy(archive['handoff'])

    @_safe
    def delivered_proof(self, turn_id, identity, *, client, require_current=True):
        """Resolve a number in one actual delivered Turn, never a global alias."""
        ref, archive, outcome_ref = self._completed(turn_id)
        arguments = archive['request']['capability_request']['arguments']
        if (arguments['client'] != client or not isinstance(identity, str)
                or identity not in archive['mapping']):
            raise ExternalContextError('external_context_citations_invalid')
        with self.records.begin() as tx:
            delivery = tx.read(DELIVERIES, turn_id)
            if (delivery is None or delivery.revision != 1
                    or delivery.payload.get('owner_id') != self.owner_id
                    or delivery.payload.get('immutable_ref') != ref
                    or delivery.payload.get('outcome_ref') != outcome_ref):
                raise ExternalContextError('external_context_not_delivered')
        if require_current:
            self.guard.validate(turn_id, arguments)
            self._validate_recall(archive)
        return deepcopy(archive['mapping'][identity]), deepcopy(arguments)

    @_safe
    def qualified_delivery(self, turn_id):
        """只读交付证明；原正文留在原归档，返回副本不新增外发或使用事实。"""
        ref, archive, outcome_ref = self._completed(turn_id)
        frozen = archive['request']
        arguments = frozen['capability_request']['arguments']
        with self.records.begin() as tx:
            guard = ExternalAgentGuard(TransactionRecords(tx), owner_id=self.owner_id, now=self.now)
            guard.validate(turn_id, arguments)
            delivery = tx.read(DELIVERIES, turn_id)
            if (delivery is None or delivery.revision != 1
                    or set(delivery.payload) != {'owner_id','turn_id','immutable_ref','outcome_ref','at'}
                    or delivery.payload['owner_id'] != self.owner_id or delivery.payload['turn_id'] != turn_id
                    or delivery.payload['immutable_ref'] != ref or delivery.payload['outcome_ref'] != outcome_ref):
                raise ExternalContextError('external_context_not_delivered')
            at = delivery.payload['at']
            try:
                stamp = datetime.fromisoformat(at) if isinstance(at,str) else None
                valid_time = stamp is not None and stamp.tzinfo is not None and stamp <= self.now()
            except (ValueError, TypeError):
                valid_time = False
            if not valid_time:
                raise ExternalContextError('external_context_not_delivered')
        result = {'owner_id':self.owner_id,'turn_id':turn_id,'immutable_ref':ref,'outcome_ref':outcome_ref,
            'client':arguments['client'],'project_id':frozen['scope']['project_id'],
            'policy_versions':frozen['policy_versions'],'privacy':frozen['privacy'],
            'refs':frozen['input']['refs'],'mapping':archive['mapping']}

        def secret_metadata(value):
            if isinstance(value,str):
                return contains_secret(value)
            if isinstance(value,dict):
                return any(secret_metadata(key) or secret_metadata(part) for key,part in value.items())
            if isinstance(value,list):
                return any(secret_metadata(part) for part in value)
            return False

        handoff = deepcopy(archive['handoff'])
        delivered = [*handoff['entries'], *handoff['profile']]
        ids = [entry['id'] for entry in delivered]
        if len(ids) != len(set(ids)) or set(ids) != set(archive['mapping']):
            raise ExternalContextError('external_context_binding_invalid')
        # 来源编号和证明不能脱敏后换身份；正文可以在返回副本安全投影。
        if secret_metadata(result) or any(secret_metadata({key:value for key,value in entry.items()
                if key not in {'title','excerpt','conditions'}}) for entry in delivered):
            raise ExternalContextError('external_context_binding_invalid')

        def redact(value):
            if isinstance(value,str):
                return redact_secrets(value)
            if isinstance(value,dict):
                return {key:redact(part) for key,part in value.items()}
            if isinstance(value,list):
                return [redact(part) for part in value]
            return value

        handoff = redact(handoff)
        handoff['text'] = (_encoded({'entries':handoff['entries'],'profile':handoff['profile']})
            if delivered else '')
        # tokens/budget 仍是原交付事实；宿主须对最终安全投影重新核验启动预算。
        return deepcopy({**result,'handoff':handoff})

    @_safe
    def report_use(self, turn_id, ids, *, client=None):
        if (not isinstance(ids, list) or any(not isinstance(identity, str) for identity in ids)
                or len(ids) != len(set(ids))):
            raise ExternalContextError('external_context_citations_invalid')
        ref, archive, outcome_ref = self._completed(turn_id)
        if client is not None and archive['request']['capability_request']['arguments']['client'] != client:
            raise ExternalContextError('external_context_citations_invalid')
        if any(identity not in archive['mapping'] for identity in ids):
            raise ExternalContextError('external_context_citations_invalid')
        delivered_ids = [entry['id'] for entry in [*archive['handoff']['entries'], *archive['handoff']['profile']]]
        if len(delivered_ids) != len(set(delivered_ids)) or set(delivered_ids) != set(archive['mapping']):
            raise ExternalContextError('external_context_binding_invalid')
        def targets(used_ids):
            result = []
            for identity in delivered_ids:
                if identity in used_ids:
                    proof = archive['mapping'][identity]
                    target = {**proof['material'], 'kind': proof['usage_kind']}
                    if target not in result:
                        result.append(target)
            return result
        with self.records.begin() as tx:
            delivery = tx.read(DELIVERIES, turn_id)
            if (delivery is None or delivery.revision != 1 or delivery.payload.get('owner_id') != self.owner_id
                    or delivery.payload.get('immutable_ref') != ref or delivery.payload.get('outcome_ref') != outcome_ref):
                raise ExternalContextError('external_context_not_delivered')
            authority = SourceEgressService(TransactionRecords(tx))
            for identity in ids:
                proof = archive['mapping'][identity]
                material, snapshot = proof['material'], proof['snapshot']
                scope = WorkScope(self.owner_id, material['project_id'])
                resolve_turn_material(tx, scope, material)
                authority.validate_snapshot(scope, snapshot)
            current = tx.read(USES, turn_id)
            payload = {'owner_id': self.owner_id, 'immutable_ref': ref, 'ids': [], 'objects': []} if current is None else deepcopy(dict(current.payload))
            if (set(payload) != {'owner_id', 'immutable_ref', 'ids', 'objects'}
                    or payload['owner_id'] != self.owner_id or payload['immutable_ref'] != ref
                    or not isinstance(payload['ids'], list)
                    or any(not isinstance(identity, str) or identity not in archive['mapping'] for identity in payload['ids'])
                    or len(payload['ids']) != len(set(payload['ids']))
                    or payload['ids'] != [identity for identity in delivered_ids if identity in payload['ids']]
                    or _encoded(payload['objects']) != _encoded(targets(payload['ids']))
                    or (current is not None and not 1 <= current.revision <= len(payload['ids']))):
                raise ExternalContextError('external_context_binding_invalid')
            for identity in ids:
                if identity in payload['ids']:
                    continue
                proof = archive['mapping'][identity]
                material = proof['material']
                target = {**material, 'kind': proof['usage_kind']}
                if target not in payload['objects']:
                    if proof['usage_kind'] is not None:
                        UsageService(TransactionRecords(tx)).record_usage(proof['usage_kind'], material['id'],
                            material['project_id'], 1.0, event_kind='citation')
                    payload['objects'].append(target)
                payload['ids'].append(identity)
            payload['ids'] = [identity for identity in delivered_ids if identity in payload['ids']]
            payload['objects'] = targets(payload['ids'])
            if payload['ids'] and (current is None or _encoded(payload) != _encoded(current.payload)):
                tx.put(USES, turn_id, payload, expected_revision=current.revision if current else 0)
                tx.commit()
        return {'turn_id': turn_id, 'ids': list(ids)}


def delivery_receipts(records, runtime_root):
    """Read-model projection only; an index row alone proves no delivery."""
    database = Path(runtime_root) / '.rebuild-data' / 'ai-turns.sqlite3'
    if not database.is_file():
        return []
    result = []
    try:
        with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as connection:
            connection.execute('PRAGMA query_only=ON')
            connection.execute('BEGIN')
            for row in records.list(DELIVERIES):
                identity, index = row.object_id, row.payload
                if row.revision != 1 or set(index) != {'owner_id', 'turn_id', 'immutable_ref', 'outcome_ref', 'at'} or index['turn_id'] != identity:
                    continue
                saved = connection.execute('SELECT payload_ref,payload_json FROM ai_turn_immutable_payloads WHERE turn_id=? AND kind=?', (identity, ARCHIVE)).fetchone()
                request = connection.execute('SELECT request_json FROM ai_turns WHERE turn_id=?', (identity,)).fetchone()
                terminal = connection.execute('SELECT event_json FROM ai_turn_events WHERE turn_id=? ORDER BY sequence DESC LIMIT 1', (identity,)).fetchone()
                outcome = connection.execute('SELECT payload_json FROM ai_turn_payloads WHERE payload_ref=? AND turn_id=? AND kind=?',
                    (index['outcome_ref'], identity, 'tool-invocation-outcome')).fetchone()
                if not all((saved, request, terminal, outcome)) or saved[0] != index['immutable_ref']:
                    continue
                archive, frozen, event, fact = (json.loads(saved[1]), json.loads(request[0]), json.loads(terminal[0]), json.loads(outcome[0]))
                if (archive['owner_id'] != index['owner_id'] or archive['turn_id'] != identity
                        or _encoded(archive['request']) != _encoded(frozen) or event['type'] != 'turn.completed'
                        or frozen['desired_outcome'] != 'external.context' or fact['status'] != 'completed'
                        or fact['capability_id'] != CAPABILITY or fact['payload_ref'] != saved[0]):
                    continue
                result.append({'at': index['at'], 'items': len(archive['handoff']['projects'])
                    if archive.get('schema_version') == '3.0.0' else len(archive['mapping'])})
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        return []
    return result
