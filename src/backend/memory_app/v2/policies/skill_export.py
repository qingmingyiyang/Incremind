"""只从同一范围的生效方法事实构造静态导出来源。"""
from .insight_time import applies
from .types import ScopeInput
from . import get, register


def v1(view, *, scene, validity, reference, scope_version):
    return (view['kind'] == 'recognition' and view['state'] == 'active'
        and bool(view['conditions'])
        and (scene is not None or view['scene'] is None)
        and get('scope', version=scope_version)(ScopeInput(scene, view['scene']))
        and validity is not None and applies(validity,
            {'mode': 'current', 'start': reference, 'end': reference}))


register('skill_export', '@1')(v1)
