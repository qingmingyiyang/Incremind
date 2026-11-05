import json

from .policies import get


POLICY = 'external_task_output'


def output_policy(version):
    if version is None:
        return None
    if version != '@1':
        raise ValueError('external_output_policy_invalid')
    return get(POLICY, version=version)()


def frozen_output_policy(request):
    spec = output_policy(request['policy_versions'].get(POLICY))
    if spec is not None:
        body = json.loads(request['input']['text'])
        if not isinstance(body, dict) or body.get('output') != spec:
            raise ValueError('external_output_policy_invalid')
    return spec


def delivery_policy_versions(request):
    frozen_output_policy(request)
    return {key: value for key, value in request['policy_versions'].items() if key != POLICY}
