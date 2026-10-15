"""只读取原项目已启用并激活的技能，不执行包内文件。"""
from pathlib import Path

from backend.recognition import RecognitionConflict
from backend.security.project_capability_profiles import ProjectCapabilityProfileStore
from core.application_skill import ApplicationSkillBindingRegistry, ApplicationSkillResolver
from core.application_skill.package_catalog import (
    ApplicationSkillCatalog, ApplicationSkillPackageLoader, ApplicationSkillSource,
)
from core.storage_provider import JsonObjectStore


class RagSkills:
    def __init__(self, root):
        self.root = Path(root)
        self.bindings = ApplicationSkillBindingRegistry(JsonObjectStore(
            self.root / '.rebuild-data', legacy_root=self.root / 'library', namespace_id='default'))
        self.profiles = ProjectCapabilityProfileStore(self.root)
        self.loader = ApplicationSkillPackageLoader()

    def _available(self, project, *, task_text, project_summary):
        profile = self.profiles.get(project)
        base = next((parent for parent in Path(__file__).resolve().parents
                     if (parent / 'config' / 'application-skills').is_dir()), None)
        sources = [ApplicationSkillSource('user', self.root / 'skills', 'user')]
        if base is not None:
            sources.append(ApplicationSkillSource('bundled', base / 'config' / 'application-skills', 'bundled'))
        enabled = set(profile.profile.enabled_skill_ids)
        catalog = ApplicationSkillCatalog().discover_selected(tuple(sources), tuple(sorted(enabled)))
        bindings = self.bindings.effective_bindings(catalog, project_id=project, consumer='answer.model-request')
        preview = ApplicationSkillResolver(self.bindings).preview(catalog, project_id=project,
            consumer='answer.model-request', task_kind='project.answer', task_text=task_text,
            project_summary=project_summary, enabled_skill_ids=tuple(sorted(enabled)))
        applicable = {item.skill_id for item in preview.selected_matches}
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
        return {'id': identity, 'binding_id': item.binding_id, 'binding_revision': item.binding_revision,
                'profile_revision': profile.store_revision, 'fingerprint': item.package.fingerprint,
                'instructions': instructions.markdown, 'task_text': task_text, 'project_summary': project_summary}

    def validate(self, project, facts):
        if any(self.read(project, fact['id'], task_text=fact['task_text'],
                         project_summary=fact['project_summary']) != fact for fact in facts):
            raise RecognitionConflict('rag_skill_changed')
