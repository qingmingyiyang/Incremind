"""Version one retains tagged, inspiration, and current-project placement."""
from .types import PlaceHint, PlaceInput, PlaceSuggestionInput, PlaceInboxInput, PlaceGroup

_INBOX_MINIMUM = 3
_INBOX_GENERIC = {'灵感', '点子', '想买', '想去', '待购', '清单'}


def _inbox_groups(request):
    topics = {}
    for item in request.items:
        for term, weight in item.terms:
            if len(term) >= 2 and term not in _INBOX_GENERIC:
                topics.setdefault(term, {})[item.id] = weight
    candidates = [(tuple(sorted(rows)), sum(rows.values()), term) for term, rows in topics.items()
        if len(rows) >= _INBOX_MINIMUM]
    groups, used = [], set()
    for ids, score, term in sorted(candidates, key=lambda row: (-len(row[0]), -row[1], -len(row[2]), row[2])):
        available = tuple(identity for identity in ids if identity not in used)
        if len(available) < _INBOX_MINIMUM:
            continue
        groups.append(PlaceGroup(term[:40], available))
        used.update(available)
    return tuple(groups)


def v1(request: PlaceInput) -> str:
    return (request.tagged_project if request.tagged_project is not None
            else 'inbox' if request.intent == 'inspiration' else request.current_project)


def _overlap(terms, vocabulary):
    return sum(weight for term, weight in terms if term in vocabulary)


def v2(request: PlaceInput | PlaceSuggestionInput | PlaceInboxInput) -> str | PlaceHint | tuple[PlaceGroup, ...] | None:
    """Retain ingress routing, then suggest from caller-owned local vocabulary."""
    if isinstance(request, PlaceInput):
        return v1(request)
    if isinstance(request, PlaceInboxInput):
        return _inbox_groups(request)
    if not isinstance(request, PlaceSuggestionInput):
        raise TypeError('invalid_place_input')
    projects = {project.project_id: project for project in request.projects
                if project.project_id not in {'me', 'inbox'}}
    examples = []
    for example in request.examples:
        if example.current_project != request.current_project or example.project_id not in projects:
            continue
        project = projects[example.project_id]
        if example.scene is not None and example.scene not in project.scenes:
            continue
        score = _overlap(request.terms, example.terms)
        if score > 0:
            examples.append((score, example.project_id, example.scene))
    if examples:
        best = max(score for score, _, _ in examples)
        winners = {(project, scene) for score, project, scene in examples if score == best}
        if len(winners) == 1:
            project, scene = next(iter(winners))
            return PlaceHint(project, scene, best)
    scores = {identity: _overlap(request.terms, project.vocabulary)
              for identity, project in projects.items()}
    best = max(scores.values(), default=0)
    winners = [identity for identity, score in scores.items() if score == best and score > 0]
    if len(winners) != 1:
        return None
    identity = winners[0]
    scenes = {name: _overlap(request.terms, vocabulary)
              for name, vocabulary in projects[identity].scenes.items()}
    scene_best = max(scenes.values(), default=0)
    scene_winners = [name for name, score in scenes.items() if score == scene_best and score > 0]
    return PlaceHint(identity, scene_winners[0] if len(scene_winners) == 1 else None, best)
