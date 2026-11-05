from copy import deepcopy

from backend.recognition import RecognitionConflict, WorkScope
from ..kernel.route_source_snapshot import native_source_snapshot
from ..kernel.receipt_projection import frozen_public_turn_requests
from ..source_egress import SourceEgressService
from .material_origins import ORIGIN_KIND, SharedMaterialOwner
from .turn_requests import validate_frozen_inputs
from .route_source_statements import statement_source


class RouteExampleOrigins:
    def __init__(self, application, *, native_reader=frozen_public_turn_requests, shared_origins_for=None, current_source=None):
        self.application = application
        state = application.state
        self.records, self.documents = state.recognition_records, state.recognition_documents
        self.models = state.recognition_models
        self.shared_origins_for, self.current_source = shared_origins_for, current_source
        self.native_reader = native_reader

    def _personal(self, project_id, *, reader=None):
        state = self.application.state
        source = reader if reader is not None else self.records
        if (state.recognition_records is not self.records or state.recognition_documents is not self.documents
                or self.models.records is not self.records or self.documents.records is not self.records
                or project_id not in {'default', 'inbox', 'me'} and source.read('v2_projects', project_id) is None):
            raise RecognitionConflict('route_example_personal_owner_changed')
        access = self.models.server_model_access
        user_id = 'local-user' if access is None else access.user_id
        if access is not None:
            # 原选择入口每次重核当前账号与设备资格，不按目录猜用户身份。
            access.selection('generation')
        return {'kind': 'personal', 'namespace': '/'.join(('personal', user_id, self.documents.namespace_id)),
            'project_id': project_id}

    def current(self, project_id, *, reader=None):
        if self.current_source is not None:
            source = self.current_source(project_id)
            if source is not None:
                if not isinstance(source, SharedMaterialOwner) or source.project_id != project_id:
                    raise RecognitionConflict('route_example_current_source_invalid')
                source.validate_current()
                return {'kind': 'shared', 'namespace': source.namespace, 'project_id': source.project_id,
                    'lease': source.lease.payload()}
        return self._personal(project_id, reader=reader)

    def _request(self, row, *, reader=None):
        if row.payload['intent'] not in {'ask', 'do'}:
            return None
        if self.native_reader is None:
            raise RecognitionConflict('route_example_native_reader_unavailable')
        entries = self.native_reader(self.records, self.application.state.recognition_runtime_root,
            row.object_id, reader=reader)
        if (len(entries) != 1 or entries[0]['public_id'] != row.object_id
                or entries[0]['public_revision'] != row.revision or entries[0]['native_request'] is None):
            raise RecognitionConflict('route_example_native_source_changed')
        return entries[0]

    def _child_origin(self, child, *, egress, reader=None):
        project = child.payload['project_id']
        source = reader if reader is not None else self.records
        native = self._request(child, reader=reader)
        if native is not None:
            request = native['native_request']
            snapshot = native_source_snapshot(self.records, request['turn_id'], reader=reader)
            if snapshot['request'] != request:
                raise RecognitionConflict('route_example_native_source_changed')
            bundle = snapshot['admission']
            has_shared = bundle is not None and any(item.kind == ORIGIN_KIND for item in bundle.items)
            if has_shared:
                if not native['qualified'] or self.shared_origins_for is None:
                    raise RecognitionConflict('route_example_shared_proof_missing')
                bound = self.shared_origins_for(request)
                from .material_origin_admission import BoundMaterialOrigins
                if not isinstance(bound, BoundMaterialOrigins):
                    raise RecognitionConflict('route_example_shared_proof_invalid')
                bound.validate_current()
                origin = {'kind': 'shared', 'namespace': bound.shared.namespace,
                    'project_id': bound.shared.project_id, 'proof': deepcopy(bound.reference)}
            else:
                origin = self._personal(project, reader=reader)
            if egress:
                if has_shared:
                    query = self.application.state.workspace_domains.query._query_for_native_request(request)
                    query.validate_answer_request(self.models, request)
                    if child.payload['intent'] != 'ask':
                        raise RecognitionConflict('route_example_shared_material_proof_unavailable')
                    from .shared_route import validate_route_example_egress
                    validate_route_example_egress(query, child)
                else:
                    validate_frozen_inputs(source, self.models, request,
                        query=self.application.state.workspace_domains.query, source_owner=self.records)
            return origin
        origin = self._personal(project, reader=reader)
        authority, scope = SourceEgressService(self.records, reader=reader), WorkScope('local-user', project)
        if child.payload['intent'] == 'inspiration':
            statement = statement_source(source, child, source_owner=self.records)
            if egress:
                authority.require(authority.snapshot(scope, statement['refs']), 'generation')
            origin['statement'] = {**statement['birth'], 'sources': statement['refs']}
        if child.payload['intent'] == 'remember':
            identity = child.payload['receipt']['remember']['item_id']
            item = source.read('workspace_items', identity)
            if item is None or item.payload.get('project_id') != project:
                raise RecognitionConflict('route_example_original_source_missing')
            snapshot = authority.snapshot(scope, [{'type': 'original_item', 'id': identity, 'revision': item.revision}])
            if egress:
                authority.require(snapshot, 'generation')
        return origin

    def __call__(self, turn_id, *, egress=False, reader=None):
        from .route_corrections import source_parts
        source = reader if reader is not None else self.records
        row = source.read('v2_turns', turn_id)
        if row is None:
            raise RecognitionConflict('route_example_source_missing')
        project = row.payload['project_id']
        origins = []
        for part in source_parts(self.records, row, reader=reader):
            identities = list(dict.fromkeys(([part['turn_id']] if part['turn_id'] is not None else [])
                + [member['turn_id'] for member in part['source_members']]))
            qualified = []
            for identity in identities:
                origin = self._child_origin(source.read('v2_turns', identity), egress=egress, reader=reader)
                if origin not in qualified:
                    qualified.append(origin)
            origins.extend(qualified)
        if not origins:
            # 删除全部部分只保存删除事实，仍须真实个人或当前共享空间资格。
            correction = source.read('v2_route_correction_reuses', turn_id)
            if correction is not None:
                return self(correction.payload['source_turn_id'], egress=egress, reader=reader)
            return self.current(project, reader=reader)
        namespaces = {(value['kind'], value['namespace']) for value in origins}
        if len(namespaces) != 1:
            raise RecognitionConflict('route_example_mixed_origin')
        return {'kind': origins[0]['kind'], 'namespace': origins[0]['namespace'], 'project_id': row.payload['project_id'],
            'sources': origins}

    def validate_egress(self, turn_id):
        return self(turn_id, egress=True)
