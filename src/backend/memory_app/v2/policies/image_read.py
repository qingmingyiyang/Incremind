"""Pure version-one image reading prompt and ordered text projection."""
from .types import ModelPolicy


def prepare(images):
    return [{'role': 'user', 'content': [
        {'type': 'text', 'text': '按图片顺序逐张识别。每张返回 text 原文和 description 画面说明。'
            '原文保留行与顺序，不补写看不清的字。画面说明不超过200字，属于模型推断。'
            '只输出 JSON 对象 {"images":[{"text":"","description":""}]}。'},
        *images]}]


def project_texts(images):
    """Build the unchanged OCR text and its actual input-ordinal coordinates."""
    if not isinstance(images, (list, tuple)) or not images:
        raise ValueError('image_read_output_invalid')
    for image in images:
        if (not isinstance(image, dict) or set(image) != {'text', 'description'}
                or any(not isinstance(image[key], str) for key in ('text', 'description'))
                or len(image['description']) > 200):
            raise ValueError('image_read_output_invalid')
    texts = [image['text'].strip() for image in images]
    if not any(texts):
        return '', []
    parts, spans, cursor = [], [], 0
    for ordinal, text in enumerate(texts, 1):
        prefix = '' if len(texts) == 1 else f'## 第{ordinal}张\n\n'
        if parts:
            cursor += 2
        start = cursor + len(prefix)
        if text:
            spans.append({'ordinal': ordinal, 'start': start, 'end': start + len(text)})
        parts.append(prefix + text)
        cursor = start + len(text)
    return '\n\n'.join(parts), spans


def decide(images):
    original, _spans = project_texts(images)
    return {'source_text': original,
        'descriptions': [image['description'].strip() for image in images]}


v1 = ModelPolicy(prepare=prepare, decide=decide)
