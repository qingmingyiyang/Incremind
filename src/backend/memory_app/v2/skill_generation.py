"""沿原辅助 Turn 生成草稿；领域 owner 负责用户保存和审阅。"""
from copy import deepcopy
import json

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr

from backend.recognition import RecognitionConflict
from backend.shared.secret_detection import contains_secret
from ..source_egress import SourceEgressService
from .memory_turn import MemoryTurn
from .policies import get, override
from .policies.pipelines import versions_for_turn
from .privacy import egress_allowed
from .skill_package import validate_document
from .turn_requests import freeze_product_turn


class SkillStep(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: StrictStr
    sources: list[StrictInt]


class SkillDocument(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: StrictStr
    description: StrictStr
    trigger: StrictStr
    steps: list[SkillStep]
    validation: list[StrictStr]


def generate_skill(models, exports, project, *, sources, scene=None, retry_token=None):
    configured = deepcopy(models.public()['generation'])
    if not egress_allowed(exports.records, models, project, 'generation'):
        raise ValueError('skill_generation_disabled')
    if retry_token is not None and (not isinstance(retry_token, str)
            or not 1 <= len(retry_token) <= 128 or not retry_token.isascii()
            or not retry_token.isprintable() or any(char.isspace() for char in retry_token)
            or contains_secret(retry_token)):
        raise ValueError('invalid_skill_generation_key')
    versions = versions_for_turn('memory.skill_export')
    with override(**versions):
        policy = get('skill_author')
        frozen = exports.sources(project, sources, scene)
        authority = SourceEgressService(exports.records)
        for source in frozen:
            authority.require(source['source_snapshot'], 'generation')
        materials = [{'type': 'recognition', 'id': source['id'],
            'revision': source['revision'], 'project_id': project} for source in frozen]

        def validate(reader=None):
            if models.public()['generation'] != configured:
                raise RecognitionConflict('skill_generation_configuration_changed')
            current = exports.sources(project, sources, scene, reader=reader)
            for source in current:
                authority.require(source['source_snapshot'], 'generation')
            if current != frozen:
                raise RecognitionConflict('skill_generation_sources_changed')

        def freeze(kind, **kwargs):
            # 输入正文仍由原材料冻结器筛选，不把来源图等旁路字段交给模型。
            by_id = {source['id']: source for source in frozen}
            kwargs['load_text'] = lambda material: policy.render_source(by_id[material['id']])
            return freeze_product_turn(kind, **kwargs)

        key = json.dumps({'sources': sources, 'scene': scene,
            'versions': versions, 'retry_token': retry_token}, sort_keys=True, ensure_ascii=False)
        turn = MemoryTurn(exports.records, models, kind='memory.skill_export', project=project,
            key=key, materials=materials, validate=validate, freeze_request=freeze)

        def validate_basis():
            # 同一个 key 只能重放原素材；场景等旁路变化不能借旧输出重新绑定来源。
            identities = {name: turn.request[name] for name in
                ('turn_id', 'session_id', 'operation_id', 'idempotency_key', 'created_at')}
            current = freeze('memory.skill_export', records=exports.records, models=models,
                project_id=project, materials=materials, local_only=turn.local,
                model_purpose='generation', capabilities=[], **identities)
            if any(current[name] != turn.request[name] for name in
                    ('scope', 'input', 'privacy', 'policy_versions', 'desired_outcome')):
                raise RecognitionConflict('skill_generation_basis_changed')

        validate_basis()
        output, _ = turn.generate(policy.prepare(frozen), response_model=SkillDocument,
            max_tokens=policy.max_tokens)
        validate_basis()
        document = validate_document(output.model_dump(), source_count=len(frozen))
        # 返回前在同一领域事务内终检；保存者再以 frozen_sources 核验同一来源。
        with exports.records.begin() as tx:
            validate(tx)
        return {'document': document, 'turn_id': turn.turn_id, 'sources': deepcopy(frozen)}
