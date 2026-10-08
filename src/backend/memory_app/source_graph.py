"""Fixed, body-free metadata for new product drafts; owners retain all edges."""
from collections.abc import Mapping
import re

from backend.recognition import RecognitionConflict
from backend.recognition.product_draft_dependencies import PRODUCT_DRAFT_OWNER_FIELDS
from backend.recognition.document_filings import FILED_EDIT_OWNER_FIELDS, DocumentFilingError, validate_filed_edit_owner
from backend.recognition.external_input_dependencies import (
    OWNER_FIELDS as EXTERNAL_INPUT_OWNER_FIELDS, metadata as external_input_metadata, ExternalInputDependencyError,
)
from .source_evidence_refs import READS

LIMIT = 256
MAX_REVISION = 9223372036854775807
ORIGIN_FIELDS = frozenset({'origin_marker_revision', 'source_project_id',
    'source_experience_id', 'source_revision'})
_ARTIFACT = {'task_revision', 'document_revision', 'document_revision_record_revision',
    'document_markdown_revision', 'context_packet_revision'}
_WORKSPACE = {'workspace_item_revision', 'document_revision', 'document_revision_record_revision',
    'document_markdown_revision'}
_LEGACY = {'legacy_review_id', 'legacy_review_revision', 'confirmed_document_revision',
    'confirmed_document_record_revision', 'confirmed_document_markdown_revision',
    'document_revision', 'document_revision_record_revision', 'document_markdown_revision'}
_OWNER_IDS = {'product_turn_id', 'task_draft_operation_id', 'task_execution_id'}
_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$')
_MATERIAL = {'kind', 'scope', 'type', 'id', 'source_revision', 'policy_revision', 'effective_purposes'}
_PROOF = {'kind', 'scope', 'product_turn_id', 'turn_id', 'tool_call_id', 'proof_revision',
    'capability_id', 'completed_sequence', 'outcome_ref', 'result_ref'}
_TYPES = {'experience', 'recognition', 'original_item', 'original_source'}
_PURPOSES = ['embedding', 'generation', 'rerank']


def identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise RecognitionConflict('product graph identity is invalid')
    return value


def revision(value, *, minimum=1):
    if type(value) is not int or not minimum <= value <= MAX_REVISION:
        raise RecognitionConflict('product graph revision is invalid')
    return value


def owner_metadata(value):
    if not isinstance(value, Mapping) or set(value) != PRODUCT_DRAFT_OWNER_FIELDS:
        raise RecognitionConflict('product graph owner is invalid')
    for field in PRODUCT_DRAFT_OWNER_FIELDS:
        (identifier if field in _OWNER_IDS else revision)(value[field])
    if value['task_draft_operation_id'] != 'deliver-' + value['product_turn_id']:
        raise RecognitionConflict('product graph operation is invalid')


def key(node):
    scope = node['scope']
    if node['kind'] == 'material':
        return 'material', scope['user_id'], scope['project_id'], node['type'], node['id']
    return ('read_proof', scope['user_id'], scope['project_id'], node['product_turn_id'],
        node['turn_id'], node['tool_call_id'])


def identity(node):
    if node['kind'] == 'read_proof':
        return key(node), tuple(sorted((k, v) for k, v in node.items() if k != 'scope'))
    dependency = node.get('dependency_revisions')
    return (key(node), node['source_revision'], node.get('incarnation'),
        None if dependency is None else tuple(sorted(dependency.items())))


def graph_identity(graph):
    return tuple(graph['roots']), tuple(identity(node) for node in graph['nodes'])


def _validate_entry(node, user):
    if not isinstance(node, Mapping) or not isinstance(node.get('scope'), Mapping):
        raise RecognitionConflict('product graph node is invalid')
    scope = node['scope']
    if set(scope) != {'user_id', 'project_id'} or scope['user_id'] != user:
        raise RecognitionConflict('product graph scope is invalid')
    identifier(scope['user_id']); identifier(scope['project_id'])
    if node.get('kind') == 'read_proof':
        if set(node) != _PROOF or not isinstance(node['capability_id'], str) or node['capability_id'] not in READS:
            raise RecognitionConflict('product graph read proof is invalid')
        for field in ('product_turn_id', 'turn_id', 'tool_call_id'):
            identifier(node[field])
        if revision(node['proof_revision']) != 1:
            raise RecognitionConflict('product graph read proof changed')
        revision(node['completed_sequence'])
        for field in ('outcome_ref', 'result_ref'):
            value = node[field]
            if (not isinstance(value, str) or not value.isascii() or len(value) > 512
                    or not re.fullmatch('crp://session/' + re.escape(node['turn_id']) + '/' +
                        ('tool-invocation-outcome' if field == 'outcome_ref' else 'tool-result') + '/[0-9a-f]{32}', value)
                    or any(character.isspace() for character in value)):
                raise RecognitionConflict('product graph read result is invalid')
        return
    if node.get('kind') != 'material' or not _MATERIAL <= set(node) or set(node) - _MATERIAL - {
            'incarnation', 'dependency_revisions'}:
        raise RecognitionConflict('product graph material is invalid')
    if not isinstance(node['type'], str) or node['type'] not in _TYPES:
        raise RecognitionConflict('product graph material type is invalid')
    identifier(node['id']); revision(node['source_revision']); revision(node['policy_revision'], minimum=0)
    if node['effective_purposes'] not in ([], _PURPOSES):
        raise RecognitionConflict('product graph purposes are invalid')
    if 'incarnation' in node and (node['type'] != 'original_source' or not isinstance(node['incarnation'], str)
            or not (node['incarnation'] == 'legacy' or re.fullmatch(r'[0-9a-f]{32}', node['incarnation']))):
        raise RecognitionConflict('product graph incarnation is invalid')
    dependency = node.get('dependency_revisions')
    if dependency is None:
        return
    if node['type'] != 'experience' or not isinstance(dependency, Mapping):
        raise RecognitionConflict('product graph dependency is invalid')
    fields = set(dependency)
    if fields == PRODUCT_DRAFT_OWNER_FIELDS:
        owner_metadata(dependency)
    elif fields == EXTERNAL_INPUT_OWNER_FIELDS:
        try:
            external_input_metadata(dependency)
        except ExternalInputDependencyError as error:
            raise RecognitionConflict('external input graph owner is invalid') from error
    elif fields == ORIGIN_FIELDS:
        identifier(dependency['source_project_id']); identifier(dependency['source_experience_id'])
        revision(dependency['origin_marker_revision']); revision(dependency['source_revision'])
        if dependency['source_project_id'] == scope['project_id']:
            raise RecognitionConflict('product graph origin scope is invalid')
    elif fields == FILED_EDIT_OWNER_FIELDS:
        try:
            validate_filed_edit_owner(dependency)
        except DocumentFilingError as error:
            raise RecognitionConflict(str(error)) from error
    elif fields in (_ARTIFACT, _WORKSPACE, _LEGACY):
        for field, value in dependency.items():
            (identifier if field == 'legacy_review_id' else revision)(value)
    else:
        raise RecognitionConflict('product graph dependency is invalid')


def validate_graph(graph, user, *, trail=(), budget=None):
    """Parse metadata only. Current owners decide closure and authorization."""
    if not isinstance(graph, Mapping) or set(graph) != {'roots', 'nodes'}:
        raise RecognitionConflict('product source graph is invalid')
    roots, nodes = graph['roots'], graph['nodes']
    if not isinstance(roots, list) or not isinstance(nodes, list) or len(nodes) > LIMIT or len(roots) > LIMIT:
        raise RecognitionConflict('product source graph is too large or invalid')
    if any(type(index) is not int or not 0 <= index < len(nodes) for index in roots) or roots != sorted(set(roots)):
        raise RecognitionConflict('product source graph roots are invalid')
    budget = set() if budget is None else budget
    keys = []
    for node in nodes:
        _validate_entry(node, user)
        own = key(node)
        if own in keys or own in trail or own not in budget and len(budget) >= LIMIT:
            raise RecognitionConflict('product source graph is cyclic, duplicated or too large')
        keys.append(own); budget.add(own)
    if keys != sorted(keys):
        raise RecognitionConflict('product source graph order is invalid')
    return graph


class SourceGraph:
    """Deduplicate scoped current CAS facts, stripping all nested graph tables."""
    def __init__(self):
        self.nodes, self.roots = {}, set()

    def add(self, node, *, root=False):
        own = key(node)
        if own in self.nodes and self.nodes[own] != node:
            raise RecognitionConflict('product source graph has conflicting material revisions')
        if own not in self.nodes and len(self.nodes) >= LIMIT:
            raise RecognitionConflict('product source graph is too large')
        self.nodes[own] = node
        if root:
            self.roots.add(own)

    def snapshot(self, snapshot, *, root=True):
        scope = snapshot['scope']
        for source in snapshot['nodes']:
            node = {key: value for key, value in source.items() if key != 'research_style_sources'}
            node.update(kind='material', scope=scope)
            dependency = source.get('dependency_revisions')
            if dependency and 'current_source_graph' in dependency:
                node['dependency_revisions'] = {k: v for k, v in dependency.items() if k != 'current_source_graph'}
                for parent in dependency['current_source_graph']['nodes']:
                    self.add(parent)
            elif dependency and 'source_snapshot' in dependency:
                node['dependency_revisions'] = {k: v for k, v in dependency.items() if k != 'source_snapshot'}
                self.snapshot(dependency['source_snapshot'], root=False)
            self.add(node)
            for style in source.get('research_style_sources', []):
                self.snapshot(style, root=False)
        if root:
            self.roots.update(('material', scope['user_id'], scope['project_id'], ref['type'], ref['id'])
                for ref in snapshot['roots'])

    def result(self):
        ordered = sorted(self.nodes)
        return {'roots': [index for index, own in enumerate(ordered) if own in self.roots],
            'nodes': [self.nodes[own] for own in ordered]}
