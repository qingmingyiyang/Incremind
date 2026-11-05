from copy import deepcopy
from contextlib import nullcontext
from urllib.parse import quote

from backend.recognition import RecognitionConflict, WorkScope
from ..original_sources import resolve_turn_material
from ..source_egress import SourceEgressService
from .privacy import freeze_turn_materials, privacy_revision, egress_allowed
from .material_origin_admission import BoundMaterialOrigins
from ..privacy_policy import global_egress_allowed
from ..transaction_records import TransactionRecords


def freeze_origin_materials(origins, models, project_id, materials, *, local_only=False, purpose='generation', reader=None):
    if not isinstance(origins, BoundMaterialOrigins) or project_id != origins.lease.project_id:
        raise RecognitionConflict('material_origin_scope_changed')
    origins.validate_current()
    personal = origins.admission.destination.records
    from .material_origin_readers import qualified_actor_reader
    actor = qualified_actor_reader(origins, reader)
    privacy_reader = actor if actor is not None else personal
    actor_revision = privacy_revision(privacy_reader)
    allowed, refs, snapshots, excluded, qualified = [], [], [], [], []
    for material in materials:
        if not isinstance(material, dict) or set(material) != {'type', 'id', 'revision', 'project_id', 'namespace'}:
            raise RecognitionConflict('material_origin_scope_changed')
        owner = origins.owner(material['project_id'], material['namespace'])
        descriptor = {key: material[key] for key in ('type', 'id', 'revision', 'project_id')}
        # 搜索与视觉的外发开关由操作者核验，原来源服务仍核验资料资格。
        source_purpose = 'generation' if purpose in {'search', 'vision'} else purpose
        from .shared_auxiliary import _qualified_commit
        from .material_origin_readers import qualified_record_read
        with _qualified_commit(origins.admission) as qualification:
            context = nullcontext(actor) if actor is not None else qualified_record_read(personal, validate=qualification._validate)
            with context as transaction:
                actor_reader = TransactionRecords(transaction)
                source_reader = qualification.reader_for(origins, owner, actor_reader=actor_reader)
                authority_owner = owner if owner is origins.shared else personal
                selected, privacy = freeze_turn_materials(TransactionRecords(source_reader), models, owner.project_id,
                    [descriptor], authority=SourceEgressService(authority_owner, reader=source_reader),
                    local_only=local_only, purpose=source_purpose)
                qualification._actor_reader(actor_reader)
        allowed.extend({**item, 'namespace': owner.namespace} for item in selected)
        refs.extend(privacy['material_refs'])
        snapshots.extend(privacy['source_snapshots'])
        excluded.extend(privacy['excluded_refs'])
        if selected:
            qualified.append(deepcopy(material))
    origins.validate_current()
    if privacy_revision(privacy_reader) != actor_revision:
        raise RecognitionConflict('material_origin_privacy_changed')
    remote = (not local_only and global_egress_allowed(models, purpose)
              and egress_allowed(origins.shared.records, models, project_id, purpose))
    personal_uri = '/'.join(quote(value, safe='') for value in (
        origins.admission.destination.owner_user_id, origins.admission.destination.namespace, purpose))
    privacy = {'mode': 'remote_allowed' if remote else 'local_only', 'allow_remote': remote,
        'pii': 'possible', 'consent_refs': ['crp://personal-model-settings/' + personal_uri] if remote else [],
        'retention': 'session', 'privacy_revision': actor_revision,
        'excluded_refs': sorted(excluded, key=lambda row: (row['project_id'], row['kind'], row['object_id'])),
        'source_snapshots': snapshots, 'material_refs': refs}
    return allowed, privacy, {'origin_ref': deepcopy(origins.reference), 'origin_materials': qualified}


def validate_origin_privacy(origins, models, project_id, privacy, *, purpose='generation', selection=None, reader=None):
    if not isinstance(origins, BoundMaterialOrigins) or project_id != origins.lease.project_id:
        raise RecognitionConflict('material_origin_scope_changed')
    origins.validate_current()
    personal = origins.admission.destination.records
    from .material_origin_readers import qualified_actor_reader
    actor = qualified_actor_reader(origins, reader)
    if privacy_revision(actor if actor is not None else personal) != privacy['privacy_revision']:
        raise RecognitionConflict('material_origin_privacy_changed')
    if privacy['allow_remote'] and not (
            global_egress_allowed(models, purpose)
            and egress_allowed(origins.shared.records, models, project_id, purpose)):
        raise RecognitionConflict('material_origin_egress_revoked')
    refs, snapshots = privacy['material_refs'], privacy['source_snapshots']
    if len(refs) != len(snapshots):
        raise RecognitionConflict('material_origin_proof_changed')
    qualified = None
    if selection is not None:
        if isinstance(selection, dict) and selection.get('schema_version') == 3:
            from .shared_rendered_input import read_rendered_capsule
            auxiliary = origins.auxiliary
            if auxiliary is None or auxiliary['selection'] != selection or auxiliary['purpose'] != purpose:
                raise RecognitionConflict('material_origin_auxiliary_changed')
            read_rendered_capsule({'turn_id': origins.reference['object_id']}, auxiliary, reference=origins.reference)
            selection = {key: selection[key] for key in ('origin_ref', 'origin_materials', 'sql_identities')}
            selection['schema_version'] = 2
        fields = {'origin_ref', 'origin_materials'}
        if (not isinstance(selection, dict) or set(selection) not in (fields, fields | {'schema_version', 'sql_identities'})
                or 'schema_version' in selection and (type(selection['schema_version']) is not int or selection['schema_version'] != 2)
                or selection['origin_ref'] != origins.reference
                or not isinstance(selection['origin_materials'], list)
                or len(selection['origin_materials']) != len(refs)):
            raise RecognitionConflict('material_origin_proof_changed')
        qualified = selection['origin_materials']
        if 'schema_version' in selection or origins.auxiliary is not None:
            from .material_origin_sql import freeze_sql_identities, validate_sql_shape
            if 'schema_version' in selection:
                validate_sql_shape(selection['sql_identities'])
            # 原事务重取完整正文与 lineage 后比较全部身份，避免此前再次逐条扫描同一清单。
            actual = freeze_sql_identities(origins, qualified, actor_reader=actor)
            if ('schema_version' not in selection and actual
                    or 'schema_version' in selection and selection['sql_identities'] != actual):
                raise RecognitionConflict('material_origin_auxiliary_sql_identity_unavailable')
    for position, (descriptor, snapshot) in enumerate(zip(refs, snapshots)):
        if qualified is None:
            owners = [owner for owner in (origins.shared, origins.profile) if owner.project_id == descriptor['project_id']]
            if len(owners) != 1:
                raise RecognitionConflict('material_origin_scope_changed')
            owner = origins.owner(owners[0].project_id, owners[0].namespace)
        else:
            material = qualified[position]
            if (not isinstance(material, dict) or set(material) != {'type', 'id', 'revision', 'project_id', 'namespace'}
                    or {key: material[key] for key in descriptor} != descriptor):
                raise RecognitionConflict('material_origin_proof_changed')
            owner = origins.owner(material['project_id'], material['namespace'])
        scope = WorkScope('local-user', owner.project_id)
        from .shared_auxiliary import _qualified_commit
        from .material_origin_readers import qualified_record_read
        with _qualified_commit(origins.admission) as qualification:
            context = nullcontext(actor) if actor is not None else qualified_record_read(personal, validate=qualification._validate)
            with context as transaction:
                actor_reader = TransactionRecords(transaction)
                source_reader = qualification.reader_for(origins, owner, actor_reader=actor_reader)
                _, roots = resolve_turn_material(source_reader, scope, descriptor)
                authority = SourceEgressService(owner if owner is origins.shared else personal, reader=source_reader)
                current = authority.snapshot(scope, roots)
                if snapshot.get('roots') != current['roots']:
                    raise RecognitionConflict('material_origin_proof_changed')
                authority.validate_snapshot(scope, snapshot)
                source_purpose = 'generation' if purpose in {'search', 'vision'} else purpose
                if privacy['allow_remote']:
                    authority.require(snapshot, source_purpose)
                qualification._actor_reader(actor_reader)
    origins.validate_current()
