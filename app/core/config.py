from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "GeoFastMap API"

    # Example: postgresql+asyncpg://user:password@localhost:5432/geofastmap
    database_url: str = (
        "postgresql+asyncpg://postgres:postgres@localhost:5432/geofastmap"
    )
    # When set, Alembic uses this URL (Postgres direct). Use when DATABASE_URL points at PgBouncer (transaction pool).
    database_url_direct: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DATABASE_URL_DIRECT", "ALEMBIC_DATABASE_URL"),
    )
    # Set true when DATABASE_URL targets PgBouncer in transaction pooling (asyncpg must disable statement cache).
    database_use_pgbouncer: bool = False
    # Per-process SQLAlchemy pool (each uvicorn worker / each worker process has its own pool).
    # Total server load: sum over all processes of (database_pool_size + database_pool_max_overflow), or use PgBouncer.
    database_pool_size: int = 5
    database_pool_max_overflow: int = 5
    database_pool_timeout: float = 30.0  # seconds to wait for a connection from the pool
    # Ephemeral engine in raster_batch (per asyncio.run job); keep small to avoid doubling many full pools.
    raster_batch_db_pool_size: int = 1
    raster_batch_db_max_overflow: int = 2

    # OGC API Features items: pagination limits
    items_default_limit: int = 100
    items_max_limit: int = 1000
    # Max vertices per feature row; geometries are subdivided at insert via ST_Subdivide(geom, N).
    features_subdivide_max_vertices: int = 256
    # Max size of one geometry (OGC WKB byte length). Rejects API writes and skips bulk-import parts over limit.
    features_max_geometry_bytes: int = 50 * 1024 * 1024  # 50 MiB; set 0 to disable

    # Bulk import: batch size for DB commits (features per transaction).
    bulk_import_batch_size: int = 3000
    # SQL VALUES tuples per INSERT when one source feature splits into many geometry parts.
    bulk_insert_parts_batch_size: int = 200
    # Logical features buffered before flushing multi-row INSERT statements (GeoJSONL fast path).
    bulk_features_per_insert: int = 32
    # GeoJSONL: orjson line reader instead of Fiona (default on for .geojsonl).
    bulk_geojsonl_fast_path: bool = True
    # Per-feature SAVEPOINT isolation (off on fast path for throughput).
    bulk_per_feature_savepoint: bool = False
    # Skip row trigger on features during bulk import; refresh collections.features_last_updated_at once at end.
    bulk_skip_features_touch_trigger: bool = True
    # Buffered read size when splitting large GeoJSONL into shard files.
    bulk_shard_split_buffer_bytes: int = 8 * 1024 * 1024
    # Emit progress updates while a large batch is still in-flight (seconds). 0 = commit-bound updates only.
    bulk_progress_heartbeat_seconds: float = 5.0
    # Post-import extent update mode: immediate (blocking), deferred (skip during job), or best_effort.
    bulk_extent_update_mode: str = "deferred"  # immediate | deferred | best_effort
    # Retry transient DB failures during bulk import/finalization.
    bulk_db_retry_max_attempts: int = 4
    bulk_db_retry_base_seconds: float = 1.0
    bulk_db_retry_max_seconds: float = 30.0
    # Resumable upload sessions (chunked upload API).
    bulk_upload_session_ttl_seconds: int = 86400
    bulk_upload_chunk_size_bytes: int = 64 * 1024 * 1024  # 64 MiB
    # Legacy parent/shard ingest (deprecated; use bulk_copy_ingest_enabled).
    bulk_sharded_ingest_enabled: bool = False
    bulk_shard_lines_per_part: int = 100000
    # COPY + staging table ingest (GeoJSONSeq and shapefile via fiona).
    bulk_copy_ingest_enabled: bool = True
    # After COPY, enqueue partition promote to a dedicated single-consumer finalize queue.
    bulk_finalize_queue_enabled: bool = True
    bulk_finalize_retry_base_seconds: float = 2.0
    bulk_finalize_retry_max_seconds: float = 120.0
    bulk_finalize_watchdog_interval_seconds: float = 60.0
    # Rows per COPY flush during staging load.
    bulk_copy_batch_rows: int = 50000
    # Parser processes for GeoJSONSeq (0 = auto: max(1, cpu_count - 1)).
    bulk_copy_parser_workers: int = 0
    # Fail running bulk jobs with no progress heartbeat after this many seconds.
    bulk_job_stale_seconds: float = 3600.0
    # Pending job still holds collection mutex (worker died before marking running): reclaim after this.
    bulk_job_pending_stale_seconds: float = 600.0
    # Interval between mutex/stale-job watchdog passes in the worker loop (seconds).
    bulk_watchdog_interval_seconds: float = 300.0
    # Replace mode: delete rows in batches to avoid one long table lock blocking other work.
    bulk_replace_delete_batch_rows: int = 25000
    # Shadow replace: append tagged rows first, delete old rows at finalize (items view keeps prior data).
    bulk_replace_shadow_import: bool = False
    # Per-collection Redis mutex TTL while a bulk import mutates features (refreshed during long jobs).
    bulk_collection_mutex_ttl_seconds: int = 86400 * 2

    # Bulk storage: where uploaded files go (shared path for API and worker). Future: s3.
    bulk_storage_type: str = "filesystem"  # filesystem | s3 (s3 reserved)
    bulk_storage_path: str = "/data/bulk-uploads"  # for filesystem; create if missing

    # Bulk queue: memory = in-process consumer; redis = separate worker(s), scalable.
    bulk_queue_type: str = "redis"  # memory | redis
    # Standalone bulk worker (`app.worker_main`): concurrent queue jobs per process (threads). Each job holds DB + CPU; raise with Postgres max_connections in mind.
    bulk_worker_max_concurrent: int = 1
    # Comma-separated collection ids allowed to auto-queue tile build after bulk import.
    # Empty = disabled (use POST /collections/{id}/tiles/build for manual/cron rebuilds).
    bulk_auto_tile_build_collections: str = ""
    # Tile worker: poll interval while waiting for bulk import to finish on the same collection.
    tile_build_bulk_wait_poll_seconds: float = 2.0
    # Items list: fail fast when bulk import holds row/table locks (avoids nginx 60s timeout).
    items_list_lock_timeout_seconds: float = 3.0
    items_list_statement_timeout_seconds: float = 30.0
    items_list_during_bulk_statement_timeout_seconds: float = 8.0
    redis_url: str = "redis://localhost:6379/0"  # used when bulk_queue_type=redis
    # TCP timeouts for redis-py (seconds). BRPOP consumer needs socket_timeout > BRPOP wait.
    redis_socket_connect_timeout_seconds: float = 10.0
    redis_socket_timeout_seconds: float = 0.0  # 0 = redis-py default (no read timeout)
    redis_brpop_socket_timeout_seconds: float = 0.0  # 0 = auto: max(30, brpop_timeout + 15)
    # Generic Redis retry/backoff knobs for queue consumers and enqueue operations.
    redis_retry_base_seconds: float = 1.0
    redis_retry_max_seconds: float = 30.0
    redis_retry_enqueue_max_attempts: int = 5
    # Hot-path Redis reads (e.g. job cancel polls during bulk import, parent shard aggregation).
    redis_retry_read_max_attempts: int = 20
    # Composite collection: Redis TTL for merged static MVT tiles (seconds). 0 = disabled.
    composite_tiles_cache_ttl_seconds: int = 3600

    # OGC API - Processes: geometric operations (intersection, erase) between collections.
    process_queue_type: str = "redis"  # redis | memory (memory = no separate worker)
    # Stream A in batches by memory size. Keep low to limit worker RAM (each batch + B in bbox held in memory).
    process_batch_max_bytes: int = 256 * 1024  # max bytes of geometry (A) per batch (default 256 KiB)
    process_batch_max_rows: int = 0  # optional cap: max rows per batch (0 = only byte limit)
    process_insert_batch_size: int = 200  # rows per INSERT commit when writing results (smaller = less memory)
    # Parallel batch workers per job (0 = use CPU count). Lower = less memory (fewer concurrent batches).
    process_batch_workers: int = 0  # 0 = os.cpu_count(), else cap threads per job
    process_progress_update_seconds: float = 2.0  # how often to update job progress (items_in/items_created)
    # Intersection pair-based flow: chunk size when reading (id_a, id_b) pairs and fetching those two features only.
    process_intersection_pair_chunk_size: int = 400  # pairs per chunk (each chunk = 2 bounded feature fetches)
    # Temp directory for process worker; cleaned on startup. Set empty to disable cleanup.
    process_temp_path: str = "/tmp/geofastmap_process_worker"
    # Statement timeout (seconds) for process worker DB connections. 0 = disabled. Helps prevent long queries from blocking API.
    process_worker_statement_timeout_seconds: int = 0  # 0 = no limit; e.g. 1800 = 30 min max per statement

    # Tiles: static MBTiles storage; dynamic tiles are served by FastAPI from PostGIS.
    tiles_storage_path: str = "/data/tiles"
    # Max features per MVT tile to avoid overloading the database (default 200k).
    tiles_mvt_max_features: int = items_max_limit
    # Redis cache TTL for dynamic tiles (seconds). 0 = no cache.
    tiles_dynamic_cache_ttl_seconds: int = 60
    # Also cache tiles with query params (limit, offset, bbox, etc.) to reduce DB load when panning/zooming.
    tiles_dynamic_cache_with_params: bool = True
    # TTL for parametrized tile cache (seconds). Max 60 to limit staleness; use invalidate button for immediate refresh.
    tiles_dynamic_cache_params_ttl_seconds: int = 60
    # Max time (seconds) for a single dynamic tile query; 0 = disabled. Helps avoid holding pool connections.
    tiles_dynamic_statement_timeout_seconds: int = 15
    # When set, dynamic tiles are generated by this worker (DB + tippecanoe). Empty = use in-process PostGIS MVT.
    tiles_dynamic_worker_url: str = ""  # e.g. http://localhost:8001
    # When non-empty, use Redis search cache + tile job queue (workers read from cache, no DB). e.g. "1" or "true".
    tiles_dynamic_use_queue: bool = False
    # TTL for cached search result GeoJSON (seconds). Workers read from this; no DB in workers. Max 60 to limit staleness.
    tiles_search_result_cache_ttl_seconds: int = 60
    # GET /collections/{collection_id}/items server-side cache (seconds). 0 = disabled.
    # This caches the full GeoJSON response for repeated identical queries (bbox, q, filters, etc.).
    collections_items_cache_ttl_seconds: int = 600
    tippecanoe_path: str = "tippecanoe"  # PATH or full path to tippecanoe binary
    tippecanoe_minzoom: int = 0
    tippecanoe_maxzoom: int = 16
    google_maps_api_key: str = ""  # optional; for Google Satellite/Hybrid basemaps in HTML

    # Rasters: COG storage (shared with Titiler worker when using file:// URLs)
    raster_storage_path: str = "/data/rasters"
    raster_upload_max_bytes: int = 500 * 1024 * 1024  # 500 MiB
    # Resumable raster upload: max bytes per HTTP part (stay under reverse-proxy limits, e.g. Cloudflare).
    # Resumable raster parts (keep under reverse-proxy body limits, e.g. Cloudflare ~100MB).
    raster_upload_chunk_size_bytes: int = 32 * 1024 * 1024  # 32 MiB; same default as bulk_upload_chunk_size_bytes
    # Internal Titiler base URL (e.g. http://titiler:8000). Empty = proxy disabled / in-app tiles only.
    titiler_internal_url: str = ""
    # Shared secret for Titiler → API internal COG fetch (optional; avoids file:// in Titiler).
    titiler_internal_secret: str = ""
    # Base URL reachable from the Titiler container for internal COG fetch (e.g. http://api:8000).
    raster_internal_fetch_base_url: str = ""
    # MosaicJSON asset hrefs: false = filesystem paths under raster_storage_path (Titiler must mount
    # the same volume as the API). True = HTTP ``.../internal/.../coverages/{id}/cog?token=...`` URLs
    # (only if Titiler cannot mount COGs; GDAL /vsicurl may mishandle long query strings).
    raster_mosaic_asset_hrefs_http: bool = False
    # API → Titiler httpx timeouts (mosaic tiles with many COG sources can exceed 60s cold read).
    titiler_http_connect_timeout_seconds: float = 3.0
    titiler_http_read_timeout_seconds: float = 30.0
    # API → Titiler retries (404 during restarts, transient 5xx). Exponential backoff between attempts.
    titiler_retry_max_attempts: int = 3
    titiler_retry_base_seconds: float = 0.15
    titiler_retry_max_seconds: float = 2.0
    # Max concurrent upstream Titiler HTTP calls per API worker (in-process LIFO wait queue when saturated). 0 = unlimited.
    titiler_upstream_max_concurrent: int = 8
    # OpenTelemetry tracing (export spans to OTEL Collector/Tempo when enabled).
    observability_tracing_enabled: bool = False
    observability_service_name: str = "geofast_api"
    observability_otlp_endpoint: str = "http://otel_collector:4317"
    # 0.0 = disabled, 1.0 = all requests sampled.
    observability_trace_sample_ratio: float = 0.1
    # In-app observability logging (Postgres-backed request events for admin debug pages).
    observability_logging_enabled: bool = True
    # Capture request body when debugging (disabled by default; increases risk/storage).
    observability_log_debug_mode: bool = False
    observability_log_debug_max_body_bytes: int = 4096
    # Raw events retention and aggregate retention.
    observability_log_retention_days: int = 7
    observability_metrics_retention_days: int = 30
    observability_cleanup_interval_seconds: int = 3600
    # Optional server list for admin load dashboard (Netdata or compatible /api/v1/data).
    # Default empty: only local procfs snapshot (no extra agent). Example:
    # [{"name":"api-host","base_url":"http://netdata:19999"}]
    observability_servers_json: str = "[]"
    # Comma-separated hostnames trusted for X-Forwarded-* (Starlette ProxyHeadersMiddleware). Use "*" for dev only.
    proxy_headers_trusted_hosts: str = "*"
    # When False, login rate limiting uses the TCP peer IP only (ignore X-Forwarded-For). Set True behind a correct reverse proxy.
    trust_x_forwarded_for_client_ip: bool = False
    # Session cookie: same_site lax|strict|none; https_only true when the site is HTTPS-only.
    session_cookie_same_site: str = "lax"
    session_cookie_https_only: bool = False
    # When True, STAC catalog root URLs must be https (admin catalog registration).
    stac_catalog_root_url_require_https: bool = False
    # When False, /docs, /redoc, and /openapi.json are disabled (recommended in production).
    expose_openapi_docs: bool = True
    # Redis cache for proxied Titiler raster tiles (STAC + COG). 0 = disabled.
    # Cold tiles still cost GDAL+network; repeats hit Redis ~1–5 ms.
    titiler_tile_cache_ttl_seconds: int = 3600
    # Mosaic tiles only: Redis TTL (seconds). Cache keys include mosaic JSON revision (etag), so a long
    # TTL does not serve stale tiles after edits. 0 = use titiler_tile_cache_ttl_seconds instead.
    titiler_mosaic_tile_cache_ttl_seconds: int = 86400
    # Do not store responses larger than this (bytes); avoids huge entries from mistakes.
    titiler_tile_cache_max_body_bytes: int = 4 * 1024 * 1024
    # Federated STAC Item Search: Redis cache TTL (seconds). 0 = no cache.
    # Raise in production (e.g. 604800 = 7d) to cut repeated POST /search traffic to upstream STAC APIs.
    stac_search_cache_ttl_seconds: int = 3600
    # GET /collections listing per registered catalog (small Redis footprint; reduces hammering catalog roots).
    stac_collections_cache_ttl_seconds: int = 604800
    # GET /collections/{c}/items/{id} JSON in Redis (shared across workers). 0 = in-process cache only (~90s).
    stac_item_redis_cache_ttl_seconds: int = 604800
    # Sent on all outbound STAC httpx requests; some CDNs/WAFs block missing or generic bot User-Agents.
    stac_http_user_agent: str = "GeoFastMap/1.0 (STAC client; +https://github.com/)"
    stac_search_http_timeout_seconds: float = 60.0
    # Federated STAC: retry POST /search on transient upstream errors (502/503/504/429).
    stac_search_http_max_retries: int = 8  # attempts after the first (8 => up to 9 tries per catalog)
    stac_search_http_retry_backoff_seconds: float = 2.0  # base delay; exponential backoff
    stac_search_http_retry_backoff_max_seconds: float = 300.0  # cap single retry wait at 5 minutes
    # Federated STAC pagination: follow rel=next links up to this many pages per catalog request.
    stac_search_http_max_pages: int = 2
    # Small delay between page requests to reduce burst pressure on upstream STAC APIs.
    stac_search_http_page_delay_seconds: float = 0.25
    stac_search_max_catalogs: int = 32
    # Mosaic planner compute queue. redis = offload heavy planning to standalone worker(s).
    mosaic_queue_type: str = "redis"  # redis | inline
    # Mosaic worker: concurrent jobs per worker process (higher = more CPU/network pressure).
    mosaic_worker_max_concurrent: int = 1
    # Large initial AOI: split full bbox into sub-bboxes when width/height exceeds this (degrees).
    mosaic_stac_initial_split_threshold_degrees: float = 6.0
    # Initial split grid for large AOI bbox. 0 = auto (2x2 or 3x3 by extent).
    mosaic_stac_initial_split_grid: int = 0
    # Parallel STAC sub-bbox searches per planning round.
    mosaic_stac_bbox_parallelism: int = 4
    # Parallel datetime-slice STAC searches per bbox.
    mosaic_stac_datetime_parallelism: int = 2
    # Per STAC /search request item limit in mosaic planner.
    mosaic_stac_fetch_limit: int = 500
    # Adaptive STAC sub-bbox splitting (planner round 0): retry flaky/low-yield bboxes by subdividing.
    # - max_split_depth: 0 disables splitting; 2 => up to 1 + 4 + 16 = 21 bboxes per seed cell (bounded by max_bbox_tasks_per_round).
    # - min_bbox_degrees: stop splitting when both width/height are below this.
    # - max_bbox_tasks_per_round: hard cap on total sub-bbox tasks per planner round.
    # - large_bbox_limit: smaller per-request STAC `limit` when bbox is large, favoring spatial diversity via tiling.
    mosaic_stac_max_split_depth: int = 2
    mosaic_stac_min_bbox_degrees: float = 0.5
    mosaic_stac_max_bbox_tasks_per_round: int = 64
    mosaic_stac_large_bbox_limit: int = 250
    # Planner void-fill rounds for uncovered AOI.
    mosaic_void_fill_max_rounds: int = 6
    # Max disconnected gap parts sampled for pinpoint void fill.
    mosaic_void_pinpoint_max_parts: int = 16
    # Stop void-fill when uncovered AOI fraction <= this threshold.
    mosaic_void_fill_min_uncovered: float = 0.001
    # Number of AOI longitude strips for same-pass date mode.
    mosaic_same_pass_num_strips: int = 8
    # Greedy cover: stop adding scenes when best marginal gain is below this fraction of the
    # current uncovered area (avoids piling many nearly redundant granules). Not applied to the first pick.
    mosaic_greedy_min_marginal_coverage_fraction: float = 0.005
    # Thumbnail fetch concurrency for footprint display attachment.
    mosaic_footprint_fetch_max_concurrent: int = 8
    # CPU concurrency for thumbnail decode + mask/geometry extraction.
    mosaic_footprint_cpu_max_concurrent: int = 4
    # HTTP timeouts for fetching thumbnail previews used in footprint display.
    mosaic_footprint_fetch_connect_timeout_seconds: float = 5.0
    mosaic_footprint_fetch_read_timeout_seconds: float = 20.0
    # Safety cap: max selected/swap items to process per plan response.
    mosaic_footprint_max_items: int = 200
    # Offload thumbnail footprint_display to Redis workers (async mosaic jobs only).
    mosaic_footprint_distributed_enabled: bool = False
    # Footprint subtasks dispatched per barrier wave (coordinator reads this).
    mosaic_footprint_distributed_wave: int = 16
    mosaic_footprint_distributed_timeout_seconds: int = 300
    # Concurrent footprint subtasks per process; 0 = use mosaic_subjob_worker_concurrency.
    mosaic_footprint_subjob_worker_concurrency: int = 0
    # STAC federation: per-request parallel catalog calls.
    mosaic_stac_catalog_parallelism: int = 4
    # STAC federation: global in-flight catalog request budget per search call.
    mosaic_stac_total_inflight_max: int = 12
    # Async mosaic plan jobs: heartbeat/progress + stale detection + client polling budget.
    mosaic_job_heartbeat_seconds: int = 10
    mosaic_job_stale_after_seconds: int = 180
    mosaic_job_client_timeout_seconds: int = 1800
    # Distributed single-job mode: split one mosaic into subjobs processed by many workers.
    mosaic_subjob_queue_enabled: bool = False
    mosaic_subjob_worker_concurrency: int = 2
    # If false, a worker running parent jobs will not also consume subtask queue.
    mosaic_subjob_consume_subtasks_while_parent_active: bool = True
    mosaic_subjob_bbox_datetime_parallelism: int = 8
    mosaic_subjob_catalog_parallelism: int = 4
    mosaic_subjob_round_timeout_seconds: int = 180
    mosaic_subjob_max_retries: int = 1
    mosaic_subjob_result_ttl_seconds: int = 3600
    mosaic_parent_fail_on_partial: bool = False
    # HTML STAC search: max merged features fetched before slicing for pagination (per search).
    stac_search_html_max_features: int = 2000
    # Max page size for GET /stac?f=html
    stac_search_html_max_limit: int = 100

    # Auth: session signing key (set in production); empty = no session auth
    auth_secret_key: str = ""
    # Default admin credentials for first-time seed (change after first login)
    auth_default_admin_username: str = "admin"
    auth_default_admin_password: str = "admin"

    @property
    def database_sync_url(self) -> str:
        """URL for sync SQLAlchemy (background threads). Replaces asyncpg with psycopg2."""
        if "+asyncpg" in self.database_url:
            return self.database_url.replace("+asyncpg", "+psycopg2", 1)
        if self.database_url.startswith("postgresql://"):
            return self.database_url  # psycopg2 default
        return self.database_url

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()

