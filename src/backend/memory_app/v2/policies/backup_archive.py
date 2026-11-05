def v1(request):
    if request['operation'] == 'limits':
        return {'archive_bytes': 1024 ** 3, 'unpacked_bytes': 4 * 1024 ** 3,
                'entry_bytes': 2 * 1024 ** 3, 'entries': 50000, 'depth': 32,
                'path_bytes': 1024, 'component_units': 255, 'envelope_bytes': 65536,
                'manifest_bytes': 64 * 1024 ** 2, 'compression_ratio': 1000,
                'central_bytes': 16 * 1024 ** 2, 'chunk_bytes': 1024 ** 2, 'compression_level': 1}
    if request['operation'] == 'scope' and request.get('scope') in {'personal-runtime', 'shared-project'}:
        return request['scope']
    raise ValueError('backup_archive_policy_invalid')
