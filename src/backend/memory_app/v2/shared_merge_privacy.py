from backend.recognition import RecognitionConflict


OPERATIONS = 'v2_shared_insight_merge_operations'
PRIVACY = 'v2_shared_insight_merge_privacy'


def graph_private(authority, scope, refs):
    unique = {(ref['type'], ref['id'], ref['revision']): ref for ref in refs}
    return any(not node['effective_purposes'] for node in authority.snapshot(scope, list(unique.values()))['nodes'])


def pending_merge_private(reader, authority, scope, candidate):
    marker = reader.read(PRIVACY, candidate.object_id)
    visited = set()
    while marker is not None:
        if marker.object_id in visited or len(visited) >= 32:
            raise RecognitionConflict('shared_merge_privacy_changed')
        visited.add(marker.object_id)
        value = marker.payload
        operation = reader.read(OPERATIONS, value['operation_id'])
        proposal = reader.read('recognition_restructure_proposals', value['proposal_id'])
        if (marker.revision != 1 or value['project_id'] != scope.project_id or type(value['private']) is not bool
                or type(value['parent_merge']) is not bool
                or operation is None or operation.revision != 1 or operation.payload.get('state') != 'prepared'
                or operation.payload.get('proposal_id') != value['proposal_id']
                or operation.payload.get('project_id') != scope.project_id
                or proposal is None or proposal.revision != 2 or proposal.payload.get('state') != 'approved'
                or proposal.payload.get('operation') != 'merge' or proposal.payload.get('origin_task_id') is not None
                or proposal.payload.get('result_candidate_ids') != [candidate.object_id]
                or proposal.payload.get('scope') != candidate.payload.get('scope')
                or operation.payload['inputs']['target_id'] != value['target_id']
                or operation.payload['inputs']['target_revision'] != value['target_revision']
                or operation.payload['inputs']['candidate_id'] != value['parent_candidate_id']
                or not any(row['id'] == value['parent_candidate_id'] for row in proposal.payload['snapshot']['candidates'])):
            raise RecognitionConflict('shared_merge_privacy_changed')
        if value['private']:
            return True
        try:
            target = {'type': 'recognition', 'id': value['target_id'], 'revision': value['target_revision']}
            if graph_private(authority, scope, [target]):
                return True
        except RecognitionConflict:
            # 先前认识改动后缺少外发证明，保留合并结果的本机用途。
            return True
        if not value['parent_merge']:
            return False
        candidate = reader.read('recognition_candidates', value['parent_candidate_id'])
        marker = reader.read(PRIVACY, value['parent_candidate_id'])
        if candidate is None or marker is None:
            return True
    return False
