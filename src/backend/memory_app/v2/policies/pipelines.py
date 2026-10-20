"""Existing pipeline recipes and deterministic Turn policy snapshots."""
from . import version

REMEMBER = ('organize', 'place', 'extract')
ASK_DO = ('route', 'scope', 'retrieve', 'rank', 'enough', 'compose')
LEARN = ('strength', 'forget', 'trigger', 'consolidate')
PIPELINES = {'remember': REMEMBER, 'ask_do': ASK_DO, 'learn': LEARN, 'search': ('search',)}
# These are real helpers and front-door decisions around the main recipe.
# Recorded with each turn so a replay or continuation uses the versions that ran.
DEPENDENCIES = {'remember': ('route', 'scope', 'retry'),
                'ask_do': ('strength', 'place', 'retry', 'ladder', 'vector', 'rerank', 'elsewhere', 'continuation', 'style'),
                'learn': ('retry', 'vector', 'rerank'), 'search': ()}
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
        # 外部 agent 上下文不调用重排，保留原有冻结策略键集合。
        return tuple(sorted(set((*ASK_DO[:-1], 'handoff',
            *(name for name in DEPENDENCIES['ask_do'] if name != 'rerank')))))
    if kind == 'media.image_read':
        return ('image_read', 'retry')
    try:
        pipeline = TURN_PIPELINES[kind]
        recipe = (*PIPELINES[pipeline], *DEPENDENCIES[pipeline])
    except KeyError as error:
        raise ValueError('unknown_policy_pipeline') from error
    if kind in {'memory.organize', 'memory.propose_insights'} and version('organize') == '@2':
        recipe = (*recipe, 'material_analysis')
        if version('material_analysis') in {'@2', '@3', '@4'}:
            recipe = (*recipe, 'context_preparation')
    if kind == 'project.task':
        recipe = (*recipe, 'context_preparation')
    return tuple(sorted(set(recipe)))


def versions_for_turn(kind):
    if kind == 'memory.skill_export':
        return {'scope': version('scope'), 'skill_export': '@1', 'skill_author': '@1'}
    selections = {name: version(name) for name in interfaces_for_turn(kind)}
    if kind in {'project.answer', 'workbench.route'}:
        selections['search'] = version('search')
    return selections
