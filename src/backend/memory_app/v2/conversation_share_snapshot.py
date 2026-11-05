import re
from urllib.parse import urlsplit

from markdown_it import MarkdownIt

from backend.recognition import RecognitionConflict, WorkScope
from backend.recognition.external_turn_facts import completed_answer_facts
from backend.shared.document_visibility import LegacyDocumentVisibility
from core.ai_kernel.product_snapshot import read_product_turn_snapshot
from core.document_engine import SQLiteDocumentRepository

from ..answer_effective import NativeAnswerSnapshot
from ..answer_proof import read_answer_input
from ..kernel.answer_source_proof import qualify_no_match
from ..kernel.receipt_projection import frozen_public_turn_requests
from ..kernel.route_source_snapshot import native_source_connection, native_source_snapshot
from ..transaction_records import TransactionRecords
from .image_read import COMMENT_SECTION_PREFIX
from .insights import insight_view, resolve_insight
from .intent import scope_tag_match
from .material_origin_admission import read_origin_item
from .material_origins import ORIGIN_KIND
from .model_material_coverage import validate_primary_material_proof_shape
from .shared_projects import SharedProjectError
from .shared_task_outcome_read import read_task_outcome
from .turn_logical_scope import TurnLogicalScope, logical_scope


_MARKS = re.compile(r'\[([0-9]+)\]|【([0-9]+)】')
_READING_MARKER = COMMENT_SECTION_PREFIX.splitlines()[4]
_TITLE_MARKS = re.compile(r'([\\`*_{}\[\]()#+.!|>~-])')
_LINE_BREAK = re.compile(r'\r\n|\r|\n')
_MARKDOWN = MarkdownIt('commonmark')


class ConversationSnapshotRecords(TransactionRecords):
    def list_matching(self, collection, **fields):
        return tuple(row for row in self.list(collection)
            if all(row.payload.get(key) == value for key, value in fields.items()))


def unavailable():
    return SharedProjectError('conversation_source_not_completed', status=409)


def _body(value):
    if not isinstance(value, str) or not value.strip():
        raise unavailable()
    if _READING_MARKER not in value:
        return value
    positions = [0, *(match.end() for match in _LINE_BREAK.finditer(value)), len(value)]
    chunks, cursor = [], 0
    # 原 Markdown 节点用于定位，未选正文与换行保持原值。
    for token in _MARKDOWN.parse(value):
        if token.type != 'html_block' or token.level != 0 or token.content.strip() != _READING_MARKER:
            continue
        first, last = token.map
        start, stop = positions[first], positions[last]
        if value[start:stop].strip() != _READING_MARKER:
            raise unavailable()
        start = value.index(_READING_MARKER, start, stop)
        chunks.append(value[cursor:start])
        cursor = start + len(_READING_MARKER)
    return ''.join((*chunks, value[cursor:]))


def _question(value):
    value = _body(value)
    tag = scope_tag_match(value)
    if tag is not None and tag[4]:
        value = (value[:tag[0]] + value[tag[1]:]).strip()
    if not value.strip():
        raise unavailable()
    return value


def _shared_lease(destination, reader, request):
    native = native_source_snapshot(destination.records, request['turn_id'], reader=reader)
    bundle = native['admission']
    origins = [] if bundle is None else [item for item in bundle.items if item.kind == ORIGIN_KIND]
    if native['request'] != request or len(origins) != 1:
        raise unavailable()
    origin = origins[0]
    lease, auxiliary = read_origin_item(request, origin,
        actor_user_id=origin.payload['actor_user_id'], personal=origin.payload['execution'])
    if auxiliary is not None:
        raise unavailable()
    return lease, origin.ref


def _answer_proof(destination, reader, facts, reference):
    request = facts['request']
    native = facts['native_snapshot']
    if native is None:
        with native_source_connection(destination.records, reader=reader) as connection:
            native = read_product_turn_snapshot(connection, request['turn_id'])
    if native['request'] != request:
        raise unavailable()
    saved = read_answer_input(NativeAnswerSnapshot(native), request['turn_id'], product=facts['result'])
    proof = saved[1].get('material_proof') if saved is not None else None
    if proof is None:
        # 旧轮仍能分享静态片段，缺少实际材料归属的引用没有下钻入口。
        return None
    validate_primary_material_proof_shape(proof, native_request=request)
    if proof['selection']['origin_ref'] != reference:
        raise unavailable()
    return proof


def _citation_material(lease, citation, candidate):
    if (not isinstance(candidate, dict) or candidate.get('url')
            or candidate.get('project_id') != lease.project_id):
        return None
    layer = citation['layer']
    entry = candidate['entry']
    if layer == 'source':
        kind, identity, revision = ('original_item', entry['item_id'], entry.get('item_revision')) if entry.get('item_id') else (
            'original_source', entry.get('source_id'), entry.get('revision'))
    elif layer in {'summary', 'note'}:
        kind, identity, revision = 'document', entry.get('document_id'), entry.get('revision')
    elif layer == 'insight':
        kind, identity, revision = 'recognition', entry.get('id'), entry.get('revision')
    else:
        return None
    if identity != citation['id'] or not isinstance(identity, str) or type(revision) is not int or revision < 1:
        return None
    return {'type': kind, 'id': identity, 'revision': revision, 'project_id': lease.project_id}


def _anchor(lease, citation, candidate, proof):
    if proof is None:
        return None
    material = _citation_material(lease, citation, candidate)
    if material is None:
        return None
    kind, identity, revision = (material[key] for key in ('type', 'id', 'revision'))
    privacy, selection = proof['privacy'], proof['selection']
    candidates = [material]
    if kind == 'original_item':
        entry = candidate['entry']
        # 发布层可能将 JSON 来源映射为原件，必须保留实际输入和原件的对应。
        candidates.extend({**material, 'type': 'original_source', 'id': source_id, 'revision': entry['revision']}
            for source_id in {entry.get('id'), entry.get('source_id')} if isinstance(source_id, str))
    choices = [(index, value) for index, value in enumerate(selection['origin_materials'])
        if {key: value[key] for key in material} in candidates]
    if len(choices) != 1:
        return None
    position, owner = choices[0]
    if (owner['namespace'] != lease.namespace or position >= len(privacy['material_refs'])
            or position >= len(privacy['source_snapshots'])
            or privacy['material_refs'][position] != {key: owner[key] for key in material}):
        return None
    nodes = [node for node in privacy['source_snapshots'][position]['nodes']
        if (node.get('type'), node.get('id'), node.get('source_revision')) == (kind, identity, revision)]
    binding = {'project_id': lease.project_id, 'namespace': lease.namespace,
        'layer': citation['layer'], 'material': material}
    if kind == 'original_source':
        if len(nodes) != 1 or not isinstance(nodes[0].get('incarnation'), str):
            return None
        return {**binding, 'incarnation': nodes[0]['incarnation']}
    if owner['type'] != kind and len(nodes) != 1:
        return None
    collection = {'original_item': 'workspace_items', 'document': 'documents', 'recognition': 'recognitions'}[kind]
    identities = [value for value in selection['sql_identities'] if (
        value['namespace'], value['collection'], value['object_id'], value['revision']) ==
        (lease.namespace, collection, identity, revision)]
    return {**binding, 'sql_identity': identities[0]} if len(identities) == 1 else None


def _static_web_citation(citation, candidate):
    if not isinstance(candidate, dict) or not isinstance(candidate.get('url'), str):
        return False
    entry = candidate['entry']
    url = urlsplit(candidate['url'])
    return (url.scheme in {'http', 'https'} and bool(url.hostname) and not url.username and not url.password
        and candidate.get('layer') == 'L0' and citation.get('layer') == 'source'
        and citation.get('url') == candidate['url'] and citation.get('id') == (entry.get('item_id') or entry['source_id'])
        and citation.get('locator', {}).get('coordinate_space') == 'search_result_text_v1')


def _unassisted_empty_answer(destination, reader, facts):
    answer, request = facts['answer'], facts['request']
    if (answer.get('no_match') is not True or answer.get('citations') or answer.get('egress_receipt_id') is not None
            or facts['result'].get('chosen') != [] or request['privacy']['material_refs'] != []):
        return False
    try:
        with native_source_connection(destination.records, reader=reader) as connection:
            snapshot = read_product_turn_snapshot(connection, request['turn_id'])
        return (snapshot['request'] == request and not snapshot['attempts']
            and not any(event['type'].startswith('model.') for event in snapshot['events'])
            and qualify_no_match(reader, facts))
    except (RecognitionConflict, ValueError, KeyError, TypeError):
        # 旧回执无法证明没有消费来源时，保留人工读取并限制远程整理。
        return False


def _answer(destination, reader, row, kind, *, evidence=None):
    project = row.payload['project_id']
    try:
        bound_request = None
        parent = row.payload.get('parent_turn_id')
        if parent is not None:
            # 分项正文保存用户片段，原生请求使用父轮为该分项冻结的执行指令。
            entries = frozen_public_turn_requests(destination.records, destination.root, row.object_id, reader=reader)
            if (len(entries) != 1 or entries[0]['parent_id'] != parent
                    or entries[0]['public_revision'] != row.revision):
                raise ValueError('conversation_answer_part_changed')
            bound_request = entries[0]['native_request']
        facts = completed_answer_facts(reader, project_id=project, turn_id=row.object_id,
            bound_child_request=bound_request)
    except ValueError as error:
        raise unavailable() from error
    numbers, citations, anchors = set(), [], []
    ask = facts['answer']
    chosen = facts['result'].get('chosen', [])
    if evidence is not None:
        evidence['unassisted_empty_answer'] = _unassisted_empty_answer(destination, reader, facts)
        evidence['static_ordinals'] = set()
    lease = proof = None
    if kind == 'shared' and ask.get('citations'):
        lease, reference = _shared_lease(destination, reader, facts['request'])
        proof = _answer_proof(destination, reader, facts, reference)
    for citation in ask.get('citations', []):
        number = citation.get('n')
        if type(number) is not int or number < 1 or number in numbers:
            raise unavailable()
        numbers.add(number)
        candidate = chosen[number - 1] if number <= len(chosen) else None
        if (citation.get('persona') or citation.get('layer') == 'persona'
                or candidate and (candidate.get('persona') or candidate.get('project_id') == 'me')):
            continue
        if evidence is not None and _static_web_citation(citation, candidate):
            evidence['static_ordinals'].add(len(citations))
        citations.append({'title': _body(citation['title']), 'quote': _body(citation['quote'])})
        anchors.append(_anchor(lease, citation, candidate, proof))
    number_text = {str(number) for number in numbers}
    body = _MARKS.sub(lambda match: '' if (match[1] or match[2]).lstrip('0') in number_text else match[0],
        _body(ask['answer']))
    return body, citations, anchors, {'id': row.object_id, 'revision': row.revision}


def _document(application, destination, reader, row, kind):
    project, intent = row.payload['project_id'], row.payload['intent']
    receipt = row.payload['receipt'][intent]
    if intent == 'do':
        try:
            outcome = read_task_outcome(application, reader, row, kind=kind)
        except (ValueError, KeyError, TypeError) as error:
            raise unavailable() from error
        if outcome is None:
            raise unavailable()
        return _body(outcome['markdown']), [], [], {'id': outcome['id'], 'revision': outcome['revision']}
    item_id = receipt.get('item_id')
    item = reader.read('workspace_items', item_id) if item_id is not None else None
    if (item is None or row.payload.get('item_id') != item_id or item.payload.get('status') != 'confirmed'
            or item.payload.get('project_id') != project):
        raise unavailable()
    identity = item.payload.get('document_id')
    if not isinstance(identity, str):
        raise unavailable()
    documents = SQLiteDocumentRepository(reader, namespace_id=destination.namespace)
    document = documents.read(identity)
    visibility = LegacyDocumentVisibility.from_repository(documents, project_id=project, logical_scope_kind=kind)
    if document is None or document.get('project_id') != project or not visibility.allows(document):
        raise unavailable()
    revision = document['revision']
    return _body(documents.markdown(identity, revision=revision)), [], [], {'id': identity, 'revision': revision}


def _inspiration(destination, reader, row):
    identity = row.payload['receipt']['inspiration']['insight']['id']
    scope = WorkScope('local-user', row.payload['project_id'])
    value = resolve_insight(reader, scope, identity)
    if value is None:
        raise unavailable()
    insight = insight_view(reader, scope, identity, service=destination.workspace.query.service)
    if insight is None:
        raise unavailable()
    return _body(insight['text']), [], [], {'id': value.object_id, 'revision': value.revision}


def read_snapshot(application, destination, reader, turn_id, project, kind, *, evidence=None):
    row = reader.read('v2_turns', turn_id)
    if row is None or row.payload.get('project_id') != project:
        raise SharedProjectError('conversation_source_unavailable', status=404)
    intent = row.payload['intent']
    external = intent == 'do' and row.payload['receipt']['do'].get('executor', 'builtin') != 'builtin'
    leases = [] if intent == 'ask' and evidence is not None else None
    # 外部产出通过自己的原生终态和交付证明核对实际个人范围。
    actual_kind = 'personal' if external else TurnLogicalScope(application).read(row, reader=reader, shared_leases=leases)
    if actual_kind != logical_scope(kind):
        raise SharedProjectError('conversation_source_unavailable', status=404)
    thread = reader.read('v2_threads', row.payload['thread_id'])
    if thread is None or thread.payload.get('project_id') != project:
        raise unavailable()
    if intent == 'ask':
        answer, citations, anchors, content = _answer(destination, reader, row, kind, evidence=evidence)
        if leases is not None and actual_kind == 'shared':
            evidence['shared_answer_leases'] = tuple(leases)
    elif intent in {'do', 'remember'}:
        answer, citations, anchors, content = _document(application, destination, reader, row, kind)
    elif intent == 'inspiration':
        answer, citations, anchors, content = _inspiration(destination, reader, row)
    else:
        raise unavailable()
    return {'question': _question(row.payload['user_text']), 'answer': answer, 'citations': citations}, anchors, {
        'turn_revision': row.revision, 'content': content}


def snapshot_markdown(snapshot):
    parts = ['# 对话', '## 问题\n\n' + snapshot['question'], '## 回答\n\n' + snapshot['answer']]
    if snapshot['citations']:
        parts.append('## 引用')
        for citation in snapshot['citations']:
            title = _TITLE_MARKS.sub(r'\\\1', citation['title'])
            quote = '> ' + _LINE_BREAK.sub(lambda match: match[0] + '> ', citation['quote'])
            parts.append('### ' + title + '\n\n' + quote)
    return '\n\n'.join(parts)
