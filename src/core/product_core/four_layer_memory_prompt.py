from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


class FourLayerMemoryPromptError(ValueError):
    """Raised when a four-layer memory prompt contract cannot be built."""


FOUR_LAYER_MEMORY_PROMPT_VERSION = "four-layer-memory-candidate-v1"


@dataclass(frozen=True, slots=True)
class MemoryLayerPromptSpec:
    layer_key: str
    target_layer: str
    title: str
    product_intent: str
    evidence_rule: str
    proposed_content_rule: str
    review_prompt_rule: str


@dataclass(frozen=True, slots=True)
class FourLayerMemoryPrompt:
    prompt_version: str
    system_prompt: str
    output_contract: Mapping[str, object]
    layer_specs: tuple[MemoryLayerPromptSpec, ...]
    provider_boundary: Mapping[str, object]


FOUR_LAYER_MEMORY_SPECS: tuple[MemoryLayerPromptSpec, ...] = (
    MemoryLayerPromptSpec(
        layer_key="atom",
        target_layer="atom",
        title="Atom Memory",
        product_intent="单条稳定事实、偏好、规则、决定或行动片段。",
        evidence_rule="至少一条直接 evidence_ref，必须能逐字追溯到 source_refs。",
        proposed_content_rule="只写一条事实或规则，不合并多个主题。",
        review_prompt_rule="提醒用户确认这条事实是否值得进入长期记忆。",
    ),
    MemoryLayerPromptSpec(
        layer_key="scenario",
        target_layer="scenario",
        title="Scenario Memory",
        product_intent="一类重复任务或场景中的触发条件、判断方式、约束和推荐动作。",
        evidence_rule="至少一条 evidence_ref，且必须说明它如何支撑场景归纳。",
        proposed_content_rule="写清场景、触发条件、适用边界和处理规则，不能只是 Atom 列表。",
        review_prompt_rule="提醒用户确认该场景归纳是否成立，是否适用于后续类似任务。",
    ),
    MemoryLayerPromptSpec(
        layer_key="series",
        target_layer="series_memory",
        title="Series Memory",
        product_intent="跨会话、跨素材、跨项目的长期连续性、风格结构、主题演化和关系。",
        evidence_rule="优先使用多条或跨时间 evidence_refs；单条证据只能提出低置信候选并说明限制。",
        proposed_content_rule="写长期模式、演化方向、稳定偏好或项目关系，不把单次场景冒充长期记忆。",
        review_prompt_rule="提醒用户确认该长期模式的适用范围、影响周期和需要保留的例外。",
    ),
    MemoryLayerPromptSpec(
        layer_key="project_skill",
        target_layer="project_skill",
        title="Project/Skill Memory",
        product_intent="可复用的项目工作法、提示词、约束、技能步骤或质量标准。",
        evidence_rule="至少一条 evidence_ref，必须能证明这是可迁移的方法或项目级规则。",
        proposed_content_rule="写成可执行的技能规则，包含适用条件、步骤、禁止事项和验证方式。",
        review_prompt_rule="提醒用户确认该技能是否应进入项目级记忆，以及适用边界。",
    ),
)


def build_four_layer_memory_prompt(
    *,
    source_kind: str,
    source_summary: str,
    allowed_layers: Sequence[str] | None = None,
) -> FourLayerMemoryPrompt:
    """Build an AI-facing prompt that can only propose reviewable memory candidates."""

    clean_source_kind = _required_text(source_kind, "source_kind")
    clean_source_summary = _required_text(source_summary, "source_summary")
    specs = _select_specs(allowed_layers)
    layer_block = "\n".join(_format_layer_spec(spec) for spec in specs)
    allowed_targets = [spec.target_layer for spec in specs]
    system_prompt = f"""你是 Chriptmas OS 的记忆候选生成器。你只能根据用户已授权输入提出 Memory Candidate，不能写入、发布、撤回或修改长期 Memory。

Prompt版本：{FOUR_LAYER_MEMORY_PROMPT_VERSION}

输入来源类型：{clean_source_kind}
输入摘要：{clean_source_summary}

工作规则：
1. 只输出 JSON，不输出解释性正文。
2. 只生成待审候选，status 必须是 pending_review。
3. review.requires_user_confirmation 必须是 true。
4. review.auto_promote_allowed 必须是 false。
5. 每个候选只能对应一个 target_layer，不允许一个候选同时发布多个对象。
6. 证据不足时不要编造候选，把原因写入 insufficient_evidence。
7. 不输出 API key、Cookie、完整本地路径、账号凭据或未授权隐私内容。
8. 不调用外部工具，不下载视频，不读取文件，不运行 OCR、ASR 或其他 Provider。
9. 不写 memory_atoms、memory_scenarios、memory_series_memory、project_skills、memory_publications 或 memory_transitions。
10. 顶层 JSON 必须且只能包含 candidates、insufficient_evidence、provider_boundary 三个字段。
11. provider_boundary 必须是对象，并原样返回：{{"provider_must_not":["publish_memory","write_staging_memory","write_long_term_memory","log_or_return_secrets","fetch_unprovided_urls","read_local_files"]}}。
12. candidates 必须是数组；没有可靠候选时返回空数组。insufficient_evidence 必须是数组；没有缺口时返回空数组。
13. 每个 candidate 的 proposed_content 和 review_prompt 必须是字符串，source_refs 与 evidence_refs 必须只复制输入提供的 source_refs。
14. candidate_type 只能是 answer_fact、answer_decision、answer_action、answer_summary、document_takeaway、other 之一。

输出形状示例（字段值需依据输入生成，不要复制示例内容）：
{{
  "candidates": [
    {{
      "target_layer": "atom",
      "candidate_type": "answer_fact",
      "status": "pending_review",
      "proposed_content": "一条可由输入证据直接支持的候选内容",
      "source_refs": [],
      "evidence_refs": [],
      "review_prompt": "请确认该候选是否值得进入长期记忆。",
      "review": {{"requires_user_confirmation": true, "auto_promote_allowed": false}}
    }}
  ],
  "insufficient_evidence": [],
  "provider_boundary": {{"provider_must_not":["publish_memory","write_staging_memory","write_long_term_memory","log_or_return_secrets","fetch_unprovided_urls","read_local_files"]}}
}}

四层候选规则：
{layer_block}
"""
    return FourLayerMemoryPrompt(
        prompt_version=FOUR_LAYER_MEMORY_PROMPT_VERSION,
        system_prompt=system_prompt,
        output_contract={
            "type": "json_object",
            "required_top_level_keys": ["candidates", "insufficient_evidence", "provider_boundary"],
            "candidate_required_keys": [
                "target_layer",
                "candidate_type",
                "proposed_content",
                "source_refs",
                "evidence_refs",
                "review_prompt",
                "review",
            ],
            "allowed_target_layers": allowed_targets,
            "allowed_candidate_types": [
                "answer_fact",
                "answer_decision",
                "answer_action",
                "answer_summary",
                "document_takeaway",
                "other",
            ],
            "review_contract": {
                "status": "pending_review",
                "requires_user_confirmation": True,
                "auto_promote_allowed": False,
                "published_memory_allowed": False,
            },
            "forbidden_outputs": [
                "memory_atoms",
                "memory_scenarios",
                "memory_series_memory",
                "project_skills",
                "memory_publications",
                "memory_transitions",
                "api_keys",
                "cookies",
                "absolute_local_paths",
            ],
        },
        layer_specs=specs,
        provider_boundary={
            "provider_may": [
                "read_authorized_prompt_input",
                "propose_reviewable_memory_candidates",
                "return_insufficient_evidence_reasons",
            ],
            "provider_must_not": [
                "publish_memory",
                "write_staging_memory",
                "write_long_term_memory",
                "log_or_return_secrets",
                "fetch_unprovided_urls",
                "read_local_files",
            ],
        },
    )


def serialize_four_layer_memory_prompt(prompt: FourLayerMemoryPrompt) -> dict[str, object]:
    return {
        "prompt_version": prompt.prompt_version,
        "system_prompt": prompt.system_prompt,
        "output_contract": dict(prompt.output_contract),
        "layer_specs": [
            {
                "layer_key": spec.layer_key,
                "target_layer": spec.target_layer,
                "title": spec.title,
                "product_intent": spec.product_intent,
                "evidence_rule": spec.evidence_rule,
                "proposed_content_rule": spec.proposed_content_rule,
                "review_prompt_rule": spec.review_prompt_rule,
            }
            for spec in prompt.layer_specs
        ],
        "provider_boundary": dict(prompt.provider_boundary),
    }


def _select_specs(allowed_layers: Sequence[str] | None) -> tuple[MemoryLayerPromptSpec, ...]:
    if allowed_layers is None:
        return FOUR_LAYER_MEMORY_SPECS
    allowed = {_required_text(layer, "allowed_layer") for layer in allowed_layers}
    selected = tuple(spec for spec in FOUR_LAYER_MEMORY_SPECS if spec.layer_key in allowed or spec.target_layer in allowed)
    if not selected:
        raise FourLayerMemoryPromptError("allowed_layers did not match any memory layer")
    return selected


def _format_layer_spec(spec: MemoryLayerPromptSpec) -> str:
    return (
        f"- {spec.title} target_layer={spec.target_layer}: {spec.product_intent} "
        f"证据规则：{spec.evidence_rule} 内容规则：{spec.proposed_content_rule} "
        f"审核提示：{spec.review_prompt_rule}"
    )


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise FourLayerMemoryPromptError(f"{field_name} is required")
    clean = value.strip()
    if not clean:
        raise FourLayerMemoryPromptError(f"{field_name} is required")
    return clean
