def v1(value):
    for name in ('project_private', 'explicit_private', 'ancestor_private'):
        if type(value.get(name)) is not bool:
            raise ValueError('archive_copy_privacy_invalid')
    authority = value.get('authority')
    if authority not in {'portable', 'nonportable_native'}:
        raise ValueError('archive_copy_authority_invalid')
    restricted = authority == 'nonportable_native'
    return {'private': restricted or any(value[name] for name in (
        'project_private', 'explicit_private', 'ancestor_private')),
        'reason': 'archive_native_authority_not_portable' if restricted else None}
