"""Queue for static MBTiles build jobs (Redis list). Job status is stored in job_store (GET /jobs/{job_id})."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.core.config import get_settings

if TYPE_CHECKING:
    from app.services.job_store import JobInfo

TILE_BUILD_QUEUE_KEY = "geofastmap:tile_build_queue"
TILE_BUILD_LATEST_PREFIX = "geofastmap:tile_build_latest:"
TILE_BUILD_PENDING_PREFIX = "geofastmap:tile_build_pending:"
TILE_BUILD_JOB_TTL = 86400 * 7  # 7 days


@dataclass
class TileBuildOptions:
    """Optional overrides for tile build (min/max zoom, attributes, densest/smallest, simplification flags). None/empty = use defaults."""
    min_zoom: int | None = None
    max_zoom: int | None = None
    include_attributes: list[str] | None = None  # if set, only these props in tiles (tippecanoe --include)
    exclude_attributes: list[str] | None = None  # if set, drop these (tippecanoe --exclude / -x)
    densest: str | None = None   # "drop" | "coalesce" (drop-densest-as-needed vs coalesce-densest-as-needed)
    smallest: str | None = None  # "drop" | "coalesce" (drop-smallest-as-needed vs coalesce-smallest-as-needed)
    # Simplification / geometry (tippecanoe -ps, -pS, -pn, -pt). True = use flag, False = omit, None = default.
    no_line_simplification: bool | None = None       # -ps / --no-line-simplification
    simplify_only_low_zooms: bool | None = None      # -pS / --simplify-only-low-zooms
    no_shared_node_simplification: bool | None = None  # -pn / --no-simplification-of-shared-nodes
    no_tiny_polygon_reduction: bool | None = None     # -pt / --no-tiny-polygon-reduction
    no_point_dropping: bool | None = None            # -r1: do not drop fraction of points at low zooms (for clustering)

    def to_dict(self) -> dict:
        out = {}
        if self.min_zoom is not None:
            out["min_zoom"] = self.min_zoom
        if self.max_zoom is not None:
            out["max_zoom"] = self.max_zoom
        if self.include_attributes is not None:
            out["include_attributes"] = self.include_attributes
        if self.exclude_attributes is not None:
            out["exclude_attributes"] = self.exclude_attributes
        if self.densest is not None:
            out["densest"] = self.densest
        if self.smallest is not None:
            out["smallest"] = self.smallest
        if self.no_line_simplification is not None:
            out["no_line_simplification"] = self.no_line_simplification
        if self.simplify_only_low_zooms is not None:
            out["simplify_only_low_zooms"] = self.simplify_only_low_zooms
        if self.no_shared_node_simplification is not None:
            out["no_shared_node_simplification"] = self.no_shared_node_simplification
        if self.no_tiny_polygon_reduction is not None:
            out["no_tiny_polygon_reduction"] = self.no_tiny_polygon_reduction
        if self.no_point_dropping is not None:
            out["no_point_dropping"] = self.no_point_dropping
        return out

    @classmethod
    def from_dict(cls, d: dict | None) -> "TileBuildOptions":
        if not d:
            return cls()
        return cls(
            min_zoom=d.get("min_zoom"),
            max_zoom=d.get("max_zoom"),
            include_attributes=d.get("include_attributes"),
            exclude_attributes=d.get("exclude_attributes"),
            densest=d.get("densest"),
            smallest=d.get("smallest"),
            no_line_simplification=d.get("no_line_simplification"),
            simplify_only_low_zooms=d.get("simplify_only_low_zooms"),
            no_shared_node_simplification=d.get("no_shared_node_simplification"),
            no_tiny_polygon_reduction=d.get("no_tiny_polygon_reduction"),
            no_point_dropping=d.get("no_point_dropping"),
        )


@dataclass
class TileBuildPayload:
    collection_id: str
    job_id: str
    options: TileBuildOptions = field(default_factory=TileBuildOptions)

    def to_json(self) -> str:
        out = {"collection_id": self.collection_id, "job_id": self.job_id}
        opts = self.options.to_dict()
        if opts:
            out["options"] = opts
        return json.dumps(out)

    @classmethod
    def from_json(cls, s: str) -> "TileBuildPayload":
        d = json.loads(s)
        opts = TileBuildOptions.from_dict(d.get("options"))
        return cls(collection_id=d["collection_id"], job_id=d["job_id"], options=opts)


def _redis():
    import redis
    return redis.from_url(get_settings().redis_url, decode_responses=True)


def _latest_key(collection_id: str) -> str:
    return f"{TILE_BUILD_LATEST_PREFIX}{collection_id}"


def _pending_key(collection_id: str) -> str:
    return f"{TILE_BUILD_PENDING_PREFIX}{collection_id}"


def create_tile_build_job(collection_id: str, owner_id: int | None = None) -> "JobInfo":
    """Create a tile build job in job_store and set as latest for this collection. Caller must enqueue."""
    from app.services.job_store import create_job
    job = create_job(collection_id, owner_id=owner_id)
    r = _redis()
    r.set(_latest_key(collection_id), job.job_id, ex=TILE_BUILD_JOB_TTL)
    return job


def get_tile_build_job(job_id: str) -> "JobInfo | None":
    """Return job from job_store (tile build and bulk import share the same store)."""
    from app.services.job_store import get_job
    return get_job(job_id)


def get_latest_tile_build_job(collection_id: str) -> "JobInfo | None":
    r = _redis()
    job_id = r.get(_latest_key(collection_id))
    if not job_id:
        return None
    return get_tile_build_job(job_id)


def update_tile_build_job(
    job_id: str,
    *,
    status: str | None = None,
    message: str | None = None,
) -> "JobInfo | None":
    """Update tile build job in job_store. status: building -> running, queued -> pending, completed/failed as-is."""
    from app.services.job_store import update_job
    if status == "building":
        status = "running"
    elif status == "queued":
        status = "pending"
    return update_job(job_id, status=status, message=message)


def get_pending_job_id(collection_id: str) -> str | None:
    """Return job_id if this collection has a queued or building job (dedup)."""
    r = _redis()
    return r.get(_pending_key(collection_id))


def set_pending(collection_id: str, job_id: str) -> None:
    r = _redis()
    r.set(_pending_key(collection_id), job_id, ex=3600)  # 1h fallback if worker dies


def clear_pending(collection_id: str) -> None:
    r = _redis()
    r.delete(_pending_key(collection_id))


def enqueue_tile_build(
    collection_id: str,
    job_id: str,
    options: TileBuildOptions | None = None,
) -> bool:
    """
    Add build job to queue. Set pending so we don't enqueue duplicate for same collection.
    Returns True if enqueued, False if already pending (use existing job_id).
    """
    settings = get_settings()
    if settings.bulk_queue_type != "redis":
        return False
    r = _redis()
    pending = _pending_key(collection_id)
    if r.get(pending):
        return False  # already queued or building
    r.set(pending, job_id, ex=3600)
    payload = TileBuildPayload(
        collection_id=collection_id,
        job_id=job_id,
        options=options or TileBuildOptions(),
    )
    r.lpush(TILE_BUILD_QUEUE_KEY, payload.to_json())
    return True
