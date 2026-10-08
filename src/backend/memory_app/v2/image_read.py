"""Bind uploaded image identities; load bytes only after current authority checks."""
import json
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path

from backend.recognition import RecognitionConflict, WorkScope
from backend.shared.llm.image_input import image_content
from ..kernel.turn_requests import freeze_turn_request
from ..source_egress import SourceEgressService
from ..transaction_records import TransactionRecords
from ..workspace_audio import _audio_original_identity
from .privacy import egress_allowed, privacy_revision, resolve_turn_material

_BINDINGS = 'v2_image_read_bindings'


def group_entries(records, item):
    """Read the sole upload owner's ordered attachment names and paths."""
    group = records.read('v2_image_groups', item['id'])
    if group is None:
        return [{'path': item['original_path'], 'name': item.get('original_name') or item.get('title') or Path(item['original_path']).name}]
    payload = group.payload
    entries = payload.get('images')
    if (payload.get('project_id') != item['project_id'] or payload.get('item_id') != item['id']
            or not isinstance(entries, list) or not entries
            or any(not isinstance(entry, Mapping) or set(entry) != {'path', 'name'}
                or not all(isinstance(entry[key], str) and entry[key] for key in ('path', 'name'))
                or Path(entry['name']).name != entry['name'] for entry in entries)
            or entries[0]['path'] != item['original_path']):
        raise RecognitionConflict('image_material_binding_invalid')
    return entries


def uploaded_images(records, runtime_root, project_id, materials):
    """The owning item's upload and group sidecar are the only image authority."""
    images = []
    root = (Path(runtime_root) / 'workspace').resolve(strict=True)
    for material in materials:
        if material['type'] != 'original_item' or material['project_id'] != project_id:
            raise RecognitionConflict('image_material_scope_conflicted')
        resolved, _ = resolve_turn_material(records, WorkScope('local-user', project_id), material)
        item = resolved['payload']
        if item.get('input_kind') != 'image':
            raise RecognitionConflict('image_material_binding_invalid')
        for ordinal, entry in enumerate(group_entries(records, item), 1):
            try:
                path = Path(entry['path']).resolve(strict=True)
            except (OSError, ValueError):
                raise RecognitionConflict('image_original_changed') from None
            if not path.is_relative_to(root) or not path.is_file():
                raise RecognitionConflict('image_original_changed')
            images.append({'item_id': item['id'], 'ordinal': ordinal,
                'identity': _audio_original_identity(path)})
    if not images:
        raise RecognitionConflict('image_material_binding_invalid')
    return images


def _descriptor(image):
    return {'item_id': image['item_id'], 'ordinal': image['ordinal'],
        'file': {key: value for key, value in image['identity'].items() if key != 'path'}}


def freeze_image_request(kind, *, records, models, project_id, materials, messages,
                         load_text=None, local_only=False, model_purpose='vision', capabilities=(),
                         policy_versions=None, **identities):
    from .policies import override
    from .policies.pipelines import versions_for_turn
    if (kind != 'media.image_read' or model_purpose != 'vision' or capabilities
            or not egress_allowed(records, models, project_id, 'vision')):
        raise RecognitionConflict('image_remote_disabled')
    configuration = models.vision_binding()
    if configuration['mode'] != 'remote' or configuration['allow_remote'] is not True or not configuration['has_key']:
        raise RecognitionConflict('image_remote_disabled')
    revision = privacy_revision(records)
    images = uploaded_images(records, models.root, project_id, materials)
    authority = SourceEgressService(records)
    refs, snapshots = [], []
    for material in materials:
        scope = WorkScope('local-user', project_id)
        resolved, roots = resolve_turn_material(records, scope, material)
        snapshot = authority.snapshot(scope, roots)
        # Retain the frozen source qualification ceiling while using vision's
        # separate global consent, never a generation impersonation.
        authority.require(snapshot, 'generation')
        refs.append(resolved['ref'])
        snapshots.append(snapshot)
    if len(messages) != 1 or messages[0].get('role') != 'user':
        raise RecognitionConflict('image_input_invalid')
    parts = messages[0].get('content', [])
    text_parts = [part for part in parts if part.get('type') == 'text']
    if len(parts) != len(text_parts) + len(images):
        raise RecognitionConflict('image_input_invalid')
    expected = [image_content(Path(image['identity']['path']).read_bytes()) for image in images]
    if [part for part in parts if part.get('type') == 'image_url'] != expected:
        raise RecognitionConflict('image_material_binding_invalid')
    frozen = [{'role': 'user', 'content': deepcopy(text_parts) +
        [{'type': 'image_ref', 'image': _descriptor(image)} for image in images]}]
    privacy = {'mode': 'remote_allowed', 'allow_remote': True, 'pii': 'possible',
        'consent_refs': ['crp://default/model-settings/vision'], 'retention': 'session',
        'privacy_revision': revision, 'excluded_refs': [], 'source_snapshots': snapshots,
        'material_refs': deepcopy(materials)}
    selected = versions_for_turn(kind) if policy_versions is None else dict(policy_versions)
    if set(selected) != set(versions_for_turn(kind)):
        raise RecognitionConflict('image_policy_binding_invalid')
    with override(**selected):
        request = freeze_turn_request(kind, project_id=project_id,
            text=json.dumps(frozen, ensure_ascii=False, sort_keys=True, separators=(',', ':')),
            privacy=privacy, refs=refs, capabilities=[], **identities)
    binding = {'project_id': project_id, 'configuration': configuration, 'images': images, 'request': request}
    # Freeze and binding share one CAS. Failure during validation leaves neither.
    _validate(records, models, request, binding)
    with records.begin() as tx:
        old = tx.read(_BINDINGS, request['turn_id'])
        if old is not None and old.payload != binding:
            raise RecognitionConflict('image_original_changed')
        if old is None:
            tx.put(_BINDINGS, request['turn_id'], binding, expected_revision=0)
        _validate(tx, models, request, binding, in_transaction=True)
        tx.commit()
    return request


def _validate(records, models, request, binding, *, purpose='vision', in_transaction=False):
    if purpose != 'vision' or request['desired_outcome'] != 'media.image_read':
        raise RecognitionConflict('image_request_invalid')
    privacy = request['privacy']
    project = request['scope']['project_id']
    if (privacy['allow_remote'] is not True or privacy['mode'] != 'remote_allowed'
            or privacy_revision(records) != privacy['privacy_revision']
            or not egress_allowed(records, models, project, 'vision')):
        raise RecognitionConflict('image_remote_disabled')
    if binding['project_id'] != project:
        raise RecognitionConflict('image_material_binding_invalid')
    if binding['configuration'] != models.vision_binding():
        raise RecognitionConflict('image_configuration_changed')
    # SourceEgress opens a UOW even for reads. Reuse this owned UOW while the
    # binding CAS is held, so its normal validator sees the same transaction.
    authority = SourceEgressService(TransactionRecords(records) if in_transaction else records)
    materials, snapshots, refs = privacy['material_refs'], privacy['source_snapshots'], request['input']['refs']
    if not materials or not len(materials) == len(snapshots) == len(refs):
        raise RecognitionConflict('image_material_binding_invalid')
    for material, snapshot, ref in zip(materials, snapshots, refs):
        scope = WorkScope('local-user', project)
        resolved, roots = resolve_turn_material(records, scope, material)
        if (ref != resolved['ref'] or snapshot['scope'] != {'user_id': 'local-user', 'project_id': project}
                or snapshot['roots'] != roots):
            raise RecognitionConflict('image_material_binding_invalid')
        authority.validate_snapshot(scope, snapshot)
        authority.require(snapshot, 'generation')
    if uploaded_images(records, models.root, project, materials) != binding['images']:
        raise RecognitionConflict('image_original_changed')
    frozen = json.loads(request['input']['text'])
    expected = [_descriptor(image) for image in binding['images']]
    actual = [part['image'] for message in frozen for part in message['content'] if part.get('type') == 'image_ref']
    if (actual != expected or request != binding['request']
            or any(part.get('type') not in {'text', 'image_ref'} for message in frozen for part in message['content'])):
        raise RecognitionConflict('image_material_binding_invalid')


def validate_image_request(records, models, request, *, purpose='vision'):
    binding = records.read(_BINDINGS, request['turn_id'])
    if binding is None:
        raise RecognitionConflict('image_material_binding_invalid')
    _validate(records, models, request, binding.payload, purpose=purpose)


def frozen_image_messages(records, models, request):
    validate_image_request(records, models, request)
    binding = records.read(_BINDINGS, request['turn_id']).payload
    frozen = json.loads(request['input']['text'])
    files = iter(binding['images'])
    messages = [{'role': message['role'], 'content': [
        image_content(Path(next(files)['identity']['path']).read_bytes()) if part['type'] == 'image_ref' else deepcopy(part)
        for part in message['content']]} for message in frozen]
    validate_image_request(records, models, request)
    return messages


def effective_image_read(records, row):
    """A read is current only for its processing run or sealed owner revision."""
    saved = records.read('v2_image_reads', row.object_id)
    image = saved.payload if saved is not None else {}
    item = row.payload
    raw_matches = image.get('source_text') == item.get('source_text')
    if not raw_matches:
        from .source_sections import paired_image_matches
        raw_matches = paired_image_matches(records, row, image)
    if (image.get('item_id') != row.object_id or image.get('project_id') != item['project_id']
            or not isinstance(image.get('run_id'), str) or not image.get('run_id')
            or not raw_matches):
        return None
    status = item.get('status')
    if status == 'processing':
        valid = image['run_id'] == item.get('processing_run_id') and image.get('owner_revision') == row.revision
    elif status in {'confirming', 'confirmed'}:
        valid = image.get('owner_status') == 'ready' and image.get('owner_revision') == item.get('reviewed_revision')
    else:
        valid = status in {'ready', 'failed'} and image.get('owner_status') == status and image.get('owner_revision') == row.revision
    return image if valid else None


def _literal_section(heading, text):
    fence = '~~~'
    while fence in text:
        fence += '~'
    return f'{heading}\n\n{fence}\n{text}\n{fence}'


COMMENT_SECTION_PREFIX = '\n\n## 评论区\n\n<!-- source_sections.comments: capture-qualified -->\n\n'


def draft_markdown(records, row, draft):
    """The complete L1 formatter: draft, proven comments, retained inference."""
    return _formatted_draft(records, row, draft)[0]


def _formatted_draft(records, row, draft):
    from ..workspace_generation import _markdown
    from .source_sections import comment_section_for_item
    markdown = _markdown(draft)
    section = None
    comments = comment_section_for_item(records, row)
    if comments is not None:
        sections = []
        for entry in comments['entries']:
            heading = (f"### 第{entry['ordinal']}条 · {entry['like_count']} 赞"
                if comments['origin'] == 'bilibili' else f"### 第{entry['ordinal']}张")
            sections.append(_literal_section(heading, entry['text']))
        section = {'count': comments['count'],
            'heading': {'start': len(markdown) + 2, 'end': len(markdown) + 2 + len('## 评论区')}}
        markdown += COMMENT_SECTION_PREFIX + '\n\n'.join(sections)
    return image_markdown(records, row, markdown), section


def comment_section_for_document(records, documents, row, *, revision=None):
    """Local display needs an exact committed L1, never an egress permission."""
    try:
        if row.payload.get('status') != 'confirmed':
            return None
        identity = row.payload.get('document_id')
        document = documents.read(identity) if isinstance(identity, str) else None
        if document is None or document.get('project_id') != row.payload['project_id']:
            return None
        selected = document['revision'] if revision is None else revision
        if type(selected) is not int or selected != document['revision'] or selected < 1:
            return None
        operation = records.read('workspace_confirmation_operations', 'confirm-' + row.object_id)
        if (operation is None or operation.payload.get('state') != 'committed'
                or operation.payload.get('document_id') != identity
                or operation.payload.get('project_id') != row.payload['project_id']
                or type(operation.payload.get('reviewed_revision')) is not int
                or operation.payload['reviewed_revision'] != row.payload.get('reviewed_revision')):
            return None
        expected, section = _formatted_draft(records, row, operation.payload['draft'])
        if (section is None or documents.markdown(identity, revision=selected) != expected
                or operation.payload.get('markdown') != expected):
            return None
        return {'document_id': identity, 'document_revision': selected,
            'coordinate_space': 'document_markdown_v1', **section}
    except (RecognitionConflict, KeyError, TypeError, ValueError, OSError):
        return None


def image_markdown(records, row, markdown):
    """Append retained visual inference to L1; never add it to OCR evidence."""
    image = effective_image_read(records, row)
    if image is None or image.get('provenance') != 'model_inference':
        return markdown
    descriptions = image.get('descriptions', [])
    if not any(descriptions):
        return markdown
    # Fenced text cannot inject summary/fact sections into Markdown readers.
    sections = ['## 看图', '<!-- image_read.model_inference: unverified -->', '模型推断']
    for ordinal, description in enumerate(descriptions, 1):
        if description:
            sections.append(_literal_section(f'### 第{ordinal}张', description))
    return markdown + '\n\n' + '\n\n'.join(sections)


def image_inference_for_document(records, documents, row, *, revision=None):
    """Qualify only the confirmed image section of an exact document body."""
    image = effective_image_read(records, row)
    if image is None or image.get('provenance') != 'model_inference' or row.payload.get('status') != 'confirmed':
        return None
    identity = row.payload.get('document_id')
    document = documents.read(identity) if isinstance(identity, str) else None
    if document is None or document.get('project_id') != row.payload['project_id']:
        return None
    selected = document['revision'] if revision is None else revision
    if type(selected) is not int or selected < 1 or selected > document['revision']:
        return None
    operation = records.read('workspace_confirmation_operations', 'confirm-' + row.object_id)
    if (operation is None or operation.payload.get('state') != 'committed'
            or operation.payload.get('document_id') != identity
            or operation.payload.get('project_id') != row.payload['project_id']
            or operation.payload.get('reviewed_revision') != image['owner_revision']):
        return None
    markdown = documents.markdown(identity, revision=selected)
    if (markdown != operation.payload['markdown']
            or markdown != draft_markdown(records, row, operation.payload['draft'])):
        return None
    return {'provenance': 'image_read.model_inference', 'epistemic_status': 'unverified',
        'document_revision': selected, 'sections': [{'ordinal': index, 'text': text}
            for index, text in enumerate(image['descriptions'], 1) if text]}


def image_only_draft(records, row):
    image = effective_image_read(records, row)
    if (image is None or image.get('provenance') != 'model_inference'
            or image.get('source_text') or not any(image.get('descriptions', []))):
        return None
    return {'title': row.payload['title'], 'summary': '', 'topics': [], 'facts': [],
        'todos': [], 'uncertainties': [], 'people': [], 'dates': [], 'suggestions': []}


def read_uploaded_images(owner, project_id, item_id, run_id, reference_factory, *, with_ranges=False):
    """Read each owned upload; keep inference and fallback in a new sidecar."""
    from ..kernel.image_read import read_images
    from ..local_image_provider import local_image_provider
    from .policies import get
    from .policies.pipelines import versions_for_turn
    row = owner.items.item_for(item_id, project_id)
    materials = [{'type': 'original_item', 'id': item_id, 'revision': row.revision, 'project_id': project_id}]
    images = uploaded_images(owner.items.records, owner.runtime_root, project_id, materials)

    def validate():
        current = owner.items.item_for(item_id, project_id)
        if (current.revision != row.revision or current.payload['status'] != 'processing'
                or current.payload.get('processing_run_id') != run_id
                or uploaded_images(owner.items.records, owner.runtime_root, project_id, materials) != images):
            raise RecognitionConflict('image_original_changed')
    validate()
    profile = owner.models.public().get('vision', {'mode': 'local'})
    fallback = profile.get('mode') == 'remote' and not egress_allowed(owner.items.records, owner.models, project_id, 'vision')
    remote = profile.get('mode') == 'remote' and not fallback
    turn_id = None
    key = 'image:' + item_id + ':' + run_id
    identity = {'kind': 'media.image_read', 'project': project_id, 'key': key, 'purpose': 'vision'}
    old = next((row for row in owner.items.records.list('v2_memory_turn_keys') if row.payload['identity'] == identity), None)
    versions = old.payload['request']['policy_versions'] if old else versions_for_turn('media.image_read')
    policy = get('image_read', version=versions['image_read'])
    if remote:
        parts = [image_content(Path(image['identity']['path']).read_bytes()) for image in images]
        messages = policy.prepare(parts)
        validate()
        def freeze(kind, **values):
            return freeze_image_request(kind, messages=messages, policy_versions=versions, **values)
        output, _, turn_id = read_images(owner.items.records, owner.models,
            project=project_id, key=key, materials=materials,
            validate=validate, freeze_request=freeze, validate_request=validate_image_request,
            remote_allowed=egress_allowed, messages=messages,
            load_messages=lambda request: frozen_image_messages(owner.items.records, owner.models, request))
        frozen = owner.items.records.read(_BINDINGS, turn_id).payload['request']
        outputs = [image.model_dump() for image in output.images]
        projection = get('image_read', version=frozen['policy_versions']['image_read']).decide(outputs)
    else:
        outputs = []
        for image in images:
            path = Path(image['identity']['path'])
            reference = reference_factory(row.payload, path)
            source = {'id': item_id, 'type': 'image', 'metadata': {'image_reference': reference.reference,
                'image_authorization': {'authorization_id': reference.record['id']}}}
            result = local_image_provider(owner.runtime_root, reference).extract_text(
                source=source, job={'required_capability': 'ocr'})
            validate()
            outputs.append({'text': result.text, 'description': ''})
        projection = policy.decide(outputs)
        projection['descriptions'] = []
    validate()
    payload = {'item_id': item_id, 'project_id': project_id, 'run_id': run_id,
        'image_count': len(images), 'descriptions': projection['descriptions'],
        'mode': 'remote' if remote else 'local', 'local_fallback': fallback, 'turn_id': turn_id,
        'provenance': 'model_inference' if remote else 'local_ocr',
        'images': images, 'source_text': projection['source_text'],
        'owner_revision': row.revision, 'owner_status': 'processing', 'policy_versions': versions}
    with owner.items.records.begin() as tx:
        current = owner.items.item_for(item_id, project_id, read=tx.read)
        if (current.revision != row.revision or current.payload['status'] != 'processing'
                or current.payload.get('processing_run_id') != run_id
                or uploaded_images(tx, owner.runtime_root, project_id, materials) != images):
            raise RecognitionConflict('image_original_changed')
        if remote:
            binding = tx.read(_BINDINGS, turn_id).payload
            _validate(tx, owner.models, binding['request'], binding, in_transaction=True)
        old = tx.read('v2_image_reads', item_id)
        tx.put('v2_image_reads', item_id, payload, expected_revision=old.revision if old else 0)
        tx.commit()
    validate()
    if with_ranges:
        from .policies.image_read import project_texts
        actual, spans = project_texts(outputs)
        if actual != projection['source_text']:
            raise RecognitionConflict('image_policy_binding_invalid')
        return actual, spans
    return projection['source_text']
