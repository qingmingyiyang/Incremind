"""extract@6 的结构化输出；旧认识输出合同不变。"""
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, model_validator

from .comparative_insights import ComparativeInsight, EvidenceSupport
from .comment_insights import CommentInsight
from .policies.extract_material import MAX_OUTPUT_CHARS, MAX_OUTPUT_ITEMS


class MaterialInsight(ComparativeInsight):
    origin: Literal['body']
    text: StrictStr = Field(min_length=1, max_length=MAX_OUTPUT_CHARS)
    claim_ids: list[StrictStr] = Field(default_factory=list)


class MaterialOutput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    complete: StrictBool
    insights: list[Annotated[MaterialInsight | CommentInsight, Field(discriminator='origin')]] = Field(max_length=MAX_OUTPUT_ITEMS)
    supports: list[EvidenceSupport] = Field(max_length=MAX_OUTPUT_ITEMS)

    @model_validator(mode='after')
    def within_resource_budget(self):
        if len(json.dumps(self.model_dump(), ensure_ascii=False)) > MAX_OUTPUT_CHARS:
            raise ValueError('insight_resource_limit')
        return self


def resolve_material_comment_candidate_inputs(reader, scope, candidate, *, turn_id, request,
        generated, comment_inputs, comparative_inputs, manifest):
    """复用实际生成与来源冻结，为@6评论消费者提供同一解码合同。

    调用方从原内核只读authority传入request、output、manifest，并先完成评论来源资格校验。
    本函数复验材料分析owner和manifest候选身份，不创建Turn或写入任何记录。
    """
    from core.document_engine import SQLiteDocumentRepository
    from backend.recognition import normalize_conditions
    from .insight_generation import _checked_material_analysis
    from .policies import get
    from .policies.extract_material import MaterialExtractDecodeInput
    from .source_sections import _exact

    if (request.get('turn_id') != turn_id or request.get('desired_outcome') != 'memory.propose_insights'
            or request.get('scope', {}).get('project_id') != scope.project_id
            or request.get('policy_versions', {}).get('extract') != '@6'
            or set(comparative_inputs) != {'neighbors', 'projects', 'material_analysis'}
            or not isinstance(manifest, dict) or set(manifest) != {
                'candidate_ids', 'proposal_keys', 'support_keys', 'generation_id'}
            or not _exact(candidate.payload.get('scope'), {'user_id': scope.user_id, 'project_id': scope.project_id})
            or candidate.payload.get('generation', {}).get('id') != generated['metadata']['generation_id']
            or manifest['generation_id'] != generated['metadata']['generation_id']):
        raise ValueError('insight_material_candidate_changed')
    document = comment_inputs['document']
    # SQL reader和事务reader共用同一正式Document读取实现；仅规范化路径供来源owner读原内核。
    path = getattr(reader, 'database_path', None)
    if path is None:
        path = next(row[2] for row in reader.connection.execute('PRAGMA database_list') if row[1] == 'main')
    records = SimpleNamespace(database_path=Path(path), read=reader.read, list=reader.list)
    analysis = _checked_material_analysis(records, SQLiteDocumentRepository(reader), scope.project_id,
        document['id'], revision=document['revision'], reader=reader)
    if not _exact(analysis, comparative_inputs['material_analysis']):
        raise ValueError('insight_material_analysis_changed')
    refs = request['privacy']['material_refs']
    if analysis is not None and any(not any(_exact(reference, saved) for saved in refs)
            for reference in analysis['original_material_refs']):
        raise ValueError('insight_material_candidate_changed')
    experiences = [reference['id'] for reference in refs if reference['type'] == 'experience']
    if len(experiences) != 1 or not _exact(candidate.payload.get('source_experience_ids'), experiences):
        raise ValueError('insight_material_candidate_changed')
    inputs = MaterialExtractDecodeInput(generated['output'], normalize_conditions,
        comparative_inputs['neighbors'], comparative_inputs['projects'], scope.project_id,
        comment_inputs['sources'], analysis)
    decoded = get('extract', version='@6').decide(inputs)
    prefix = f"candidate-v2-{document['id']}-r{document['revision']}-"
    expected_ids = [prefix + str(index) for index in range(1, len(decoded.rows) + 1)]
    if (not decoded.valid or not _exact(manifest['candidate_ids'], expected_ids)
            or candidate.object_id not in expected_ids or not isinstance(manifest['proposal_keys'], list)
            or len(manifest['proposal_keys']) != len(expected_ids)
            or any(not isinstance(key, str) or not (key == 'candidate-' + str(index)
                or key.startswith('candidate-' + str(index) + ':turn:'))
                for index, key in enumerate(manifest['proposal_keys'], 1))
            or not _exact(manifest['support_keys'], ['support-' + str(index)
                for index in range(1, len(decoded.supports) + 1)])):
        raise ValueError('insight_material_candidate_changed')
    return inputs
