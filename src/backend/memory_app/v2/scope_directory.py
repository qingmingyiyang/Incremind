"""显式调用的内部目录候选；只映射既有归属，不生成主题或引用正文。"""
from urllib.parse import urlencode

from backend.recognition import RecognitionConflict
from ..workspace_contracts import _project
from .overviews import ScopeOverviews
from .policies import get, version
from .policies.types import ScopeInput
from .privacy import is_private_project


class ScopeDirectory:
    """仅供已授权隔离仓库使用；原来源owner可能初始化辅助文件，非在线只读入口。"""

    def __init__(self, records, documents):
        self.records, self.documents = records, documents
        self.overviews = ScopeOverviews(records, documents, None)

    def _inputs(self, project, scene, scope_version):
        if is_private_project(self.records, project):
            raise RecognitionConflict('directory_private_scope')
        signature, summaries = self.overviews._snapshot(project, None)
        visible = get('scope', version=scope_version)
        eligible = {item['id'] for item in summaries}
        members = []
        assignments = {}
        for member in signature['members']:
            # 不把无合格摘要的成员及私密来源身份带入目录或其内部绑定。
            if member['id'] not in eligible:
                continue
            assignment = member.get('assignment')
            if assignment:
                payload = assignment['payload']
                if (payload.get('project_id') != project or 'scene' not in payload
                        or payload['scene'] is not None and not isinstance(payload['scene'], str)):
                    raise RecognitionConflict('directory_assignment_invalid')
                assigned = payload['scene']
            else:
                assigned = None
            if visible(ScopeInput(scene, assigned)):
                members.append(member)
                assignments[member['id']] = assigned
        project_row = self.records.read('v2_projects', project)
        binding = {**signature, 'scene': scene, 'members': members,
                   'project_revision': project_row.revision if project_row else 0}
        return binding, [item for item in summaries if item['id'] in assignments], assignments

    def build(self, project, scene=None, *, scope_version=None):
        """目录只含真实摘要位置和来源身份；引用仍须经原Query及SourceEgress重新准入。"""
        project = _project(project)
        if scene is not None and (not isinstance(scene, str) or not scene.strip()):
            raise ValueError('invalid_directory_scene')
        selected_scope = scope_version or version('scope')
        binding, summaries, assignments = self._inputs(project, scene, selected_scope)
        entries = []
        for item in summaries:
            entries.append({
                'document_id': item['id'], 'document_revision': item['revision'],
                'assigned_scene': assignments[item['id']],
                'summary_window': {'start': item['start'], 'end': item['end'],
                                   'coordinate_space': 'document_markdown_v1'},
                'source_roots': item['snapshot']['roots'],
                'href': '#' + urlencode({'view': 'library', 'project_id': project,
                                        'layer': 'summary', 'document_id': item['id']}),
            })
        groups = []
        for assigned in sorted({entry['assigned_scene'] for entry in entries},
                               key=lambda value: (value is not None, value or '')):
            groups.append({'scene': assigned, 'document_ids': [entry['document_id']
                           for entry in entries if entry['assigned_scene'] == assigned]})
        result = {'candidate_version': 1, 'scope_version': selected_scope,
                  'project_id': project, 'scene': scene, 'groups': groups, 'entries': entries,
                  'coverage': {'eligible_summary_count': len(entries),
                               'summary_characters': sum(item['end'] - item['start'] for item in summaries)},
                  '_binding': binding}
        if self._inputs(project, scene, selected_scope) != (binding, summaries, assignments):
            raise RecognitionConflict('directory_inputs_changed')
        return result

    def validate(self, candidate):
        """只核候选仍是当前目录，不授予引用、外发或执行权限。"""
        if not isinstance(candidate, dict) or candidate.get('candidate_version') != 1:
            raise RecognitionConflict('directory_candidate_invalid')
        current = self.build(candidate.get('project_id'), candidate.get('scene'),
                             scope_version=candidate.get('scope_version'))
        if candidate != current:
            raise RecognitionConflict('directory_candidate_changed')
