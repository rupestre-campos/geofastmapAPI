"""Insert features with ST_Subdivide so no row has more than max_vertices (default 256)."""
from __future__ import annotations

import json

from sqlalchemy import text


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
    }
    if _is_empty_or_null_wkt(wkt):
        sql = """
            INSERT INTO features (id, collection_id, part_index, geometry, properties, created_at, updated_at)
            VALUES (:id, :cid, 0, NULL, CAST(:props AS jsonb), :now, :now)
        """
        return sql, params
    params["wkt"] = wkt
    params["max_vertices"] = max_vertices
    sql = """
        INSERT INTO features (id, collection_id, part_index, geometry, properties, created_at, updated_at)
        SELECT :id, :cid, (row_number() OVER ())::int - 1, g, CAST(:props AS jsonb), :now, :now
        FROM ST_Subdivide(ST_GeomFromText(:wkt, 4326), :max_vertices) AS g
    """
    return sql, params
