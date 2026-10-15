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
        # 翻书可能带关联摘要，选择前冻结文档与遗忘旁路修订。
        document_versions = {row.object_id:row.revision for row in records.list('documents')}
        preference_versions = {collection:{row.object_id:row.revision for row in records.list(collection)}
                               for collection in ('recognition_recall_preferences', 'v2_document_recall')}
        plan = self.query.prepare_ask(scope.project_id, question, evidence_limit=limit)
        consult_bookshelf(self.query, scope.project_id, question, plan)
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
            materials.append(material)
            ids = set(candidate.get('document_ids', []))
            if candidate['kind'] == 'document':
                ids.add(entry['id'])
            for identity in ids:
                if identity not in document_versions:
                    raise RecognitionConflict('product recall document changed during selection')
                documents[identity] = {'id':identity, 'revision':document_versions[identity],
                                       'project_id':own.project_id}
                preferences[('v2_document_recall', identity)] = preference_versions['v2_document_recall'].get(identity, 0)
            if candidate['kind'] == 'recognition':
                preferences[('recognition_recall_preferences', entry['id'])] = preference_versions['recognition_recall_preferences'].get(entry['id'], 0)
            collection = {'recognition':'recognitions', 'document':'documents', 'source':'sources'}[candidate['kind']]
            ref = f"crp://default/{collection}/{entry['id']}"
            results.append({'object_id':entry['id'], 'layer':candidate['layer'],
                            'title':candidate['title'], 'content':candidate['excerpt'],
                            'source_refs':[ref], 'trust_status':'published', 'score':candidate['score'],
                            'bookshelf':material['bookshelf']})
        # 画像沿自己的项目来源继承，仅冻结实际入选事实。
        for item in profile['items']:
            materials.append({'kind':'recognition', 'id':item['id'], 'revision':item['revision'],
                              'project_id':'me', 'snapshot':deepcopy(item['snapshot']), 'bookshelf':False,
                              'item_id':None, 'item_revision':None})
            preferences[('recognition_recall_preferences', item['id'])] = preference_versions['recognition_recall_preferences'].get(item['id'], 0)
            results.append({'object_id':item['id'], 'layer':'persona', 'title':'已确认画像',
                            'content':item['content'], 'source_refs':[f"crp://default/recognitions/{item['id']}"],
                            'trust_status':'published', 'score':1, 'bookshelf':False})
        binding = {'version':1, 'materials':materials,
                   'documents':list(documents.values()),
                   'preferences':[{'collection':collection, 'id':identity, 'revision':revision}
                                  for (collection, identity), revision in sorted(preferences.items())]}
        output = {'summary':f'recalled {len(results)} evidence items', 'payload_ref':None, 'receipt_ref':None,
                  'evidence_refs':[ref for item in results for ref in item['source_refs']], 'result':results}
        return output, binding
