"""Compatibility import for the neutral retained-document facts reader."""
from backend.recognition.document_filings import COLLECTION, FILED_ID, DocumentFilingError
from backend.recognition.document_filings import filing_experience as read_filing_experience
from backend.recognition import RecognitionConflict


def filing_experience(*args, **kwargs):
    try:
        return read_filing_experience(*args, **kwargs)
    except DocumentFilingError as error:
        raise RecognitionConflict(str(error)) from error
