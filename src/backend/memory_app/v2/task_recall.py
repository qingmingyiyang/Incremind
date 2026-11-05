"""任务召回复用 WorkspaceQuery，证明只冻结修订及来源，不复制正文。"""
from copy import deepcopy

from backend.recognition import RecognitionConflict
from ..research_reads import scope_for
from .bookshelf import consult_bookshelf


class TaskRecall:
    def __init__(self, query, turns, fallback, agents=None):
        self.query, self.turns, self.fallback, self.agents = query, turns, fallback, agents

    def invoke(self, request):
        # 产品任务先持久化来源证明，才允许将正文交给执行内核。
        frozen = self.turns.get_request(request['turn_id'])
        if frozen and frozen.get('desired_outcome') == 'project.task':
            raise RecognitionConflict('product recall requires the research read boundary')
        return self.fallback.invoke(request)

    def prepare_research_read(self, request):
        frozen = self.turns.get_request(request['turn_id'])
        if not frozen or frozen.get('desired_outcome') != 'project.task':
            return None
        if frozen['scope'] != request['scope'] or frozen['privacy'] != request['privacy']:
            raise RecognitionConflict('product recall frozen scope changed')
        from ..research_sources import ReadControl
        from ..retrieval_models import retrieval_authority
        control = ReadControl(None, self.query.records, self.turns, self.agents, frozen)

        def validate_parent():
            context = request.get('execution_context')
            if context is not None:
                context.checkpoint()
            # 派生问题可能来自已经读过的正文，复用原发送边界而非只验证新候选。
            with control._qualified_sources():
                pass

        with retrieval_authority(validate_parent):
            result = self._prepare(request)
            validate_parent()
            return result

    def _prepare(self, request):
        scope = scope_for(request)
        arguments = request.get('arguments', {})
        question = str(arguments.get('query', '')).strip()
        if not question:
            raise ValueError('memory.recall requires query')
        # 工具条数只收紧产品选择，旧 layer/trust 参数不扩大资格。
        limit = max(0, min(12, int(arguments.get('limit', 12))))
        records = self.query.records
        shared = self.query.origin_admission
        # 翻书可能带关联摘要，选择前冻结文档与遗忘旁路修订。
        document_versions = {row.object_id:row.revision for row in records.list('documents')}
        preference_versions = {collection:{row.object_id:row.revision for row in records.list(collection)}
                               for collection in ('recognition_recall_preferences', 'v2_document_recall')}
        profile_versions = ({row.object_id: row.revision for row in
            self.query.profile_owner.records.list('recognition_recall_preferences')}
            if shared is not None else preference_versions['recognition_recall_preferences'])
        collected = self.query.collect_candidates(scope.project_id, question)
        qualified, excluded = [], list(collected['excluded_sources'])
        for candidate in collected['candidates']:
            if candidate.get('snapshot') is None:
                # 历史和部分交付仍可阅读；产品外发召回只选择已有原始来源证明的资料。
                if len(excluded) < 20:
                    excluded.append({'type': candidate['kind'], 'id': candidate['entry']['id'],
                        'revision': candidate['entry']['revision'], 'reason': 'source_unavailable'})
                continue
            qualified.append(candidate)
        plan = self.query.prepare_ask(scope.project_id, question, evidence_limit=limit,
            collected={**collected, 'candidates': qualified, 'excluded_sources': excluded})
        consult_bookshelf(self.query, scope.project_id, question, plan, require_source_snapshot=True)
        self.query.validate_ask_plan(plan)
        profile = plan['profile']
        # 翻书会追加资料，最终条数仍先为已经入选的画像留足名额。
        selected = plan['chosen'][:max(0, limit - len(profile['items']))]
        materials, results, documents, preferences = [], [], {}, {}
        for candidate in selected:
            entry = candidate['entry']
            own = candidate.get('scope', scope)
            snapshot = deepcopy(candidate.get('snapshot'))
            if snapshot is None:
                raise RecognitionConflict('product recall has no original authority')
            material = {'kind':candidate['kind'], 'id':entry['id'], 'revision':entry['revision'],
                        'project_id':own.project_id, 'snapshot':snapshot,
                        'bookshelf':bool(candidate.get('bookshelf')),
                        'item_id':entry.get('item_id'), 'item_revision':entry.get('item_revision')}
            if shared is not None:
                material['namespace'] = candidate.get('namespace', self.query.source_namespace)
            materials.append(material)
            ids = set(candidate.get('document_ids', []))
            if candidate['kind'] == 'document':
                ids.add(entry['id'])
            for identity in ids:
                if identity not in document_versions:
                    raise RecognitionConflict('product recall document changed during selection')
                documents[identity] = {'id':identity, 'revision':document_versions[identity],
                                       'project_id':own.project_id}
                if shared is not None:
                    documents[identity]['namespace'] = material['namespace']
                preferences[('v2_document_recall', identity, material.get('namespace'))] = preference_versions['v2_document_recall'].get(identity, 0)
            if candidate['kind'] == 'recognition':
                preferences[('recognition_recall_preferences', entry['id'], material.get('namespace'))] = preference_versions['recognition_recall_preferences'].get(entry['id'], 0)
            collection = {'recognition':'recognitions', 'document':'documents', 'source':'sources'}[candidate['kind']]
            ref = f"crp://default/{collection}/{entry['id']}"
            if shared is not None:
                from ..source_evidence_refs import qualified_material_ref
                ref = qualified_material_ref({'type': {'source': 'original_source'}.get(candidate['kind'], candidate['kind']),
                    'id': entry['id'], 'revision': entry['revision'], 'project_id': own.project_id,
                    'namespace': material['namespace']})
            results.append({'object_id':entry['id'], 'layer':candidate['layer'],
                            'title':candidate['title'], 'content':candidate['excerpt'],
                            'source_refs':[ref], 'trust_status':'published', 'score':candidate['score'],
                            'bookshelf':material['bookshelf']})
        # 画像沿自己的项目来源继承，仅冻结实际入选事实。
        for item in profile['items']:
            material = {'kind':'recognition', 'id':item['id'], 'revision':item['revision'],
                              'project_id':'me', 'snapshot':deepcopy(item['snapshot']), 'bookshelf':False,
                              'item_id':None, 'item_revision':None}
            if shared is not None:
                material['namespace'] = shared.personal['namespace']
            materials.append(material)
            preferences[('recognition_recall_preferences', item['id'], material.get('namespace'))] = profile_versions.get(item['id'], 0)
            reference = f"crp://default/recognitions/{item['id']}"
            if shared is not None:
                from ..source_evidence_refs import qualified_material_ref
                reference = qualified_material_ref({'type': 'recognition', 'id': item['id'],
                    'revision': item['revision'], 'project_id': 'me', 'namespace': material['namespace']})
            results.append({'object_id':item['id'], 'layer':'persona', 'title':'已确认画像',
                            'content':item['content'], 'source_refs':[reference],
                            'trust_status':'published', 'score':1, 'bookshelf':False})
        binding = {'version':1, 'materials':materials,
                   'documents':list(documents.values()),
                   'preferences':[{'collection':collection, 'id':identity, 'revision':revision,
                       **({'namespace': namespace} if shared is not None else {})}
                       for (collection, identity, namespace), revision in sorted(preferences.items())]}
        if shared is not None:
            from ..kernel.answer_continuations import encode
            guards = {}
            for name in ('overview_guard', 'bookshelf_guard', 'search_guard'):
                guard = plan.get(name)
                if guard is not None:
                    value = getattr(guard, 'frozen_binding' if name == 'search_guard' else 'continuation', None)
                    if value is None:
                        raise RecognitionConflict('shared_task_recall_guard_unavailable')
                    guards[name] = value
            facts = {key: value for key, value in plan.items() if key not in
                ('overview_guard', 'bookshelf_guard', 'search_guard', 'history_guard', 'expires_monotonic', 'state', 'result')}
            binding['shared_selection'] = encode({'plan': facts, 'guards': guards, 'limit': limit})
        output = {'summary':f'recalled {len(results)} evidence items', 'payload_ref':None, 'receipt_ref':None,
                  'evidence_refs':[ref for item in results for ref in item['source_refs']], 'result':results}
        return output, binding
