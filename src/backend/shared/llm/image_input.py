"""Convert actual image bytes into ordered OpenAI-compatible content parts.

pi-ai ImageContent uses base64 data and mimeType in a user content array. This
adapter targets our existing Chat Completions gateway's image_url data URI.
Only in-memory local bytes are accepted; no remote image URL is fetched.
"""
import base64
from io import BytesIO


def image_content(data):
    if not isinstance(data, bytes) or not data:
        raise ValueError('image_input_invalid')
    from PIL import Image
    try:
        with Image.open(BytesIO(data)) as image:
            image.load()
            mime = {'PNG': 'image/png', 'JPEG': 'image/jpeg', 'WEBP': 'image/webp',
                    'GIF': 'image/gif'}.get(image.format)
            if mime is None:
                target = BytesIO()
                image.convert('RGB').save(target, format='PNG')
                data, mime = target.getvalue(), 'image/png'
    except Exception:
        raise ValueError('image_input_invalid') from None
    encoded = base64.b64encode(data).decode('ascii')
    return {'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,{encoded}'}}
