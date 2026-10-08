"""Request-local read-only adapter over the existing Bilibili discovery service."""
from datetime import datetime, timezone
from uuid import uuid4
from .bilibili_favorite_collection import build_bilibili_favorite_collection_service


class _DiscoveryMemoryStore:
    namespace_id = 'default'

    def __init__(self):
        self.values = {}

    def read(self, collection, identity):
        return self.values.get((collection, identity))

    def write(self, collection, identity, payload, *, expected_revision):
        if expected_revision != 0 or (collection, identity) in self.values:
            raise ValueError('discovery_store_conflict')
        self.values[(collection, identity)] = dict(payload)
        return 1


def discover_bilibili_favorites(*, url, project_id):
    service = build_bilibili_favorite_collection_service(_DiscoveryMemoryStore(), namespace_id='default')
    snapshot = service.resolve(source_url=url, project_id=project_id,
        snapshot_id='discovery-' + uuid4().hex, resolved_at=datetime.now(timezone.utc).isoformat())
    return [item['url'] for item in snapshot.payload['items']]
