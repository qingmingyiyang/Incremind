"""Versioned decision registry with evaluation-local overrides."""
from contextlib import contextmanager
from contextvars import ContextVar
import re
from threading import RLock

ACTIVE = {
    'vector': '@1',
    'retry': '@1',
    'image_read': '@1',
    'organize': '@1',
    'place': '@2',
    'extract': '@5',
    'route': '@1',
    'scope': '@2',
    'retrieve': '@3',
    'rank': '@2',
    'strength': '@1',
    'forget': '@1',
    'enough': '@1',
    'compose': '@3',
    'trigger': '@2',
    'consolidate': '@2',
    'handoff': '@1',
    'reask': '@1',
    'outcome_correction': '@1',
    'review': '@1',
    'elsewhere': '@1',
    'gap': '@1',
    'search': '@1',
    'continuation': '@2',
    'style': '@1',
}
_REGISTRY = {}
_LOCK = RLock()
_OVERRIDES = ContextVar('memory_policy_overrides', default={})
_NAME = re.compile(r'^[a-z][a-z0-9_]*$')
_VERSION = re.compile(r'^@[1-9][0-9]*$')


def _version(value):
    if isinstance(value, str) and value.isascii() and value.isdigit():
        value = '@' + value
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise ValueError('invalid_policy_version')
    return value


def register(interface, version):
    """Register once; replacement requires a new version, never mutation."""
    if not isinstance(interface, str) or not _NAME.fullmatch(interface):
        raise ValueError('invalid_policy_interface')
    selected = _version(version)

    def install(implementation):
        if not callable(implementation):
            raise ValueError('invalid_policy_implementation')
        with _LOCK:
            versions = _REGISTRY.setdefault(interface, {})
            if selected in versions:
                raise ValueError('policy_already_registered')
            versions[selected] = implementation
        return implementation
    return install


def _selected_version(interface):
    selected = _OVERRIDES.get().get(interface, ACTIVE.get(interface))
    if selected is None:
        raise ValueError('unknown_policy_interface')
    selected = _version(selected)
    if selected not in _REGISTRY.get(interface, {}):
        raise ValueError('unknown_policy_version')
    return selected


def version(interface):
    return _selected_version(interface)


def get(interface, *, version=None):
    selected = _version(version) if version is not None else _selected_version(interface)
    try:
        return _REGISTRY[interface][selected]
    except (KeyError, TypeError) as error:
        raise ValueError('unknown_policy_version') from error


@contextmanager
def override(**selections):
    # Resolve the whole batch before setting a context so bad input is atomic.
    selections = {name: _version(value) for name, value in selections.items()}
    for name, selected in selections.items():
        get(name, version=selected)
    token = _OVERRIDES.set({**_OVERRIDES.get(), **selections})
    try:
        yield
    finally:
        _OVERRIDES.reset(token)


def parse_overrides(options):
    """Parse repeated CLI selections; the last selection of an interface wins."""
    selections = {}
    for option in options or ():
        name, separator, selected = option.partition('=')
        if not separator or not name or not selected:
            raise ValueError('policy must be INTERFACE=VERSION')
        get(name, version=selected)
        selections[name] = _version(selected)
    return selections


# Static imports of pure policy modules keep the complete registry available
# without importing a store, application, model gateway, or service instance.
from . import organize, place, extract, route, scope, retrieve, rank, strength, forget, enough, compose, trigger, consolidate, image_read, handoff, retry
from . import extract_comments
from . import outcome_correction

for _module in (organize, place, extract, route, scope, retrieve, rank, strength,
                forget, enough, compose, trigger, consolidate, image_read, handoff):
    register(_module.__name__.rsplit('.', 1)[-1], '@1')(_module.v1)

register('scope', '@2')(scope.v2)
register('rank', '@2')(rank.v2)
register('handoff', '@2')(handoff.v2)
register('extract', '@2')(extract.v2)
register('extract', '@3')(extract_comments.v3)
register('extract', '@4')(extract_comments.v4)
register('extract', '@5')(extract_comments.v5)
register('trigger', '@2')(trigger.v2)
register('consolidate', '@2')(consolidate.v2)
register('retry', '@1')(retry.decide)

from . import reask
register('reask', '@1')(reask.v1)
register('outcome_correction', '@1')(outcome_correction.v1)

from . import review
register('review', '@1')(review.v1)
from . import elsewhere
register('elsewhere', '@1')(elsewhere.v1)

from . import gap
register('gap', '@1')(gap.v1)

from . import search
register('search', '@1')(search.v1)
register('place', '@2')(place.v2)

from . import continuation, style
register('continuation', '@1')(continuation.v1)
register('continuation', '@2')(continuation.v2)
register('style', '@1')(style.v1)

from . import nudge
register('remind', '@1')(nudge.remind_v1)
register('nudge', '@1')(nudge.v1)
register('nudge', '@2')(nudge.v2)

from . import external_task_input
register('external_task_input', '@1')(external_task_input.v1)

from . import proxy_context
register('proxy_context', '@1')(proxy_context.v1)

from . import proxy_record
register('proxy_record', '@1')(proxy_record.v1)

from . import skill_export
from . import skill_author

from . import vector
register('vector', '@1')(vector.v1)
