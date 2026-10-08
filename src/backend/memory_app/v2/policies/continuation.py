"""成果续写的纯选择规则；评分由原样例能力注入。"""
from math import isfinite
from dataclasses import dataclass
import json
import re


# 初始八组校准的原评分：自动续写最低 .136364，不相关任务最高 0。
# 保留两者中点阈值；形式词规范化不使用诊断集重新拟合阈值。
SCORE_THRESHOLD = .068182
SCENE_BONUS = .05
MARGIN_THRESHOLD = .025
_COMMAND = re.compile(r'^(?:编写|撰写|制定|准备|补充|增加|新增|给)')
_OUTPUT_FORM = re.compile(r'手册|方案|报告|总结|复盘|清单|指南|记录')
_ADDITION = re.compile(r'补充|增加|新增|补上')


def _topic(text):
    # 命令与成果形式不是主题证据，评分仍由原 similarity 完成。
    return _OUTPUT_FORM.sub('', _ADDITION.sub('', _COMMAND.sub('', text.strip()))).strip()


def _choose(task_text, outcomes, *, project_id, score_text, scene=None,
       continue_from=None, force_new=False, scene_in_threshold=False):
    """返回脱离仓储的选择；调用方继续负责版本、来源与私密资格。"""
    if force_new and continue_from is not None:
        raise ValueError('conflicting_continuation_choice')
    if not callable(score_text):
        raise ValueError('invalid_continuation_score')
    candidates = [row for row in outcomes if row['project_id'] == project_id]
    identities = [row['document_id'] for row in candidates]
    if len(set(identities)) != len(identities):
        raise ValueError('duplicate_outcome_identity')

    def result(identity, reason, score=0.0, margin=0.0):
        return {'document_id': identity, 'reason': reason,
                'score': round(score, 6), 'margin': round(margin, 6)}

    if force_new:
        return result(None, 'forced_new')
    if continue_from is not None:
        if continue_from not in identities:
            raise ValueError('outcome_unavailable')
        return result(continue_from, 'explicit')
    topic = _topic(task_text)
    if not topic:
        return result(None, 'no_match')
    ranked = []
    for row in candidates:
        scores = [score_text(topic, _topic(row[key])) for key in ('title', 'task_text')]
        if any(type(score) not in (int, float) or not isfinite(score) or not 0 <= score <= 1 for score in scores):
            raise ValueError('invalid_continuation_score')
        score = max(scores)
        rank = score + (SCENE_BONUS if scene is not None and row['scene'] == scene else 0)
        # 新版本让同场景的主题证据参与资格判断；零主题仍低于原阈值。
        if (rank if scene_in_threshold else score) <= SCORE_THRESHOLD:
            continue
        ranked.append((rank, row['document_id'], score))
    if not ranked:
        return result(None, 'no_match')
    ranked.sort(key=lambda row: (-row[0], row[1]))
    best = ranked[0]
    margin = best[0] - ranked[1][0] if len(ranked) > 1 else best[0]
    if margin <= MARGIN_THRESHOLD:
        return result(None, 'ambiguous', best[2], margin)
    return result(best[1], 'automatic', best[2], margin)


_PATCH_INSTRUCTION = (
    '依据当前任务和新材料，只返回 complete.patches，不返回完整稿件。'
    '优先修改最小小节，path 和 after_path 是从顶层开始的完整标题路径。'
    'update.body 仅为指定标题下的正文，不包含该标题；'
    'add 在 after_path 指定小节之后加入 level 层级的新标题和正文。'
    '保留全部旧标题，不删除、不改名；保留用户修改的段落原文，只允许追加内容。'
    'protected_paragraphs 是必须保留的用户段落，不能覆盖或删减。'
    '输出示例仅说明字段结构，不能把示例文字写入稿件。'
)


def _patch_output():
    return {'type': 'complete', 'patches': [
        {'kind': 'update', 'path': ['标题', '子标题'], 'body': '该标题下的新正文'},
        {'kind': 'add', 'after_path': ['标题', '子标题'], 'level': 3,
         'title': '新标题', 'body': '新标题下的正文'},
    ]}


def _instruction(payload):
    # 稳定序列化，并将原稿和校验反馈作为数据保留。
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def patch_instruction(previous_markdown, *, protected_paragraphs=()):
    """生成包含原稿和用户段落保护的补丁请求。"""
    return _instruction({'instruction': _PATCH_INSTRUCTION,
                         'previous_markdown': previous_markdown,
                         'protected_paragraphs': list(protected_paragraphs),
                         'output': _patch_output()})


def retry_instruction(error):
    """保留全部补丁约束，校验错误仅作为重试反馈。"""
    return _instruction({'instruction': _PATCH_INSTRUCTION,
                         'feedback_role': '仅依据校验反馈修正上一补丁，不改变原稿保护规则。',
                         'validation_error': error, 'output': _patch_output()})


def fallback_instruction():
    """补丁失败后使用原 complete.summary 格式另写草稿。"""
    return _instruction({'instruction': '依据当前任务和新材料独立生成新草稿，不修改旧稿。'
                         '只返回 complete.summary，内容为新草稿的完整 Markdown 正文。',
                         'output': {'type': 'complete', 'summary': '新草稿的完整 Markdown 正文'}})


@dataclass(frozen=True)
class Policy:
    """选择和提示词由同一注册版本提供。"""

    scene_in_threshold: bool = False

    def __call__(self, task_text, outcomes, *, project_id, score_text, scene=None,
                 continue_from=None, force_new=False):
        return _choose(task_text, outcomes, project_id=project_id, score_text=score_text,
                       scene=scene, continue_from=continue_from, force_new=force_new,
                       scene_in_threshold=self.scene_in_threshold)

    patch_instruction = staticmethod(patch_instruction)
    retry_instruction = staticmethod(retry_instruction)
    fallback_instruction = staticmethod(fallback_instruction)


v1 = Policy()
v2 = Policy(scene_in_threshold=True)
