"""Queue for OGC API - Processes jobs (intersection, erase). Uses Redis list; job status in job_store."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from app.core.config import get_settings

PROCESS_QUEUE_KEY = "geofastmap:process_queue"
PROCESS_JOB_IDS_KEY = "geofastmap:process_job_ids"
PROCESS_JOB_META_PREFIX = "geofastmap:process_job_meta:"


@dataclass
class ProcessJobPayload:
    """Process jobs for OGC API - Processes.

    Modes:
    - Collection vs collection: collection_id_a, collection_id_b.
    - Single feature vs layers: feature_ref or feature_geojson + collection_ids.
    - Single-layer tools (e.g. buffer): collection_id_a (+ optional feature_ids, distance).
    """
    job_id: str
    process_id: str  # e.g. "intersection", "erase", "buffer"
    collection_id_a: str = ""
    collection_id_b: str = ""
    # Single-feature vs layers mode
    feature_ref: dict | None = None  # {"collection_id": "...", "feature_id": "..."}
    feature_geojson: dict | None = None  # GeoJSON Feature or FeatureCollection
    collection_ids: list[str] = field(default_factory=list)
    # Single-layer tools
    feature_ids: list[str] = field(default_factory=list)
    buffer_distance_degrees: float | None = None
    group_by_property: str | None = None
    # Single-layer measure tool (update properties in-place)
    measure_op: str | None = None  # "area" | "length" | "perimeter"
    measure_unit: str | None = None  # e.g. "m2", "ha", "ac", "km2", "m", "km"
    measure_field: str | None = None  # properties key to write (e.g. "area_m2")
    # Optional: enqueue static tile build after process completes
    queue_compute_tiles: bool = True
    tile_build_options: dict | None = None  # matches TileBuildOptions.to_dict()
    # Result: optional explicit name; if update_existing, result_collection_id is existing collection to overwrite
    result_collection_id: str | None = None
    update_existing: bool = False

    def to_json(self) -> str:
        out = {
            "job_id": self.job_id,
            "process_id": self.process_id,
            "collection_id_a": self.collection_id_a,
            "collection_id_b": self.collection_id_b,
        }
        if self.collection_ids:
            out["collection_ids"] = self.collection_ids
            if self.feature_ref:
                out["feature_ref"] = self.feature_ref
            if self.feature_geojson:
                out["feature_geojson"] = self.feature_geojson
        if self.feature_ids:
            out["feature_ids"] = self.feature_ids
        if self.buffer_distance_degrees is not None:
            out["buffer_distance_degrees"] = self.buffer_distance_degrees
        if self.group_by_property is not None:
            out["group_by_property"] = self.group_by_property
        if self.measure_op is not None:
            out["measure_op"] = self.measure_op
        if self.measure_unit is not None:
            out["measure_unit"] = self.measure_unit
        if self.measure_field is not None:
            out["measure_field"] = self.measure_field
        if self.queue_compute_tiles is False:
            out["queue_compute_tiles"] = False
        if self.tile_build_options:
            out["tile_build_options"] = self.tile_build_options
        if self.result_collection_id:
            out["result_collection_id"] = self.result_collection_id
        if self.update_existing:
            out["update_existing"] = True
        return json.dumps(out)

    @classmethod
    def from_json(cls, s: str) -> "ProcessJobPayload":
        d = json.loads(s)
        return cls(
            job_id=d["job_id"],
            process_id=d["process_id"],
            collection_id_a=d.get("collection_id_a", ""),
            collection_id_b=d.get("collection_id_b", ""),
            feature_ref=d.get("feature_ref"),
            feature_geojson=d.get("feature_geojson"),
            collection_ids=d.get("collection_ids") or [],
            feature_ids=d.get("feature_ids") or [],
            buffer_distance_degrees=d.get("buffer_distance_degrees"),
            group_by_property=d.get("group_by_property"),
            measure_op=d.get("measure_op"),
            measure_unit=d.get("measure_unit"),
            measure_field=d.get("measure_field"),
            queue_compute_tiles=d.get("queue_compute_tiles", True),
            tile_build_options=d.get("tile_build_options"),
            result_collection_id=d.get("result_collection_id"),
            update_existing=d.get("update_existing", False),
        )

    @property
    def is_feature_vs_layers(self) -> bool:
        return bool(self.collection_ids and (self.feature_ref or self.feature_geojson))


def _redis():
    import redis
    return redis.from_url(get_settings().redis_url, decode_responses=True)


def _meta_key(job_id: str) -> str:
    return f"{PROCESS_JOB_META_PREFIX}{job_id}"


def store_process_job_meta(
    job_id: str,
    process_id: str,
    collection_id_a: str = "",
    collection_id_b: str = "",
    collection_ids: list[str] | None = None,
    feature_source: str = "",
    feature_id: str | None = None,
    result_collection_id: str | None = None,
    update_existing: bool = False,
) -> None:
    """Store process job metadata for listing on the processing page and for recovery."""
    if get_settings().process_queue_type != "redis":
        return
    try:
        r = _redis()
        key = _meta_key(job_id)
        mapping = {
            "job_id": job_id,
            "process_id": process_id,
            "collection_id_a": collection_id_a,
            "collection_id_b": collection_id_b,
        }
        if collection_ids is not None:
            mapping["collection_ids"] = json.dumps(collection_ids)
        if feature_source:
            mapping["feature_source"] = feature_source[:500]
        if feature_id:
            mapping["feature_id"] = feature_id[:200]
        if result_collection_id:
            mapping["result_collection_id"] = result_collection_id[:200]
        if update_existing:
            mapping["update_existing"] = "1"
        r.hset(key, mapping=mapping)
        r.expire(key, 86400 * 7)
        r.lpush(PROCESS_JOB_IDS_KEY, job_id)
        r.ltrim(PROCESS_JOB_IDS_KEY, 0, 99)
        r.expire(PROCESS_JOB_IDS_KEY, 86400 * 7)
    except Exception:
        pass


def get_process_job_meta(job_id: str) -> dict | None:
    """Return process job metadata dict or None. collection_ids is parsed from JSON if present."""
    if get_settings().process_queue_type != "redis":
        return None
    try:
        r = _redis()
        key = _meta_key(job_id)
        raw = r.hgetall(key)
        if not raw:
            return None
        out = dict(raw)
        if "collection_ids" in out:
            try:
                out["collection_ids"] = json.loads(out["collection_ids"])
            except Exception:
                out["collection_ids"] = []
        return out
    except Exception:
        return None


def set_process_job_result(job_id: str, result_collection_id: str) -> None:
    """Store result collection id when process job completes."""
    if get_settings().process_queue_type != "redis":
        return
    try:
        r = _redis()
        r.hset(_meta_key(job_id), "result_collection_id", result_collection_id)
    except Exception:
        pass


def list_process_job_ids(limit: int = 50) -> list[str]:
    """Return recent process job ids (newest first)."""
    if get_settings().process_queue_type != "redis":
        return []
    try:
        r = _redis()
        return r.lrange(PROCESS_JOB_IDS_KEY, 0, limit - 1)
    except Exception:
        return []


def enqueue_process_job(payload: ProcessJobPayload) -> bool:
    """Push job to process queue. Returns True if enqueued."""
    if get_settings().process_queue_type != "redis":
        return False
    r = _redis()
    r.lpush(PROCESS_QUEUE_KEY, payload.to_json())
    if payload.is_feature_vs_layers:
        src = "reference " + payload.feature_ref.get("collection_id", "") + "/" + payload.feature_ref.get("feature_id", "") if payload.feature_ref else "GeoJSON"
        fid = payload.feature_ref.get("feature_id") if payload.feature_ref else None
        store_process_job_meta(
            payload.job_id,
            payload.process_id,
            collection_ids=payload.collection_ids,
            feature_source=src,
            feature_id=fid,
            result_collection_id=payload.result_collection_id,
            update_existing=payload.update_existing,
        )
    else:
        store_process_job_meta(
            payload.job_id,
            payload.process_id,
            payload.collection_id_a,
            payload.collection_id_b,
            result_collection_id=payload.result_collection_id,
            update_existing=payload.update_existing,
        )
    return True
