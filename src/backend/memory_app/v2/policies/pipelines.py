"""Existing pipeline recipes and deterministic Turn policy snapshots."""
from . import version

REMEMBER = ('organize', 'place', 'extract')
ASK_DO = ('route', 'scope', 'retrieve', 'rank', 'enough', 'compose')
LEARN = ('strength', 'forget', 'trigger', 'consolidate')
PIPELINES = {'remember': REMEMBER, 'ask_do': ASK_DO, 'learn': LEARN, 'search': ('search',)}
# These are real helpers and front-door decisions around the main recipe.
DEPENDENCIES = {'remember': ('route', 'scope', 'retry'), 'ask_do': ('strength', 'place', 'retry'), 'learn': ('retry',), 'search': ()}
TURN_PIPELINES = {
    'media.image_read': 'remember',
    'memory.organize': 'remember',
    'memory.propose_insights': 'remember',
    'memory.place': 'remember',
    'project.answer': 'ask_do',
    'project.task': 'ask_do',
    'workbench.route': 'ask_do',
    'memory.consolidate': 'learn',
    'memory.link_suggest': 'learn',
    'memory.overview': 'learn',
    'external.context': 'ask_do',
    'web.search': 'search',
}


def interfaces_for_turn(kind):
    if kind == 'memory.skill_export':
        return ('scope', 'skill_export', 'skill_author')
    if kind == 'external.context':
        return tuple(sorted(set((*ASK_DO[:-1], 'handoff', *DEPENDENCIES['ask_do']))))
    if kind == 'media.image_read':
        return ('image_read', 'retry')
    try:
        pipeline = TURN_PIPELINES[kind]
        recipe = (*PIPELINES[pipeline], *DEPENDENCIES[pipeline])
    except KeyError as error:
        raise ValueError('unknown_policy_pipeline') from error
    return tuple(sorted(set(recipe)))


def versions_for_turn(kind):
    if kind == 'memory.skill_export':
        return {'scope': version('scope'), 'skill_export': '@1', 'skill_author': '@1'}
    selections = {name: version(name) for name in interfaces_for_turn(kind)}
    if kind in {'project.answer', 'workbench.route'}:
        selections['search'] = version('search')
    return selections
