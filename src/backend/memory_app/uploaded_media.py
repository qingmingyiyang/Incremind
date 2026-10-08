"""Read uploaded local media through existing domain adapters."""


VIDEO_SUFFIXES = frozenset({".mp4", ".mkv", ".mov", ".avi", ".m4v"})
MAX_VIDEO_BYTES = 2 * 1024 * 1024 * 1024
UPLOAD_REQUEST_BYTES = MAX_VIDEO_BYTES + 1024 * 1024


class UploadedImageReference:
    """A read-only authorization projection of this one actual upload."""
    def __init__(self, item, path):
        self.reference = 'workspace://' + item['id']
        self.record = {'id': 'image-upload-' + item['id'], 'source_id': item['id'],
            'image_reference': self.reference, 'path': str(path), 'status': 'authorized'}

    def read(self, collection, identity):
        return dict(self.record) if collection == 'authorized_file_refs' and identity == self.record['id'] else None

    def list(self, collection):
        return [dict(self.record)] if collection == 'authorized_file_refs' else []


def read_uploaded_image(owner, item, project_id, item_id, run_id):
    try:
        return owner.read_images(project_id, item_id, run_id, UploadedImageReference)
    except Exception:
        # Provider stderr and local paths must never become a public error or log.
        raise ValueError('image_processing_failed') from None
