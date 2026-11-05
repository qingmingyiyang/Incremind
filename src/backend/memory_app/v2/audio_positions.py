import math
import tempfile
import wave
from pathlib import Path

from fastapi import HTTPException
from core.product_core.audio_asset_transcriber import AudioTranscriptSegment, _segments_text
from core.storage_provider import JsonObjectStore
from ..rebuild_storage import build_rebuild_object_store
from ..uploaded_media import original_path
from ..workspace_audio import (
    _validated_audio_transcription, _cloud_audio_derivative, _split_workspace_wav_chunk,
)


class AudioPositions:
    def __init__(self, records, runtime_root):
        self.records = records
        self.root = Path(runtime_root)

    def timeline(self, item):
        if item.get('input_kind') not in {'audio', 'video'} or item.get('platform'):
            return None
        try:
            checkpoint = _validated_audio_transcription(item, self.root)
            if checkpoint is None:
                return None
            store, storage = build_rebuild_object_store(self.root)
            if checkpoint['store_kind'] == 'workspace_asr_internal':
                store = JsonObjectStore(self.root / 'workspace' / 'asr-internal',
                                       namespace_id=storage.namespace_id)
            output = store.read('media_processing_outputs', checkpoint['output_id'])
            raw = output.get('segments') if isinstance(output, dict) else None
            if not isinstance(raw, list) or not raw:
                return None
            segments, spans, offset = [], [], 0
            previous_start, previous_end = -1.0, -1.0
            for segment in raw:
                if not isinstance(segment, dict) or not isinstance(segment.get('text'), str):
                    return None
                start, end = segment.get('start_seconds'), segment.get('end_seconds')
                if (isinstance(start, bool) or isinstance(end, bool)
                        or not isinstance(start, (int, float)) or not isinstance(end, (int, float))
                        or not math.isfinite(start) or not math.isfinite(end)
                        or start < 0 or end <= start or start < previous_start or end < previous_end):
                    return None
                text = segment['text'].strip()
                if not text:
                    continue
                if segments:
                    offset += 1
                segments.append(AudioTranscriptSegment(float(start), float(end), segment['text']))
                spans.append((offset, offset + len(text), float(start), float(end)))
                offset += len(text)
                previous_start, previous_end = start, end
            if not segments or _segments_text(segments) != item.get('source_text'):
                return None
            asset = store.read('audio_asset_refs', checkpoint['audio_asset_id'])
            return spans, checkpoint, asset
        except HTTPException as error:
            if error.status_code != 409:
                raise
            return None
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            return None

    def seconds(self, item, start, end):
        if (type(start) is not int or type(end) is not int
                or start < 0 or end <= start or end > len(item.get('source_text', ''))):
            return None
        timeline = self.timeline(item)
        if timeline is None:
            return None
        intersecting = [span for span in timeline[0] if span[0] < end and span[1] > start]
        return [intersecting[0][2], intersecting[-1][3]] if intersecting else None

    def citation_seconds(self, candidate, window, coordinate_space):
        if candidate.get('layer') != 'L0' or coordinate_space != 'workspace_source_text_v1':
            return None
        identity = candidate.get('entry', {}).get('item_id')
        row = self.records.read('workspace_items', identity) if identity else None
        if row is None or row.payload.get('project_id') != candidate.get('project_id'):
            return None
        return self.seconds(row.payload, window.get('start'), window.get('end'))

    def clip(self, item, start, end):
        try:
            start, end = float(start), float(end)
        except (TypeError, ValueError, OverflowError):
            raise HTTPException(400, 'invalid_audio_clip_range') from None
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise HTTPException(400, 'invalid_audio_clip_range')
        timeline = self.timeline(item)
        if timeline is None:
            raise HTTPException(404, 'audio_clip_unavailable')
        if not any(span[2] <= start < end <= span[3] for span in timeline[0]):
            # 跨段原句允许覆盖相邻段，首尾必须来自同一份已核验对照。
            starts = {span[2] for span in timeline[0]}
            ends = {span[3] for span in timeline[0]}
            if start not in starts or end not in ends:
                raise HTTPException(400, 'invalid_audio_clip_range')
        path = original_path(self.root, item)
        cache = self.root / 'cache' / 'audio-clips'
        if not cache.resolve(strict=False).is_relative_to(self.root.resolve()):
            raise HTTPException(404, 'audio_clip_unavailable')
        cache.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='clip-', dir=cache)
        request_root = Path(temporary.name)
        try:
            # 只复用本次转写对应的受管派生文件，其他路径重新从受管原件解码。
            checkpoint = timeline[1]
            managed = self.root / 'workspace' / 'transcripts' / (checkpoint['run_id'] + '.wav')
            audio = managed.resolve(strict=False)
            managed_root = (self.root / 'workspace' / 'transcripts').resolve(strict=False)
            asset = timeline[2]
            retained_path = asset.get('path') if isinstance(asset, dict) else None
            if (checkpoint['store_kind'] != 'rebuild' or not managed_root.is_relative_to(self.root.resolve())
                    or not audio.is_relative_to(managed_root)
                    or not isinstance(retained_path, str) or asset.get('media_type') != 'audio/wav'
                    or Path(retained_path).resolve(strict=False) != audio or not audio.is_file()):
                audio, _ = _cloud_audio_derivative(path, request_root, 'audio')
            try:
                with wave.open(str(audio), 'rb') as source:
                    if (source.getframerate(), source.getnchannels(), source.getsampwidth()) != (16000, 1, 2):
                        raise ValueError('invalid_audio_derivative')
                    duration = source.getnframes() / source.getframerate()
            except (wave.Error, EOFError, ValueError):
                audio, duration = _cloud_audio_derivative(path, request_root, 'audio')
            if end > duration or start >= duration:
                raise HTTPException(400, 'invalid_audio_clip_range')
            first = max(0.0, start - 3.0)
            last = min(duration, end + 3.0, first + 90.0)
            output = request_root / 'clip.wav'
            _split_workspace_wav_chunk(str(audio), str(output), first, last)
            return output, temporary.cleanup
        except BaseException:
            temporary.cleanup()
            raise
