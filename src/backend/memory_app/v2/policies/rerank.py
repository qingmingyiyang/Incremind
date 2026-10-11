"""按层重排的输入限额与排名融合，不读取存储或调用模型。"""
import math


def v1(base_ids=None, scores=None, *, scene_priorities=None):
    if base_ids is None:
        return {'candidate_limit': 12, 'input_tokens': 6000, 'candidate_tokens': 1200}
    if set(base_ids) != set(scores) or any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            for value in scores.values()):
        raise ValueError('invalid_rerank_scores')
    if len(base_ids) < 2:
        return list(base_ids)
    # 两边先转成排名分数，避免不同模型分数的尺度改变融合权重。
    denominator = len(base_ids) - 1
    base = {identity: 1 - index / denominator for index, identity in enumerate(base_ids)}
    position = {identity: index for index, identity in enumerate(base_ids)}
    ordered = sorted(base_ids, key=lambda identity: (-scores[identity], position[identity]))
    reranked = {identity: 1 - index / denominator for index, identity in enumerate(ordered)}
    return sorted(base_ids, key=lambda identity: (
        -round(0.4 * base[identity] + 0.6 * reranked[identity], 6),
        (scene_priorities or {}).get(identity, 0), position[identity]))


def v2(base_ids=None, scores=None, *, scene_priorities=None):
    """独立重排服务容量未知时按真实成本装包，不借用生成模型窗口。"""
    if base_ids is None:
        return {'input_tokens': 6000, 'dynamic_frontier': True,
                'capacity_basis': 'unknown_rerank_service_fallback'}
    return v1(base_ids, scores, scene_priorities=scene_priorities)


def frontier_ceiling(ceiling, consumed, prepared_count):
    """先为至少两项比较保留表示空间，其余按真实剩余额度继续装入。"""
    if any(type(value) is not int or value < 0 for value in (ceiling, consumed, prepared_count)):
        raise ValueError('invalid_rerank_frontier_cost')
    return consumed + max(0, ceiling - consumed) // max(1, 2 - prepared_count)


v2.frontier_ceiling = frontier_ceiling


from . import register
register('rerank', '@2')(v2)
