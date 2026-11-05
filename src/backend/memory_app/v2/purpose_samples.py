import base64
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import wave
import zlib

import av
from PIL import Image, ImageDraw, ImageFont

from backend.shared.llm.image_input import image_content
from core.ai_kernel import validate_turn_request
from ..kernel.memory_turn import MemoryTurn
from .privacy import egress_allowed, is_private_project
from .policies import get
from .purpose_test_audio import PHRASE, SAMPLE_ID, WAV_BYTES, ZLIB_BASE64
from .turn_requests import freeze_product_turn, validate_frozen_inputs


CHECKS = 'v2_purpose_checks'
PROOF = 'model-purpose-sample-v1'
SEARCH_QUERY = 'Python official documentation'
_SEAL = object()
CATALOG = {
    'vision': {'id': 'vision-model-test-v1', 'label': '固定文字图样 MODEL TEST 123', 'text': 'MODEL TEST 123', 'media_type': 'image/png'},
    'asr': {'id': SAMPLE_ID, 'label': '离线合成语音 One two three.', 'text': PHRASE, 'media_type': 'audio/wav'},
    'search': {'id': 'search-python-docs-v1', 'label': SEARCH_QUERY, 'text': SEARCH_QUERY, 'media_type': 'text/plain'},
}


def sample_bytes(purpose):
    if purpose == 'asr':
        raw = zlib.decompress(base64.b64decode(ZLIB_BASE64, validate=True))
        if len(raw) != WAV_BYTES:
            raise ValueError('purpose_test_sample_invalid')
        with wave.open(BytesIO(raw)) as audio:
            if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) != (8000, 1, 1):
                raise ValueError('purpose_test_sample_invalid')
        resampler = av.AudioResampler(format='s16', layout='mono', rate=16000)
        frames = []
        with av.open(BytesIO(raw)) as recording:
            for frame in recording.decode(audio=0):
                frames.extend(resampler.resample(frame))
            frames.extend(resampler.resample(None))
        output = BytesIO()
        with wave.open(output, 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b''.join(frame.to_ndarray().tobytes() for frame in frames))
        return output.getvalue()
    if purpose == 'vision':
        image = Image.new('RGB', (400, 96), 'white')
        ImageDraw.Draw(image).text((16, 24), CATALOG[purpose]['text'], font=ImageFont.load_default(size=36), fill='black')
        output = BytesIO()
        image.save(output, format='PNG')
        return output.getvalue()
    if purpose == 'search':
        return SEARCH_QUERY.encode('utf8')
    raise ValueError('purpose_test_purpose_invalid')


def sample_catalog():
    return [{'purpose': purpose, **value, 'bytes': len(sample_bytes(purpose))} for purpose, value in CATALOG.items()]


@dataclass(frozen=True)
class RegisteredPurposeSample:
    records: object
    root: Path
    command_id: str
    project_id: str
    purpose: str
    path: Path
    raw: bytes
    seal: object

    @classmethod
    def create(cls, records, root, command_id, project_id, purpose, *, consume, validate):
        raw = sample_bytes(purpose)
        root = Path(root).resolve(strict=True)
        area = root / 'model-checks'
        directory = area / command_id
        path = directory / {'vision': 'sample.png', 'asr': 'sample.wav', 'search': 'sample.txt'}[purpose]
        if area.is_symlink() or directory.is_symlink() or path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError('purpose_test_sample_unavailable')
        validate()
        consume(len(raw))
        directory.mkdir(parents=True, exist_ok=False)
        with path.open('xb') as output:
            output.write(raw)
        sample = cls(records, root, command_id, project_id, purpose, path, raw, _SEAL)
        sample.validate()
        return sample

    @property
    def descriptor(self):
        return {'schema_version': 1, 'id': CATALOG[self.purpose]['id'], 'purpose': self.purpose,
            'command_id': self.command_id, 'project_id': self.project_id,
            'media_type': CATALOG[self.purpose]['media_type'], 'bytes': len(self.raw),
            'content_base64': base64.b64encode(self.raw).decode('ascii')}

    @classmethod
    def for_cleanup(cls, records, root, command_id):
        row = records.read(CHECKS, command_id)
        if row is None or row.payload.get('purpose') not in CATALOG:
            raise ValueError('purpose_test_sample_unavailable')
        purpose, project = row.payload['purpose'], row.payload['project_id']
        root = Path(root).resolve(strict=True)
        path = root / 'model-checks' / command_id / {'vision': 'sample.png', 'asr': 'sample.wav', 'search': 'sample.txt'}[purpose]
        if (root / 'model-checks').is_symlink() or path.parent.is_symlink() or path.is_symlink():
            raise ValueError('purpose_test_sample_unavailable')
        sample = cls(records, root, command_id, project, purpose, path, sample_bytes(purpose), _SEAL)
        if row.payload.get('input_proof') != sample.descriptor:
            raise ValueError('purpose_test_sample_changed')
        return sample

    def validate(self):
        row = self.records.read(CHECKS, self.command_id)
        if (self.seal is not _SEAL or not isinstance(self.purpose, str) or self.purpose not in CATALOG
                or self.raw != sample_bytes(self.purpose) or row is None or row.payload.get('project_id') != self.project_id
                or row.payload.get('purpose') != self.purpose or row.payload.get('sample_id') != CATALOG[self.purpose]['id']
                or row.payload.get('status') != 'running' or self.path.is_symlink()
                or self.path.resolve(strict=True).parent != (self.root / 'model-checks' / self.command_id).resolve(strict=True)
                or self.path.read_bytes() != self.raw):
            raise ValueError('purpose_test_sample_changed')
        return self.descriptor

    def freeze(self, kind, **values):
        self.validate()
        if self.purpose == 'vision':
            values['text'] = CATALOG[self.purpose]['label']
        request = freeze_product_turn(kind, **values)
        payload = self.descriptor
        if self.purpose == 'vision':
            payload['messages'] = get('image_read', version=request['policy_versions']['image_read']).prepare([image_content(self.raw)])
        proof = MemoryTurn.store_for(self.records).immutable_payload_reference(request['turn_id'], PROOF)
        with self.records.begin() as tx:
            row = tx.read(CHECKS, self.command_id)
            staged = {'turn_id': request['turn_id'], 'payload_ref': proof, 'payload': payload}
            if row.payload.get('frozen_input') not in (None, staged):
                raise ValueError('purpose_test_sample_changed')
            if row.payload.get('frozen_input') is None:
                tx.put(CHECKS, self.command_id, {**row.payload, 'frozen_input': staged}, expected_revision=row.revision)
                tx.commit()
        request['input']['refs'].append({'kind': 'asset', 'object_id': CATALOG[self.purpose]['id'], 'uri': proof})
        return validate_turn_request(request)

    def validate_request(self, records, models, request, *, purpose):
        self.validate()
        validate_frozen_inputs(records, models, request, purpose=purpose)
        store = MemoryTurn.store_for(records)
        proof = store.get_immutable_payload(request['turn_id'], PROOF)
        expected = self.descriptor
        if self.purpose == 'vision':
            expected['messages'] = get('image_read', version=request['policy_versions']['image_read']).prepare([image_content(self.raw)])
        staged = records.read(CHECKS, self.command_id).payload.get('frozen_input')
        reference = store.immutable_payload_reference(request['turn_id'], PROOF)
        if (staged != {'turn_id': request['turn_id'], 'payload_ref': reference, 'payload': expected}
                or proof is not None and proof != (reference, expected) or request['input']['refs'] != [
                    {'kind': 'asset', 'object_id': CATALOG[self.purpose]['id'], 'uri': reference}]):
            raise ValueError('purpose_test_sample_changed')

    def publish(self, request, models):
        self.validate_request(self.records, models, request, purpose=self.purpose)
        store = MemoryTurn.store_for(self.records)
        if store.get_request(request['turn_id']) != request:
            raise ValueError('purpose_test_turn_not_accepted')
        staged = self.records.read(CHECKS, self.command_id).payload['frozen_input']
        reference = store.get_or_create_immutable_payload(request['turn_id'], PROOF, staged['payload'])
        if reference != staged['payload_ref']:
            raise ValueError('purpose_test_sample_changed')
        return staged['payload']

    def image_messages(self, request, models):
        return self.publish(request, models)['messages']

    def require_remote(self, models):
        self.validate()
        if is_private_project(self.records, self.project_id) or not egress_allowed(self.records, models, self.project_id, self.purpose):
            raise ValueError('purpose_test_remote_disabled')

    def dispose(self):
        if self.path.parent != self.root / 'model-checks' / self.command_id or self.path.is_symlink():
            raise ValueError('purpose_test_sample_unavailable')
        if self.path.is_file():
            if self.path.read_bytes() != self.raw:
                raise ValueError('purpose_test_sample_changed')
            self.path.unlink()
        if self.path.parent.exists():
            self.path.parent.rmdir()


class RegisteredImageReference:
    def __init__(self, sample):
        if type(sample) is not RegisteredPurposeSample or sample.purpose != 'vision':
            raise ValueError('purpose_test_sample_invalid')
        sample.validate()
        self.sample = sample
        self.reference = 'model-check://' + sample.command_id
        self.record = {'id': sample.command_id, 'source_id': sample.command_id,
            'image_reference': self.reference, 'path': str(sample.path), 'status': 'authorized'}

    def read(self, collection, identity):
        self.sample.validate()
        return dict(self.record) if collection == 'authorized_file_refs' and identity == self.record['id'] else None

    def list(self, collection):
        self.sample.validate()
        return [dict(self.record)] if collection == 'authorized_file_refs' else []
