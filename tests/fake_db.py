"""
In-memory fake store and CRUD implementations for tests. No real database required.
Uses the same ORM models (Collection, Feature) and geo utils so route serialization works.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any, Tuple
from uuid import uuid4

from app.models.collection import Collection
from app.models.feature import Feature
from app.schemas.collection import CollectionCreate, Extent, CollectionPatch, CollectionReplace
from app.schemas.feature import FeatureCreate, FeaturePatch, FeatureReplace
from app.utils.geo import geojson_to_wkt_element, geometry_to_geojson
from app.utils.property_filter import property_value_to_like_pattern
from app.utils.property_filters import PropertyFilter, PropertyOp


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Store:
    """In-memory store for collections and features. Cleared per test via fixture."""

    def __init__(self) -> None:
        self.collections: dict[str, Collection] = {}
        self.features: dict[tuple[str, str], Feature] = {}  # (collection_id, feature_id) -> Feature

    def _bbox_from_features(self, collection_id: str) -> Extent | None:
        """Compute extent from feature geometries in this collection."""
        coords: list[tuple[float, float]] = []
        for (cid, _), f in self.features.items():
            if cid != collection_id or f.geometry is None:
                continue
            gj = geometry_to_geojson(f.geometry)
            if not gj or "coordinates" not in gj:
                continue
            self._collect_coords(gj["coordinates"], coords)
        if not coords:
            return None
        xs = [c[0] for c in coords]
        ys = [c[1] for c in coords]
        return Extent(
            bbox=[[min(xs), min(ys), max(xs), max(ys)]],
            crs="http://www.opengis.net/def/crs/OGC/1.3/CRS84",
        )

    @staticmethod
    def _collect_coords(value: Any, out: list[tuple[float, float]]) -> None:
        if isinstance(value, (int, float)):
            return
        if isinstance(value, (list, tuple)):
            if len(value) >= 2 and isinstance(value[0], (int, float)):
                out.append((float(value[0]), float(value[1])))
            else:
                for item in value:
                    Store._collect_coords(item, out)

    def _feature_matches_structured_filter(self, f: Feature, pf: PropertyFilter) -> bool:
        """True if feature matches one structured filter (key:op:value)."""
        props = f.properties or {}
        val = props.get(pf.key)
        str_val = str(val) if val is not None else ""
        try:
            num_val = float(pf.value)
        except ValueError:
            num_val = None
        if pf.op == PropertyOp.EQ:
            return str_val == pf.value
        if pf.op == PropertyOp.NE:
            return str_val != pf.value
        if pf.op == PropertyOp.LIKE:
            # Simple substring for fake (SQL LIKE has %/_ wildcards; we approximate)
            return pf.value in str_val
        if pf.op == PropertyOp.ILIKE:
            return pf.value.lower() in str_val.lower()
        if num_val is not None and pf.op in (PropertyOp.GT, PropertyOp.GTE, PropertyOp.LT, PropertyOp.LTE):
            try:
                fnum = float(str_val)
            except ValueError:
                fnum = None
            if fnum is not None:
                if pf.op == PropertyOp.GT:
                    return fnum > num_val
                if pf.op == PropertyOp.GTE:
                    return fnum >= num_val
                if pf.op == PropertyOp.LT:
                    return fnum < num_val
                if pf.op == PropertyOp.LTE:
                    return fnum <= num_val
        if pf.op == PropertyOp.GT:
            return str_val > pf.value
        if pf.op == PropertyOp.GTE:
            return str_val >= pf.value
        if pf.op == PropertyOp.LT:
            return str_val < pf.value
        if pf.op == PropertyOp.LTE:
            return str_val <= pf.value
        return False

    def _feature_matches_property_filters(self, f: Feature, property_filters: dict[str, str]) -> bool:
        """True if feature properties match all attribute filters (exact or * partial)."""
        props = f.properties or {}
        for key, value in property_filters.items():
            prop_val = props.get(key)
            if prop_val is None:
                return False
            str_val = str(prop_val)
            pattern, use_like = property_value_to_like_pattern(value)
            if use_like and pattern is not None:
                # pattern has % for SQL; for Python: *value -> endswith, value* -> startswith, *value* -> in
                if value.startswith("*") and value.endswith("*"):
                    if value[1:-1] not in str_val:
                        return False
                elif value.startswith("*"):
                    if not str_val.endswith(value[1:]):
                        return False
                elif value.endswith("*"):
                    if not str_val.startswith(value[:-1]):
                        return False
                else:
                    if str_val != value:
                        return False
            else:
                if str_val != value:
                    return False
        return True

    def _feature_in_bbox(self, f: Feature, bbox: tuple[float, float, float, float]) -> bool:
        if f.geometry is None:
            return False
        gj = geometry_to_geojson(f.geometry)
        if not gj or "coordinates" not in gj:
            return False
        coords: list[tuple[float, float]] = []
        self._collect_coords(gj["coordinates"], coords)
        if not coords:
            return False
        minx, miny, maxx, maxy = bbox
        for x, y in coords:
            if minx <= x <= maxx and miny <= y <= maxy:
                return True
        return False


class FakeCollectionTilesCrud:
    """No-op tiles CRUD for tests (no static tiles, no Martin views)."""

    async def get_collection_tiles(self, db: Any, collection_id: str):
        return None

    async def get_max_feature_updated_at(self, db: Any, collection_id: str):
        return None

    @staticmethod
    def martin_view_name(collection_id: str) -> str:
        import re
        safe = re.sub(r"[^a-zA-Z0-9_]", "_", collection_id)
        return f"tiles_{safe}" if safe else "tiles_unnamed"


class FakeCollectionsCrud:
    """CRUD for collections backed by Store. Same async interface as app.crud.collections."""

    def __init__(self, store: Store) -> None:
        self._store = store

    async def list_collections(self, db: Any) -> Sequence[Collection]:
        return sorted(self._store.collections.values(), key=lambda c: c.id)

    async def get_collection(self, db: Any, collection_id: str) -> Collection | None:
        return self._store.collections.get(collection_id)

    async def get_collection_bbox_from_features(self, db: Any, collection_id: str) -> Extent | None:
        return self._store._bbox_from_features(collection_id)

    async def get_collections_bboxes(self, db: Any) -> dict[str, Extent]:
        result: dict[str, Extent] = {}
        for cid in self._store.collections:
            ext = self._store._bbox_from_features(cid)
            if ext is not None:
                result[cid] = ext
        return result

    async def recompute_and_update_collection_extent(
        self, db: Any, collection_id: str
    ) -> Extent | None:
        c = self._store.collections.get(collection_id)
        if c is None:
            return None
        extent = await self.get_collection_bbox_from_features(db, collection_id)
        c.extent = extent.model_dump() if extent else None
        c.updated_at = _now()
        return extent

    async def create_collection(self, db: Any, data: CollectionCreate) -> Collection:
        now = _now()
        collection = Collection(
            id=data.id,
            title=data.title,
            description=data.description,
            extent=data.extent.model_dump() if data.extent else None,
            created_at=now,
            updated_at=now,
        )
        self._store.collections[data.id] = collection
        return collection

    async def replace_collection(
        self, db: Any, collection_id: str, data: CollectionReplace
    ) -> Collection | None:
        c = self._store.collections.get(collection_id)
        if c is None:
            return None
        c.title = data.title
        c.description = data.description
        c.extent = data.extent.model_dump() if data.extent else None
        c.updated_at = _now()
        return c

    async def patch_collection(
        self, db: Any, collection_id: str, data: CollectionPatch
    ) -> Collection | None:
        c = self._store.collections.get(collection_id)
        if c is None:
            return None
        if "title" in data.model_fields_set:
            c.title = data.title
        if "description" in data.model_fields_set:
            c.description = data.description
        if "extent" in data.model_fields_set:
            c.extent = data.extent.model_dump() if data.extent else None
        c.updated_at = _now()
        return c

    async def delete_collection(self, db: Any, collection_id: str) -> bool:
        if collection_id not in self._store.collections:
            return False
        del self._store.collections[collection_id]
        # Remove features in this collection
        for key in list(self._store.features.keys()):
            if key[0] == collection_id:
                del self._store.features[key]
        return True


class FakeFeaturesCrud:
    """CRUD for features backed by Store. Same async interface as app.crud.features."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def _sort_key(self, f: Feature, sortby: str | None, sortdesc: bool) -> Any:
        if not sortby:
            return f.id
        if sortby == "id":
            return f.id
        if sortby == "created_at":
            return f.created_at or datetime.min.replace(tzinfo=timezone.utc)
        # property
        val = (f.properties or {}).get(sortby)
        return (val is None, val)

    async def list_features_for_collection(self, db: Any, collection_id: str) -> Sequence[Feature]:
        return [
            f for (cid, _), f in sorted(self._store.features.items())
            if cid == collection_id
        ]

    async def list_features_paginated(
        self,
        db: Any,
        collection_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
        bbox: tuple[float, float, float, float] | None = None,
        datetime_start: datetime | None = None,
        datetime_end: datetime | None = None,
        sortby: str | None = None,
        sortdesc: bool = False,
        property_filters: dict[str, str] | None = None,
        structured_filters: Sequence[PropertyFilter] | None = None,
        fulltext_q: str | None = None,
        collection_feature_count: int | None = None,
    ) -> Tuple[Sequence[Feature], int]:
        items = [
            f for (cid, _), f in self._store.features.items()
            if cid == collection_id
        ]
        if bbox:
            items = [f for f in items if self._store._feature_in_bbox(f, bbox)]
        if property_filters:
            items = [f for f in items if self._store._feature_matches_property_filters(f, property_filters)]
        if structured_filters:
            for pf in structured_filters:
                items = [f for f in items if self._store._feature_matches_structured_filter(f, pf)]
        if fulltext_q:
            q = fulltext_q.lower()
            def flat(f: Feature) -> str:
                return " ".join(str(v) for v in (f.properties or {}).values()).lower()
            items = [f for f in items if q in flat(f)]
        if datetime_start is not None:
            items = [f for f in items if (f.created_at or datetime.min.replace(tzinfo=timezone.utc)) >= datetime_start]
        if datetime_end is not None:
            items = [f for f in items if (f.created_at or datetime.max.replace(tzinfo=timezone.utc)) <= datetime_end]
        total = len(items)
        items.sort(key=lambda f: self._sort_key(f, sortby, sortdesc), reverse=sortdesc)
        page = items[offset : offset + limit]
        return (page, total)

    async def get_feature(
        self, db: Any, collection_id: str, feature_id: str
    ) -> Feature | None:
        return self._store.features.get((collection_id, feature_id))

    async def create_feature(self, db: Any, data: FeatureCreate) -> Feature:
        geometry_wkt = geojson_to_wkt_element(
            data.geometry.model_dump() if data.geometry else None
        )
        now = _now()
        feature_id = str(uuid4())
        feature = Feature(
            id=feature_id,
            collection_id=data.collection_id,
            geometry=geometry_wkt,
            properties=data.properties,
            created_at=now,
            updated_at=now,
        )
        self._store.features[(data.collection_id, feature_id)] = feature
        return feature

    async def replace_feature(
        self, db: Any, collection_id: str, feature_id: str, data: FeatureReplace
    ) -> bool:
        f = self._store.features.get((collection_id, feature_id))
        if f is None:
            return False
        f.geometry = geojson_to_wkt_element(
            data.geometry.model_dump() if data.geometry else None
        )
        f.properties = data.properties
        f.updated_at = _now()
        return True

    async def update_feature(
        self, db: Any, collection_id: str, feature_id: str, data: FeaturePatch
    ) -> Feature | None:
        f = self._store.features.get((collection_id, feature_id))
        if f is None:
            return None
        if "geometry" in data.model_fields_set:
            f.geometry = (
                geojson_to_wkt_element(data.geometry.model_dump())
                if data.geometry is not None
                else None
            )
        if "properties" in data.model_fields_set:
            existing = f.properties or {}
            f.properties = {**existing, **(data.properties or {})}
        f.updated_at = _now()
        return f

    async def delete_feature(
        self, db: Any, collection_id: str, feature_id: str
    ) -> bool:
        key = (collection_id, feature_id)
        if key not in self._store.features:
            return False
        del self._store.features[key]
        return True
