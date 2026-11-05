from types import MappingProxyType


MODEL_BILLING_HTTP_STATUS = MappingProxyType({
    'server_model_balance_insufficient': 402,
    'server_model_balance_account_blocked': 402,
    'server_model_balance_bound_unavailable': 409,
    'server_model_balance_price_unavailable': 409,
    'server_model_balance_model_forbidden': 403,
    'server_model_binding_changed': 409,
    'server_model_purpose_unavailable': 409,
    'server_model_profile_disabled': 409,
    'server_model_credential_unavailable': 409,
    'server_model_secret_unavailable': 409,
    'server_model_not_allowed': 403,
    'server_model_balance_job_unauthorized': 403,
    'server_model_billing_owner_unavailable': 409,
    'server_model_billing_unavailable': 409,
})


def model_billing_status(code):
    return MODEL_BILLING_HTTP_STATUS.get(code) if type(code) is str else None


def model_billing_code(error, error_type):
    # 只有调用方指定的精确异常类型和单个固定代码能够跨过错误边界。
    if type(error) is not error_type or len(error.args) != 1:
        return None
    code = error.args[0]
    return code if model_billing_status(code) is not None else None
