"""Versioned decision registry with evaluation-local overrides."""
from contextlib import contextmanager
from contextvars import ContextVar
import re
from threading import RLock

ACTIVE = {
    'steer': '@2',
    'watch': '@1',
    'rerank': '@2',
    'vector': '@1',
    'ladder': '@15',
    'retry': '@2',
    'image_read': '@1',
    'organize': '@1',
    'material_analysis': '@1',
    'context_preparation': '@3',
    'place': '@2',
    'extract': '@5',
    'route': '@5',
    'agent_data': '@4',
    'scope': '@2',
    'retrieve': '@9',
    'rank': '@4',
    'angle_route': '@1',
    'project_angle_learning': '@1',
    'strength': '@1',
    'forget': '@1',
    'enough': '@1',
    'compose': '@37',
    'situation_pack': '@1',
    'trigger': '@2',
    'consolidate': '@3',
    'handoff': '@1',
    'reask': '@1',
    'outcome_correction': '@1',
    'review': '@1',
    'elsewhere': '@1',
    'gap': '@1',
    'search': '@1',
    'continuation': '@2',
    'style': '@1',
    'evidence_pick': '@1',
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
from . import extract_material
from . import extract_inspiration
from . import material_analysis
from . import material_analysis_fallback, context_preparation
from . import material_analysis_nullable
from . import material_analysis_dialogue, material_angles, material_topics
from . import situation_pack
from . import outcome_correction
from . import agent_data
from . import transcript_correction
from . import angle_route
from . import project_angle_learning
register('transcript_correction', '@1')(transcript_correction.v1)
register('angle_route', '@1')(angle_route.v1)
register('project_angle_learning', '@1')(project_angle_learning.v1)

for _module in (organize, place, extract, route, scope, retrieve, rank, strength,
                forget, enough, compose, trigger, consolidate, image_read, handoff):
    register(_module.__name__.rsplit('.', 1)[-1], '@1')(_module.v1)

register('scope', '@2')(scope.v2)
register('route', '@2')(route.v2)
register('route', '@3')(route.v3)
register('route', '@4')(route.v4)
register('route', '@5')(route.v5)
from . import route_correction
register('route_correction', '@1')(route_correction.v1)
register('route_correction', '@2')(route_correction.v2)
register('organize', '@2')(organize.v1)
register('material_analysis', '@1')(material_analysis.v1)
register('material_analysis', '@2')(material_analysis_fallback.v2)
register('material_analysis', '@3')(material_analysis_nullable.v3)
register('material_analysis', '@4')(material_analysis_dialogue.v4)
register('material_analysis', '@5')(material_analysis_dialogue.v5)
register('material_analysis', '@6')(material_angles.v6)
register('material_analysis', '@7')(material_topics.v7)
register('situation_pack', '@1')(situation_pack.v1)
register('context_preparation', '@1')(context_preparation.v1)
register('context_preparation', '@2')(context_preparation.v2)
register('context_preparation', '@3')(context_preparation.v3)
register('rank', '@2')(rank.v2)
register('rank', '@3')(rank.v3)
register('handoff', '@2')(handoff.v2)
register('handoff', '@3')(handoff.v3)
register('handoff', '@4')(handoff.v4)
register('extract', '@2')(extract.v2)
register('extract', '@3')(extract_comments.v3)
register('extract', '@4')(extract_comments.v4)
register('extract', '@5')(extract_comments.v5)
register('extract', '@6')(extract_material.v6)
register('extract', '@7')(extract_inspiration.v7)
register('extract', '@8')(extract_material.v8)
register('trigger', '@2')(trigger.v2)
register('consolidate', '@2')(consolidate.v2)
from . import consolidate_steer
register('consolidate', '@3')(consolidate_steer.v3)
register('retry', '@1')(retry.decide)
register('retry', '@2')(retry.v2)
register('retry', '@3')(retry.v3)

from . import inspiration_schedule
register('inspiration_schedule', '@1')(inspiration_schedule.v1)
from . import shared_consolidation
register('shared_consolidation', '@1')(shared_consolidation.v1)

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

from . import external_task_output
register('external_task_output', '@1')(external_task_output.v1)

from . import attachment_context
register('attachment_context', '@1')(attachment_context.v1)

from . import proxy_context
register('proxy_context', '@1')(proxy_context.v1)

from . import proxy_record
register('proxy_record', '@1')(proxy_record.v1)

from . import skill_export
from . import skill_author

from . import vector
register('vector', '@1')(vector.v1)

from . import ladder
register('ladder', '@1')(ladder.v1)
register('ladder', '@2')(ladder.v2)
register('ladder', '@3')(ladder.v3)
register('ladder', '@4')(ladder.v4)

from . import rerank
register('rerank', '@1')(rerank.v1)

from . import evidence_pick
register('evidence_pick', '@1')(evidence_pick.v1)

from . import steer, stale, outcome_update
register('steer', '@1')(steer.v1)
register('steer', '@2')(steer.v2)
register('stale', '@1')(stale.v1)
register('outcome_update', '@1')(outcome_update.v1)

from . import watch
register('watch', '@1')(watch.v1)

from . import constraint_scope
register('constraint_scope', '@1')(constraint_scope.v1)

from . import backup_schedule, backup_archive
register('backup_schedule', '@1')(backup_schedule.v1)
register('backup_archive', '@1')(backup_archive.v1)

from . import native_mcp_auth_v1
register('native_mcp_auth', '@1')(native_mcp_auth_v1.native_mcp_auth)

from . import restore_preview
register('restore_preview', '@1')(restore_preview.v1)

from . import archive_copy
register('archive_copy', '@1')(archive_copy.v1)
