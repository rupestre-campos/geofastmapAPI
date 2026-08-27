"""Insert features with ST_Subdivide so no row has more than max_vertices (default 256)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterator

from sqlalchemy import text
from sqlalchemy.orm import Session

# When geometry has more coords than this, subdivide in Python first to avoid huge WKT/alloc in DB.
MAX_COORDS_FOR_DB_SUBDIVIDE = 35_000


def _coord_count(geom: Any) -> int:
    """Approximate number of coordinates (vertices) in the geometry."""
    if geom is None or geom.is_empty:
        return 0
    try:
        if geom.geom_type in ("Polygon",):
            n = len(geom.exterior.coords) + sum(len(h.coords) for h in geom.interiors)
            return n
        if geom.geom_type in ("MultiPolygon", "GeometryCollection"):
            return sum(_coord_count(g) for g in geom.geoms)
        if geom.geom_type in ("LineString",):
            return len(geom.coords)
        if geom.geom_type in ("MultiLineString", "MultiPoint"):
            return sum(len(g.coords) for g in geom.geoms)
        if geom.geom_type == "Point":
            return 1
    except Exception:
        pass
    return 0


def subdivide_geometry_by_vertices(geom: Any, max_vertices: int) -> list[Any]:
    """
    Recursively split geometry by bounding box so each piece has at most ~max_vertices.
    Returns list of Shapely geometries. Avoids sending one huge WKT to the DB.
    """
    if geom is None or geom.is_empty:
        return []
    if max_vertices is None or max_vertices <= 0:
        max_vertices = 256
    n = _coord_count(geom)
    if n <= max_vertices:
        return [geom]
    try:
        from shapely.geometry import box
    except ImportError:
        return [geom]
    minx, miny, maxx, maxy = geom.bounds
    dx = maxx - minx
    dy = maxy - miny
    if dx <= 0 and dy <= 0:
        return [geom]
    # Split along the longer axis
    if dx >= dy:
        mid = minx + dx * 0.5
        left = box(minx, miny, mid, maxy)
        right = box(mid, miny, maxx, maxy)
    else:
        mid = miny + dy * 0.5
        left = box(minx, miny, maxx, mid)
        right = box(minx, mid, maxx, maxy)
    out: list[Any] = []
    for cell in (left, right):
        try:
            inter = geom.intersection(cell)
            if inter.is_empty:
                continue
            if inter.geom_type == "GeometryCollection":
                for g in inter.geoms:
                    if g is None or g.is_empty:
                        continue
                    out.extend(subdivide_geometry_by_vertices(g, max_vertices))
            else:
                out.extend(subdivide_geometry_by_vertices(inter, max_vertices))
        except Exception:
            out.append(geom)
            break
    return out if out else [geom]


def _is_empty_or_null_wkt(wkt: str | None) -> bool:
    if not wkt or not wkt.strip():
        return True
    u = wkt.strip().upper()
    return u == "EMPTY" or u.endswith(" EMPTY")


def insert_feature_subdivided_sql(
    feature_id: str,
    collection_id: str,
    wkt: str | None,
    properties: dict | None,
    now,
    max_vertices: int = 256,
    bulk_import_job_id: str | None = None,
) -> tuple[str, dict]:
    """
    Return (sql, params) to insert one logical feature, subdividing geometry into parts with ≤ max_vertices.
    Caller runs execute(text(sql), params). If geometry is null/empty, inserts one row with NULL geometry.
    """
    props_json = json.dumps(properties) if properties else None
    params = {
        "id": feature_id,
        "cid": collection_id,
        "props": props_json,
        "now": now,
        "bulk_jid": bulk_import_job_id,
    }
    if _is_empty_or_null_wkt(wkt):
        sql = """
            INSERT INTO features (id, collection_id, part_index, geometry, properties, created_at, updated_at, bulk_import_job_id)
            VALUES (:id, :cid, 0, NULL, CAST(:props AS jsonb), :now, :now, :bulk_jid)
        """
        return sql, params
    params["wkt"] = wkt
    params["max_vertices"] = max_vertices
    sql = """
        INSERT INTO features (id, collection_id, part_index, geometry, properties, created_at, updated_at, bulk_import_job_id)
        SELECT :id, :cid, (row_number() OVER ())::int - 1, g, CAST(:props AS jsonb), :now, :now, :bulk_jid
        FROM ST_Subdivide(ST_Force2D(ST_GeomFromText(:wkt, 4326)), :max_vertices) AS g
    """
    return sql, params


def insert_feature_parts_batched(
    feature_id: str,
    collection_id: str,
    wkt_list: list[str],
    properties: dict | None,
    now: Any,
    batch_size: int = 80,
    bulk_import_job_id: str | None = None,
) -> Iterator[tuple[str, dict]]:
    """
    Yield (sql, params) for batched INSERT of one logical feature as multiple part_index rows.
    Use when the geometry was already subdivided in Python (large geometry path).
    Each batch has at most batch_size rows; params are wkt_0, wkt_1, ... for that batch.
    """
    props_json = json.dumps(properties) if properties else None
    for start in range(0, len(wkt_list), batch_size):
        chunk = wkt_list[start : start + batch_size]
        params: dict[str, Any] = {
            "id": feature_id,
            "cid": collection_id,
            "props": props_json,
            "now": now,
            "bulk_jid": bulk_import_job_id,
        }
        values_parts = []
        for i, wkt in enumerate(chunk):
            if not wkt or _is_empty_or_null_wkt(wkt):
                continue
            key = f"wkt_{i}"
            params[key] = wkt
            part_idx = start + len(values_parts)
            values_parts.append(
                f"(:id, :cid, {part_idx}, ST_Force2D(ST_GeomFromText(:{key}, 4326)), CAST(:props AS jsonb), :now, :now, :bulk_jid)"
            )
        if not values_parts:
            continue
        sql = """
            INSERT INTO features (id, collection_id, part_index, geometry, properties, created_at, updated_at, bulk_import_job_id)
            VALUES """
        sql += ", ".join(values_parts)
        yield sql, params


@dataclass
class PendingFeatureInsert:
    """One logical feature queued for batched INSERT."""

    feature_id: str
    wkt: str | None
    properties: dict | None
    wkt_list: list[str] | None = None


@dataclass
class FeatureInsertBuffer:
    """Accumulate logical features; flush as multi-statement batches."""

    collection_id: str
    now: Any
    max_vertices: int = 256
    bulk_import_job_id: str | None = None
    insert_parts_batch_size: int = 200
    _pending: list[PendingFeatureInsert] = field(default_factory=list)

    def add(
        self,
        feature_id: str,
        wkt: str | None,
        properties: dict | None,
        *,
        wkt_list: list[str] | None = None,
    ) -> None:
        self._pending.append(
            PendingFeatureInsert(
                feature_id=feature_id,
                wkt=wkt,
                properties=properties,
                wkt_list=wkt_list,
            )
        )

    def __len__(self) -> int:
        return len(self._pending)

    def flush(self, session: Session) -> int:
        """Execute pending inserts; return number of logical features flushed."""
        if not self._pending:
            return 0
        count = len(self._pending)
        for item in self._pending:
            if item.wkt_list:
                for sql, params in insert_feature_parts_batched(
                    item.feature_id,
                    self.collection_id,
                    item.wkt_list,
                    item.properties,
                    self.now,
                    batch_size=self.insert_parts_batch_size,
                    bulk_import_job_id=self.bulk_import_job_id,
                ):
                    session.execute(text(sql), params)
            else:
                sql, params = insert_feature_subdivided_sql(
                    item.feature_id,
                    self.collection_id,
                    item.wkt,
                    item.properties,
                    self.now,
                    self.max_vertices,
                    bulk_import_job_id=self.bulk_import_job_id,
                )
                session.execute(text(sql), params)
        self._pending.clear()
        return count
