"""Human-readable job type labels and structured `details` for UI / API (bulk import, tile build, geoprocess)."""

from __future__ import annotations

import json
from typing import Any

from app.services.job_store import JobInfo

# OGC / app process ids → short labels for tables
PROCESS_TYPE_LABELS: dict[str, str] = {
    "intersection": "Intersection (two layers)",
    "erase": "Erase / difference (two layers)",
    "buffer": "Buffer (single layer)",
    "explode": "Explode geometries (single layer)",
    "make_valid": "Make valid (single layer)",
    "union": "Union / dissolve (single layer)",
    "measure": "Measure (single layer, in-place)",
}


def _message_suggests_tile_build(message: str | None) -> bool:
    if not message:
        return False
    m = message.lower()
    needles = (
        "tile build",
        "tippecanoe",
        "pmtiles",
        "mbtiles",
        "building tiles",
        "static tiles",
        "vector tiles",
        "tile worker",
    )
    return any(n in m for n in needles)


def _process_meta_to_details(meta: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "process_id": meta.get("process_id"),
        "collection_id_a": meta.get("collection_id_a") or "",
        "collection_id_b": meta.get("collection_id_b") or "",
    }
    cids = meta.get("collection_ids")
    if isinstance(cids, list):
        out["collection_ids"] = cids
        out["mode"] = "feature_vs_layers"
        out["feature_source"] = meta.get("feature_source")
        out["feature_id"] = meta.get("feature_id")
    else:
        out["mode"] = "collection_vs_collection"
        if (meta.get("collection_id_a") or "") == (meta.get("collection_id_b") or "") and meta.get("collection_id_a"):
            out["mode"] = "single_layer"
    if meta.get("result_collection_id"):
        out["result_collection_id"] = meta.get("result_collection_id")
    ue = meta.get("update_existing")
    out["update_existing"] = ue in ("1", "true", True)
    return out


def build_job_view_dict(
    job: JobInfo,
    *,
    meta: dict[str, Any] | None = None,
    is_tile_build_latest: bool = False,
) -> dict[str, Any]:
    """
    Merge job_store fields with classification and a `details` object for JSON display.

    Classification:
    - Redis process meta → geoprocess job
    - Else if this job is the latest tile-build marker for its collection, or message looks like tile build → tile build
    - Else → bulk import
    """
    d: dict[str, Any] = dict(job.to_dict())
    msg = job.message or ""

    details: dict[str, Any] = {
        "job_id": job.job_id,
        "collection_id": job.collection_id,
        "status": job.status,
        "message": job.message,
        "items_in": job.items_in,
        "items_created": job.items_created,
        "items_failed": job.items_failed,
        "created_at": d.get("created_at"),
        "updated_at": d.get("updated_at"),
        "finished_at": d.get("finished_at"),
    }

    if meta and meta.get("process_id"):
        pid = str(meta["process_id"])
        d["job_category"] = "process"
        d["job_type_label"] = PROCESS_TYPE_LABELS.get(pid, pid.replace("_", " ").title())
        d["process_id"] = pid
        details["process"] = _process_meta_to_details(meta)
        if meta.get("collection_ids"):
            n = len(meta["collection_ids"])
            d["input_summary"] = f"Feature vs {n} layer(s)"
        elif meta.get("collection_id_a") and meta.get("collection_id_b"):
            a, b = meta["collection_id_a"], meta["collection_id_b"]
            d["input_summary"] = f"{a} × {b}" if a != b else f"Layer: {a}"
        else:
            d["input_summary"] = job.collection_id
        d["details"] = details
        if meta.get("result_collection_id"):
            d["result_collection_id"] = meta.get("result_collection_id")
        return d

    is_tile = bool(is_tile_build_latest) or _message_suggests_tile_build(msg)
    d["job_category"] = "tile_build" if is_tile else "bulk_import"
    d["job_type_label"] = "Static tile build (MBTiles / PMTiles)" if is_tile else "Bulk import"
    d["input_summary"] = f"Collection: {job.collection_id}"
    if is_tile:
        details["tile_build"] = {
            "collection_id": job.collection_id,
            "matched_latest_tile_job_pointer": bool(is_tile_build_latest),
            "note": "Tippecanoe / static vector tiles for this collection.",
        }
    else:
        details["bulk_import"] = {
            "collection_id": job.collection_id,
            "note": "Import from uploaded file (shapefile, GeoJSON, etc.).",
        }
    d["details"] = details
    return d
