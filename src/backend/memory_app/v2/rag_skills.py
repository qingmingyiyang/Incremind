"""只读取原项目已启用并激活的技能，不执行包内文件。"""
from pathlib import Path
import json
from uuid import NAMESPACE_URL, uuid5

from backend.recognition import RecognitionConflict
from backend.security.project_capability_profiles import ProjectCapabilityProfileStore
from core.application_skill import ApplicationSkillBindingRegistry, ApplicationSkillResolver
from core.application_skill.package_catalog import (
    ApplicationSkillCatalog, ApplicationSkillPackageLoader, ApplicationSkillSource,
)
from core.storage_provider import JsonObjectStore


class RagSkills:
    def __init__(self, root, *, origin_admission=None):
        self.root = Path(root)
        self.origin_admission = origin_admission
        self.bindings = ApplicationSkillBindingRegistry(JsonObjectStore(
            self.root / '.rebuild-data', legacy_root=self.root / 'library', namespace_id='default'))
        self.profiles = ProjectCapabilityProfileStore(self.root)
        self.loader = ApplicationSkillPackageLoader()

    @classmethod
    def for_query(cls, query):
        return cls(query.answer_turns.root, origin_admission=query.origin_admission)

    def project_key(self, project):
        admission = self.origin_admission
        if admission is None:
            return project
        admission.validate_execution_owner()
        admission.lease.validate(admission.registry, admission.actor)
        if self.root.resolve() != admission.destination.root or project != admission.lease.project_id:
            raise RecognitionConflict('rag_skill_origin_changed')
        identity = ['shared-rag-v1', admission.actor.user_id, admission.destination.namespace,
            admission.lease.namespace, project]
        return 'shared-rag-v1-' + uuid5(NAMESPACE_URL, json.dumps(identity, separators=(',', ':'))).hex

    def scope(self, project):
        key = self.project_key(project)
        if self.origin_admission is None:
            return None
        admission = self.origin_admission
        return {'schema_version': 1, 'project_key': key, 'source_namespace': admission.lease.namespace,
            'actor_user_id': admission.actor.user_id, 'actor_namespace_id': admission.destination.namespace}

    def _available(self, project, *, task_text, project_summary):
        key = self.project_key(project)
        profile = self.profiles.get(key)
        base = next((parent for parent in Path(__file__).resolve().parents
                     if (parent / 'config' / 'application-skills').is_dir()), None)
        sources = [ApplicationSkillSource('user', self.root / 'skills', 'user')]
        if base is not None:
            sources.append(ApplicationSkillSource('bundled', base / 'config' / 'application-skills', 'bundled'))
        enabled = set(profile.profile.enabled_skill_ids)
        catalog = ApplicationSkillCatalog().discover_selected(tuple(sources), tuple(sorted(enabled)))
        bindings = self.bindings.effective_bindings(catalog, project_id=key, consumer='answer.model-request')
        preview = ApplicationSkillResolver(self.bindings).preview(catalog, project_id=key,
            consumer='answer.model-request', task_kind='project.answer', task_text=task_text,
            project_summary=project_summary, enabled_skill_ids=tuple(sorted(enabled)))
        applicable = {item.skill_id for item in preview.selected_matches}
        self.project_key(project)
        # 用户和内置技能按显式启用ID及原Resolver治理；plugin另受来源开关约束。
        return profile, [item for item in bindings if item.package.skill_id in applicable
            and (item.package.source_kind != 'plugin' or 'plugin' in profile.profile.enabled_sources)]

    def search(self, project, query, limit, *, task_text, project_summary):
        _, available = self._available(project, task_text=task_text, project_summary=project_summary)
        terms = query.casefold().split()
        rows = [{'id': 'skill:' + item.package.skill_id, 'title': item.package.name,
                 'description': item.package.description,
                 'score': sum(term in (item.package.name + ' ' + item.package.description).casefold() for term in terms)}
                for item in available]
        return sorted(rows, key=lambda row: (-row['score'], row['id']))[:limit]

    def read(self, project, identity, *, task_text, project_summary):
        profile, available = self._available(project, task_text=task_text, project_summary=project_summary)
        item = next((item for item in available if identity == 'skill:' + item.package.skill_id), None)
        if item is None:
            raise RecognitionConflict('rag_skill_unavailable')
        instructions = self.loader.load_instructions(item.package)
        scope = self.scope(project)
        return {'id': identity, 'binding_id': item.binding_id, 'binding_revision': item.binding_revision,
                'profile_revision': profile.store_revision, 'fingerprint': item.package.fingerprint,
                'instructions': instructions.markdown, 'task_text': task_text, 'project_summary': project_summary,
                **({'origin_scope': scope} if scope is not None else {})}

    def validate(self, project, facts):
        self.project_key(project)
        if any(self.read(project, fact['id'], task_text=fact['task_text'],
                         project_summary=fact['project_summary']) != fact for fact in facts):
            raise RecognitionConflict('rag_skill_changed')
