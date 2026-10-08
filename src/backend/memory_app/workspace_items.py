"""Workspace item storage, optimistic writes and processing ownership."""

from __future__ import annotations

from uuid import uuid4
from fastapi import HTTPException
from .processing_lease import ProcessingLease, ProcessingLeaseConflict
from .transaction_records import TransactionRecords
from .workspace_contracts import _COLLECTION, _now, _project, _public
from core.storage_provider.source_retrieval_index import project_original


class WorkspaceItems:
    def __init__(self, records, processing_lease, lock):
        self.records = records
        self.processing_lease = processing_lease
        self.lock = lock

    def with_records(self, records):
        """Borrow a caller's unit of work with this owner's lease and lock."""
        return WorkspaceItems(records, self.processing_lease, self.lock)

    def item_for(self, item_id: str, project_id: str, *, read=None):
        row = (self.records.read if read is None else read)(_COLLECTION, item_id)
        if row is None or row.payload.get("project_id") != project_id:
            raise HTTPException(404, "item_not_found")
        return row

    def update(self, item_id: str, project_id: str, allowed: set[str], *,
               expected_run_id: str | None = None, source_sections=None, **changes):
        from .v2.source_sections import record_source_change
        if expected_run_id is not None:
            try:
                # Seal the image read against the final owner revision while
                # the existing processing lease holds the same transaction.
                with self.records.begin() as tx:
                    previous = self.item_for(item_id, project_id, read=tx.read)
                    lease = self.processing_lease
                    enlisted = ProcessingLease(TransactionRecords(tx), lease.collection,
                        lease.instance_id, lease.clock, lease.ttl_seconds)
                    result = enlisted.apply(item_id, project_id, expected_run_id, changes)
                    record_source_change(tx, previous, tx.read(_COLLECTION, item_id),
                        capture=source_sections, run_id=expected_run_id)
                    image = tx.read('v2_image_reads', item_id)
                    retained_image = False
                    if image is not None and image.payload.get('run_id') != expected_run_id:
                        from .v2.source_sections import paired_image_matches
                        retained_image = paired_image_matches(tx, tx.read(_COLLECTION, item_id), image.payload)
                    if (image is not None and image.payload.get('project_id') == project_id
                            and (image.payload.get('run_id') == expected_run_id or retained_image)):
                        tx.put('v2_image_reads', item_id, {**image.payload,
                            'owner_revision': result['revision'],
                            'owner_status': result['status']}, expected_revision=image.revision)
                    tx.commit()
                return result
            except ProcessingLeaseConflict as exc:
                raise HTTPException(409, exc.code) from None
        with self.lock:
            row = self.item_for(item_id, project_id)
            if row.payload["status"] not in allowed:
                raise HTTPException(409, "invalid_item_state")
            payload = {**row.payload, **changes}
            with self.records.begin() as tx:
                updated = tx.put(_COLLECTION, item_id, payload, expected_revision=row.revision)
                record_source_change(tx, row, updated, capture=source_sections)
                project_original(tx, item_id, project_id, updated.revision, str(payload.get('source_text') or ''),
                                 document_id=payload.get('document_id'), previous_document_id=row.payload.get('document_id'))
                tx.commit()
            return {**payload, "revision": updated.revision}

    def create(self, project_id: str, kind: str, title: str, source_text: str, **extra):
        item_id = "workspace-" + uuid4().hex
        payload = dict(id=item_id, project_id=project_id, input_kind=kind, title=title,
                       source_text=source_text, status="staged", draft=None, error=None,
                       document_id=None, created_at=_now(), **extra)
        return self.create_upload(payload)

    def create_upload(self, payload, *, images=None):
        """Create an original and its ordered upload sidecar in one owner UOW."""
        item_id, project_id = payload['id'], payload['project_id']
        from .v2.source_sections import create_section_owner
        with self.records.begin() as tx:
            created = tx.put(_COLLECTION, item_id, payload, expected_revision=0)
            create_section_owner(tx, created)
            project_original(tx, item_id, project_id, created.revision, str(payload.get('source_text') or ''),
                             document_id=payload.get('document_id'))
            if images is not None:
                tx.put('v2_image_groups', item_id, {'item_id': item_id,
                    'project_id': project_id, 'images': images}, expected_revision=0)
            tx.commit()
        return {**payload, "revision": created.revision}

    def public_row(self, row):
        return _public({**row.payload, "revision": row.revision})

    def bind_link_images(self, item_id, project_id, url, *, expected_revision, runtime_root):
        from .v2.image_read import uploaded_images
        from .v2.source_sections import bind_image_link
        from backend.recognition import RecognitionConflict
        try:
            with self.lock, self.records.begin() as tx:
                row = self.item_for(item_id, project_id, read=tx.read)
                if type(expected_revision) is not int or row.revision != expected_revision:
                    raise HTTPException(409, 'item_revision_conflicted')
                if (row.payload.get('status') != 'staged' or row.payload.get('input_kind') != 'image'
                        or row.payload.get('source_text')):
                    raise HTTPException(409, 'invalid_item_state')
                materials = [{'type': 'original_item', 'id': item_id,
                    'revision': row.revision, 'project_id': project_id}]
                images = uploaded_images(tx, runtime_root, project_id, materials)
                bind_image_link(tx, row, url, images)
                tx.commit()
                return self.public_row(row)
        except RecognitionConflict as error:
            raise HTTPException(409, error.code) from None

    def list_items(self, project_id: str = "default"):
        project_id = _project(project_id)
        self.processing_lease.recover_expired()
        items = [self.public_row(row) for row in
                 self.records.list_matching(_COLLECTION, project_id=project_id)]
        return {"items": sorted(items, key=lambda item: item["created_at"], reverse=True)}

    async def retry(self, item_id: str, body: dict):
        project_id = _project(body.get("project_id", "default"))
        return _public(self.update(item_id, project_id, {"failed"}, status="staged", error=None,
                              processing_consent=None))
