"""同用户词表产品入口；事务与解析规则由已有 Product Core 承担。"""
import json

from fastapi import APIRouter, HTTPException, Request

from core.product_core.asr_lexicon_service import MAX_IMPORT_BYTES, AsrLexiconError, AsrLexiconService


def install_asr_lexicon_routes(application, *, records, runtime_root):
    service = AsrLexiconService(records, runtime_root=runtime_root)
    application.state.asr_lexicon = service
    router = APIRouter(prefix="/api/v2/asr/lexicon")

    @router.get("")
    def read_lexicon():
        return service.read()

    @router.post("/import")
    async def import_lexicon(request: Request):
        content_length = request.headers.get("content-length", "0")
        if not content_length.isdecimal():
            raise HTTPException(400, "invalid_asr_lexicon_import")
        if int(content_length) > MAX_IMPORT_BYTES * 6 + 1024:
            raise HTTPException(413, "asr_lexicon_import_too_large")
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_IMPORT_BYTES * 6 + 1024:
                raise HTTPException(413, "asr_lexicon_import_too_large")
            raw.extend(chunk)
        try:
            body = json.loads(raw)
            if not isinstance(body, dict) or not {"text", "expected_revision"} <= set(body) or not set(body) <= {"text", "format", "expected_revision", "preview"}:
                raise AsrLexiconError()
            return service.import_text(**body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(400, "invalid_asr_lexicon_import") from None
        except AsrLexiconError as error:
            status = 409 if error.code == "asr_lexicon_revision_conflict" else 413 if error.code == "asr_lexicon_import_too_large" else 400
            raise HTTPException(status, error.code) from None

    application.include_router(router)
