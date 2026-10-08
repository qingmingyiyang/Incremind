"""Validate task metadata before publishing any durable dispatch permit."""


def validate_divisions(specs, identities):
    graph = {}
    result = {}
    for spec in specs:
        key = spec['assignment_id']
        value = spec.get('division')
        if not isinstance(value, dict) or set(value) != {'goal', 'deliverable', 'depends_on'}:
            raise ValueError('task division metadata is invalid')
        if any(not isinstance(value[name], str) or not value[name].strip()
               or len(value[name]) > 2000 for name in ('goal', 'deliverable')):
            raise ValueError('task division description is invalid')
        deps = value['depends_on']
        if (not isinstance(deps, list) or any(not isinstance(dep, str) for dep in deps)
                or len(deps) != len(set(deps)) or key in deps
                or not set(deps).issubset(identities)):
            raise ValueError('task division dependencies are invalid')
        graph[key] = set(deps)
        result[key] = {**value, 'depends_on':[identities[dep] for dep in deps]}
    completed = set()
    while len(completed) < len(graph):
        ready = {key for key, deps in graph.items() if key not in completed and deps <= completed}
        if not ready:
            raise ValueError('task division dependencies contain a cycle')
        completed.update(ready)
    return result
