"""Frozen historical research request used only by boundary tests."""
from uuid import uuid4
from backend.memory_app.workspace_contracts import _now
READ_CAPABILITIES = ['memory.recall', 'source.evidence.read', 'workbench.question.answer', 'memory.candidate.evidence.read', 'project_skill.evidence.read']

def research_request(turn_id, project, text):
    identity = uuid4().hex
    return {'schema_version': '1.0.0', 'turn_id': 'turn-' + identity,
        'session_id': 'session-' + turn_id, 'operation_id': 'op-research-' + identity,
        'idempotency_key': 'research-' + turn_id,
        'scope': {'kind': 'project', 'project_id': project, 'series_id': None},
        'input': {'kind': 'text', 'text': text, 'refs': []},
        'desired_outcome': 'project.answer',
        'privacy': {'mode': 'remote_allowed', 'allow_remote': True, 'pii': 'possible',
                    'consent_refs': ['crp://default/consent/' + turn_id], 'retention': 'local_durable'},
        'capability_policy': {'allowed': READ_CAPABILITIES, 'denied': [], 'require_approval': []},
        'context_policy': {'include_project_skill': False, 'include_memory': False,
                           'include_session_history': False, 'max_context_bytes': 11744},
        'approval_policy': {'mode': 'explicit', 'auto_approve_read_only': True}, 'created_at': _now()}
