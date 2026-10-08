from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
import hashlib
import os
from pathlib import Path
import re

from .ports import ObjectStorePort


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REPARSE_POINT = 0x0400


class AssetOwnershipGraphError(ValueError):
    """Raised when an asset ownership inventory request is invalid."""


@dataclass(frozen=True, slots=True)
class AssetOwnershipNode:
    node_id: str
    authority: str
    object_id: str
    storage_class: str
    evidence_class: str
    rebuildability: str
    content_sha256: str | None
    byte_count: int | None
    owned_bytes: bool
    status: str
    blockers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AssetOwnershipEdge:
    edge_id: str
    source_node_id: str
    target_node_id: str
    relation: str
    authoritative: bool


@dataclass(frozen=True, slots=True)
class AssetOwnershipGraph:
    schema_version: str
    complete: bool
    node_count: int
    edge_count: int
    blocker_count: int
    nodes: tuple[AssetOwnershipNode, ...]
    edges: tuple[AssetOwnershipEdge, ...]
    blockers: tuple[str, ...]


class BuildAssetOwnershipGraph:
    """Build a body-free ownership graph across current and migration stores.

    The graph is diagnostic authority. It never deletes bytes and never treats
    SQLite migration staging records as current product authority.
    """

    def __init__(
        self,
        store: ObjectStorePort,
        *,
        allowed_media_roots: Sequence[Path] = (),
        source_asset_records: Sequence[Mapping[str, object]] | None = None,
        source_asset_link_records: Sequence[Mapping[str, object]] | None = None,
        source_asset_authority: str = "workbench_original_assets",
        source_blob_authority: str = "original_blob_store",
    ) -> None:
        self._store = store
        self._media_roots = tuple(
            root.expanduser().resolve(strict=False) for root in allowed_media_roots
        )
        self._source_assets = (
            tuple(source_asset_records)
            if source_asset_records is not None
            else None
        )
        self._source_asset_links = (
            tuple(source_asset_link_records)
            if source_asset_link_records is not None
            else None
        )
        self._source_asset_authority = source_asset_authority
        self._source_blob_authority = source_blob_authority

    def execute(
        self,
        *,
        sqlite_blobs: Sequence[Mapping[str, object]] = (),
        sqlite_assets: Sequence[Mapping[str, object]] = (),
        sqlite_links: Sequence[Mapping[str, object]] = (),
    ) -> AssetOwnershipGraph:
        nodes: dict[str, AssetOwnershipNode] = {}
        edges: dict[str, AssetOwnershipEdge] = {}
        graph_blockers: set[str] = set()

        sources = {
            _required_id(item.get("id"), "source id"): item
            for item in _list_sources_including_deleted(self._store)
        }
        source_nodes = {
            source_id: _put_node(
                nodes,
                authority="sources",
                object_id=source_id,
                storage_class="structured_authority",
                evidence_class="source_identity",
                rebuildability="not_applicable",
                status=_source_status(source),
            )
            for source_id, source in sources.items()
        }

        asset_nodes: dict[str, str] = {}
        original_blob_nodes: dict[str, str] = {}
        original_blob_identity: dict[str, tuple[int | None, str | None]] = {}
        asset_records = (
            self._source_assets
            if self._source_assets is not None
            else self._store.list("workbench_original_assets")
        )
        for asset in asset_records:
            asset_id = _required_id(asset.get("id"), "asset id")
            blockers: set[str] = set()
            sha = _sha(asset.get("sha256"), blockers, "invalid_original_asset_sha256")
            byte_count = _bytes(asset.get("byte_count"), blockers)
            if not isinstance(asset.get("vault_ref"), str):
                blockers.add("original_asset_vault_ref_missing")
            node_id = _put_node(
                nodes,
                authority=self._source_asset_authority,
                object_id=asset_id,
                storage_class="owned_original_bytes",
                evidence_class="evidence_required",
                rebuildability="not_rebuildable",
                content_sha256=sha,
                byte_count=byte_count,
                owned_bytes=False,
                status=str(asset.get("link_status") or "unknown"),
                blockers=blockers,
            )
            asset_nodes[asset_id] = node_id
            if sha is not None:
                vault_ref = (
                    asset.get("vault_ref")
                    if isinstance(asset.get("vault_ref"), str)
                    else None
                )
                previous = original_blob_identity.get(sha)
                blob_blockers: set[str] = set()
                if previous is not None and previous != (byte_count, vault_ref):
                    graph_blockers.add(f"original_blob_identity_conflict:{sha}")
                    blob_blockers.add("shared_blob_identity_conflict")
                original_blob_identity.setdefault(sha, (byte_count, vault_ref))
                blob_node = original_blob_nodes.get(sha)
                if blob_node is None:
                    blob_node = _put_node(
                        nodes,
                        authority=self._source_blob_authority,
                        object_id=sha,
                        storage_class="owned_original_bytes",
                        evidence_class="evidence_required",
                        rebuildability="not_rebuildable",
                        content_sha256=sha,
                        byte_count=byte_count,
                        owned_bytes=True,
                        status="present_by_authority",
                        blockers=blob_blockers,
                    )
                    original_blob_nodes[sha] = blob_node
                _put_edge(
                    edges,
                    node_id,
                    blob_node,
                    "maps_to_owned_bytes",
                    authoritative=True,
                )

        link_records = (
            self._source_asset_links
            if self._source_asset_links is not None
            else self._store.list("source_asset_links")
        )
        for link in link_records:
            link_id = _required_id(link.get("id"), "source asset link id")
            source_id = _optional_id(link.get("source_id"))
            asset_id = _optional_id(link.get("asset_id"))
            if source_id not in source_nodes or asset_id not in asset_nodes:
                graph_blockers.add(f"dangling_source_asset_link:{link_id}")
                continue
            _put_edge(
                edges,
                source_nodes[source_id],
                asset_nodes[asset_id],
                "owns_original_evidence",
                authoritative=True,
            )
            asset = nodes[asset_nodes[asset_id]]
            link_hash = link.get("content_hash")
            if asset.content_sha256 and link_hash != asset.content_sha256:
                graph_blockers.add(f"source_asset_hash_conflict:{link_id}")

        outputs = {
            _required_id(item.get("id"), "media output id"): item
            for item in self._store.list("media_processing_outputs")
        }
        jobs = {
            _required_id(item.get("id"), "media job id"): item
            for item in self._store.list("media_processing_jobs")
        }
        for audio in self._store.list("audio_asset_refs"):
            audio_id = _required_id(audio.get("id"), "audio asset id")
            blockers: set[str] = set()
            source_id = _optional_id(audio.get("source_id"))
            path_value = audio.get("path")
            byte_count = _bytes(audio.get("size_bytes"), blockers)
            if audio.get("path_scope") != "local_generated_audio_track":
                blockers.add("media_path_scope_unverified")
            if not isinstance(path_value, str) or not path_value:
                blockers.add("media_path_missing")
            else:
                self._verify_media_path(Path(path_value), byte_count, blockers)
            audio_node = _put_node(
                nodes,
                authority="audio_asset_refs",
                object_id=audio_id,
                storage_class="owned_media_derivative",
                evidence_class="derived_cache",
                rebuildability="rebuildable_from_source",
                byte_count=byte_count,
                owned_bytes=True,
                status=str(audio.get("status") or "unknown"),
                blockers=blockers,
            )
            if source_id in source_nodes:
                _put_edge(
                    edges,
                    source_nodes[source_id],
                    audio_node,
                    "produces_rebuildable_derivative",
                    authoritative=True,
                )
            else:
                graph_blockers.add(f"audio_asset_source_missing:{audio_id}")
            matched_outputs = [
                item
                for item in outputs.values()
                if item.get("audio_asset_id") == audio_id
            ]
            if not matched_outputs:
                graph_blockers.add(f"audio_asset_output_missing:{audio_id}")
            for output in matched_outputs:
                output_id = _required_id(output.get("id"), "media output id")
                output_node = _put_node(
                    nodes,
                    authority="media_processing_outputs",
                    object_id=output_id,
                    storage_class="structured_derivative",
                    evidence_class="derived_metadata",
                    rebuildability="rebuildable_from_source",
                    status=str(output.get("status") or "unknown"),
                )
                _put_edge(
                    edges,
                    output_node,
                    audio_node,
                    "describes_derivative",
                    authoritative=True,
                )
                job_id = _optional_id(output.get("job_id"))
                if job_id not in jobs:
                    graph_blockers.add(f"media_output_job_missing:{output_id}")

        for read in self._store.list("source_content_reads"):
            read_id = _required_id(read.get("id"), "source content read id")
            blockers: set[str] = set()
            source_id = _optional_id(read.get("source_id"))
            text = read.get("text")
            declared_hash = _sha(
                read.get("text_sha256"),
                blockers,
                "content_read_sha256_missing",
            )
            actual_hash = (
                hashlib.sha256(text.encode("utf-8")).hexdigest()
                if isinstance(text, str)
                else None
            )
            if actual_hash is None:
                blockers.add("content_read_body_missing")
            elif declared_hash != actual_hash:
                blockers.add("content_read_hash_conflict")
            byte_count = _bytes(read.get("byte_count"), blockers)
            node = _put_node(
                nodes,
                authority="source_content_reads",
                object_id=read_id,
                storage_class="structured_body",
                evidence_class="extracted_evidence",
                rebuildability="refetch_or_reextract",
                content_sha256=actual_hash or declared_hash,
                byte_count=byte_count,
                owned_bytes=False,
                status=str(read.get("status") or "unknown"),
                blockers=blockers,
            )
            if source_id in source_nodes:
                _put_edge(
                    edges,
                    source_nodes[source_id],
                    node,
                    "owns_extracted_body",
                    authoritative=True,
                )
            else:
                graph_blockers.add(f"content_read_source_missing:{read_id}")

        for source_id, source in sources.items():
            if isinstance(source.get("original_url"), str):
                ref_node = _put_node(
                    nodes,
                    authority="sources",
                    object_id=f"{source_id}:external-url",
                    storage_class="external_reference",
                    evidence_class="source_locator",
                    rebuildability="externally_resolvable",
                    owned_bytes=False,
                    status="reference_only",
                )
                _put_edge(
                    edges,
                    source_nodes[source_id],
                    ref_node,
                    "references_external_locator",
                    authoritative=True,
                )

        self._add_sqlite_staging(
            nodes,
            edges,
            graph_blockers,
            sqlite_blobs=sqlite_blobs,
            sqlite_assets=sqlite_assets,
            sqlite_links=sqlite_links,
        )
        all_blockers = set(graph_blockers)
        for node in nodes.values():
            all_blockers.update(
                f"{node.node_id}:{blocker}" for blocker in node.blockers
            )
        ordered_nodes = tuple(sorted(nodes.values(), key=lambda item: item.node_id))
        ordered_edges = tuple(sorted(edges.values(), key=lambda item: item.edge_id))
        return AssetOwnershipGraph(
            schema_version="1.0.0",
            complete=not all_blockers,
            node_count=len(ordered_nodes),
            edge_count=len(ordered_edges),
            blocker_count=len(all_blockers),
            nodes=ordered_nodes,
            edges=ordered_edges,
            blockers=tuple(sorted(all_blockers)),
        )

    def _verify_media_path(
        self,
        path: Path,
        byte_count: int | None,
        blockers: set[str],
    ) -> None:
        if not self._media_roots:
            blockers.add("media_storage_root_unconfigured")
            return
        expanded = path.expanduser()
        if (
            not expanded.is_absolute()
            or expanded.is_symlink()
            or _is_reparse(expanded)
        ):
            blockers.add("media_path_linked_or_relative")
            return
        lexical = Path(os.path.abspath(expanded))
        lexical_root = next(
            (root for root in self._media_roots if _is_relative_to(lexical, root)),
            None,
        )
        if lexical_root is None:
            blockers.add("media_path_outside_owned_root")
            return
        if _contains_reparse(lexical.parent, lexical_root):
            blockers.add("media_path_reparse_chain")
            return
        resolved = path.expanduser().resolve(strict=False)
        if not any(_is_relative_to(resolved, root) for root in self._media_roots):
            blockers.add("media_path_outside_owned_root")
            return
        if not resolved.is_file() or resolved.is_symlink():
            blockers.add("media_file_unavailable")
            return
        if byte_count is not None and resolved.stat().st_size != byte_count:
            blockers.add("media_byte_count_conflict")

    @staticmethod
    def _add_sqlite_staging(
        nodes: dict[str, AssetOwnershipNode],
        edges: dict[str, AssetOwnershipEdge],
        graph_blockers: set[str],
        *,
        sqlite_blobs: Sequence[object],
        sqlite_assets: Sequence[object],
        sqlite_links: Sequence[object],
    ) -> None:
        if not (sqlite_blobs or sqlite_assets or sqlite_links):
            return
        blob_nodes: dict[str, str] = {}
        for blob in sqlite_blobs:
            object_id = _record_id(blob)
            payload = _payload(blob)
            blockers: set[str] = {"migration_staging_not_production_authority"}
            sha = _sha(
                payload.get("sha256") or object_id,
                blockers,
                "migration_blob_sha256_invalid",
            )
            blob_nodes[object_id] = _put_node(
                nodes,
                authority="sqlite.asset_blobs",
                object_id=object_id,
                storage_class="migration_staging",
                evidence_class="content_addressed_candidate",
                rebuildability="not_applicable",
                content_sha256=sha,
                owned_bytes=False,
                status="staged",
                blockers=blockers,
            )
        staged_assets: dict[str, str] = {}
        for asset in sqlite_assets:
            object_id = _record_id(asset)
            payload = _payload(asset)
            blob_sha = _optional_id(payload.get("blob_sha256"))
            staged_assets[object_id] = _put_node(
                nodes,
                authority="sqlite.original_assets",
                object_id=object_id,
                storage_class="migration_staging",
                evidence_class="authority_candidate",
                rebuildability="not_applicable",
                content_sha256=blob_sha if blob_sha and _SHA256.fullmatch(blob_sha) else None,
                owned_bytes=False,
                status="staged",
                blockers=("migration_staging_not_production_authority",),
            )
            if blob_sha in blob_nodes:
                _put_edge(
                    edges,
                    staged_assets[object_id],
                    blob_nodes[blob_sha],
                    "maps_to_content_addressed_blob",
                    authoritative=False,
                )
            else:
                graph_blockers.add(f"migration_asset_blob_missing:{object_id}")
        for link in sqlite_links:
            link_id = _record_id(link)
            payload = _payload(link)
            asset_id = _optional_id(payload.get("asset_id"))
            if asset_id not in staged_assets:
                graph_blockers.add(f"migration_link_asset_missing:{link_id}")


def serialize_asset_ownership_graph(graph: AssetOwnershipGraph) -> dict[str, object]:
    return {
        "schema_version": graph.schema_version,
        "complete": graph.complete,
        "node_count": graph.node_count,
        "edge_count": graph.edge_count,
        "blocker_count": graph.blocker_count,
        "nodes": [asdict(node) for node in graph.nodes],
        "edges": [asdict(edge) for edge in graph.edges],
        "blockers": list(graph.blockers),
    }


def _put_node(
    nodes: dict[str, AssetOwnershipNode],
    *,
    authority: str,
    object_id: str,
    storage_class: str,
    evidence_class: str,
    rebuildability: str,
    content_sha256: str | None = None,
    byte_count: int | None = None,
    owned_bytes: bool = False,
    status: str,
    blockers: Sequence[str] = (),
) -> str:
    node_id = f"{authority}:{object_id}"
    node = AssetOwnershipNode(
        node_id=node_id,
        authority=authority,
        object_id=object_id,
        storage_class=storage_class,
        evidence_class=evidence_class,
        rebuildability=rebuildability,
        content_sha256=content_sha256,
        byte_count=byte_count,
        owned_bytes=owned_bytes,
        status=status,
        blockers=tuple(sorted(set(blockers))),
    )
    existing = nodes.get(node_id)
    if existing is not None and existing != node:
        raise AssetOwnershipGraphError(f"conflicting ownership node: {node_id}")
    nodes[node_id] = node
    return node_id


def _put_edge(
    edges: dict[str, AssetOwnershipEdge],
    source_node_id: str,
    target_node_id: str,
    relation: str,
    *,
    authoritative: bool,
) -> None:
    edge_id = f"{source_node_id}->{relation}->{target_node_id}"
    edges[edge_id] = AssetOwnershipEdge(
        edge_id=edge_id,
        source_node_id=source_node_id,
        target_node_id=target_node_id,
        relation=relation,
        authoritative=authoritative,
    )


def _required_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise AssetOwnershipGraphError(f"{label} is invalid")
    return value


def _list_sources_including_deleted(
    store: ObjectStorePort,
) -> Sequence[Mapping[str, object]]:
    """Retention diagnostics must retain the identity of deleted Sources."""

    list_including_deleted = getattr(store, "list_including_deleted", None)
    if callable(list_including_deleted):
        return list_including_deleted("sources")
    return store.list("sources")


def _source_status(source: Mapping[str, object]) -> str:
    lifecycle = source.get("library_lifecycle")
    if isinstance(lifecycle, Mapping):
        status = lifecycle.get("status")
        if isinstance(status, str) and status:
            return status
    return "active"


def _optional_id(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _sha(value: object, blockers: set[str], blocker: str) -> str | None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        blockers.add(blocker)
        return None
    return value


def _bytes(value: object, blockers: set[str]) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        blockers.add("byte_count_invalid")
        return None
    return value


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_reparse(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & _REPARSE_POINT)


def _contains_reparse(path: Path, root: Path) -> bool:
    current = path
    while _is_relative_to(current, root):
        if current.exists() and _is_reparse(current):
            return True
        if current == root:
            break
        current = current.parent
    return False


def _record_id(record: object) -> str:
    value = getattr(record, "record_id", None)
    if isinstance(value, str) and value:
        return value
    if isinstance(record, Mapping):
        return _required_id(record.get("id"), "migration record id")
    raise AssetOwnershipGraphError("migration record id is invalid")


def _payload(record: object) -> Mapping[str, object]:
    value = getattr(record, "payload", None)
    if isinstance(value, Mapping):
        return value
    if isinstance(record, Mapping):
        payload = record.get("payload")
        return payload if isinstance(payload, Mapping) else record
    raise AssetOwnershipGraphError("migration record payload is invalid")
