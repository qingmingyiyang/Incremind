import base64
from io import BytesIO

from pypdf import PdfReader


def pdf_content(data):
    if not isinstance(data, bytes) or not data:
        raise ValueError('pdf_input_invalid')
    reader = PdfReader(BytesIO(data))
    if reader.is_encrypted and reader.decrypt('') == 0:
        raise ValueError('document_password_required')
    if not len(reader.pages):
        raise ValueError('pdf_input_invalid')
    # 仅供 OXR 内部适配，原生 PDF 字节沿既有附件身份冻结。
    encoded = base64.b64encode(data).decode('ascii')
    return {'type': 'image_url', 'image_url': {'url': f'data:application/pdf;base64,{encoded}'}}
