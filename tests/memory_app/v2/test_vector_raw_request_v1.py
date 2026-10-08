"""原始纯策略没有派生别名时，真实请求校验仅接受模型名。"""
import pytest

from backend.memory_app.local_vectors import LocalVectorError, validate_request
from backend.memory_app.v2.policies import get


@pytest.mark.parametrize('identity', ['model', 'wrong_dimension', 'wrong_version'])
def test_raw_vector_policy_accepts_model_name_and_rejects_other_keys(identity):
    raw = get('vector', version='@1')()
    # 显式移除登记阶段的字面别名，表达独立激活后的纯策略合同。
    policy = {key: value for key, value in raw.items() if key != 'model_key'}
    assert 'model_key' not in policy
    model = {
        'model': policy['model'],
        'wrong_dimension': f"{policy['model']}:{policy['dims'] + 1}:vector@1",
        'wrong_version': f"{policy['model']}:{policy['dims']}:vector@2",
    }[identity]
    body = {'model': model, 'input': ['固定查询'], 'input_type': 'query'}
    if identity == 'model':
        assert validate_request(body, policy=policy) == (('固定查询',), 'query')
    else:
        with pytest.raises(LocalVectorError, match='^invalid_local_vector_request$'):
            validate_request(body, policy=policy)
