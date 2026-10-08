"""代理复用原查询与 Kernel 交接；当前原领域只拥有本机用户。"""
from dataclasses import dataclass
from collections.abc import Mapping
from datetime import datetime, timezone
import json
import logging
from uuid import uuid4

from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.transaction_records import TransactionRecords
from backend.recognition import WorkScope
from backend.shared.secret_detection import REDACTED_SECRET, redact_secrets

from .external_agent_settings import external_agent_settings
from .external_context import ExternalContext, DELIVERIES
from .contextual_chunk_vectors import embedding_input_validation
from .intent import parse_scope_tag
from .ladder import plan_ladder
from .layers import summary_of
from .links import InsightLinks
from .policies import get, override
from .privacy import external_egress_allowed, resolve_turn_material
from .profile import confirmed_profile, validate_profile
from .route import resolve_scope_project


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _clean(text, credentials):
    value = redact_secrets(text)
    for credential in credentials:
        if isinstance(credential, str) and credential:
            value = value.replace(credential, REDACTED_SECRET)
    return value


def _validate_copy(value, credentials):
    """检查原字符串，避免 JSON 转义改变凭据字面值。"""
    if isinstance(value, str):
        if _clean(value, credentials) != value:
            raise ValueError('external_proxy_selected_secret')
    elif isinstance(value, Mapping):
        for key, item in value.items():
            _validate_copy(key, credentials)
            _validate_copy(item, credentials)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _validate_copy(item, credentials)


def selection_from_candidate(query, candidate):
    """将原 Query 坐标投影到原交接 schema，不重建召回算法。"""
    entry, project, layer = candidate['entry'], candidate['scope'].project_id, candidate['layer']
    if layer == 'L3':
        return {'type':'recognition', 'id':entry['id'], 'revision':entry['revision'],
                'project_id':project, 'layer':'L3', 'windows':[]}
    windows, offset = candidate['windows'], 0
    if candidate['kind'] == 'document' and layer != 'L0':
        material = {'type':'document', 'id':entry['id'], 'revision':entry['revision'], 'project_id':project}
        text = query.documents.markdown(entry['id'], revision=entry['revision'])
        if layer == 'L2':
            _, offset, end = summary_of(text)
            if any(not offset <= w.start < w.end <= end for w in windows):
                raise ValueError('external_proxy_handoff_invalid')
    else:
        original = candidate['kind'] == 'document'
        material = {'type':'original_item' if original else 'original_source',
            'id':entry['item_id'] if original else entry['id'],
            'revision':entry['item_revision'] if original else entry['revision'], 'project_id':project}
        row, _ = resolve_turn_material(query.records, candidate['scope'], material)
        body = row['payload']
        text = (body.get('source_text') if original else
                body['metadata'].get('content_snapshot') or body['metadata'].get('content'))
    if not isinstance(text, str) or any(text[w.start:w.end] != w.text for w in windows):
        raise ValueError('external_proxy_handoff_invalid')
    return {**material, 'layer':layer,
            'windows':[{'start':w.start-offset, 'end':w.end-offset} for w in windows]}


@dataclass(frozen=True)
class PreparedHandoff:
    turn_id: str
    project_id: str
    client: str
    text: str
    _context: ExternalContext
    _receipt: str

    def validate(self):
        """实际发送前读原终态、交付及当时来源，不新增目录或使用事实。"""
        try:
            _, archive, _ = self._context._completed(self.turn_id)
            row = self._context.records.read(DELIVERIES, self.turn_id)
            if row is None or row.revision != 1 or _encoded(row.payload) != self._receipt:
                raise ValueError
            self._context.guard.validate(self.turn_id, archive['request']['capability_request']['arguments'])
        except Exception:
            raise ValueError('external_proxy_handoff_invalid') from None


class ProxyHandoff:
    def __init__(self, query, context, *, runtime, runner):
        if (not isinstance(context, ExternalContext)
                or getattr(query, 'records', None) is not context.records
                or getattr(query, 'documents', None) is not context.documents
                or not callable(getattr(query, 'collect_candidates', None))
                or not callable(getattr(query, 'query_entries', None))
                or context.owner_id != 'local-user' or context.runtime is not runtime
                or context.turns is None or runtime is None or runner is None):
            raise ValueError('external_proxy_owner_invalid')
        self.query, self.context, self.runtime, self.runner = query, context, runtime, runner

    def _check_selected_secrets(self, selections, credentials):
        """原交接将保存证据正文，先检查拟复制字段并保持原事实不变。"""
        with self.context.records.begin() as tx:
            authority = SourceEgressService(TransactionRecords(tx))
            for selection in selections:
                material = {key:selection[key] for key in ('type','id','revision','project_id')}
                scope = WorkScope(self.context.owner_id, selection['project_id'])
                row, roots = resolve_turn_material(tx, scope, material)
                snapshot = authority.snapshot(scope, roots)
                entry = self.context._entry(tx, row, selection, snapshot)
                _validate_copy({'material':material, 'snapshot':snapshot, 'entry':entry}, credentials)

    def _check_retrieval_secrets(self, project, credentials):
        """在原索引和可选模型输入之前只读原资料，不另建召回能力。"""
        for entry in self.query.query_entries(project):
            _validate_copy(entry, credentials)
        for entry in self.query.service.retrieval_entries(scope=WorkScope(self.context.owner_id, project)):
            _validate_copy(entry, credentials)

    def prepare(self, client, text, *, credentials=()):
        """注入失败只放弃记忆；调用方一直保留客户端原始 HTTP 字节。"""
        identity = None
        try:
            if client not in {'claude', 'codex'} or not isinstance(text, str):
                return None
            clean = _clean(text, credentials)
            project = resolve_scope_project(self.context.records, clean, 'default')
            _, scene, question = parse_scope_tag(clean)
            if not question or not external_egress_allowed(self.context.records, project, client):
                return None
            settings = external_agent_settings(self.context.records)
            if project == 'me' and not settings['include_profile']:
                return None
            self._check_retrieval_secrets(project, credentials)
            if settings['include_profile'] and external_egress_allowed(self.context.records, 'me', client):
                self._check_retrieval_secrets('me', credentials)
            policy = get('proxy_context', version='@1')
            budget = policy(operation='budget')
            def validate_copy(value):
                _validate_copy(value, credentials)
            with embedding_input_validation(validate_copy):
                collected = self.query.collect_candidates(project, question, scene=scene, situation=question)
            with override(**collected['policy_versions']):
                links = InsightLinks(self.context.records, self.query.service)
                planned = plan_ladder(collected['candidates'], question, token_budget=budget,
                    methods=collected.get('method_candidates', ()), situation=question,
                    neighbors=lambda key: links.neighbors(project, key))
            selections = [selection_from_candidate(self.query, row) for row in planned['chosen']]
            profile = (confirmed_profile(self.context.records, self.query.service, validate_input=validate_copy)
                if settings['include_profile'] and external_egress_allowed(self.context.records, 'me', client) else None)
            if profile:
                validate_profile(self.context.records, self.query.service, profile)
                selections.extend({'type':'recognition','id':row['id'],'revision':row['revision'],
                    'project_id':'me','layer':'L3','windows':[]} for row in profile['items'])
            if not selections:
                return None
            self._check_selected_secrets(selections, credentials)
            identity = 'turn-' + uuid4().hex
            request = {'client':client,'tool':'recall','query':question,
                'scope':{'user_id':self.context.owner_id,'project_id':project},'budget':budget}
            self.context.prepare(identity, request, selections, session_id='proxy-' + identity,
                operation_id='proxy-' + identity, idempotency_key='proxy-' + identity,
                created_at=datetime.now(timezone.utc).isoformat(), validate_archive=validate_copy)
            handoff = self.context.execute(identity, runtime=self.runtime, runner=self.runner)
            rendered = _clean(policy(handoff), credentials)
            if not rendered:
                return None
            row = self.context.records.read(DELIVERIES, identity)
            result = PreparedHandoff(identity, project, client, rendered, self.context, _encoded(row.payload))
            result.validate()
            return result
        except Exception:
            # 原接受成功而准备失败时沿原 owner 收口，不修复或重做已终态任务。
            if identity is not None:
                try:
                    self.runtime.fail_accepted_turn(identity)
                except Exception:
                    logging.getLogger(__name__).warning('external_proxy_cleanup_failed')
            return None
